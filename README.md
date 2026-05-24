# MnDOT Bridge Intelligence Tool

A proof-of-concept infrastructure inspection intelligence tool built for the Diagnostic Imaging assignment. Collects, structures, analyzes, and presents bridge inspection data across 5 Minnesota counties using an agentic AI pipeline.

**Live data:** 2,630 bridges · 7,728 inspection records · 33,000+ extracted defects · 74 AI-generated insights

---

## Quick Start

```bash
# 1. Clone and set up
git clone <repo>
cd infrastraucture-bridges
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt

# 2. Add your Gemini API key
cp .env.example .env
echo "GOOGLE_API_KEY=your_key_here" > .env

# 3. Start the dashboard (database already populated)
.venv/bin/python -m uvicorn backend.api.main:app --reload --port 8000

# Open: http://localhost:8000
```

> **Note:** The `data/` directory contains the pre-populated SQLite database and all downloaded PDFs. No pipeline re-run is needed to demo the dashboard.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  DATA SOURCES                                               │
│  ┌──────────────────┐    ┌──────────────────────────────┐   │
│  │  FHWA NBI        │    │  MnDOT Bridge Reports Portal │   │
│  │  (federal API)   │    │  reports.dot.state.mn.us     │   │
│  │  Structured CSV  │    │  Inspection PDFs + Photos    │   │
│  └────────┬─────────┘    └──────────────┬───────────────┘   │
│           │                             │                    │
│  PIPELINE (Stage 1 & 2)                │                    │
│  ┌────────▼─────────────────────────────▼───────────────┐   │
│  │  download_nbi_data.py   scrape_mndot_reports.py      │   │
│  │  parse_nbi_data.py      pdf_extractor.py             │   │
│  │         ↓                       ↓                    │   │
│  │      SQLite DB          defect_agent.py              │   │
│  │  (bridges/inspections)  recommendation_agent.py      │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                             │
│  INSIGHT AGENTS (Stage 3)                                  │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  trend_agent.py    — deterioration trends over time  │   │
│  │  risk_agent.py     — composite risk scoring          │   │
│  │  pattern_agent.py  — systemic defect patterns        │   │
│  └────────────────────────┬─────────────────────────────┘   │
│                           │                                  │
│  API + DASHBOARD          │                                  │
│  ┌────────────────────────▼─────────────────────────────┐   │
│  │  FastAPI (backend/api/main.py)                       │   │
│  │  Leaflet.js map · Bridge detail panel · Insights     │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

---

## Data Sources

### 1. FHWA National Bridge Inventory (NBI)
- **URL:** https://www.fhwa.dot.gov/bridge/nbi/ascii.cfm
- **What it provides:** Structured annual data for all ~600,000 US bridges in a 445-character fixed-width format. One row per bridge per year.
- **Why used over MnDOT-only:** NBI is the *authoritative upstream source* — states submit their data to FHWA. It gives clean, machine-readable, multi-year quantitative condition ratings (2023–2025) without PDF parsing.
- **Fields extracted:** Condition ratings (deck/superstructure/substructure, 0–9 scale), GPS coordinates, ADT traffic, material type, load ratings, structural flags.

### 2. MnDOT Bridge Reports Portal
- **URL:** https://reports.dot.state.mn.us/bridgereports/
- **What it provides:** Official PDF inspection reports with inspector narratives and embedded photos.
- **Why used:** Rich free-text content that NBI doesn't capture — specific defect descriptions, maintenance recommendations, inspection photos.
- **Technical challenge:** The portal runs on SAP BusinessObjects/Crystal Reports with a dynamically-generated `bttoken` session token. Required Playwright (headless Chromium) to intercept the PDF URL rather than simple `requests` scraping.

---

## Pipeline Stages

### Stage 1 — Data Collection
```bash
# Download NBI structured data (2023, 2024, 2025)
python -m backend.scripts.download_nbi_data

# Parse NBI into database
python -m backend.scripts.parse_nbi_data

# Scrape MnDOT inspection PDFs (requires US IP / VPN)
python -m backend.scripts.scrape_mndot_reports
```

### Stage 2 — Extraction & Structuring
```bash
# Run LLM extraction agents on all downloaded PDFs
python -m backend.scripts.run_agents

# Or process a single bridge for testing:
python -m backend.scripts.run_agents --bridge 2440
```

