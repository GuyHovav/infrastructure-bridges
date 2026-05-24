"""
Insight Agent #2: High-Risk Bridge Ranker.

WHAT THIS AGENT DOES
--------------------
Scores every bridge on a composite risk index and surfaces the highest-risk
assets for each county.

RISK MODEL
----------
Bridge risk is a function of two independent dimensions:

  1. Condition risk — how structurally compromised is the bridge?
     Derived from NBI condition ratings (0-9 scale) and defect severity counts.

  2. Consequence risk — how bad would a failure be?
     Derived from traffic volume (ADT) and functional classification.
     A bridge carrying 50,000 cars/day failing is catastrophically worse
     than one carrying 200 cars/day.

Final risk score = condition_score × consequence_score (0–100 scale)

This multiplicative model is standard practice in infrastructure risk
assessment: a bridge in terrible condition but with no traffic is low risk;
a bridge in good condition but carrying a highway is medium risk; a bridge
in poor condition carrying a highway is high risk.

WHY THIS IS VALUABLE
--------------------
The NBI alone ranks bridges by condition. But condition alone doesn't
capture impact. This agent produces a defensible, transparent risk ranking
that incorporates both — which is exactly what maintenance budget decisions
require.

OUTPUT
------
Writes one Insight row per top-risk bridge (top 10 per county) plus one
county-level summary Insight, with insight_type='risk'.
"""
import warnings
warnings.filterwarnings("ignore")

from sqlalchemy.orm import Session
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate

from backend.config import settings
from backend.db.models import Bridge, Inspection, Insight, Defect, County


# ─── Risk Scoring Constants ────────────────────────────────────────────────────

# How many top-risk bridges to surface per county.
# 10 gives a meaningful prioritized list without overwhelming the dashboard.
TOP_N_PER_COUNTY = 10

# NBI condition thresholds for condition scoring.
# These align with FHWA definitions:
#   ≥7 = Good, 5-6 = Fair, 3-4 = Poor, ≤2 = Critical / Structurally Deficient
CONDITION_SCORE_MAP = {
    9: 0,  10: 0,            # Excellent
    8: 5,                     # Very Good
    7: 10,                    # Good
    6: 25,                    # Satisfactory
    5: 40,                    # Fair
    4: 65,                    # Poor (SD threshold)
    3: 80,                    # Serious
    2: 90,                    # Critical
    1: 98,                    # Imminent Failure
    0: 100,                   # Failed
}

# ADT (Average Daily Traffic) buckets for consequence scoring.
# These breakpoints are aligned with FHWA functional classification thresholds.
def _adt_consequence(adt: int | None) -> float:
    """
    Convert Average Daily Traffic to a consequence score (0–100).

    Higher traffic = higher consequence if the bridge fails.
    Returns 50 (mid-range) if ADT is unknown — conservative assumption.
    """
    if adt is None:
        return 50.0   # Unknown traffic — assume moderate consequence
    if adt >= 50_000:
        return 100.0  # Major highway / urban arterial
    if adt >= 25_000:
        return 85.0
    if adt >= 10_000:
        return 70.0
    if adt >= 5_000:
        return 55.0
    if adt >= 1_000:
        return 35.0
    return 15.0       # Rural low-volume road


# ─── LLM Setup ─────────────────────────────────────────────────────────────────

def _build_llm():
    """Use the analysis model for risk narrative generation."""
    return ChatGoogleGenerativeAI(
        model=settings.gemini_model_analysis,
        google_api_key=settings.google_api_key,
        temperature=0.2,
    )


BRIDGE_NARRATIVE_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "You are a senior bridge inspection engineer advising on maintenance "
     "prioritization. Write concise, factual risk assessments. Maximum 2 sentences."),
    ("human",
     "Bridge {bridge_id} ({name}) carries {adt} vehicles/day. "
     "NBI condition ratings: deck={deck}, superstructure={super}, substructure={sub}. "
     "Extracted defects: {critical} critical, {severe} severe, {moderate} moderate. "
     "Risk score: {score:.0f}/100. Write a brief risk assessment."),
])

