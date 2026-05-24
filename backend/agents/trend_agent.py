"""
Insight Agent #1: Deterioration Trend Agent.

WHAT THIS AGENT DOES
--------------------
Identifies bridges whose structural condition is measurably worsening
across the three NBI inspection years in our database (2023, 2024, 2025).

A bridge "deteriorates" when its NBI condition ratings (deck, superstructure,
substructure) decline over time. This agent distinguishes between:
  - Stable decline:     consistent 1-point drops year over year
  - Accelerating:       the rate of decline is increasing (e.g. 7→6→4)
  - Sudden drop:        a large single-year fall (≥2 points in one year)

WHY ALGORITHMIC + LLM (NOT JUST LLM)
--------------------------------------
We compute the numbers ourselves (Python, pure math — fast and deterministic)
and then ask Gemini to write a human-readable narrative for each finding.
This is more reliable than asking an LLM to do the math: LLMs can hallucinate
numerical reasoning, but they excel at turning a table of numbers into
clear, actionable prose.

The pattern is:
  DB query → compute trend metrics → Gemini writes narrative → store as Insight

OUTPUT
------
Writes Insight rows to the database with:
  insight_type = "trend"
  severity     = "info" | "warning" | "critical"  (based on rate of decline)
  supporting_data = the year-by-year rating history (for dashboard charts)
"""
import warnings
warnings.filterwarnings("ignore")

from sqlalchemy.orm import Session
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate

from backend.config import settings
from backend.db.models import Bridge, Inspection, Insight, County


# ─── Thresholds ────────────────────────────────────────────────────────────────

# A bridge needs at least this many years of data to assess a trend.
# With only one year we can't compute change; with two we get direction;
# with three we can detect acceleration.
MIN_YEARS_FOR_TREND = 2

# Minimum total decline (across all years) to be worth reporting.
# A bridge that went 7→7→6 lost 1 point — probably normal aging.
# A bridge that went 7→6→4 lost 3 points — that's a trend worth flagging.
MIN_TOTAL_DECLINE_TO_REPORT = 2

# Severity thresholds (total decline over the analysis window)
SEVERITY_WARNING  = 2   # 2-point decline → warning
SEVERITY_CRITICAL = 3   # 3+ point decline → critical


# ─── LLM Setup ─────────────────────────────────────────────────────────────────

def _build_llm():
    """
    Use the analysis model (gemini-2.5-pro) for insight narrative generation.
    This is a reasoning task — we pay the higher cost because the quality
    of the narrative directly affects the dashboard's value to the user.
    """
    return ChatGoogleGenerativeAI(
        model=settings.gemini_model_analysis,
        google_api_key=settings.google_api_key,
        temperature=0.2,  # Slight creativity for readable prose, but mostly factual
    )


NARRATIVE_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "You are a senior bridge inspection engineer. Write concise, factual "
     "engineering insights about bridge condition trends. Use technical language "
     "appropriate for infrastructure professionals. Maximum 3 sentences."),
    ("human",
     "Bridge {bridge_id} ({name}) in {county} County shows the following "
     "NBI condition trend over {years}:\n\n"
     "{trend_table}\n\n"
     "The minimum condition rating (worst component) declined by {total_decline} "
     "points over this period. Write a brief engineering insight describing this "
     "trend and its implications for maintenance prioritization."),
])


# ─── Computation ───────────────────────────────────────────────────────────────

def _min_condition(inspection: Inspection) -> int | None:
    """
    Return the worst (minimum) NBI condition rating across all three
    major structural components for a given inspection record.

    We use the minimum — not the average — because a bridge's safety is
    determined by its weakest component. A bridge with deck=8, super=3,
    sub=7 has an effective condition of 3.

    Returns None if no component ratings are available (e.g. culverts
    with different rating fields).
    """
    ratings = [
        r for r in [
            inspection.deck_condition,
            inspection.superstructure_condition,
            inspection.substructure_condition,
        ]
        if r is not None
    ]
    return min(ratings) if ratings else None


