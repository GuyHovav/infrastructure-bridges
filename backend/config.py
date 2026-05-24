"""
Centralised application settings.

Uses pydantic-settings to load configuration from environment variables and
a .env file. This keeps secrets (like API keys) out of source code while
providing type-safe, validated configuration with sensible defaults.

Design decision: A single Settings object is instantiated at module level
(the `settings` singleton). Every other module imports this instead of reading
env vars directly, which gives us one place to change configuration.
"""
from pydantic_settings import BaseSettings
from pathlib import Path

# Project root directory (one level above the backend/ package)
BASE_DIR = Path(__file__).parent.parent


class Settings(BaseSettings):
    """
    All configurable parameters for the application.

    Values can be overridden by:
      1. Setting environment variables (highest priority)
      2. Entries in the .env file at the project root
      3. The defaults defined here (lowest priority)
    """

    # -- Google Gemini Configuration ------------------------------------------
    # We use two Gemini models with different cost/capability tradeoffs:
    #   - gemini-2.0-flash: fast and cheap, ideal for high-volume structured
    #     extraction across 2,630 bridge PDFs (defects, recommendations)
    #   - gemini-2.5-pro: strongest reasoning, used for complex analysis agents
    #     (trend analysis, cross-county pattern discovery, risk scoring)
    # Get your API key at: https://aistudio.google.com/app/apikey
    google_api_key: str = ""
    gemini_model_extraction: str = "gemini-2.0-flash"
    gemini_model_analysis: str = "gemini-2.5-pro"

    # -- Database Configuration -----------------------------------------------
    # SQLite for this proof-of-concept: zero setup, single-file DB, portable.
    # In production we'd switch to PostgreSQL + PostGIS for:
    #   - Concurrent write access (multiple agents running in parallel)
    #   - Spatial queries (find bridges within X km of a location)
    #   - Better JSON support for insight data
    #
    # We maintain two connection strings because SQLAlchemy needs different
    # drivers for sync vs async access:
    #   - "sqlite+aiosqlite:///" → async driver for FastAPI (non-blocking)
    #   - "sqlite:///"          → sync driver for data-loading scripts
    database_url: str = f"sqlite+aiosqlite:///{BASE_DIR}/data/bridges.db"
    database_url_sync: str = f"sqlite:///{BASE_DIR}/data/bridges.db"

    # -- Data Paths -----------------------------------------------------------
    # Organised by processing stage:
    #   raw/nbi/   → downloaded FHWA files, one subfolder per year
    #   raw/pdfs/  → bridge inspection report PDFs (from MnDOT)
    #   processed/ → extracted images, cleaned data
    raw_nbi_dir: Path = BASE_DIR / "data" / "raw" / "nbi"
    raw_pdf_dir: Path = BASE_DIR / "data" / "raw" / "pdfs"
    images_dir: Path = BASE_DIR / "data" / "processed" / "images"

    # -- NBI Data Configuration -----------------------------------------------
    # Years to download from the National Bridge Inventory.
    # The assignment requires "at least 3 inspection cycles", so we pull
    # the three most recent years of data. Each annual NBI submission
    # represents one inspection cycle.
    nbi_years: list[int] = [2023, 2024, 2025]
    mn_state_code: str = "27"  # Minnesota FIPS state code

    # -- Target Counties ------------------------------------------------------
    # We selected 5 Minnesota counties for analysis. The mapping is
    # county name → 3-digit FIPS county code (used in NBI records).
    #
    # Selection rationale:
    #   - Hennepin (053): Largest county, most bridges, dense urban area
    #   - Ramsey (123): Urban, aging infrastructure (Minneapolis-St. Paul)
    #   - St. Louis (137): Largest county by area, diverse rural bridge types
    #   - Polk (119): DI Global client — they quote Rich Sanders from Polk
    #                 County on their website, shows we did our homework
    #   - Olmsted (109): Rochester area, mix of urban and rural bridges
    target_counties: dict[str, str] = {
        "Hennepin": "053",
        "Ramsey": "123",
        "St. Louis": "137",
        "Polk": "119",
        "Olmsted": "109",
    }

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Module-level singleton — import this from anywhere in the project
settings = Settings()
