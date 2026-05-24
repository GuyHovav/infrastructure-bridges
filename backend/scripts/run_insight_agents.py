"""
Script: Run all three insight generation agents.

This is the third and final stage of the pipeline:

  Stage 1: scrape_mndot_reports.py  — downloads PDFs
  Stage 2: run_agents.py            — extracts defects & recommendations
  Stage 3: run_insight_agents.py    — generates engineering insights  ← THIS

AGENTS
------
  1. Deterioration Trend Agent  — bridges whose condition is declining over time
  2. High-Risk Bridge Ranker    — composite condition × consequence scoring
  3. Defect Pattern Agent       — systemic defects recurring across bridges

RUN THIS AFTER run_agents.py has processed a meaningful number of PDFs.
The insight agents need defect data to produce useful results.
As a guide: aim for at least 50 processed bridges per county before running.

USAGE
-----
    python -m backend.scripts.run_insight_agents

    # Re-run to refresh insights with newest data:
    python -m backend.scripts.run_insight_agents

All three agents are idempotent — they clear and rewrite their previous
outputs on every run, so you can safely re-run as more data arrives.

MODEL USED
----------
gemini-2.5-pro (the analysis model, not the extraction model).
These agents do reasoning over aggregated data, not high-volume extraction,
so the higher-capability model is worth the cost.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import time

from backend.config import settings
from backend.db.session import get_sync_db
from backend.db.models import Defect, Inspection
from backend.agents.trend_agent import run_trend_agent
from backend.agents.risk_agent import run_risk_agent
from backend.agents.pattern_agent import run_pattern_agent


def _check_data_readiness(db) -> tuple[int, int]:
    """
    Check whether the database has enough extracted data to produce
    meaningful insights.

    Returns (processed_inspections, total_defects).
    Prints a warning if the numbers are low.
    """
    processed = db.query(Inspection).filter(
        Inspection.inspector_notes != None
    ).count()
    defects = db.query(Defect).count()
    return processed, defects


def main():
    print("=" * 60)
    print("MnDOT Bridge Insights — Analysis Agent Pipeline")
    print("=" * 60)
    print(f"\nAnalysis model : {settings.gemini_model_analysis}")

    db = get_sync_db()
    try:
        # ── Data readiness check ──────────────────────────────────────────────
        processed, defects = _check_data_readiness(db)
        print(f"Inspections processed : {processed}")
        print(f"Defects in database   : {defects}")

        if defects < 100:
            print(
                "\n⚠  Warning: fewer than 100 defects in the database. "
                "Run run_agents.py first to extract defect data from PDFs. "
                "Insight quality will be low with limited data."
            )
            # We don't abort — insights will still be generated, just less meaningful

        print()

        # ── Agent 1: Deterioration Trends ─────────────────────────────────────
        print("--- Agent 1: Deterioration Trend Agent ---")
        t0 = time.time()
        trend_count = run_trend_agent(db)
        print(f"  ✓ {trend_count} trend insights generated  ({time.time()-t0:.1f}s)\n")

        # ── Agent 2: Risk Ranking ─────────────────────────────────────────────
        print("--- Agent 2: High-Risk Bridge Ranker ---")
        t0 = time.time()
        risk_count = run_risk_agent(db)
        print(f"  ✓ {risk_count} risk insights generated  ({time.time()-t0:.1f}s)\n")

        # ── Agent 3: Defect Patterns ──────────────────────────────────────────
        print("--- Agent 3: Defect Pattern Agent ---")
        t0 = time.time()
        pattern_count = run_pattern_agent(db)
        print(f"  ✓ {pattern_count} pattern insights generated  ({time.time()-t0:.1f}s)\n")

        # ── Summary ───────────────────────────────────────────────────────────
        total = trend_count + risk_count + pattern_count
        print("=" * 60)
        print(f"✅ Done. {total} total insights written to database.")
        print(f"   Trend insights   : {trend_count}")
        print(f"   Risk insights    : {risk_count}")
        print(f"   Pattern insights : {pattern_count}")
        print("\nRe-run anytime to refresh insights with the latest data.")

    finally:
        db.close()


if __name__ == "__main__":
    main()
