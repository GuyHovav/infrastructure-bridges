"""
NBI fixed-width ASCII file parser.

BACKGROUND
----------
The National Bridge Inventory (NBI) is maintained by the Federal Highway
Administration (FHWA). Every state submits bridge data annually in a
445-character fixed-width ASCII format — one line per bridge.

This format dates back to the mainframe era. Each field occupies a fixed
character range (e.g., characters 77-80 = latitude degrees). There are no
delimiters — you must know the exact column positions to extract data.

The format specification is published at:
  https://www.fhwa.dot.gov/bridge/nbi/format.cfm
The full coding guide with field descriptions:
  https://www.fhwa.dot.gov/bridge/mtguide.pdf

THIS MODULE
-----------
Provides:
  - NBI_FIELDS: Complete field layout (column ranges + names)
  - NBIRecord: A dataclass holding parsed fields for one bridge
  - Lookup tables: Human-readable names for coded values
  - parse_nbi_line(): Parses one 445-char line → NBIRecord

Design decisions:
  - We use a dataclass (not a dict) for type safety and IDE autocomplete.
  - We only extract ~35 fields that are relevant to our analysis,
    not all ~140 fields in the spec. This keeps the code manageable.
  - Lookup tables (material codes, etc.) translate cryptic NBI codes
    into readable names for the dashboard.
"""
from dataclasses import dataclass, field
from typing import Optional


# =============================================================================
# NBI Field Layout — verified against the official FHWA record format:
#   https://www.fhwa.dot.gov/bridge/nbi/format.cfm
# =============================================================================
# Format: (start_column, end_column, field_name, human_description)
#
# Column numbers are 1-indexed (as in the FHWA spec).
# We convert to 0-indexed Python slices inside parse_nbi_line().
#
# IMPORTANT: The NBI "Item number" (e.g., Item 58 = Deck Condition) is NOT
# the same as the column position. Item 58 is at column 259, not column 58.
# This distinction tripped us up in the first version of this parser.
#
# Not all fields are listed here — only the ones we extract.
# The full spec has ~90+ items spanning 445 characters.
# =============================================================================

NBI_FIELDS = [
    # -- Identity & Location --
    (1,   3,   "state_code",              "State Code (Item 1)"),
    (4,   18,  "structure_number",        "Structure Number (Item 8)"),
    (19,  19,  "record_type",             "Record Type (Item 5A)"),
    (20,  20,  "route_prefix",            "Route Signing Prefix (Item 5B)"),
    (21,  21,  "service_level",           "Designated Level of Service (Item 5C)"),
    (22,  26,  "route_number",            "Route Number (Item 5D)"),
    (27,  27,  "direction_suffix",        "Directional Suffix (Item 5E)"),
    (28,  29,  "highway_district",        "Highway Agency District (Item 2)"),
    (30,  32,  "county_code",             "County Code (Item 3)"),
    (33,  37,  "place_code",              "Place Code (Item 4)"),
    (38,  61,  "features_intersected",    "Features Intersected (Item 6A)"),
    (63,  80,  "facility_carried",        "Facility Carried (Item 7)"),
    (81,  105, "location",                "Location (Item 9)"),

    # -- Coordinates --
    # Latitude (Item 16): 8 chars, format DDMMSSSS (SS.SS encoded as SSSS)
    (130, 137, "latitude",                "Latitude (Item 16)"),
    # Longitude (Item 17): 9 chars, format DDDMMSSSS
    (138, 146, "longitude",               "Longitude (Item 17)"),

    # -- Classification & Ownership --
    (151, 152, "owner_code",              "Maintenance Responsibility (Item 21)"),
    (155, 156, "functional_class",        "Functional Class (Item 26)"),

    # -- Age & Construction --
    (157, 160, "year_built",              "Year Built (Item 27)"),

    # -- Traffic --
    (165, 170, "adt",                     "Average Daily Traffic (Item 29)"),
    (171, 174, "year_adt",                "Year of ADT (Item 30)"),

    # -- Material & Design Type --
    (202, 202, "material_code",           "Kind of Material/Design (Item 43A)"),
    (203, 204, "design_code",             "Type of Design/Construction (Item 43B)"),
    (205, 205, "approach_material",       "Approach Material (Item 44A)"),
    (206, 207, "approach_design",         "Approach Design (Item 44B)"),

    # -- Span counts --
    (208, 210, "number_of_spans",         "Number of Spans in Main Unit (Item 45)"),
    (211, 214, "num_approach_spans",      "Number of Approach Spans (Item 46)"),

    # -- Dimensions --
    (223, 228, "structure_length",        "Structure Length (Item 49)"),
    (239, 242, "deck_width",              "Deck Width, Out-to-Out (Item 52)"),

    # -- Condition Ratings (THE CORE DATA — 0 to 9 scale) --
    # These are SINGLE-CHARACTER fields. Each is one column.
    (259, 259, "deck_condition",          "Deck Condition (Item 58)"),
    (260, 260, "superstructure_cond",     "Superstructure Condition (Item 59)"),
    (261, 261, "substructure_cond",       "Substructure Condition (Item 60)"),
    (262, 262, "channel_condition",       "Channel/Channel Protection (Item 61)"),
    (263, 263, "culvert_condition",       "Culvert Condition (Item 62)"),

    # -- Load Ratings --
    (265, 267, "operating_rating",        "Operating Rating (Item 64)"),
    (269, 271, "inventory_rating",        "Inventory Rating (Item 66)"),

    # -- Appraisal Ratings (single-character fields) --
    (272, 272, "structural_eval",         "Structural Evaluation (Item 67)"),
    (273, 273, "deck_geometry_eval",      "Deck Geometry (Item 68)"),
    (274, 274, "underclear_eval",         "Underclearances (Item 69)"),
    (275, 275, "bridge_posting",          "Bridge Posting (Item 70)"),
    (276, 276, "waterway_adequacy",       "Waterway Adequacy (Item 71)"),
    (277, 277, "approach_alignment",      "Approach Roadway Alignment (Item 72)"),

    # -- Inspection Metadata --
    (287, 290, "inspection_date",         "Inspection Date (Item 90) — MMYY"),

    # -- Reconstructed & Truck Traffic --
    (362, 365, "year_reconstructed",      "Year Reconstructed (Item 106)"),
    (370, 371, "truck_pct",               "Average Daily Truck Traffic (Item 109)"),
]


