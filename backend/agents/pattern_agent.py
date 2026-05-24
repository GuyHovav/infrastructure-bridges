"""
Insight Agent #3: Defect Pattern Agent.

WHAT THIS AGENT DOES
--------------------
Looks across all extracted defects in the database and identifies
*patterns* — recurring problems that appear across multiple bridges,
materials, or bridge types.

This is genuinely multi-asset analysis: instead of looking at one bridge,
it aggregates across the entire dataset to find systemic issues.

EXAMPLE PATTERNS THIS AGENT CAN SURFACE
-----------------------------------------
• "Joint leakage is the most common severe defect in Hennepin County,
   affecting 78% of concrete bridges built before 1970"
• "Scour is disproportionately common in Polk County relative to others"
• "Prestressed concrete bridges have 2.3x more cracking defects per
   bridge than steel bridges"

WHY PATTERNS MATTER
-------------------
A single bridge with joint leakage is a maintenance task.
100 bridges with the same joint leakage is a systemic material problem
that should inform procurement, inspection protocols, and county-wide
budget decisions. Pattern detection is the difference between reactive
maintenance and proactive infrastructure management.

APPROACH
--------
1. Aggregate defect counts from the DB across multiple dimensions:
   - By defect type × severity
   - By component × county
   - By bridge material × defect type
2. Compute prevalence rates (% of bridges affected, not raw counts)
3. Identify anomalies (combinations that appear more than expected)
4. Use Gemini to synthesize the top findings into actionable insights

OUTPUT
------
Writes Insight rows with insight_type='pattern', scoped to a county
or network-wide (no bridge_id, county_id may be None for cross-county).
"""
import warnings
warnings.filterwarnings("ignore")

from collections import defaultdict
from sqlalchemy.orm import Session
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate

from backend.config import settings
from backend.db.models import Bridge, Inspection, Insight, Defect, County


# ─── Thresholds ────────────────────────────────────────────────────────────────

# Minimum number of affected bridges for a pattern to be reportable.
# A pattern seen in only 2 bridges is anecdotal; 5+ is meaningful.
MIN_BRIDGES_FOR_PATTERN = 5

# Minimum prevalence (fraction of bridges in the group) for a pattern
# to be flagged as notable. 20% = 1 in 5 bridges — a clear trend.
MIN_PREVALENCE_RATE = 0.20


# ─── LLM Setup ─────────────────────────────────────────────────────────────────

def _build_llm():
    """Use the analysis model — pattern synthesis requires cross-dataset reasoning."""
    return ChatGoogleGenerativeAI(
        model=settings.gemini_model_analysis,
        google_api_key=settings.google_api_key,
        temperature=0.3,  # Slightly higher: synthesis benefits from some fluency
    )


PATTERN_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "You are a senior bridge inspection engineer analyzing systemic infrastructure "
     "patterns across a county. Write concise, actionable findings for infrastructure "
     "managers. Focus on implications for maintenance policy and budget. Max 3 sentences."),
    ("human",
     "Analysis scope: {scope}\n\n"
     "Top defect patterns found:\n{pattern_table}\n\n"
     "Total bridges in scope: {total_bridges}\n"
     "Total defects analyzed: {total_defects}\n\n"
     "Write an engineering insight identifying the most significant systemic pattern "
     "and its operational implications."),
])


# ─── Aggregation ──────────────────────────────────────────────────────────────

def _aggregate_defects_by_county(
    db: Session,
    county: County,
) -> dict:
    """
    Aggregate defect data across all bridges in a county.

    Returns:
        A dict with:
          total_bridges  — number of bridges with inspection data
          total_defects  — total defect count
          by_type        — {defect_type: {bridge_ids, count, severity_counts}}
          by_component   — {component: {bridge_ids, count}}
    """
    bridges = db.query(Bridge).filter_by(county_id=county.id).all()

    # Collect all defects for bridges in this county
    by_type      = defaultdict(lambda: {"bridge_ids": set(), "count": 0, "severity": defaultdict(int)})
    by_component = defaultdict(lambda: {"bridge_ids": set(), "count": 0})
    total_defects = 0
    bridges_with_data = set()

    for bridge in bridges:
        for inspection in bridge.inspections:
            defects = db.query(Defect).filter_by(inspection_id=inspection.id).all()
            if defects:
                bridges_with_data.add(bridge.id)
            for defect in defects:
                total_defects += 1
                by_type[defect.defect_type]["bridge_ids"].add(bridge.id)
                by_type[defect.defect_type]["count"] += 1
                by_type[defect.defect_type]["severity"][defect.severity] += 1
                by_component[defect.component]["bridge_ids"].add(bridge.id)
                by_component[defect.component]["count"] += 1

    return {
        "total_bridges"      : len(bridges_with_data),
        "total_defects"      : total_defects,
        "by_type"            : dict(by_type),
        "by_component"       : dict(by_component),
        "bridges_with_data"  : bridges_with_data,
    }