def _compute_trend(inspections: list[Inspection]) -> dict | None:
    """
    Compute trend metrics for a bridge across its inspection history.

    Args:
        inspections: List of Inspection rows, sorted by data_year ascending.

    Returns:
        A dict with trend metrics, or None if insufficient data.

        Keys:
          years        — list of years in the analysis window
          min_ratings  — list of min condition ratings per year
          total_decline — total drop from first to last year
          max_single_year_drop — worst single-year deterioration
          is_accelerating — True if the rate of decline increased year-over-year
    """
    # Build year → min_rating mapping, skipping years with no ratings
    year_ratings = []
    for insp in sorted(inspections, key=lambda i: i.data_year):
        mc = _min_condition(insp)
        if mc is not None:
            year_ratings.append((insp.data_year, mc))

    if len(year_ratings) < MIN_YEARS_FOR_TREND:
        return None

    years     = [y for y, _ in year_ratings]
    ratings   = [r for _, r in year_ratings]

    total_decline = ratings[0] - ratings[-1]  # Positive = getting worse

    # Only report bridges that are actually declining meaningfully
    if total_decline < MIN_TOTAL_DECLINE_TO_REPORT:
        return None

    # Compute year-over-year drops (positive = deteriorating)
    drops = [ratings[i] - ratings[i+1] for i in range(len(ratings)-1)]

    max_single_year_drop = max(drops) if drops else 0

    # Acceleration: is the rate of decline increasing?
    # Example: drops of [1, 2] means the bridge is deteriorating faster each year
    is_accelerating = len(drops) >= 2 and drops[-1] > drops[0]

    return {
        "years"                : years,
        "min_ratings"          : ratings,
        "total_decline"        : total_decline,
        "max_single_year_drop" : max_single_year_drop,
        "is_accelerating"      : is_accelerating,
    }


def _determine_severity(trend: dict) -> str:
    """Map trend metrics to an insight severity level for dashboard color-coding."""
    if trend["total_decline"] >= SEVERITY_CRITICAL or trend["is_accelerating"]:
        return "critical"
    if trend["total_decline"] >= SEVERITY_WARNING:
        return "warning"
    return "info"


def _format_trend_table(trend: dict) -> str:
    """Format year-by-year ratings as a readable table for the LLM prompt."""
    lines = ["Year  | Min Condition Rating", "------|--------------------"]
    for year, rating in zip(trend["years"], trend["min_ratings"]):
        lines.append(f"{year}  | {rating}")
    return "\n".join(lines)


# ─── Main Agent Function ───────────────────────────────────────────────────────

def run_trend_agent(db: Session) -> int:
    """
    Run the Deterioration Trend Agent across all bridges in the database.

    For each bridge with sufficient inspection history, computes condition
    trends and generates a Gemini-written narrative insight. Results are
    stored as Insight rows with insight_type='trend'.

    Idempotent: deletes existing trend insights before re-running so the
    agent can be safely re-executed as more data accumulates.

    Args:
        db: SQLAlchemy sync session (caller is responsible for closing).

    Returns:
        Number of trend insights generated and stored.
    """
    # Clear existing trend insights so we don't accumulate duplicates
    # on re-runs. Analysis agents are designed to be re-run periodically
    # as new inspection data arrives.
    db.query(Insight).filter_by(
        agent_name="deterioration_trend_agent"
    ).delete()
    db.commit()

    llm = _build_llm()
    narrative_chain = NARRATIVE_PROMPT | llm

    bridges = db.query(Bridge).all()
    count   = 0

    for bridge in bridges:
        inspections = bridge.inspections  # Already ordered by data_year (see models.py)

        trend = _compute_trend(inspections)
        if not trend:
            continue  # Not enough data or no meaningful decline

        severity = _determine_severity(trend)
        county   = db.query(County).filter_by(id=bridge.county_id).first()

        # ── Ask Gemini to write the insight narrative ────────────────────────
        trend_table = _format_trend_table(trend)
        response = narrative_chain.invoke({
            "bridge_id"     : bridge.structure_number,
            "name"          : bridge.facility_carried or "Unknown",
            "county"        : county.name if county else "Unknown",
            "years"         : f"{trend['years'][0]}–{trend['years'][-1]}",
            "trend_table"   : trend_table,
            "total_decline" : trend["total_decline"],
        })
        narrative = response.content

        # ── Build a concise title for the dashboard card ─────────────────────
        accel_note = " (accelerating)" if trend["is_accelerating"] else ""
        title = (
            f"Bridge {bridge.structure_number}: "
            f"{trend['total_decline']}-point condition decline "
            f"({trend['years'][0]}–{trend['years'][-1]}){accel_note}"
        )

        # ── Store the insight ─────────────────────────────────────────────────
        insight = Insight(
            insight_type   = "trend",
            agent_name     = "deterioration_trend_agent",
            bridge_id      = bridge.id,
            county_id      = bridge.county_id,
            title          = title,
            description    = narrative,
            severity       = severity,
            confidence_score = 0.9,  # High confidence — based on hard NBI numbers
            supporting_data = {
                # Stored as JSON so the dashboard can render a sparkline chart
                "years"      : trend["years"],
                "ratings"    : trend["min_ratings"],
                "total_decline"        : trend["total_decline"],
                "max_single_year_drop" : trend["max_single_year_drop"],
                "is_accelerating"      : trend["is_accelerating"],
            },
        )
        db.add(insight)
        count += 1

    db.commit()
    return count
