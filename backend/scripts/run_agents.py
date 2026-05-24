"""
Script: Run extraction agents on downloaded bridge inspection PDFs.

WHAT THIS DOES
--------------
Processes each downloaded PDF through two LLM agents:
  1. Defect Agent       — extracts structured defect records
  2. Recommendation Agent — extracts maintenance/repair actions

Results are written to the SQLite database as Defect and Recommendation
rows linked to the correct Inspection record.

WHY TWO SEPARATE AGENTS (NOT ONE)
----------------------------------
Defects and Recommendations are different extraction tasks:
  - Defects describe what IS wrong (observable conditions)
  - Recommendations describe what TO DO (proposed actions)
Splitting them gives each agent a focused, unambiguous job. A single
combined agent tends to conflate the two or miss records for one category
when the other is prominent in the text.

HOW THE DB LINKAGE WORKS
-------------------------
The PDF filename encodes the bridge structure number (e.g. inspection_2440.pdf).
We look up the most recent Inspection row for that bridge and attach all
extracted Defect and Recommendation rows to it via inspection_id.

If no Inspection row exists for a bridge, we skip it with a warning —
it means parse_nbi_data.py hasn't been run yet for that bridge.

IDEMPOTENCY
-----------
Before processing a PDF, we check if Defect rows already exist for that
inspection. If they do, we skip it. This makes the script safe to re-run
after partial failures or interruptions.

USAGE
-----
    # Process all downloaded PDFs:
    python -m backend.scripts.run_agents

    # Test on one bridge before committing to the full run:
    python -m backend.scripts.run_agents --bridge 2440

    # Process only the first N PDFs (useful for cost estimation):
    python -m backend.scripts.run_agents --limit 10
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import argparse
import traceback
from pathlib import Path

from tqdm import tqdm
from sqlalchemy import desc

from backend.config import settings
from backend.db.session import get_sync_db
from backend.db.models import Bridge, Inspection, Defect, Recommendation
from backend.agents.pdf_extractor import extract_text, extract_images
from backend.agents.defect_agent import extract_defects
from backend.agents.recommendation_agent import extract_recommendations


# ─── DB Helpers ───────────────────────────────────────────────────────────────

def get_latest_inspection(db, bridge_id: int) -> Inspection | None:
    """
    Return the most recent Inspection row for a bridge.

    We target the latest year because that's what the PDF corresponds to
    (MnDOT generates the report from current inspection data).

    Args:
        db:        SQLAlchemy sync session.
        bridge_id: Primary key of the Bridge row.

    Returns:
        The most recent Inspection row, or None if no inspections exist.
    """
    return (
        db.query(Inspection)
        .filter_by(bridge_id=bridge_id)
        .order_by(desc(Inspection.data_year))
        .first()
    )


def already_processed(db, inspection_id: int) -> bool:
    """
    Check if this inspection has already been processed by the agent.

    We use the presence of Defect rows as the idempotency signal.
    Even bridges with zero defects will have been 'processed' — we
    distinguish that case by checking the inspector_notes field instead
    (it gets written regardless of defect count).

    Args:
        db:            SQLAlchemy sync session.
        inspection_id: Primary key of the Inspection row.

    Returns:
        True if this inspection has already been through the agent pipeline.
    """
    # If inspector_notes is populated, the pipeline has run for this inspection
    insp = db.query(Inspection).filter_by(id=inspection_id).first()
    return insp is not None and insp.inspector_notes is not None


def _deduplicate_defects(defects: list[dict]) -> list[dict]:
    """
    Remove duplicate defect records before DB insertion.

    The MnDOT PDFs sometimes embed multiple inspection years in a single
    document (e.g., a 2024 report includes historical 2022 data in the same
    pages). The LLM processes the full text and may extract the same defect
    description multiple times — once per time it appears across pages.

    We deduplicate on (defect_type, description) as the natural unique key:
    the same physical defect will always be described with the same type and
    text, even if it appears on multiple pages.

    Args:
        defects: Raw list of defect dicts from the LLM agent.

    Returns:
        Deduplicated list, preserving the first occurrence of each unique
        (defect_type, description) pair.
    """
    seen: set[tuple] = set()
    unique: list[dict] = []
    for d in defects:
        key = (d.get("defect_type", ""), d.get("description", ""))
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def _deduplicate_recommendations(recs: list[dict]) -> list[dict]:
    """
    Remove duplicate recommendation records before DB insertion.

    Same rationale as _deduplicate_defects — multi-year PDF content causes
    the LLM to extract the same recommendation multiple times.

    Deduplicates on (category, action_description) as the unique key.
    """
    seen: set[tuple] = set()
    unique: list[dict] = []
    for r in recs:
        key = (r.get("category", ""), r.get("action_description", ""))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def write_results(
    db,
    inspection: Inspection,
    report_text: str,
    defects: list[dict],
    recommendations: list[dict],
) -> None:
    """
    Persist extracted data to the database in a single transaction.

    Writes:
      - Defect rows (one per extracted defect, after deduplication)
      - Recommendation rows (one per extracted recommendation, after deduplication)
      - Updates Inspection.inspector_notes with the raw PDF text
        (useful for later full-text search or re-running agents)

    Deduplication rationale: MnDOT PDFs sometimes contain multi-year data in
    a single document. The LLM extracts defects from the full text, which can
    produce duplicate records for the same physical defect that appears across
    multiple year sections. We deduplicate before inserting.

    Args:
        db:              SQLAlchemy sync session.
        inspection:      The Inspection row to link results to.
        report_text:     Full extracted PDF text (stored for audit/re-run).
        defects:         List of defect dicts from extract_defects().
        recommendations: List of recommendation dicts from extract_recommendations().
    """
    # Store the raw text on the inspection row for audit trail + future re-runs
    # Truncate to avoid hitting SQLite's practical text limits (reports can be long)
    inspection.inspector_notes = report_text[:50_000]

    # Deduplicate before inserting — multi-year PDFs cause repeated extractions
    unique_defects = _deduplicate_defects(defects)
    unique_recs    = _deduplicate_recommendations(recommendations)

    # Write Defect rows
    for d in unique_defects:
        db.add(Defect(inspection_id=inspection.id, **d))

    # Write Recommendation rows
    for r in unique_recs:
        db.add(Recommendation(inspection_id=inspection.id, **r))

    db.commit()


# ─── Per-PDF Processing ───────────────────────────────────────────────────────

def process_pdf(db, pdf_path: Path) -> dict:
    """
    Run the full extraction pipeline on one bridge inspection PDF.

    Steps:
      1. Parse structure number from filename (inspection_2440.pdf → "2440")
      2. Look up Bridge and Inspection in the database
      3. Skip if already processed (idempotency)
      4. Extract text from PDF
      5. Run Defect Extraction Agent
      6. Run Recommendation Extraction Agent
      7. Extract and save embedded images
      8. Write all results to the database

    Args:
        db:       SQLAlchemy sync session.
        pdf_path: Path to the PDF file.

    Returns:
        A status dict with keys: structure_number, status, defects, recommendations.
        status is one of: "processed", "skipped", "no_inspection", "error"
    """
    # ── Parse bridge ID from filename ─────────────────────────────────────────
    # Filenames follow the pattern: inspection_{structure_number}.pdf
    stem = pdf_path.stem  # e.g. "inspection_2440"
    structure_number = stem.replace("inspection_", "")

    # ── Database lookup ───────────────────────────────────────────────────────
    bridge = db.query(Bridge).filter_by(structure_number=structure_number).first()
    if not bridge:
        # Bridge is in the PDF directory but not in the DB — shouldn't happen
        # in normal operation, but safe to handle gracefully.
        return {"structure_number": structure_number, "status": "no_bridge",
                "defects": 0, "recommendations": 0}

    inspection = get_latest_inspection(db, bridge.id)
    if not inspection:
        # PDF downloaded but NBI data not yet loaded — user needs to run
        # parse_nbi_data.py first.
        return {"structure_number": structure_number, "status": "no_inspection",
                "defects": 0, "recommendations": 0}

    # ── Idempotency check ─────────────────────────────────────────────────────
    if already_processed(db, inspection.id):
        return {"structure_number": structure_number, "status": "skipped",
                "defects": 0, "recommendations": 0}

    # ── Extract text from PDF ─────────────────────────────────────────────────
    report_text = extract_text(pdf_path)
    if not report_text.strip():
        return {"structure_number": structure_number, "status": "empty_pdf",
                "defects": 0, "recommendations": 0}

    # ── Run LLM extraction agents ─────────────────────────────────────────────
    defects         = extract_defects(structure_number, report_text)
    recommendations = extract_recommendations(structure_number, report_text)

    # ── Extract images from PDF ───────────────────────────────────────────────
    # Images are saved to disk; we don't write InspectionImage rows here yet
    # (that's a separate pipeline step — images need captioning first).
    extract_images(pdf_path, structure_number)

    # ── Persist to database ───────────────────────────────────────────────────
    write_results(db, inspection, report_text, defects, recommendations)

    return {
        "structure_number": structure_number,
        "status"          : "processed",
        "defects"         : len(defects),
        "recommendations" : len(recommendations),
        "defects_after_dedup" : None,  # populated by write_results internally
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(single_bridge: str | None = None, limit: int | None = None) -> None:
    """
    Main entry point: find all downloaded PDFs and run the agent pipeline.

    Args:
        single_bridge: If set, process only this bridge structure number.
        limit:         If set, process at most this many PDFs (for cost/time tests).
    """
    print("=" * 60)
    print("MnDOT Bridge Inspection — Agent Pipeline")
    print("=" * 60)
    print(f"\nExtraction model : {settings.gemini_model_extraction}")
    print(f"PDF directory    : {settings.raw_pdf_dir}")

    # ── Find PDFs to process ──────────────────────────────────────────────────
    if single_bridge:
        pdfs = sorted(
            (settings.raw_pdf_dir / single_bridge).glob("inspection_*.pdf")
        )
    else:
        pdfs = sorted(settings.raw_pdf_dir.rglob("inspection_*.pdf"))

    if limit:
        pdfs = pdfs[:limit]

    print(f"PDFs to process  : {len(pdfs)}\n")

    if not pdfs:
        print("No PDFs found. Run scrape_mndot_reports.py first.")
        return

    # ── Counters for the summary ──────────────────────────────────────────────
    processed     = 0
    skipped       = 0
    errors        = 0
    total_defects = 0
    total_recs    = 0

    db = get_sync_db()
    try:
        for pdf_path in tqdm(pdfs, desc="Processing PDFs", unit="pdf"):
            try:
                result = process_pdf(db, pdf_path)

                status = result["status"]
                if status == "processed":
                    processed     += 1
                    total_defects += result["defects"]
                    total_recs    += result["recommendations"]
                    print(
                        f"  ✓ {result['structure_number']:>10}  "
                        f"{result['defects']} defects  "
                        f"{result['recommendations']} recommendations"
                    )
                elif status == "skipped":
                    skipped += 1
                else:
                    # no_bridge, no_inspection, empty_pdf
                    print(f"  ⚠ {result['structure_number']:>10}  [{status}]")

            except Exception as e:
                # Catch per-PDF errors so one failure doesn't stop the whole run.
                # Print the error and move on — the PDF can be retried later.
                errors += 1
                name = pdf_path.stem
                print(f"  ✗ {name}  ERROR: {e}")
                if "--verbose" in sys.argv:
                    traceback.print_exc()

    finally:
        db.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"✅ Done.")
    print(f"   Processed   : {processed}")
    print(f"   Skipped     : {skipped}  (already done — safe to re-run)")
    print(f"   Errors      : {errors}")
    print(f"   Defects extracted        : {total_defects}")
    print(f"   Recommendations extracted: {total_recs}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run LLM extraction agents on MnDOT bridge inspection PDFs"
    )
    parser.add_argument(
        "--bridge",
        help="Process only this bridge structure number (e.g. --bridge 2440).",
        default=None,
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Process at most N PDFs. Useful for testing cost/time before full run.",
        default=None,
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full stack traces on errors.",
    )
    args = parser.parse_args()
    main(single_bridge=args.bridge, limit=args.limit)