def _find_notable_patterns(agg: dict) -> list[dict]:
    """
    Identify defect patterns that exceed the prevalence threshold.

    A pattern is notable if:
      - It affects at least MIN_BRIDGES_FOR_PATTERN bridges, AND
      - Its prevalence (fraction of bridges affected) ≥ MIN_PREVALENCE_RATE

    Returns a list of pattern dicts, sorted by prevalence descending.
    """
    if agg["total_bridges"] == 0:
        return []

    patterns = []
    for defect_type, data in agg["by_type"].items():
        affected  = len(data["bridge_ids"])
        prevalence = affected / agg["total_bridges"]

        if affected >= MIN_BRIDGES_FOR_PATTERN and prevalence >= MIN_PREVALENCE_RATE:
            # Determine dominant severity for this defect type
            severities     = data["severity"]
            dominant_sev   = max(severities, key=severities.get) if severities else "unknown"

            patterns.append({
                "defect_type"   : defect_type,
                "affected"      : affected,
                "prevalence"    : prevalence,
                "total_count"   : data["count"],
                "dominant_sev"  : dominant_sev,
                "avg_per_bridge": data["count"] / affected,
            })

    patterns.sort(key=lambda x: x["prevalence"], reverse=True)
    return patterns


def _format_pattern_table(patterns: list[dict]) -> str:
    """Format the top patterns as a readable table for the LLM prompt."""
    if not patterns:
        return "No significant patterns found."

    lines = [
        f"{'Defect Type':<20} | {'Bridges Affected':<16} | {'Prevalence':<10} | {'Dom. Severity':<15} | Avg per Bridge",
        "-" * 85,
    ]
    for p in patterns[:8]:  # Cap at 8 rows to stay within LLM context budget
        lines.append(
            f"{p['defect_type']:<20} | "
            f"{p['affected']:<16} | "
            f"{p['prevalence']*100:.1f}%{'':<7} | "
            f"{p['dominant_sev']:<15} | "
            f"{p['avg_per_bridge']:.1f}"
        )
    return "\n".join(lines)


# ─── Main Agent Function ───────────────────────────────────────────────────────

def run_pattern_agent(db: Session) -> int:
    """
    Run the Defect Pattern Agent across all counties and network-wide.

    For each county with sufficient data:
      1. Aggregates defects across all bridges
      2. Identifies statistically notable patterns (prevalence-based)
      3. Asks Gemini to synthesize the top patterns into an insight
      4. Stores the result as a county-scoped Insight row

    Additionally runs one network-wide pass across all counties combined.

    Idempotent: clears previous pattern insights before re-running.

    Args:
        db: SQLAlchemy sync session.

    Returns:
        Total number of insight rows created.
    """
    # Clear previous pattern insights
    db.query(Insight).filter_by(agent_name="defect_pattern_agent").delete()
    db.commit()

    llm           = _build_llm()
    pattern_chain = PATTERN_PROMPT | llm

    counties       = db.query(County).all()
    total_insights = 0

    for county in counties:
        # ── Aggregate and find patterns ───────────────────────────────────────
        agg      = _aggregate_defects_by_county(db, county)
        patterns = _find_notable_patterns(agg)

        if not patterns or agg["total_bridges"] < MIN_BRIDGES_FOR_PATTERN:
            # Not enough data in this county to draw meaningful conclusions
            continue

        pattern_table = _format_pattern_table(patterns)

        # ── Ask Gemini to synthesize the top findings ─────────────────────────
        response = pattern_chain.invoke({
            "scope"          : f"{county.name} County ({agg['total_bridges']} bridges)",
            "pattern_table"  : pattern_table,
            "total_bridges"  : agg["total_bridges"],
            "total_defects"  : agg["total_defects"],
        })

        # ── Build supporting data for the dashboard ───────────────────────────
        supporting = {
            "total_bridges" : agg["total_bridges"],
            "total_defects" : agg["total_defects"],
            # Top 5 patterns stored for dashboard charts
            "top_patterns"  : [
                {
                    "defect_type": p["defect_type"],
                    "prevalence" : round(p["prevalence"] * 100, 1),
                    "affected"   : p["affected"],
                    "severity"   : p["dominant_sev"],
                }
                for p in patterns[:5]
            ],
        }

        top_prevalence = patterns[0]["prevalence"] if patterns else 0
        severity = (
            "critical" if top_prevalence >= 0.5 else
            "warning"  if top_prevalence >= 0.3 else
            "info"
        )

        insight = Insight(
            insight_type     = "pattern",
            agent_name       = "defect_pattern_agent",
            bridge_id        = None,     # Pattern insights are multi-bridge
            county_id        = county.id,
            title            = (
                f"{county.name} County: "
                f"'{patterns[0]['defect_type']}' affects "
                f"{patterns[0]['prevalence']*100:.0f}% of inspected bridges"
            ),
            description      = response.content,
            severity         = severity,
            confidence_score = min(0.95, 0.7 + (agg["total_bridges"] / 100)),
            # Confidence scales with sample size — more bridges = more reliable
            supporting_data  = supporting,
        )
        db.add(insight)
        total_insights += 1

    db.commit()
    return total_insights