# =============================================================================
# Parsed Record
# =============================================================================

@dataclass
class NBIRecord:
    """
    A single parsed NBI bridge record.

    All fields are Optional because NBI data can have missing/blank values.
    String fields default to "" (empty string) rather than None for easier
    downstream processing (no need to check for None before .strip(), etc.).
    """
    state_code: str = ""
    structure_number: str = ""
    county_code: str = ""
    facility_carried: str = ""
    location: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    owner_code: str = ""
    functional_class: str = ""
    year_built: Optional[int] = None
    year_reconstructed: Optional[int] = None
    number_of_spans: Optional[int] = None
    num_approach_spans: Optional[int] = None
    adt: Optional[int] = None
    year_adt: Optional[int] = None
    material_code: str = ""
    design_code: str = ""
    approach_material: str = ""
    approach_design: str = ""
    deck_width: Optional[float] = None
    structure_length: Optional[float] = None
    deck_condition: Optional[int] = None
    superstructure_cond: Optional[int] = None
    substructure_cond: Optional[int] = None
    channel_condition: Optional[int] = None
    culvert_condition: Optional[int] = None
    operating_rating: Optional[float] = None
    inventory_rating: Optional[float] = None
    structural_eval: Optional[int] = None
    deck_geometry_eval: Optional[int] = None
    underclear_eval: Optional[int] = None
    waterway_adequacy: Optional[int] = None
    approach_alignment: Optional[int] = None
    inspection_date: str = ""
    sufficiency_rating: Optional[float] = None
    sd_fo_status: str = ""
    truck_pct: Optional[float] = None


# =============================================================================
# Lookup Tables
# =============================================================================
# The NBI uses numeric codes for categorical data. These tables translate
# codes into human-readable names for the dashboard and analysis reports.

MATERIAL_CODES = {
    "1": "Concrete", "2": "Concrete Continuous", "3": "Steel",
    "4": "Steel Continuous", "5": "Prestressed Concrete",
    "6": "Prestressed Concrete Continuous", "7": "Wood or Timber",
    "8": "Masonry", "9": "Aluminum/Wrought Iron", "0": "Other",
}

DESIGN_CODES = {
    "1": "Slab", "2": "Stringer/Multi-beam", "3": "Girder/Floorbeam",
    "4": "Tee Beam", "5": "Box Beam/Girders - Multiple", "6": "Box Beam/Girders - Single",
    "7": "Frame", "8": "Orthotropic", "9": "Truss - Deck",
    "10": "Truss - Through", "11": "Arch - Deck", "12": "Arch - Through",
    "13": "Suspension", "14": "Stayed Girder", "15": "Movable - Lift",
    "16": "Movable - Bascule", "17": "Movable - Swing", "18": "Tunnel",
    "19": "Culvert", "20": "Mixed Types", "21": "Segmental Box Girder",
    "22": "Channel Beam",
}

