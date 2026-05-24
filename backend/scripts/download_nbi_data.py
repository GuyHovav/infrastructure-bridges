"""
Script: Download NBI ASCII data files for Minnesota.

WHAT THIS DOES
--------------
Downloads bridge inspection data from the Federal Highway Administration (FHWA)
for Minnesota, for the years configured in settings.nbi_years (2023, 2024, 2025).

The FHWA publishes National Bridge Inventory data as per-state files at:
  https://www.fhwa.dot.gov/bridge/nbi/ascii.cfm

Each state's data is a single text file containing one line per bridge
in the 445-character fixed-width format (parsed by nbi_parser.py).

WHY THIS DATA SOURCE
--------------------
The original assignment pointed to MnDOT's bridge reports site
(reports.dot.state.mn.us/bridgereports), which was intermittently unavailable.
The FHWA NBI is actually a stronger choice because:
  1. It's the authoritative federal dataset (states submit TO this)
  2. It's structured data (no PDF parsing needed for the core data)
  3. Annual snapshots from 1992-2025 give us rich historical data
  4. It's reliably available and well-documented

DOWNLOAD STRATEGY
-----------------
FHWA's file naming isn't 100% consistent across years, so we try multiple
URL patterns and fall back to scraping the year's index page if needed.
Files are organized as: data/raw/nbi/{year}/{filename}

USAGE
-----
    python -m backend.scripts.download_nbi_data
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import re
import requests
from pathlib import Path
from tqdm import tqdm

from backend.config import settings


# Base URL for all NBI downloads on the FHWA website
NBI_BASE_URL = "https://www.fhwa.dot.gov/bridge/nbi"

# Minnesota's FIPS state code — used in file naming
MN_FIPS = "27"

# The FHWA isn't perfectly consistent with file naming across years.
# Some years use state abbreviation (MN), others use FIPS code (27).
# We try all known patterns until one works.
#
# As of 2023-2025, the actual pattern is: MN{2-digit year}.txt
# (e.g., MN23.txt, MN24.txt, MN25.txt) — plain text, not zipped.
# We keep the old ZIP patterns as fallbacks for older/future years.
URL_PATTERNS = [
    "{base}/{year}/MN{year_short}.txt",   # ← Current pattern (2023+)
    "{base}/{year}/MN{year}.zip",
    "{base}/{year}/mn{year}.zip",
    "{base}/{year}/del{fips}.zip",
    "{base}/{year}/DEL{fips}.zip",
]


def find_nbi_download_url(year: int) -> str | None:
    """
    Locate the download URL for Minnesota's NBI data for a given year.

    Strategy:
      1. Try each known URL pattern with a HEAD request (fast, no download)
      2. If none match, scrape the year's index page for links containing "mn" or "27"
      3. Return the first working URL, or None if nothing found
    """
    session = requests.Session()
    # Identify ourselves as a research tool — good web scraping etiquette
    session.headers.update({"User-Agent": "Mozilla/5.0 (research/data-download)"})

    # Strategy 1: Try known URL patterns
    for pattern in URL_PATTERNS:
        url = pattern.format(base=NBI_BASE_URL, year=year, fips=MN_FIPS,
                             year_short=str(year)[-2:])
        try:
            # HEAD request checks if URL exists without downloading the file
            r = session.head(url, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                print(f"  ✓ Found: {url}")
                return url
        except Exception:
            pass

    # Strategy 2: Scrape the year's index page for download links
    index_url = f"{NBI_BASE_URL}/ascii{year}.cfm"
    try:
        r = session.get(index_url, timeout=15)
        if r.status_code == 200:
            # Look for href links that mention MN, mn, or 27 and end in .zip or .txt
            links = re.findall(r'href=["\']([^"\']*(?:mn|MN|27)[^"\']*\.(?:zip|txt))["\']', r.text)
            for link in links:
                # Convert relative URLs to absolute
                full = link if link.startswith("http") else f"https://www.fhwa.dot.gov{link}"
                print(f"  ✓ Scraped: {full}")
                return full
    except Exception as e:
        print(f"  ⚠ Could not scrape index page: {e}")

    return None


def download_file(url: str, dest: Path) -> bool:
    """
    Download a file from URL to local path, with a progress bar.

    Uses streaming (stream=True) to avoid loading the entire file into
    memory at once — important for large state-level NBI files (~1-5 MB).
    """
    try:
        r = requests.get(url, stream=True, timeout=60,
                        headers={"User-Agent": "Mozilla/5.0 (research)"})
        r.raise_for_status()

        # Use content-length header for progress bar (if server provides it)
        total = int(r.headers.get("content-length", 0))

        with open(dest, "wb") as f, tqdm(
            total=total, unit="B", unit_scale=True, desc=dest.name
        ) as pbar:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
                pbar.update(len(chunk))
        return True
    except Exception as e:
        print(f"  ✗ Download failed: {e}")
        return False


def extract_zip(zip_path: Path, dest_dir: Path) -> list[Path]:
    """
    Extract .txt/.asc files from a ZIP archive.

    NBI files are often distributed as ZIPs containing a single .txt file.
    We only extract text-based files and ignore any other content.
    """
    import zipfile
    extracted = []
    with zipfile.ZipFile(zip_path, "r") as z:
        for name in z.namelist():
            if name.lower().endswith((".txt", ".asc")):
                z.extract(name, dest_dir)
                extracted.append(dest_dir / name)
                print(f"  Extracted: {name}")
    return extracted


def main():
    """
    Download NBI files for all configured years.

    Skips years that have already been downloaded (idempotent — safe to re-run).
    Handles both ZIP and direct TXT downloads.
    """
    settings.raw_nbi_dir.mkdir(parents=True, exist_ok=True)

    for year in settings.nbi_years:
        print(f"\n{'='*50}")
        print(f"Downloading NBI data for Minnesota — {year}")
        print(f"{'='*50}")

        # Each year gets its own subfolder
        year_dir = settings.raw_nbi_dir / str(year)
        year_dir.mkdir(exist_ok=True)

        # Skip if we already have data for this year (idempotent)
        existing = list(year_dir.glob("*.txt")) + list(year_dir.glob("*.asc"))
        if existing:
            print(f"  ✓ Already have data: {[f.name for f in existing]}")
            continue

        # Find the download URL
        url = find_nbi_download_url(year)
        if not url:
            print(f"  ✗ Could not find download URL for {year}. Skipping.")
            continue

        # Download the file
        filename = url.split("/")[-1]
        dest = year_dir / filename

        if download_file(url, dest):
            # If it's a ZIP, extract the text file inside
            if dest.suffix.lower() == ".zip":
                txt_files = extract_zip(dest, year_dir)
                if txt_files:
                    print(f"  ✓ Ready: {txt_files}")
            else:
                print(f"  ✓ Downloaded: {dest}")

    print("\n✅ NBI download complete.")


if __name__ == "__main__":
    main()