COUNTY_SUMMARY_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "You are a senior bridge inspection engineer writing an executive summary "
     "for a county infrastructure report. Be concise and actionable. Max 3 sentences."),
    ("human",
     "County: {county}. Total bridges analyzed: {total}. "
     "High-risk bridges (score ≥70): {high_risk}. "
     "Average risk score: {avg_score:.1f}/100. "
     "Highest risk bridge: {top_bridge} (score {top_score:.0f}/100, ADT {top_adt}). "
     "Write a county-level risk summary for infrastructure managers."),
])


# ─── Scoring Functions ────────────────────────────────────────────────────────

def _condition_score(inspection: Inspection | None) -> float:
    """
    Compute a condition risk score (0–100) from NBI ratings.

    Uses the worst (minimum) of deck, superstructure, substructure ratings
    as the governing condition — the weakest link determines bridge safety.

    Returns 50 (mid-range) if no inspection data exists.
    """
    if inspection is None:
        return 50.0

    ratings = [
        r for r in [
            inspection.deck_condition,
            inspection.superstructure_condition,
            inspection.substructure_condition,
        ]
        if r is not None
    ]

    if not ratings:
        return 50.0

    worst = min(ratings)
    return CONDITION_SCORE_MAP.get(worst, 50.0)


def _defect_penalty(db: Session, inspection: Inspection | None) -> float:
    """
    Add a risk penalty based on the number and severity of extracted defects.

    This supplements NBI ratings (which are coarse 0-9 numbers) with the
    richer information extracted by our defect agent. A bridge rated NBI=5
    (Fair) but with 10 critical defects is riskier than one with 0 critical.

    Penalty scale:
      Each critical defect adds 3 points (max 15)
      Each severe defect adds 1 point (max 10)
    Total max penalty: 25 points (proportional to condition score)
    """
    if inspection is None:
        return 0.0

    defects = db.query(Defect).filter_by(inspection_id=inspection.id).all()
    if not defects:
        return 0.0

    critical_count = sum(1 for d in defects if d.severity == "critical")
    severe_count   = sum(1 for d in defects if d.severity == "severe")

    penalty = min(critical_count * 3, 15) + min(severe_count * 1, 10)
    return float(penalty)


def _compute_risk_score(
    db: Session,
    bridge: Bridge,
    inspection: Inspection | None,
) -> dict:
    """
    Compute the composite risk score for one bridge.

    Formula:
      condition_raw = condition_score + defect_penalty  (capped at 100)
      consequence   = adt_consequence(bridge.adt)
      risk_score    = (condition_raw * consequence) / 100

    This gives a 0-100 score where both condition and consequence must be
    elevated for a bridge to rank as high-risk.

    Returns a dict with the score breakdown for transparency/audit.
    """
    cond_score  = _condition_score(inspection)
    defect_pen  = _defect_penalty(db, inspection)
    cond_raw    = min(100.0, cond_score + defect_pen)
    consequence = _adt_consequence(bridge.adt)
    risk_score  = (cond_raw * consequence) / 100.0

    # Collect defect counts for the LLM narrative
    defects = []
    if inspection:
        defects = db.query(Defect).filter_by(inspection_id=inspection.id).all()

    return {
        "risk_score"       : risk_score,
        "condition_raw"    : cond_raw,
        "consequence"      : consequence,
        "critical_defects" : sum(1 for d in defects if d.severity == "critical"),
        "severe_defects"   : sum(1 for d in defects if d.severity == "severe"),
        "moderate_defects" : sum(1 for d in defects if d.severity == "moderate"),
    }


# ─── Main Agent Function ───────────────────────────────────────────────────────

