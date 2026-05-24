"""
FastAPI application entry point.

ARCHITECTURE
------------
This is a single-process FastAPI app that serves two things:
  1. A REST API at /api/... — consumed by the dashboard frontend
  2. Static files at / — the dashboard HTML/CSS/JS itself

Serving the frontend from the same process keeps deployment simple:
one command starts everything. For production we'd put Nginx in front,
but for this proof-of-concept it's appropriate.

DATABASE
--------
We use synchronous SQLAlchemy sessions here (not async) for simplicity.
The dashboard is a read-heavy, low-concurrency tool — sync sessions with
a thread-pool worker model (FastAPI's default) are perfectly adequate.
Switching to async would add complexity without meaningful benefit at this scale.

CORS
----
CORS is open (*) for development. In production, lock this to the
specific frontend domain.

STARTUP
-------
    uvicorn backend.api.main:app --reload --port 8000

Then open: http://localhost:8000
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func, desc

from backend.db.session import get_sync_db
from backend.db.models import Bridge, County, Inspection, Defect, Recommendation, Insight
from backend.api.schemas.models import (
    CountySummary, BridgeMapFeature, BridgeDetail,
    InspectionDetail, DefectDetail, RecommendationDetail, InsightCard,
)
from backend.config import settings

# ─── App Setup ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Bridge Intelligence Dashboard API",
    description=(
        "REST API for the MnDOT Bridge Inspection Intelligence Tool. "
        "Serves bridge condition data, AI-extracted defects, and "
        "engineering insights across 5 Minnesota counties."
    ),
    version="1.0.0",
)

# Open CORS for development — restrict to specific origin in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ─── DB Dependency ─────────────────────────────────────────────────────────────

def get_db():
    """
    FastAPI dependency that yields a database session and closes it after
    the request completes. Using a generator ensures the session is always
    closed even if the request raises an exception.
    """
    db = get_sync_db()
    try:
        yield db
    finally:
        db.close()


# ─── County Endpoints ──────────────────────────────────────────────────────────

@app.get("/api/counties", response_model=list[CountySummary], tags=["Counties"])
def list_counties(db: Session = Depends(get_db)):
    """
    Return summary statistics for all 5 target counties.
    Used to populate the county selector sidebar.
    """
    counties = db.query(County).all()
    result = []
    for county in counties:
        bridges = db.query(Bridge).filter_by(county_id=county.id).all()

        # Collect the most recent min_condition rating for each bridge
        conditions = []
        critical   = 0
        processed  = 0

        for bridge in bridges:
            if not bridge.inspections:
                continue
            latest = sorted(bridge.inspections, key=lambda i: i.data_year)[-1]
            if latest.min_condition is not None:
                conditions.append(latest.min_condition)
                if latest.min_condition <= 4:
                    critical += 1
            if latest.inspector_notes is not None:
                processed += 1

        avg_cond = sum(conditions) / len(conditions) if conditions else None

        result.append(CountySummary(
            id             = county.id,
            name           = county.name,
            fips_code      = county.fips_code,
            total_bridges  = len(bridges),
            avg_condition  = round(avg_cond, 2) if avg_cond else None,
            critical_count = critical,
            processed_count= processed,
        ))

    # Sort by average condition ascending (worst counties first)
    result.sort(key=lambda c: c.avg_condition or 9)
    return result


# ─── Bridge Endpoints ──────────────────────────────────────────────────────────

@app.get("/api/bridges", response_model=list[BridgeMapFeature], tags=["Bridges"])
def list_bridges_for_map(
    county_id: int | None = Query(None, description="Filter by county DB id"),
    min_condition: int | None = Query(None, description="Minimum condition rating (0-9)"),
    max_condition: int | None = Query(None, description="Maximum condition rating (0-9)"),
    db: Session = Depends(get_db),
):
    """
    Return all bridges with coordinates for the map layer.
    Optionally filter by county and/or condition range.
    Only bridges with valid lat/lng are returned (unmapped bridges are skipped).
    """
    query = db.query(Bridge).filter(
        Bridge.latitude.isnot(None),
        Bridge.longitude.isnot(None),
    )
    if county_id:
        query = query.filter_by(county_id=county_id)

    bridges = query.all()
    result  = []

    for bridge in bridges:
        # Use most recent inspection for condition data
        latest = (
            sorted(bridge.inspections, key=lambda i: i.data_year)[-1]
            if bridge.inspections else None
        )
        mc = latest.min_condition if latest else None

        # Apply condition filter if provided
        if min_condition is not None and (mc is None or mc < min_condition):
            continue
        if max_condition is not None and (mc is None or mc > max_condition):
            continue

        county = db.query(County).filter_by(id=bridge.county_id).first()

        result.append(BridgeMapFeature(
            structure_number    = bridge.structure_number,
            facility_carried    = bridge.facility_carried,
            latitude            = bridge.latitude,
            longitude           = bridge.longitude,
            min_condition       = mc,
            health_index        = latest.health_index if latest else None,
            adt                 = bridge.adt,
            year_built          = bridge.year_built,
            county_name         = county.name if county else None,
            structurally_deficient = latest.structurally_deficient if latest else None,
        ))

    return result


@app.get("/api/bridges/{structure_number}", response_model=BridgeDetail, tags=["Bridges"])
def get_bridge_detail(structure_number: str, db: Session = Depends(get_db)):
    """
    Return full detail for one bridge, including all inspection years
    with their extracted defects and recommendations.
    This powers the bridge detail panel on the right side of the dashboard.
    """
    bridge = db.query(Bridge).filter_by(structure_number=structure_number).first()
    if not bridge:
        raise HTTPException(status_code=404, detail=f"Bridge {structure_number!r} not found")

    county = db.query(County).filter_by(id=bridge.county_id).first()

    # Build inspection detail list (all years, chronological)
    inspections_out = []
    for insp in sorted(bridge.inspections, key=lambda i: i.data_year):
        defects = db.query(Defect).filter_by(inspection_id=insp.id).all()
        recs    = db.query(Recommendation).filter_by(inspection_id=insp.id).all()

        inspections_out.append(InspectionDetail(
            id                     = insp.id,
            data_year              = insp.data_year,
            inspection_date        = insp.inspection_date,
            deck_condition         = insp.deck_condition,
            superstructure_condition = insp.superstructure_condition,
            substructure_condition = insp.substructure_condition,
            channel_condition      = insp.channel_condition,
            min_condition          = insp.min_condition,
            health_index           = insp.health_index,
            sufficiency_rating     = insp.sufficiency_rating,
            structurally_deficient = insp.structurally_deficient,
            defects=[
                DefectDetail(
                    id=d.id, defect_type=d.defect_type, severity=d.severity,
                    component=d.component, description=d.description,
                    location_on_bridge=d.location_on_bridge,
                ) for d in defects
            ],
            recommendations=[
                RecommendationDetail(
                    id=r.id, priority_level=r.priority_level,
                    action_description=r.action_description,
                    category=r.category, estimated_cost=r.estimated_cost,
                ) for r in recs
            ],
        ))

    return BridgeDetail(
        structure_number   = bridge.structure_number,
        facility_carried   = bridge.facility_carried,
        feature_intersected= bridge.feature_intersected,
        location_description= bridge.location_description,
        county_name        = county.name if county else None,
        latitude           = bridge.latitude,
        longitude          = bridge.longitude,
        year_built         = bridge.year_built,
        year_reconstructed = bridge.year_reconstructed,
        material_name      = bridge.material_name,
        design_name        = bridge.design_name,
        number_of_spans    = bridge.number_of_spans,
        structure_length   = bridge.structure_length,
        deck_width         = bridge.deck_width,
        adt                = bridge.adt,
        owner_name         = bridge.owner_name,
        inspections        = inspections_out,
    )


# ─── Insights Endpoints ────────────────────────────────────────────────────────

@app.get("/api/insights", response_model=list[InsightCard], tags=["Insights"])
def list_insights(
    county_id: int | None = Query(None, description="Filter by county DB id"),
    insight_type: str | None = Query(None, description="trend | risk | pattern"),
    severity: str | None = Query(None, description="info | warning | critical"),
    limit: int = Query(50, description="Max results to return"),
    db: Session = Depends(get_db),
):
    """
    Return AI-generated engineering insights.
    Supports filtering by county, type, and severity.
    Results are ordered by severity (critical first) then confidence.
    """
    # Severity ordering for sorting: critical > warning > info
    SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}

    query = db.query(Insight)
    if county_id is not None:
        query = query.filter_by(county_id=county_id)
    if insight_type:
        query = query.filter_by(insight_type=insight_type)
    if severity:
        query = query.filter_by(severity=severity)

    insights = query.limit(limit * 3).all()  # Fetch more, then sort in Python

    # Sort by severity then confidence
    insights.sort(
        key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), -(i.confidence_score or 0))
    )
    insights = insights[:limit]

    result = []
    for ins in insights:
        county = db.query(County).filter_by(id=ins.county_id).first() if ins.county_id else None
        bridge = db.query(Bridge).filter_by(id=ins.bridge_id).first() if ins.bridge_id else None

        result.append(InsightCard(
            id               = ins.id,
            insight_type     = ins.insight_type,
            agent_name       = ins.agent_name,
            title            = ins.title,
            description      = ins.description,
            severity         = ins.severity,
            confidence_score = ins.confidence_score,
            county_name      = county.name if county else None,
            bridge_id        = ins.bridge_id,
            structure_number = bridge.structure_number if bridge else None,
            supporting_data  = ins.supporting_data,
            generated_at     = str(ins.generated_at) if ins.generated_at else None,
        ))

    return result


# ─── Bridge Images Endpoint ────────────────────────────────────────────────────

@app.get("/api/bridges/{structure_number}/images", tags=["Bridges"])
def list_bridge_images(structure_number: str):
    """
    Return a list of image URLs for a bridge's extracted inspection photos.

    Images are extracted from the MnDOT PDF reports by pdf_extractor.py
    and saved to data/processed/images/{structure_number}/.
    We scan the directory on request — no DB table needed.

    Returns:
        List of dicts: [{url, page_number, image_index, filename}]
    """
    img_dir = settings.images_dir / structure_number
    if not img_dir.exists():
        return []

    results = []
    # Sort filenames so they come out in page/index order
    for img_file in sorted(img_dir.iterdir()):
        if img_file.suffix.lower() not in {".jpg", ".jpeg", ".png", ".gif"}:
            continue
        # Parse page and image index from filename: page02_img00.jpg
        name = img_file.stem  # e.g. "page02_img00"
        parts = name.split("_")
        try:
            page_num  = int(parts[0].replace("page", ""))
            img_index = int(parts[1].replace("img", ""))
        except (IndexError, ValueError):
            page_num  = 0
            img_index = 0

        results.append({
            "url"        : f"/images/{structure_number}/{img_file.name}",
            "filename"   : img_file.name,
            "page_number": page_num,
            "image_index": img_index,
        })

    return results


# ─── Static Frontend ───────────────────────────────────────────────────────────

# The frontend directory sits at the project root level (alongside backend/)
FRONTEND_DIR = Path(__file__).parent.parent.parent / "frontend"

# Serve extracted bridge inspection images at /images/{structure_number}/{filename}
IMAGES_DIR = settings.images_dir
if IMAGES_DIR.exists():
    app.mount("/images", StaticFiles(directory=str(IMAGES_DIR)), name="images")

if FRONTEND_DIR.exists():
    # Serve JS, CSS, images etc. from /static/
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def serve_dashboard():
        """Serve the main dashboard HTML file."""
        return FileResponse(str(FRONTEND_DIR / "index.html"))
