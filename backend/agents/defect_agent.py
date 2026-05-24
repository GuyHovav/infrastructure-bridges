"""
Defect Extraction Agent.

Reads the free-text inspection narrative from a bridge PDF and extracts
structured defect records using Gemini.

WHAT A DEFECT IS
----------------
A defect is a specific physical problem observed during inspection:
  - Cracking in the deck concrete
  - Corrosion on steel girders
  - Spalling on substructure piers
  - Scour erosion at footings
  - Joint failures, delamination, section loss, etc.

AGENT DESIGN
------------
We use LangChain's structured output (with_structured_output) to have
Gemini return a typed list of defects — no JSON parsing fragility.

Model choice: gemini-2.0-flash
  - Fast (important for 2,630 bridges)
  - Cheap (high-volume extraction task)
  - Excellent at structured information extraction from technical text

The prompt is designed to be:
  1. Explicit about field definitions (so the model doesn't hallucinate)
  2. Conservative (only extract what's actually stated, no inference)
  3. Grounded in the specific NBI vocabulary inspectors use
"""
import warnings
warnings.filterwarnings("ignore")

from typing import Optional
from pydantic import BaseModel, Field

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate

from backend.config import settings


# ─── Output Schema ─────────────────────────────────────────────────────────────

class Defect(BaseModel):
    """A single defect extracted from the inspection report text."""

    defect_type: str = Field(
        description=(
            "Category of defect. Use standard bridge inspection terms: "
            "cracking, spalling, corrosion, delamination, scour, settlement, "
            "joint_failure, section_loss, efflorescence, leakage, collision_damage, "
            "fatigue, undermining, other."
        )
    )
    severity: Optional[str] = Field(
        default="unknown",
        description=(
            "Severity level: 'minor' (cosmetic, no structural concern), "
            "'moderate' (warrants monitoring or near-term repair), "
            "'severe' (significant structural concern), "
            "'critical' (immediate safety hazard). "
            "Use 'unknown' only if the severity cannot be determined from the text."
        )
    )
    component: str = Field(
        description=(
            "Bridge component affected: deck, superstructure, substructure, "
            "bearing, joint, railing, approach, channel, culvert, paint_system, other."
        )
    )
    description: str = Field(
        description="Verbatim or close-paraphrase of the inspector's description of this defect."
    )
    location_on_bridge: Optional[str] = Field(
        default=None,
        description="Specific location if stated (e.g. 'span 3 south pier', 'north abutment', 'bay 5-6')."
    )


class DefectList(BaseModel):
    """Container for all defects extracted from one inspection report."""
    defects: list[Defect] = Field(
        default_factory=list,
        description="All defects found. Empty list if no defects are described."
    )


# ─── Prompt ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert bridge inspection analyst. Your job is to extract \
structured defect records from Minnesota bridge inspection reports.

RULES:
1. Only extract defects that are explicitly described in the text.
2. Do NOT infer, assume, or add defects not mentioned.
3. If the report says "no defects" or "good condition", return an empty list.
4. One defect per distinct problem — don't merge multiple issues into one.
5. Use the inspector's own language for the description field.
6. Severity mapping guide:
   - CS1 (Condition State 1) or ratings 7-9 → minor
   - CS2 or ratings 5-6 → moderate
   - CS3 or ratings 3-4 → severe
   - CS4 or ratings 0-2 or "critical structural deficiency" → critical"""

HUMAN_PROMPT = """Extract all defects from this bridge inspection report.

Bridge ID: {bridge_id}

REPORT TEXT:
{report_text}"""


# ─── Agent ─────────────────────────────────────────────────────────────────────

def build_defect_agent() -> object:
    """
    Build and return the defect extraction chain.

    Uses LangChain's with_structured_output() which instructs Gemini to
    return a response conforming to the DefectList Pydantic schema.
    This is more reliable than parsing free-form JSON.

    Returns:
        A runnable LangChain chain: dict → DefectList
    """
    llm = ChatGoogleGenerativeAI(
        model=settings.gemini_model_extraction,
        google_api_key=settings.google_api_key,
        temperature=0,        # Deterministic — extraction, not generation
    )

    structured_llm = llm.with_structured_output(DefectList)

    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human", HUMAN_PROMPT),
    ])

    return prompt | structured_llm


def extract_defects(bridge_id: str, report_text: str) -> list[dict]:
    """
    Extract defects from an inspection report text.

    Args:
        bridge_id:   The bridge structure number (for context in the prompt).
        report_text: Full extracted text from the PDF.

    Returns:
        List of dicts ready to be inserted as Defect ORM rows:
        [{"defect_type": ..., "severity": ..., "component": ...,
          "description": ..., "location_on_bridge": ..., "source": "llm"}, ...]
    """
    agent = build_defect_agent()

    result: DefectList = agent.invoke({
        "bridge_id"  : bridge_id,
        "report_text": report_text[:15_000],  # Safety cap — ~10 pages of text
    })

    return [
        {
            "defect_type"       : d.defect_type,
            "severity"          : d.severity,
            "component"         : d.component,
            "description"       : d.description,
            "location_on_bridge": d.location_on_bridge,
            "source"            : "llm",
        }
        for d in result.defects
    ]