OWNER_CODES = {
    "01": "State Highway Agency", "02": "County Highway Agency",
    "03": "Town or Township Highway Agency", "04": "City or Municipal Highway Agency",
    "11": "State Park, Forest, or Reservation Agency",
    "12": "Local Park, Forest, or Reservation Agency",
    "21": "Other State Agencies", "25": "Other Local Agencies",
    "26": "Private", "27": "Railroad", "28": "Unknown",
    "31": "U.S. Forest Service", "32": "National Park Service",
    "33": "Bureau of Indian Affairs", "34": "Bureau of Fish and Wildlife",
    "35": "U.S. Corps of Engineers", "36": "Bureau of Reclamation",
    "37": "Other Federal Agencies", "40": "U.S. Army",
    "41": "U.S. Navy/Marines", "42": "U.S. Air Force",
    "43": "Other Military Agencies", "60": "Other Federal Agencies",
    "62": "Bureau of Land Management", "64": "Tribal Government",
    "66": "Bureau of Indian Affairs", "68": "Other",
}

# The 0-9 condition rating scale is the backbone of bridge assessment.
# It's standardised across all 600,000+ bridges in the US inventory.
CONDITION_RATINGS = {
    0: "Failed",             # Out of service, beyond corrective action
    1: "Imminent Failure",   # Bridge may be closed; corrective action may restore light service
    2: "Critical",           # Advanced deterioration; may need bridge closure
    3: "Serious",            # Seriously affected primary structural components
    4: "Poor",               # Advanced section loss, deterioration, spalling, or scour
    5: "Fair",               # Minor section loss, cracking, spalling, or scour
    6: "Satisfactory",       # Structural elements show some minor deterioration
    7: "Good",               # Some minor problems noted
    8: "Very Good",          # No problems noted
    9: "Excellent",          # No problems noted
}


# =============================================================================
# Parsing Utilities
# =============================================================================

