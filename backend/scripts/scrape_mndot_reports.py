"""
Script: Scrape MnDOT bridge inspection reports.

WHAT THIS DOES
--------------
Downloads bridge inspection report PDFs from MnDOT's public portal:
  https://reports.dot.state.mn.us/bridgereports/

For each bridge in our database (target counties), it:
  1. Drives the report form in headless Chromium (Playwright)
  2. Intercepts the `viewrpt.cwr` network request that delivers the PDF
  3. Downloads the PDF using the captured URL (with session authentication)
  4. Saves it to data/raw/pdfs/{structure_number}/

WHY PLAYWRIGHT (NOT requests)
------------------------------
The MnDOT portal is a SAP BusinessObjects (BOE) / Crystal Reports server
fronted by an ASP.NET Master page. The flow requires JavaScript execution:

  1. FormDefinition.aspx → fill form → click "View Report"
  2. rpt.js opens a new tab with the BOE OpenDocument viewer
  3. The BOE viewer POSTs to CrystalReports/view.do
  4. view.do redirects to viewrpt.cwr?...&bttoken=<session-token>
  5. viewrpt.cwr responds with application/pdf

Step 4 contains a `bttoken` (session bearer token) that is dynamically
generated per session. Only a real browser executing the JavaScript can
obtain this token. We intercept it using Playwright's network interception.

NETWORK FLOW (confirmed by Playwright diagnostics 2026-05-19)
-------------------------------------------------------------
  FormDefinition.aspx?rID=3488660
    → POST (bridge number, radio params)
    → rpt.js opens new tab
  BOE/OpenDocument/2405022217/OpenDocument/opendoc/openDocument.jsp
    → BOE viewer HTML (contains two frames)
  BOE/OpenDocument/2405022217/CrystalReports/view.do  [POST]
    → redirects to
  BOE/OpenDocument/2405022217/CrystalReports/viewrpt.cwr
    ?id=3488660&init=html&language=en&doclocale=en_US
    &rpi=0&bypassLatestInstance=true&cafWebSesInit=true
    &bttoken=<session-bearer-token>
    → 200 application/pdf  ← THIS IS THE PDF

REQUIRES A US-BASED CONNECTION
-------------------------------
The MnDOT server blocks non-US traffic.
Run via a US VPN or proxy if you're outside the United States.

USAGE
-----
    python -m backend.scripts.scrape_mndot_reports
    python -m backend.scripts.scrape_mndot_reports --bridge 2440
    python -m backend.scripts.scrape_mndot_reports --dry-run
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import re
import time
import argparse
from pathlib import Path

import requests
from tqdm import tqdm
from playwright.sync_api import (
    sync_playwright, BrowserContext, Page,
    Response as PWResponse,
    TimeoutError as PWTimeout,
)

from backend.config import settings
from backend.db.session import get_sync_db
from backend.db.models import Bridge, County


# ─── Constants ────────────────────────────────────────────────────────────────

BASE_URL  = "https://reports.dot.state.mn.us/bridgereports/"
FORM_URL  = BASE_URL + "FormDefinition.aspx?rID=3488660"

# Polite delay between bridge requests
REQUEST_DELAY_SEC = 2.0

# Form element selectors (by HTML id — stable across sessions)
SEL_BRIDGE_INPUT = "#MainContent_SingleBridge"
SEL_VIEW_BUTTON  = "#MainContent_View"
SEL_TOGGLE_BOTH  = "input[name='Master$MainContent$BridgeInfoToggle'][value='1']"
SEL_FORMAT_PDF   = "input[name='Master$MainContent$rdlFormat'][value='P']"

# The CrystalReports URL segment that identifies the PDF endpoint
PDF_URL_MARKER = "viewrpt.cwr"

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ─── Site Probe ───────────────────────────────────────────────────────────────

def probe_site(page: Page) -> bool:
    """Navigate to the form and verify the site is reachable."""
    try:
        page.goto(FORM_URL, timeout=30_000, wait_until="domcontentloaded")
        page.wait_for_selector(SEL_BRIDGE_INPUT, timeout=10_000)
        print(f"  Site reachable ✓  (title: {page.title()!r})")
        return True
    except PWTimeout:
        print("  ✗ Timed out — is US VPN connected?")
        return False
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return False


# ─── Report Download ──────────────────────────────────────────────────────────

def download_report_for_bridge(
    page: Page,
    context: BrowserContext,
    structure_number: str,
    dest: Path,
    dry_run: bool = False,
) -> bool:
    """
    Submit the report form for one bridge and save the resulting PDF.

    Strategy:
    1. Register a Playwright network response listener that watches for the
       `viewrpt.cwr` URL — this is the endpoint that serves the PDF.
    2. Fill and submit the form — this kicks off the multi-step BOE flow.
    3. The listener captures the `viewrpt.cwr` URL including the `bttoken`.
    4. We use that URL + the current session cookies to download the PDF.

    Returns True on success, False on failure.
    """
    if dest.exists():
        # Validate the file is a real, complete PDF — not a partial file left
        # behind by a hard crash (power-off mid-download). A valid PDF:
        #   1. Starts with the PDF magic bytes: %PDF
        #   2. Is at least 10 KB (real inspection reports are 100–500 KB)
        try:
            size_kb = dest.stat().st_size / 1024
            with open(dest, "rb") as f:
                magic = f.read(4)
            if magic == b"%PDF" and size_kb >= 10:
                print(f"    ✓ Already downloaded: {dest.name} ({size_kb:.0f} KB)")
                return True
            else:
                print(f"    ⚠ Corrupt/partial file found ({size_kb:.1f} KB, magic={magic!r}) — re-downloading")
                dest.unlink()
        except Exception:
            dest.unlink(missing_ok=True)

    if dry_run:
        print(f"    [dry-run] Would download bridge {structure_number}")
        return True

    # ── Set up network interception to capture the PDF URL ───────────────────
    captured_pdf_url: list[str] = []  # mutable container for closure

    def on_response(response: PWResponse) -> None:
        if PDF_URL_MARKER in response.url and response.status == 200:
            ct = response.headers.get("content-type", "")
            if "pdf" in ct.lower():
                captured_pdf_url.append(response.url)
                print(f"    ✓ Intercepted PDF response: {response.url[:80]}...")

    try:
        # ── Fill and submit the form ─────────────────────────────────────────
        page.goto(FORM_URL, timeout=30_000, wait_until="domcontentloaded")
        page.wait_for_selector(SEL_BRIDGE_INPUT, timeout=10_000)

        bridge_input = page.locator(SEL_BRIDGE_INPUT)
        bridge_input.click(click_count=3)
        bridge_input.fill(structure_number)

        page.locator(SEL_FORMAT_PDF).check()
        page.locator(SEL_TOGGLE_BOTH).check()

        print(f"    Submitting form for bridge {structure_number}...")

        # Register response listener on the context so it fires across all pages/frames
        context.on("response", on_response)

        # Click "View Report" — rpt.js will open a new tab
        with context.expect_page(timeout=60_000) as new_page_info:
            page.locator(SEL_VIEW_BUTTON).click()

        new_page = new_page_info.value

        # Wait for the BOE viewer to fully render the report.
        # The PDF endpoint is hit as part of the Crystal Reports frame loading.
        # "networkidle" waits until all network requests are complete.
        print(f"    BOE report rendering (waiting for PDF request)...")
        try:
            new_page.wait_for_load_state("networkidle", timeout=120_000)
        except PWTimeout:
            pass  # networkidle may not be reached; check if we got the URL anyway

        # Remove the listener
        context.remove_listener("response", on_response)

        # Close the viewer tab
        new_page.close()

    except PWTimeout:
        context.remove_listener("response", on_response)
        print(f"    ✗ Timeout waiting for BOE viewer (bridge {structure_number})")
        return False
    except Exception as e:
        context.remove_listener("response", on_response)
        print(f"    ✗ Form submission error: {e}")
        return False

    if not captured_pdf_url:
        print(f"    ✗ Did not intercept a viewrpt.cwr PDF response")
        return False

    pdf_url = captured_pdf_url[0]

    # ── Download the PDF via requests using the browser's session cookies ─────
    # The bttoken in the URL is sufficient to authenticate the download;
    # we also pass cookies for belt-and-suspenders.
    cookies = {c["name"]: c["value"] for c in context.cookies()}
    headers = {
        "User-Agent": UA,
        "Accept": "application/pdf,*/*",
        "Referer": FORM_URL,
    }

    try:
        r = requests.get(
            pdf_url,
            cookies=cookies,
            headers=headers,
            stream=True,
            timeout=120,
        )
        r.raise_for_status()

        content_type = r.headers.get("content-type", "").lower()
        if "pdf" not in content_type:
            print(f"    ✗ Expected PDF, got: {content_type!r}")
            return False

        total = int(r.headers.get("content-length", 0))
        with open(dest, "wb") as f, tqdm(
            total=total, unit="B", unit_scale=True,
            desc=f"    {dest.name}", leave=False,
        ) as pbar:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
                pbar.update(len(chunk))

        size_kb = dest.stat().st_size / 1024
        if size_kb < 5:
            print(f"    ✗ File too small ({size_kb:.1f} KB) — likely an error, not a PDF")
            dest.unlink()
            return False

        print(f"    ✓ Saved: {dest.name} ({size_kb:.0f} KB)")
        return True

    except requests.exceptions.Timeout:
        print(f"    ✗ Timeout downloading PDF")
    except Exception as e:
        print(f"    ✗ Download failed: {e}")

    if dest.exists():
        dest.unlink()
    return False


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(single_bridge: str | None = None, dry_run: bool = False):
    """
    Main entry point.

    If `single_bridge` is provided, only process that one bridge (test mode).
    If `dry_run` is True, probe site access without downloading.
    """
    print("=" * 60)
    print("MnDOT Bridge Inspection Report Scraper (Playwright)")
    print("=" * 60)
    if dry_run:
        print("  *** DRY RUN — no files will be downloaded ***")
    print(f"\nTarget portal : {BASE_URL}")
    print(f"Report form   : {FORM_URL}")

    db = get_sync_db()
    try:
        if single_bridge:
            bridges = db.query(Bridge).filter_by(
                structure_number=single_bridge
            ).all()
            if not bridges:
                print(f"\n✗ Bridge {single_bridge!r} not found in database.")
                return
        else:
            target_fips = set(settings.target_counties.values())
            counties = db.query(County).filter(
                County.fips_code.in_(target_fips)
            ).all()
            bridges = []
            for county in counties:
                bridges.extend(
                    db.query(Bridge).filter_by(county_id=county.id).all()
                )

        print(f"\nBridges to process: {len(bridges)}")

        downloaded = 0
        skipped    = 0
        failed     = 0

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                accept_downloads=True,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                user_agent=UA,
            )
            page = context.new_page()

            print("\n--- Step 1: Probing site ---")
            if not probe_site(page):
                print("\n✗ Cannot reach MnDOT portal. Check US VPN connection.")
                return

            print(f"\n--- Step 2: Downloading reports for {len(bridges)} bridge(s) ---\n")

            for bridge in tqdm(bridges, desc="Bridges", unit="bridge"):
                bridge_dir = settings.raw_pdf_dir / bridge.structure_number
                bridge_dir.mkdir(parents=True, exist_ok=True)

                dest = bridge_dir / f"inspection_{bridge.structure_number}.pdf"

                print(f"\n  Bridge {bridge.structure_number} ({bridge.facility_carried})")

                success = download_report_for_bridge(
                    page, context,
                    bridge.structure_number,
                    dest,
                    dry_run=dry_run,
                )

                if success:
                    if dest.exists():
                        downloaded += 1
                    else:
                        skipped += 1
                else:
                    failed += 1

                if not dry_run:
                    time.sleep(REQUEST_DELAY_SEC)

            context.close()
            browser.close()

        print(f"\n{'=' * 60}")
        print(f"✅ Done.")
        print(f"   Downloaded  : {downloaded}")
        print(f"   Skipped     : {skipped}  (already existed or dry-run)")
        print(f"   Failed      : {failed}")
        print(f"   PDFs saved to: {settings.raw_pdf_dir}")

    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scrape MnDOT bridge inspection report PDFs (headless browser)"
    )
    parser.add_argument(
        "--bridge",
        help=(
            "Only process this one bridge structure number (e.g. --bridge 2440). "
            "Use this to test before running the full scrape."
        ),
        default=None,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Probe the site and list bridges but do not download anything.",
    )
    args = parser.parse_args()
    main(single_bridge=args.bridge, dry_run=args.dry_run)
