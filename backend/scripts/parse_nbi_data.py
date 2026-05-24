"""
Script: Parse downloaded NBI ASCII files and load them into the SQLite database.

WHAT THIS DOES
--------------
Reads the fixed-width NBI text files downloaded by download_nbi_data.py,
extracts records for our 5 target counties, and loads them into the database.

For each matching record, it creates/updates:
  1. A County row (one per county, created on first encounter)
  2. A Bridge row (one per unique structure number)
  3. An Inspection row (one per bridge per year)

DATA FLOW
---------
    NBI ASCII file (one per year, ~13,000 lines for MN)
        → filter to 5 target counties (~200-800 bridges per county)
        → parse each line into an NBIRecord (via nbi_parser.py)
        → upsert County, Bridge, and Inspection rows

UPSERT PATTERN
--------------
We use "upsert" (update-or-insert) rather than simple insert because:
  - The same bridge appears in every year's file
  - Running the script multiple times should be safe (idempotent)
  - Bridge static attributes should reflect the most recent data

COMPUTED FIELDS
--------------
During loading, we also compute derived fields:
  - min_condition: the worst condition rating across deck/super/sub
  - health_index: a weighted composite score (0-100) for dashboards

USAGE
-----
    python -m backend.scripts.parse_nbi_data
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pathlib import Path
from tqdm import tqdm

from backend.config import settings
from backend.db.session import init_db_sync, get_sync_db
from backend.db.models import Bridge, County, Inspection
from backend.utils.nbi_parser import (
    parse_nbi_line,
    MATERIAL_CODES, DESIGN_CODES, OWNER_CODES,
    CONDITION_RATINGS,
)


def compute_health_index(deck, superstr, substr, culvert) -> float | None:
    """
    Calculate a composite health index on a 0-100 scale.

    For regular bridges (non-culverts), we use a weighted average of
    the three main condition ratings:
      - Superstructure: 40% weight (most critical structural component)
      - Substructure: 30% weight (foundation and supports)
      - Deck: 30% weight (riding surface, most exposed to wear)

    For culvert-type structures, the culvert rating replaces all three
    (NBI uses a single rating for culverts instead of deck/super/sub).

    The result is normalised to 0-100 by dividing by the max rating (9).

    Example: deck=7, superstructure=6, substructure=5
      → (7×0.30 + 6×0.40 + 5×0.30) / 9 × 100 = 66.7
    """
    if culvert is not None:
        return culvert / 9.0 * 100

    vals = [
        (deck, 0.30),
        (superstr, 0.40),
        (substr, 0.30),
    ]
    total_weight = 0.0
    weighted_sum = 0.0
    for val, weight in vals:
        if val is not None:
            weighted_sum += val * weight
            total_weight += weight

    if total_weight == 0:
        return None
    return (weighted_sum / total_weight) / 9.0 * 100


def upsert_county(db, fips_code: str, name: str) -> County:
    """
    Get or create a County row.

    Counties are created on first encounter and reused for subsequent bridges.
    The FIPS code is the unique identifier.
    """
    c = db.query(County).filter_by(fips_code=fips_code).first()
    if not c:
        c = County(fips_code=fips_code, name=name, state="MN")
        db.add(c)
        db.flush()  # Flush to get the auto-generated ID immediately
    return c


def upsert_bridge(db, record, county: County) -> Bridge:
    """
    Get or create a Bridge row, updating static fields from the latest data.

    The structure_number is the unique natural key for bridges in the NBI.
    Static fields (location, material, dimensions) are always overwritten
    with the latest year's data, because small corrections happen between years.

    We also translate raw NBI codes into human-readable names using the
    lookup tables (e.g., material_code "5" → "Prestressed Concrete").
    """
    b = db.query(Bridge).filter_by(structure_number=record.structure_number).first()
    if not b:
        b = Bridge(
            structure_number=record.structure_number,
            county_id=county.id,
        )
        db.add(b)

    # Update all static fields with the most recent data
    b.latitude = record.latitude
    b.longitude = record.longitude
    b.facility_carried = record.facility_carried
    b.location_description = record.location
    b.owner_code = record.owner_code
    b.owner_name = OWNER_CODES.get(record.owner_code.zfill(2), record.owner_code)
    b.functional_class = record.functional_class
    b.year_built = record.year_built
    b.year_reconstructed = record.year_reconstructed
    b.material_code = record.material_code
    b.material_name = MATERIAL_CODES.get(record.material_code.strip(), record.material_code)
    b.design_code = record.design_code
    b.design_name = DESIGN_CODES.get(record.design_code.strip(), record.design_code)
    b.number_of_spans = record.number_of_spans
    b.structure_length = record.structure_length
    b.deck_width = record.deck_width
    b.adt = record.adt
    b.adt_year = record.year_adt

    db.flush()
    return b


def upsert_inspection(db, record, bridge: Bridge, year: int) -> Inspection:
    """
    Get or create an Inspection row for a bridge in a given year.

    Each inspection captures the bridge's condition at one point in time.
    The (bridge_id, data_year) pair is unique — enforced by a DB constraint.

    We also compute derived fields here:
      - min_condition: worst of deck/super/sub (quick health indicator)
      - health_index: weighted composite for dashboard display
      - structurally_deficient: True if any main rating ≤ 4
    """
    insp = db.query(Inspection).filter_by(bridge_id=bridge.id, data_year=year).first()
    if not insp:
        insp = Inspection(bridge_id=bridge.id, data_year=year)
        db.add(insp)

    # -- NBI condition ratings --
    insp.inspection_date = record.inspection_date
    insp.deck_condition = record.deck_condition
    insp.superstructure_condition = record.superstructure_cond
    insp.substructure_condition = record.substructure_cond
    insp.channel_condition = record.channel_condition
    insp.culvert_condition = record.culvert_condition

    # -- Load and appraisal ratings --
    insp.operating_rating = record.operating_rating
    insp.inventory_rating = record.inventory_rating
    insp.structural_eval = record.structural_eval
    insp.deck_geometry_eval = record.deck_geometry_eval
    insp.underclearance_eval = record.underclear_eval
    insp.waterway_adequacy = record.waterway_adequacy
    insp.approach_alignment = record.approach_alignment
    insp.sufficiency_rating = record.sufficiency_rating
    insp.sd_fo_status = record.sd_fo_status

    # -- Derived: structurally deficient flag --
    # A bridge is "SD" if any of the three main condition ratings is ≤ 4 (Poor or worse).
    # This is a federal classification that affects funding eligibility.
    insp.structurally_deficient = (record.sd_fo_status == "SD") if record.sd_fo_status else None

    # -- Derived: minimum condition (quick "worst case" indicator) --
    # This lets us sort bridges by their weakest component.
    mins = [x for x in [record.deck_condition, record.superstructure_cond,
                          record.substructure_cond] if x is not None]
    insp.min_condition = min(mins) if mins else None

    # -- Derived: health index (0-100 composite for visualisation) --
    insp.health_index = compute_health_index(
        record.deck_condition, record.superstructure_cond,
        record.substructure_cond, record.culvert_condition
    )

    db.flush()
    return insp


def load_nbi_file(db, filepath: Path, year: int, target_counties: dict[str, str]):
    """
    Parse one NBI file and load matching county bridges into the database.

    The NBI file contains ALL bridges in Minnesota (~13,000). We filter
    to just our 5 target counties using the FIPS code at columns 21-23.

    Performance: We use a county cache to avoid repeated DB lookups.
    Each county is queried once, then reused for all its bridges.
    """
    # Build reverse lookup: FIPS code → county name (e.g., "053" → "Hennepin")
    fips_to_name = {v: k for k, v in target_counties.items()}
    county_cache: dict[str, County] = {}

    loaded = 0
    skipped = 0

    # Read entire file into memory (files are <5MB, this is fine)
    # latin-1 encoding handles any non-ASCII characters in older NBI files
    with open(filepath, "r", encoding="latin-1") as f:
        lines = f.readlines()

    for line in tqdm(lines, desc=f"  Parsing {year}", unit="rec"):
        record = parse_nbi_line(line)
        if not record:
            skipped += 1
            continue

        # Filter: only process bridges in our target counties
        county_fips = record.county_code.zfill(3)  # Pad to 3 digits (e.g., "53" → "053")
        if county_fips not in fips_to_name:
            skipped += 1
            continue

        county_name = fips_to_name[county_fips]

        # Use cache to avoid repeated county lookups
        if county_fips not in county_cache:
            county_cache[county_fips] = upsert_county(db, county_fips, county_name)
        county = county_cache[county_fips]

        # Upsert bridge and its inspection for this year
        bridge = upsert_bridge(db, record, county)
        upsert_inspection(db, record, bridge, year)

        loaded += 1

    # Commit all changes for this file in one transaction
    db.commit()
    print(f"  ✓ Year {year}: loaded {loaded} records, skipped {skipped}")
    return loaded


def main():
    """
    Main entry point: initialise the database and load all NBI files.

    The script is idempotent — running it again will update existing records
    rather than creating duplicates (thanks to the upsert pattern).
    """
    print("Initialising database...")
    init_db_sync()

    db = get_sync_db()
    try:
        total = 0
        for year in settings.nbi_years:
            year_dir = settings.raw_nbi_dir / str(year)
            txt_files = list(year_dir.glob("*.txt")) + list(year_dir.glob("*.asc"))

            if not txt_files:
                print(f"⚠ No NBI files found for {year} in {year_dir}")
                print(f"  Run: python -m backend.scripts.download_nbi_data first")
                continue

            print(f"\nLoading NBI data — {year}")
            for f in txt_files:
                total += load_nbi_file(db, f, year, settings.target_counties)

        print(f"\n✅ Done. Total records loaded: {total}")

        # Print a summary of what's in the database now
        counties = db.query(County).all()
        print(f"\nDatabase summary:")
        for c in counties:
            bridges = db.query(Bridge).filter_by(county_id=c.id).count()
            inspections = db.query(Inspection).join(Bridge).filter(Bridge.county_id == c.id).count()
            print(f"  {c.name}: {bridges} bridges, {inspections} inspections")

    finally:
        db.close()


if __name__ == "__main__":
    main()
