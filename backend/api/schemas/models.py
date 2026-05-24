"""
Pydantic response schemas for the dashboard API.

These define the shape of every JSON response the API returns.
Keeping schemas separate from ORM models (db/models.py) is intentional:
  - ORM models represent the database structure
  - API schemas represent what clients see
The two don't need to match — this lets us evolve either independently.
"""
from __future__ import annotations
from typing import Optional, Any
from pydantic import BaseModel


class CountySummary(BaseModel):
    """Summary stats for one county — used in the sidebar county list."""
    id: int
    name: str
    fips_code: str
    total_bridges: int
    avg_condition: Optional[float]   # Average min NBI condition (0-9)
    critical_count: int              # Bridges with any rating ≤ 4
    processed_count: int             # Bridges with extracted defect data


class BridgeMapFeature(BaseModel):
    """
    A single bridge as a GeoJSON-compatible feature for the Leaflet map.
    Lat/lng + enough attributes to color-code and label the marker.
    """
    structure_number: str
    facility_carried: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    min_condition: Optional[int]     # 0-9, governs marker color
    health_index: Optional[float]    # 0-100 composite score
    adt: Optional[int]               # Average daily traffic
    year_built: Optional[int]
    county_name: Optional[str]
    structurally_deficient: Optional[bool]


class DefectDetail(BaseModel):
    """One extracted defect record."""
    id: int
    defect_type: str
    severity: str
    component: str
    description: str
    location_on_bridge: Optional[str]


class RecommendationDetail(BaseModel):
    """One extracted recommendation record."""
    id: int
    priority_level: str
    action_description: str
    category: str
    estimated_cost: Optional[float]


class InspectionDetail(BaseModel):
    """One year's inspection record with its extracted defects and recommendations."""
    id: int
    data_year: int
    inspection_date: Optional[str]
    deck_condition: Optional[int]
    superstructure_condition: Optional[int]
    substructure_condition: Optional[int]
    channel_condition: Optional[int]
    min_condition: Optional[int]
    health_index: Optional[float]
    sufficiency_rating: Optional[float]
    structurally_deficient: Optional[bool]
    defects: list[DefectDetail]
    recommendations: list[RecommendationDetail]


class BridgeDetail(BaseModel):
    """
    Full detail for one bridge — shown in the right-side detail panel
    when a user clicks a bridge on the map.
    """
    structure_number: str
    facility_carried: Optional[str]
    feature_intersected: Optional[str]
    location_description: Optional[str]
    county_name: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    year_built: Optional[int]
    year_reconstructed: Optional[int]
    material_name: Optional[str]
    design_name: Optional[str]
    number_of_spans: Optional[int]
    structure_length: Optional[float]
    deck_width: Optional[float]
    adt: Optional[int]
    owner_name: Optional[str]
    inspections: list[InspectionDetail]  # Chronological, all years


class InsightCard(BaseModel):
    """
    One insight card for the insights panel.
    supporting_data is passed through as-is so the frontend can
    render charts from it (sparklines, bar charts, ranked lists).
    """
    id: int
    insight_type: str       # trend | risk | pattern
    agent_name: str
    title: str
    description: str
    severity: str           # info | warning | critical
    confidence_score: Optional[float]
    county_name: Optional[str]
    bridge_id: Optional[int]
    structure_number: Optional[str]
    supporting_data: Optional[Any]
    generated_at: Optional[str]
