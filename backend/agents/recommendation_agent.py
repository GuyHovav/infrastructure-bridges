"""
Recommendation Extraction Agent.

Reads the free-text inspection narrative from a bridge PDF and extracts
structured maintenance/repair recommendations using Gemini.

WHAT A RECOMMENDATION IS
------------------------
A recommendation is an action proposed by the inspector or implied by
the findings:
  - "Monitor crack width annually"
  - "Apply crack sealant on deck surface by next inspection cycle"
  - "Replace expansion joints — deterioration is significant"
  - "Post load restriction on structure"
  - "Perform underwater inspection of pier footings"

PRIORITY LEVELS
---------------
  routine     — Regular maintenance (cleaning, minor adjustments)
  preventive  — Arrest deterioration before it worsens (sealing, painting)
  corrective  — Repair existing damage (patching, grinding, replacement)
  urgent      — Immediate safety concern (load posting, emergency repair)

Model choice: gemini-2.0-flash (same as defect agent — high volume task)
"""
import warnings
warnings.filterwarnings("ignore")

from typing import Optional
from pydantic import BaseModel, Field

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate

from backend.config import settings


# ─── Output Schema ─────────────────────────────────────────────────────────────

class Recommendation(BaseModel):
    """A single maintenance or repair recommendation from the inspection report."""

    priority_level: Optional[str] = Field(
        default="routine",
        description=(
            "Priority: 'routine' (regular maintenance), "
            "'preventive' (stop deterioration), "
            "'corrective' (fix existing damage), "
            "'urgent' (immediate safety concern)."
        )
    )
    action_description: str = Field(
        description="What needs to be done — verbatim or close paraphrase of the inspector's words."
    )
    category: Optional[str] = Field(
        default="other",
        description=(
            "Work category: paint, repair, replace, seal, monitor, "
            "load_restrict, close, underwater_inspection, other."
        )
    )
    estimated_cost: Optional[float] = Field(
        default=None,
        description="Estimated cost in USD if explicitly stated in the report. Null if not mentioned."
    )


class RecommendationList(BaseModel):
    """Container for all recommendations extracted from one inspection report."""
    recommendations: list[Recommendation] = Field(
        default_factory=list,
        description="All recommendations found. Empty list if none are described."
    )


# ─── Prompt ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert bridge inspection analyst. Your job is to extract \
structured maintenance and repair recommendations from Minnesota bridge inspection reports.

RULES:
1. Only extract recommendations explicitly stated or clearly implied by the findings.
2. Do NOT invent actions not mentioned in the report.
3. If the report concludes no action is needed, return an empty list.
4. One recommendation per distinct action — don't bundle multiple actions together.
5. Use the inspector's own language for the action_description field.
6. Priority mapping guide:
   - "monitor" or "observe" → routine or preventive
   - "repair", "seal", "paint" → preventive or corrective (based on urgency)
   - "replace", "rehabilitate" → corrective
   - "load post", "close", "emergency" → urgent"""

HUMAN_PROMPT = """Extract all maintenance and repair recommendations from this bridge inspection report.

Bridge ID: {bridge_id}

REPORT TEXT:
{report_text}"""


# ─── Agent ─────────────────────────────────────────────────────────────────────

def build_recommendation_agent() -> object:
    """
    Build and return the recommendation extraction chain.

    Returns:
        A runnable LangChain chain: dict → RecommendationList
    """
    llm = ChatGoogleGenerativeAI(
        model=settings.gemini_model_extraction,
        google_api_key=settings.google_api_key,
        temperature=0,
    )

    structured_llm = llm.with_structured_output(RecommendationList)

    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human", HUMAN_PROMPT),
    ])

    return prompt | structured_llm


def extract_recommendations(bridge_id: str, report_text: str) -> list[dict]:
    """
    Extract recommendations from an inspection report text.

    Args:
        bridge_id:   The bridge structure number.
        report_text: Full extracted text from the PDF.

    Returns:
        List of dicts ready for Recommendation ORM rows:
        [{"priority_level": ..., "action_description": ...,
          "category": ..., "estimated_cost": ..., "source": "llm"}, ...]
    """
    agent = build_recommendation_agent()

    result: RecommendationList = agent.invoke({
        "bridge_id"  : bridge_id,
        "report_text": report_text[:15_000],
    })

    return [
        {
            "priority_level"    : r.priority_level,
            "action_description": r.action_description,
            "category"          : r.category,
            "estimated_cost"    : r.estimated_cost,
            "source"            : "llm",
        }
        for r in result.recommendations
    ]