Two extraction agents run per PDF:
- **Defect Agent** (`gemini-2.0-flash`) — extracts structured defect records: type, severity, component, location
- **Recommendation Agent** (`gemini-2.0-flash`) — extracts maintenance actions: priority, category, description

### Stage 3 — Insight Generation
```bash
# Run all three analysis agents
python -m backend.scripts.run_insight_agents
```

Three analysis agents operate on the full database:
- **Trend Agent** — identifies bridges with declining condition ratings year-over-year
- **Risk Agent** — composite risk scoring (condition × traffic × age × structural flags)
- **Pattern Agent** — systemic defects recurring across multiple bridges (county-wide prevalence analysis)

---

## Target Counties

| County | FIPS | Bridges | Rationale |
|--------|------|---------|-----------|
| Hennepin | 053 | 947 | Largest county, densest urban infrastructure (Minneapolis) |
| Ramsey | 123 | 346 | Urban aging infrastructure (St. Paul) |
| St. Louis | 137 | 693 | Largest by area, diverse rural bridge types |
| Polk | 119 | 270 | Representative rural county with a mix of aging timber and concrete bridges along major river crossings |
| Olmsted | 109 | 374 | Rochester area, mix of urban/rural bridges |

---

## Database Schema

```
County (5 rows)
  └─ Bridge (2,630)            — physical attributes, GPS, traffic data
       └─ Inspection (7,728)   — one row per bridge per year (2023/24/25)
            ├─ Defect (33,000+)         — LLM-extracted from PDFs
            ├─ Recommendation (4,400+)  — LLM-extracted from PDFs
            └─ InspectionImage          — photos extracted from PDFs

Insight (74)  — links to Bridge and/or County
```

**Key design decisions:**
- `Bridge` is the central entity with a stable NBI `structure_number` as its natural key
- `Inspection` is the time-series backbone — one row per year enables trend analysis
- `Defect.source = "llm"` provides audit traceability for every AI-extracted record
- `Insight.supporting_data` (JSON) stores chart-ready data for the frontend

---

## AI / LLM Integration

| Agent | Model | Task | Why this model |
|-------|-------|------|----------------|
| Defect Agent | `gemini-2.0-flash` | Structured extraction from PDF text | Fast + cheap for high-volume (2,630 PDFs) |
| Recommendation Agent | `gemini-2.0-flash` | Structured extraction | Same — high-volume, repetitive task |
| Trend Agent | `gemini-2.5-pro` | Multi-year condition analysis | Reasoning over time-series data |
| Risk Agent | `gemini-2.5-pro` | Composite scoring + narrative | Requires nuanced engineering judgment |
| Pattern Agent | `gemini-2.5-pro` | Cross-county pattern synthesis | Cross-dataset reasoning |

All extraction agents use `with_structured_output()` with Pydantic schemas — no JSON parsing fragility, typed output guaranteed.

---

## Dashboard Features

- **Map view** — Leaflet.js, color-coded by bridge condition (green→red scale)
- **County filter** — sidebar with aggregate stats per county
- **Condition filter** — filter map markers by min/max condition rating
- **Bridge detail panel** — full inspection history, extracted defects, recommendations
- **Inspection photos** — embedded images extracted from MnDOT PDFs
- **Insights panel** — AI-generated findings, filterable by type and severity

---

## Running the Full Pipeline (fresh environment)

```bash
# All stages in order:
python -m backend.scripts.download_nbi_data      # ~2 min
python -m backend.scripts.parse_nbi_data         # ~1 min
python -m backend.scripts.scrape_mndot_reports   # ~4 hours (US VPN required)
python -m backend.scripts.run_agents             # ~10 hours (Gemini API)
python -m backend.scripts.run_insight_agents     # ~5 min

# Or use the background launcher for scraper + agents:
./run_background.sh
tail -f logs/agents.log
```

---

## Project Review Discussion Points

See [`SECURITY.md`](./SECURITY.md) for detailed security analysis and talking points.

**Key topics covered:**
- Architectural decisions (two data sources, sync vs async, SQLite for PoC)
- Security (secrets management, SQL injection, CORS, path traversal, prompt injection)
- Scalability (PostgreSQL migration, async agents, queue-based parallelism)
- Agentic design (why 5 separate agents, model selection rationale, idempotency)
- Data quality (dedup on multi-year PDFs, fallback for missing LLM fields)