def parse_condition(raw: str) -> Optional[int]:
    """
    Parse an NBI condition rating field.

    Returns an integer 0-9, or None if the field is blank or "N"
    (N means "not applicable" — e.g., culvert rating on a non-culvert bridge).
    """
    raw = raw.strip()
    if not raw or raw in ("N", " "):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def parse_float(raw: str) -> Optional[float]:
    """Parse a numeric field to float, returning None if blank or invalid."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_int(raw: str) -> Optional[int]:
    """
    Parse a numeric field to int, returning None if blank, zero, or invalid.

    Returns None for zero because many NBI fields use 0 or "0000" to mean
    "not reported" rather than an actual zero value.
    """
    raw = raw.strip()
    if not raw or raw == "0000":
        return None
    try:
        v = int(raw)
        return v if v > 0 else None
    except ValueError:
        return None


def decode_latlon(degrees: str, minutes: str, seconds: str, is_lon: bool = False) -> Optional[float]:
    """
    Convert NBI's Degrees-Minutes-Seconds encoding to decimal degrees.

    NBI stores coordinates in DMS format across multiple fixed-width fields:
      Latitude:  DDMMSSSS  (degrees=4 chars, minutes=2 chars, seconds=4 chars)
      Longitude: DDDMMSSSS (degrees=5 chars, minutes=2 chars, seconds=4 chars)

    The seconds field encodes hundredths (SSSS = seconds * 100), so we divide
    by 100 before converting.

    For US bridges, longitude is always West, so we negate it to get standard
    negative-West decimal degrees used by mapping libraries.
    """
    try:
        d = float(degrees.strip() or 0)
        m = float(minutes.strip() or 0)
        # Seconds are encoded as SSSS where actual seconds = SSSS / 100
        s = float(seconds.strip() or 0) / 100
        decimal = d + m / 60 + s / 3600
        # Negate longitude because US bridges are in the Western hemisphere
        return -decimal if is_lon else decimal
    except (ValueError, ZeroDivisionError):
        return None


# =============================================================================
# Main Parser
# =============================================================================

def parse_nbi_line(line: str) -> Optional[NBIRecord]:
    """
    Parse a single 445-character NBI record into a typed NBIRecord.

    Returns None for malformed lines (too short or unparseable).
    Lines shorter than 100 chars are assumed to be headers or blank lines.

    The `get()` helper uses 1-indexed column numbers to match the FHWA spec,
    making it easy to cross-reference with the official documentation.
    """
    if len(line.rstrip('\n\r')) < 100:
        return None

    # Pad to 445 chars in case the line is slightly short (some older files)
    line = line.rstrip('\n\r').ljust(445)

    def get(start: int, end: int) -> str:
        """Extract characters from 1-indexed inclusive column range."""
        return line[start - 1:end]

    r = NBIRecord()

    # -- Identity (verified against FHWA spec) --
    r.state_code = get(1, 3).strip()          # Item 1: cols 1-3
    r.structure_number = get(4, 18).strip()   # Item 8: cols 4-18 (15-char unique ID)
    r.county_code = get(30, 32).strip()       # Item 3: cols 30-32 (3-digit FIPS)
    r.facility_carried = get(63, 80).strip()  # Item 7: cols 63-80 (what's ON the bridge)
    r.location = get(81, 105).strip()         # Item 9: cols 81-105

    # -- Coordinates (Item 16: cols 130-137, Item 17: cols 138-146) --
    # Latitude: 8 chars = DDMMSSSS (2-digit degrees, 2-digit minutes, 4-digit seconds×100)
    r.latitude = decode_latlon(get(130, 131), get(132, 133), get(134, 137), is_lon=False)
    # Longitude: 9 chars = DDDMMSSSS (3-digit degrees, 2-digit minutes, 4-digit seconds×100)
    r.longitude = decode_latlon(get(138, 140), get(141, 142), get(143, 146), is_lon=True)
    # Sanity check: Minnesota is between ~43°N and ~49°N latitude
    if r.latitude and (r.latitude < 40 or r.latitude > 50):
        r.latitude = None
        r.longitude = None

    # -- Ownership & Classification --
    r.owner_code = get(151, 152).strip()        # Item 21: cols 151-152
    r.functional_class = get(155, 156).strip()  # Item 26: cols 155-156
    r.year_built = parse_int(get(157, 160))     # Item 27: cols 157-160 (4-digit year)
    r.number_of_spans = parse_int(get(208, 210))   # Item 45: cols 208-210
    r.num_approach_spans = parse_int(get(211, 214)) # Item 46: cols 211-214

    # -- Traffic --
    r.adt = parse_int(get(165, 170))            # Item 29: cols 165-170 (6 digits)
    r.year_adt = parse_int(get(171, 174))       # Item 30: cols 171-174

    # -- Material & Design Type --
    # These codes tell us what the bridge is made of and how it's constructed.
    # Crucial for analysis: concrete bridges deteriorate differently from steel.
    r.material_code = get(202, 202).strip()       # Item 43A: col 202 (1 char)
    r.design_code = get(203, 204).strip()         # Item 43B: cols 203-204 (2 chars)
    r.approach_material = get(205, 205).strip()   # Item 44A: col 205
    r.approach_design = get(206, 207).strip()     # Item 44B: cols 206-207

    # -- Physical Dimensions --
    # Structure length (Item 49): cols 223-228, 6 chars, stored in tenths of meters.
    # Deck width (Item 52): cols 239-242, 4 chars, stored in tenths of meters.
    len_val = parse_float(get(223, 228))
    r.structure_length = len_val / 10 if len_val is not None else None
    deck_val = parse_float(get(239, 242))
    r.deck_width = deck_val / 10 if deck_val is not None else None

    # -- Condition Ratings (THE MOST IMPORTANT DATA) --
    # These are SINGLE-CHARACTER fields at specific column positions.
    # Each is a 0-9 rating (or 'N' for not applicable).
    # IMPORTANT: column position ≠ item number! Item 58 is at column 259.
    r.deck_condition = parse_condition(get(259, 259))         # Item 58: col 259
    r.superstructure_cond = parse_condition(get(260, 260))    # Item 59: col 260
    r.substructure_cond = parse_condition(get(261, 261))      # Item 60: col 261
    r.channel_condition = parse_condition(get(262, 262))      # Item 61: col 262
    r.culvert_condition = parse_condition(get(263, 263))      # Item 62: col 263

    # -- Load Ratings --
    # Item 64 (cols 265-267) and Item 66 (cols 269-271): 3 digits, metric tons × 10.
    op_val = parse_float(get(265, 267))
    r.operating_rating = op_val / 10 if op_val is not None else None
    inv_val = parse_float(get(269, 271))
    r.inventory_rating = inv_val / 10 if inv_val is not None else None

    # -- Appraisal Ratings (single-character fields) --
    r.structural_eval = parse_condition(get(272, 272))       # Item 67: col 272
    r.deck_geometry_eval = parse_condition(get(273, 273))    # Item 68: col 273
    r.underclear_eval = parse_condition(get(274, 274))       # Item 69: col 274
    r.waterway_adequacy = parse_condition(get(276, 276))     # Item 71: col 276
    r.approach_alignment = parse_condition(get(277, 277))    # Item 72: col 277

    # -- Inspection Metadata --
    r.inspection_date = get(287, 290).strip()  # Item 90: cols 287-290, format MMYY

    # -- Sufficiency Rating --
    # Not in the standard 445-char submittal format, but sometimes included
    # in download files. We skip it for now — it can be computed from other fields.
    r.sufficiency_rating = None

    # -- SD/FO Status --
    # The download-only field CAT10 (Bridge Condition) is at col 434.
    r.sd_fo_status = get(434, 434).strip()

    # -- Year Reconstructed & Truck % --
    r.year_reconstructed = parse_int(get(362, 365))     # Item 106: cols 362-365
    r.truck_pct = parse_float(get(370, 371))            # Item 109: cols 370-371

    return r