def run_risk_agent(db: Session) -> int:
    """
    Run the High-Risk Bridge Ranker across all counties.

    For each county:
      1. Scores every bridge using the composite risk model
      2. Ranks bridges by score
      3. Generates Gemini narratives for the top-N
      4. Generates a county-level summary insight
      5. Stores all results as Insight rows

    Idempotent: deletes existing risk insights before re-running.

    Args:
        db: SQLAlchemy sync session.

    Returns:
        Total number of insight rows created.
    """
    # Clear previous risk insights for a clean re-run
    db.query(Insight).filter_by(agent_name="risk_ranker_agent").delete()
    db.commit()

    llm              = _build_llm()
    bridge_chain     = BRIDGE_NARRATIVE_PROMPT | llm
    county_chain     = COUNTY_SUMMARY_PROMPT | llm

    counties = db.query(County).all()
    total_insights = 0

    for county in counties:
        bridges = db.query(Bridge).filter_by(county_id=county.id).all()
        if not bridges:
            continue

        # Score every bridge in this county
        scored = []
        for bridge in bridges:
            # Use the most recent inspection (highest year) for scoring
            latest_insp = (
                sorted(bridge.inspections, key=lambda i: i.data_year)[-1]
                if bridge.inspections else None
            )
            score_data = _compute_risk_score(db, bridge, latest_insp)
            scored.append((bridge, latest_insp, score_data))

        # Sort by risk score descending, take top N
        scored.sort(key=lambda x: x[2]["risk_score"], reverse=True)
        top_bridges = scored[:TOP_N_PER_COUNTY]

        # ── Generate bridge-level insights ────────────────────────────────────
        for bridge, inspection, score_data in top_bridges:
            risk = score_data["risk_score"]
            severity = (
                "critical" if risk >= 70 else
                "warning"  if risk >= 40 else
                "info"
            )

            response = bridge_chain.invoke({
                "bridge_id" : bridge.structure_number,
                "name"      : bridge.facility_carried or "Unknown",
                "adt"       : f"{bridge.adt:,}" if bridge.adt else "unknown",
                "deck"      : inspection.deck_condition if inspection else "N/A",
                "super"     : inspection.superstructure_condition if inspection else "N/A",
                "sub"       : inspection.substructure_condition if inspection else "N/A",
                "critical"  : score_data["critical_defects"],
                "severe"    : score_data["severe_defects"],
                "moderate"  : score_data["moderate_defects"],
                "score"     : risk,
            })

            insight = Insight(
                insight_type     = "risk",
                agent_name       = "risk_ranker_agent",
                bridge_id        = bridge.id,
                county_id        = county.id,
                title            = (
                    f"Bridge {bridge.structure_number}: "
                    f"Risk score {risk:.0f}/100 "
                    f"({bridge.adt:,} ADT)" if bridge.adt
                    else f"Bridge {bridge.structure_number}: Risk score {risk:.0f}/100"
                ),
                description      = response.content,
                severity         = severity,
                confidence_score = 0.85,
                supporting_data  = {
                    "risk_score"      : round(risk, 1),
                    "condition_score" : round(score_data["condition_raw"], 1),
                    "consequence"     : round(score_data["consequence"], 1),
                    "adt"             : bridge.adt,
                    "defects"         : {
                        "critical": score_data["critical_defects"],
                        "severe"  : score_data["severe_defects"],
                        "moderate": score_data["moderate_defects"],
                    },
                },
            )
            db.add(insight)
            total_insights += 1

        # ── Generate county-level summary insight ─────────────────────────────
        all_scores  = [s[2]["risk_score"] for s in scored]
        avg_score   = sum(all_scores) / len(all_scores) if all_scores else 0
        high_risk_n = sum(1 for s in all_scores if s >= 70)

        top_bridge, _, top_score_data = scored[0]
        county_response = county_chain.invoke({
            "county"     : county.name,
            "total"      : len(bridges),
            "high_risk"  : high_risk_n,
            "avg_score"  : avg_score,
            "top_bridge" : top_bridge.structure_number,
            "top_score"  : top_score_data["risk_score"],
            "top_adt"    : (
                f"{top_bridge.adt:,}" if top_bridge.adt else "unknown"
            ),
        })

        county_insight = Insight(
            insight_type     = "risk",
            agent_name       = "risk_ranker_agent",
            bridge_id        = None,   # County-level — not tied to one bridge
            county_id        = county.id,
            title            = (
                f"{county.name} County: "
                f"{high_risk_n} high-risk bridges of {len(bridges)} total"
            ),
            description      = county_response.content,
            severity         = "critical" if high_risk_n > 5 else "warning",
            confidence_score = 0.90,
            supporting_data  = {
                "total_bridges" : len(bridges),
                "high_risk"     : high_risk_n,
                "avg_score"     : round(avg_score, 1),
                # Top 10 for dashboard ranked list
                "top_bridges"   : [
                    {
                        "structure_number": b.structure_number,
                        "name"            : b.facility_carried,
                        "risk_score"      : round(s["risk_score"], 1),
                        "adt"             : b.adt,
                    }
                    for b, _, s in top_bridges
                ],
            },
        )
        db.add(county_insight)
        total_insights += 1

    db.commit()
    return total_insights
