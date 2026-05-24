/**
 * Bridge Intelligence Dashboard — Main Application Logic
 *
 * This file wires together:
 *   - The Leaflet map (bridge markers, click events)
 *   - The left sidebar (county cards, insight feed, condition filter)
 *   - The right panel (bridge detail, inspection history, defects)
 *
 * ARCHITECTURE
 * ------------
 * All data comes from the FastAPI backend at /api/...
 * We use plain fetch() — no framework, no state library.
 * State is held in a simple object (AppState) and the UI
 * is re-rendered by explicit function calls when state changes.
 *
 * DATA FLOW
 * ---------
 *  1. On load:  fetchCounties()  → renders county sidebar
 *               fetchBridges()   → populates map markers
 *               fetchInsights()  → renders insight feed
 *               fetchGlobalStats() → topbar numbers
 *
 *  2. County click:  AppState.activeCountyId = id
 *                    → re-fetches bridges (filtered) + insights
 *
 *  3. Map marker click: fetchBridgeDetail(structureNumber)
 *                    → renders right panel
 *
 *  4. Condition slider: AppState.minCond/maxCond updated
 *                    → updateMapMarkers() (client-side, no re-fetch)
 *
 *  5. Insight tab: AppState.insightType updated → re-fetches insights
 */

const API = "";  // Same origin — FastAPI serves both API and frontend

// ── Application State ────────────────────────────────────────
const AppState = {
  activeCountyId : null,
  insightType    : "",
  minCond        : 0,
  maxCond        : 9,
  allBridges     : [],
  activeMarkers  : [],
};

// ── Bridge Search ─────────────────────────────────────────────
// Searches client-side against AppState.allBridges (already loaded).
// Matches on structure_number prefix OR facility_carried substring.
// Keyboard-navigable (↑↓ to move, Enter to select, Escape to close).

const searchInput   = document.getElementById("bridge-search");
const searchResults = document.getElementById("search-results");
const searchClear   = document.getElementById("search-clear");
let   searchIndex   = -1;  // Currently highlighted result row

function runSearch() {
  const q = searchInput.value.trim().toLowerCase();
  searchClear.classList.toggle("hidden", q.length === 0);
  searchIndex = -1;

  if (q.length < 2) {
    searchResults.classList.add("hidden");
    return;
  }

  // Use includes (not startsWith) so pasting a full ID like "27039"
  // or a mid-string fragment still finds a match
  const hits = AppState.allBridges.filter(b => {
    const idMatch   = b.structure_number.toLowerCase().includes(q);
    const nameMatch = (b.facility_carried || "").toLowerCase().includes(q);
    return idMatch || nameMatch;
  }).slice(0, 12);

  if (hits.length === 0) {
    searchResults.innerHTML = `<div class="search-no-results">No bridges found for "${searchInput.value}"</div>`;
    searchResults.classList.remove("hidden");
    return;
  }

  searchResults.innerHTML = hits.map((b, i) => {
    const color = conditionColor(b.min_condition);
    const cond  = b.min_condition != null ? `NBI ${b.min_condition} · ` : "";
    const adt   = b.adt ? `${b.adt.toLocaleString()} ADT` : "";
    return `
      <div class="search-result-item" data-idx="${i}" data-id="${b.structure_number}"
           onclick="selectSearchResult('${b.structure_number}')">
        <div class="result-dot" style="background:${color}"></div>
        <div class="result-info">
          <div class="result-id">${b.structure_number}</div>
          <div class="result-name">${b.facility_carried || "Unknown"}</div>
          <div class="result-meta">${cond}${adt}</div>
        </div>
      </div>
    `;
  }).join("");

  searchResults.classList.remove("hidden");
}

// input: fires on every keystroke
searchInput.addEventListener("input", runSearch);

// paste: the value isn't in the DOM yet when the paste event fires,
// so defer by one tick to let the browser commit the pasted text
searchInput.addEventListener("paste", () => setTimeout(runSearch, 0));

// change: catches autofill and other programmatic value changes
searchInput.addEventListener("change", runSearch);

// Keyboard navigation within the dropdown
searchInput.addEventListener("keydown", e => {
  const items = searchResults.querySelectorAll(".search-result-item");
  if (!items.length) return;

  if (e.key === "ArrowDown") {
    e.preventDefault();
    searchIndex = Math.min(searchIndex + 1, items.length - 1);
    items.forEach((el, i) => el.classList.toggle("active", i === searchIndex));
    items[searchIndex]?.scrollIntoView({ block: "nearest" });
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    searchIndex = Math.max(searchIndex - 1, 0);
    items.forEach((el, i) => el.classList.toggle("active", i === searchIndex));
    items[searchIndex]?.scrollIntoView({ block: "nearest" });
  } else if (e.key === "Enter" && searchIndex >= 0) {
    const id = items[searchIndex]?.dataset.id;
    if (id) selectSearchResult(id);
  } else if (e.key === "Escape") {
    closeSearch();
  }
});

// Clear button
searchClear.addEventListener("click", closeSearch);

// Close dropdown when clicking outside
document.addEventListener("click", e => {
  if (!e.target.closest(".panel-search")) closeSearch();
});

function closeSearch() {
  searchInput.value = "";
  searchClear.classList.add("hidden");
  searchResults.classList.add("hidden");
  searchIndex = -1;
}

function selectSearchResult(structureNumber) {
  /**
   * Called when user clicks or presses Enter on a search result.
   * 1. Closes the search dropdown
   * 2. Flies the map to the bridge location
   * 3. Opens the marker's popup once the fly animation ends
   *    (identical to the user physically clicking the dot on the map)
   * 4. Opens the bridge detail panel on the right
   */
  closeSearch();

  const bridge = AppState.allBridges.find(b => b.structure_number === structureNumber);
  const marker = markersByBridge[structureNumber];

  if (bridge?.latitude && bridge?.longitude) {
    map.flyTo([bridge.latitude, bridge.longitude], 14, { duration: 1 });

    // Wait for the fly animation to finish before opening the popup.
    // If we open it immediately the map pans away from it mid-animation.
    map.once("moveend", () => {
      if (marker) marker.openPopup();
    });
  } else if (marker) {
    // Bridge has no coordinates but marker exists — open popup in place
    marker.openPopup();
  }

  // Load bridge detail in the right panel (can start fetching immediately
  // while the fly animation plays — they run in parallel)
  fetchBridgeDetail(structureNumber);
}

// ── Condition Color Scale ────────────────────────────────────
// Maps NBI condition rating (0-9) to a display color.
// These thresholds match FHWA definitions used in the stylesheet.
function conditionColor(rating) {
  if (rating == null) return "#64748b";  // Unknown — grey
  if (rating >= 7)   return "#22c55e";  // Good
  if (rating >= 5)   return "#eab308";  // Fair
  if (rating >= 3)   return "#f97316";  // Poor
  return                     "#ef4444"; // Critical (0-2)
}

function conditionLabel(rating) {
  if (rating == null) return "Unknown";
  if (rating >= 7)   return "Good";
  if (rating >= 5)   return "Fair";
  if (rating >= 3)   return "Poor";
  return                     "Critical";
}

// ── Map Initialization ───────────────────────────────────────
// CartoDB Dark Matter tiles — matches our dark dashboard aesthetic
const map = L.map("map", { zoomControl: true }).setView([46.5, -93.5], 7);

L.tileLayer("https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png", {
  attribution: "© OpenStreetMap contributors © CARTO",
  maxZoom: 19,
}).addTo(map);

// Marker layer group — makes it easy to clear and re-add markers
const markerLayer = L.layerGroup().addTo(map);

// Lookup map: structure_number → Leaflet marker
// Populated in updateMapMarkers() so search and other features
// can find and interact with individual markers by bridge ID.
const markersByBridge = {};

// ── API Helpers ───────────────────────────────────────────────

async function apiFetch(path) {
  /**
   * Wrapper around fetch() that handles JSON parsing and errors.
   * Returns null on failure (so callers can gracefully degrade).
   */
  try {
    const res = await fetch(API + path);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (err) {
    console.error(`API error [${path}]:`, err);
    return null;
  }
}

// ── Global Stats (topbar) ─────────────────────────────────────

async function fetchGlobalStats() {
  const counties = await apiFetch("/api/counties");
  if (!counties) return;

  const totalBridges  = counties.reduce((s, c) => s + c.total_bridges, 0);
  const totalCritical = counties.reduce((s, c) => s + c.critical_count, 0);
  const totalProcessed= counties.reduce((s, c) => s + c.processed_count, 0);

  document.getElementById("global-stats").innerHTML = `
    <div class="stat-pill">
      <div class="value">${totalBridges.toLocaleString()}</div>
      <div class="label">Bridges</div>
    </div>
    <div class="stat-pill">
      <div class="value">${totalCritical.toLocaleString()}</div>
      <div class="label">Poor / Critical</div>
    </div>
    <div class="stat-pill">
      <div class="value">${totalProcessed.toLocaleString()}</div>
      <div class="label">AI-Processed</div>
    </div>
    <div class="stat-pill">
      <div class="value">5</div>
      <div class="label">Counties</div>
    </div>
  `;

  // Mark API as online
  document.getElementById("api-status").className = "status-dot online";
  document.getElementById("api-status-label").textContent = "Live";
}

// ── County Sidebar ────────────────────────────────────────────

async function fetchCounties() {
  const counties = await apiFetch("/api/counties");
  if (!counties) {
    document.getElementById("county-list").innerHTML =
      `<div class="empty-state">Could not load counties.</div>`;
    return;
  }

  const html = counties.map(c => {
    // Average condition drives the color bar on the left of each county card
    const color = conditionColor(c.avg_condition ? Math.round(c.avg_condition) : null);
    const avgLabel = c.avg_condition ? c.avg_condition.toFixed(1) : "—";
    return `
      <div class="county-card" data-id="${c.id}" data-name="${c.name}"
           data-tip="Click to focus map on ${c.name} County · ${c.total_bridges} bridges · ${c.critical_count} poor/critical"
           onclick="selectCounty(${c.id}, '${c.name}')">
        <div class="county-bar" style="background:${color}"></div>
        <div class="county-info">
          <div class="county-name">${c.name}</div>
          <div class="county-meta">${c.total_bridges.toLocaleString()} bridges · avg NBI ${avgLabel}</div>
        </div>
        <div class="county-badge" style="background:${color}22; color:${color}">
          ${c.critical_count} ⚠
        </div>
      </div>
    `;
  }).join("");

  document.getElementById("county-list").innerHTML = html;
}

function selectCounty(countyId, countyName) {
  // Toggle off if clicking the already-active county
  if (AppState.activeCountyId === countyId) {
    AppState.activeCountyId = null;
    document.querySelectorAll(".county-card").forEach(el => el.classList.remove("active"));
    document.getElementById("btn-show-all").classList.remove("active");
    // Re-fetch all bridges and zoom back to the full Minnesota view
    fetchBridgesForCounty(null).then(() => {
      map.flyTo([46.5, -93.5], 7, { duration: 1 });
    });
  } else {
    AppState.activeCountyId = countyId;
    document.querySelectorAll(".county-card").forEach(el => {
      el.classList.toggle("active", parseInt(el.dataset.id) === countyId);
    });
    // Fetch only this county's bridges — updateMapMarkers() will fitBounds after
    fetchBridgesForCounty(countyId);
  }

  fetchInsights();
}

// "Show All" button
document.getElementById("btn-show-all").addEventListener("click", () => {
  AppState.activeCountyId = null;
  document.querySelectorAll(".county-card").forEach(el => el.classList.remove("active"));
  document.getElementById("btn-show-all").classList.add("active");
  // Reload all bridges and zoom back to Minnesota overview
  fetchBridgesForCounty(null).then(() => {
    map.flyTo([46.5, -93.5], 7, { duration: 1.2 });
  });
  fetchInsights();
});

// ── Map Markers ───────────────────────────────────────────────

async function fetchBridges() {
  /**
   * Loads all bridge map features from the API and stores them in
   * AppState.allBridges. We fetch once and filter client-side for
   * the condition slider — no round-trip needed on slider change.
   */
  document.getElementById("map-bridge-count").textContent = "Loading bridges…";

  const bridges = await apiFetch("/api/bridges");
  if (!bridges) {
    document.getElementById("map-bridge-count").textContent = "Failed to load bridges.";
    return;
  }

  AppState.allBridges = bridges;
  updateMapMarkers();
}

function updateMapMarkers() {
  /**
   * Re-renders map markers based on current AppState filters.
   * Called on: initial load, county filter change, condition slider change.
   * Does NOT re-fetch — operates on the cached AppState.allBridges.
   */
  markerLayer.clearLayers();

  const filtered = AppState.allBridges.filter(b => {
    // County filter
    if (AppState.activeCountyId) {
      // We don't have county_id in map features — match by county_name approach
      // is unreliable; instead re-fetch with county filter for precision.
      // For now, client-side filter is skipped and fetchBridgesForCounty is used.
    }
    // Condition filter
    const mc = b.min_condition;
    if (mc == null) return true; // Show unrated bridges
    return mc >= AppState.minCond && mc <= AppState.maxCond;
  });

  // Determine how many to show and fit the map
  let count = 0;
  const bounds = [];

  filtered.forEach(b => {
    if (!b.latitude || !b.longitude) return;

    const color  = conditionColor(b.min_condition);
    const radius = b.adt && b.adt > 10000 ? 6 : 4;  // Larger dot for high-traffic bridges

    // Use CircleMarker — much faster to render than Icon-based markers
    // for thousands of points
    const marker = L.circleMarker([b.latitude, b.longitude], {
      radius       : radius,
      fillColor    : color,
      fillOpacity  : 0.85,
      color        : "rgba(255,255,255,0.3)",
      weight       : 1,
    });

    // Popup with bridge summary (shown on hover)
    const adt = b.adt ? b.adt.toLocaleString() + " ADT" : "ADT unknown";
    marker.bindPopup(`
      <div class="popup-id">${b.structure_number}</div>
      <div class="popup-name">${b.facility_carried || "Bridge"}</div>
      <div class="popup-meta">${b.county_name || ""} · Built ${b.year_built || "?"} · ${adt}</div>
      <div class="popup-cond" style="color:${color}">
        NBI ${b.min_condition ?? "—"} — ${conditionLabel(b.min_condition)}
        ${b.structurally_deficient ? " · <strong>Structurally Deficient</strong>" : ""}
      </div>
    `, { maxWidth: 260 });

    // Click → load full detail in right panel
    marker.on("click", () => {
      fetchBridgeDetail(b.structure_number);
    });

    marker.addTo(markerLayer);
    markersByBridge[b.structure_number] = marker;  // Store for programmatic access
    bounds.push([b.latitude, b.longitude]);
    count++;
  });

  document.getElementById("map-bridge-count").textContent =
    `${count.toLocaleString()} bridges shown`;

  // Fit map to visible bridges when filtering by county
  if (AppState.activeCountyId && bounds.length > 0) {
    map.fitBounds(L.latLngBounds(bounds), { padding: [40, 40] });
  }
}

async function fetchBridgesForCounty(countyId) {
  /**
   * When a county is selected, re-fetch bridges scoped to that county
   * from the API (server-side filter = correct results).
   */
  const path = countyId
    ? `/api/bridges?county_id=${countyId}`
    : "/api/bridges";

  const bridges = await apiFetch(path);
  if (!bridges) return;

  AppState.allBridges = bridges;
  updateMapMarkers();
}

// ── Condition Slider ───────────────────────────────────────────

const sliderMin = document.getElementById("filter-min");
const sliderMax = document.getElementById("filter-max");
const rangeLabel = document.getElementById("condition-range-label");

function updateConditionFilter() {
  let min = parseInt(sliderMin.value);
  let max = parseInt(sliderMax.value);
  if (min > max) { min = max; sliderMin.value = min; }  // Prevent cross-over

  AppState.minCond = min;
  AppState.maxCond = max;

  rangeLabel.textContent = (min === 0 && max === 9)
    ? "All"
    : `${min} – ${max}`;

  updateMapMarkers();
}

sliderMin.addEventListener("input", updateConditionFilter);
sliderMax.addEventListener("input", updateConditionFilter);

// ── Insights Feed ─────────────────────────────────────────────

async function fetchInsights() {
  document.getElementById("insight-feed").innerHTML = `
    <div class="skeleton-list">
      <div class="skeleton-item tall"></div>
      <div class="skeleton-item tall"></div>
      <div class="skeleton-item tall"></div>
    </div>
  `;

  let path = "/api/insights?limit=40";
  if (AppState.activeCountyId) path += `&county_id=${AppState.activeCountyId}`;
  if (AppState.insightType)    path += `&insight_type=${AppState.insightType}`;

  const insights = await apiFetch(path);
  if (!insights || insights.length === 0) {
    document.getElementById("insight-feed").innerHTML =
      `<div class="empty-state">No insights available for this selection.</div>`;
    return;
  }

  const html = insights.map(ins => `
    <div class="insight-card ${ins.severity}"
         data-tip="${ins.insight_type.toUpperCase()} · Severity: ${ins.severity} · Confidence: ${ins.confidence_score ? Math.round(ins.confidence_score*100)+'%' : '—'}${ins.structure_number ? ' · Click to go to bridge ' + ins.structure_number : ''}"
         onclick="onInsightClick('${ins.structure_number || ""}')">
      <div class="insight-type-badge ${ins.insight_type}">${ins.insight_type}</div>
      <div class="insight-title">${ins.title}</div>
      <div class="insight-desc">${ins.description}</div>
      <div class="insight-meta">
        ${ins.county_name ? ins.county_name + " County · " : ""}
        ${ins.structure_number ? "Bridge " + ins.structure_number + " · " : ""}
        Confidence ${ins.confidence_score ? Math.round(ins.confidence_score * 100) + "%" : "—"}
      </div>
    </div>
  `).join("");

  document.getElementById("insight-feed").innerHTML = html;
}

function onInsightClick(structureNumber) {
  /**
   * When the user clicks an insight card, if the insight is linked to
   * a specific bridge, load that bridge's detail in the right panel.
   */
  if (structureNumber) fetchBridgeDetail(structureNumber);
}

// Insight type tab switching
document.getElementById("insight-tabs").addEventListener("click", e => {
  const tab = e.target.closest(".tab");
  if (!tab) return;

  document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
  tab.classList.add("active");

  AppState.insightType = tab.dataset.type;
  fetchInsights();
});

// ── Bridge Detail Panel ───────────────────────────────────────

let activeInspectionYear = null;

async function fetchBridgeDetail(structureNumber) {
  document.getElementById("bridge-placeholder").classList.add("hidden");
  const detailEl = document.getElementById("bridge-detail");
  detailEl.classList.remove("hidden");
  detailEl.innerHTML = `<div class="skeleton-list" style="padding:20px">
    <div class="skeleton-item"></div>
    <div class="skeleton-item tall"></div>
    <div class="skeleton-item tall"></div>
  </div>`;

  // Fetch bridge detail and images in parallel
  const [bridge, images] = await Promise.all([
    apiFetch(`/api/bridges/${structureNumber}`),
    apiFetch(`/api/bridges/${structureNumber}/images`),
  ]);

  if (!bridge) {
    detailEl.innerHTML = `<div class="empty-state" style="padding:20px">Failed to load bridge detail.</div>`;
    return;
  }

  // Set the most recent year as default active inspection
  const years = bridge.inspections.map(i => i.data_year).sort((a,b) => b-a);
  activeInspectionYear = years[0] || null;

  renderBridgeDetail(bridge, images || []);
}

function renderBridgeDetail(bridge, images) {
  // Keep images cached on the bridge object so switchYear can re-render without re-fetching
  if (images !== undefined) bridge._images = images;
  const imgs = bridge._images || [];
  /**
   * Renders the full bridge detail panel.
   * Split into sub-sections so individual parts can be re-rendered
   * when the user switches inspection years (without re-fetching).
   */
  const detailEl = document.getElementById("bridge-detail");

  // Find the active inspection
  const insp = bridge.inspections.find(i => i.data_year === activeInspectionYear)
            || bridge.inspections[bridge.inspections.length - 1];

  // Build year tabs HTML
  const years = bridge.inspections.map(i => i.data_year).sort((a,b) => b-a);
  const yearTabsHtml = years.map(y => `
    <button class="year-tab ${y === activeInspectionYear ? "active" : ""}"
            onclick="switchYear(${y})">
      ${y}
    </button>
  `).join("");

  // NBI condition grid for this inspection
  const nbiHtml = renderNbiGrid(insp);

  // Trend sparkline (if 2+ years of data)
  const sparkHtml = bridge.inspections.length >= 2
    ? renderSparkline(bridge.inspections, `spark-${bridge.structure_number}`)
    : "";

  // Defects for selected year
  const defectsHtml = renderDefects(insp);

  // Recommendations for selected year
  const recsHtml = renderRecommendations(insp);

  // Photos section (from extracted PDF images)
  const photosHtml = renderImages(imgs, bridge.structure_number);

  detailEl.innerHTML = `
    <!-- Header -->
    <div class="detail-header">
      <div class="detail-bridge-id">Bridge ${bridge.structure_number}</div>
      <div class="detail-bridge-name">${bridge.facility_carried || "Unknown"}</div>
      <div class="detail-bridge-sub">
        ${bridge.county_name || ""} County
        ${bridge.feature_intersected ? " · over " + bridge.feature_intersected : ""}
      </div>
    </div>

    <!-- Inspection Photos -->
    ${photosHtml ? `
    <div class="detail-section">
      <div class="detail-section-title">Inspection Photos (${imgs.length})</div>
      ${photosHtml}
    </div>` : ""}

    <!-- Physical attributes -->
    <div class="detail-section">
      <div class="detail-section-title">Structure</div>
      <table class="attr-table">
        <tr><td class="attr-key">Built</td>
            <td class="attr-val">${bridge.year_built || "—"}${bridge.year_reconstructed ? " (recon. " + bridge.year_reconstructed + ")" : ""}</td></tr>
        <tr><td class="attr-key">Material</td>
            <td class="attr-val">${bridge.material_name || "—"}</td></tr>
        <tr><td class="attr-key">Design</td>
            <td class="attr-val">${bridge.design_name || "—"}</td></tr>
        <tr><td class="attr-key">Spans</td>
            <td class="attr-val">${bridge.number_of_spans || "—"}</td></tr>
        <tr><td class="attr-key">Length</td>
            <td class="attr-val">${bridge.structure_length ? bridge.structure_length.toFixed(1) + " m" : "—"}</td></tr>
        <tr><td class="attr-key">Deck width</td>
            <td class="attr-val">${bridge.deck_width ? bridge.deck_width.toFixed(1) + " m" : "—"}</td></tr>
        <tr><td class="attr-key">ADT</td>
            <td class="attr-val">${bridge.adt ? bridge.adt.toLocaleString() : "—"}</td></tr>
        <tr><td class="attr-key">Owner</td>
            <td class="attr-val">${bridge.owner_name || "—"}</td></tr>
      </table>
    </div>

    <!-- Condition trend sparkline -->
    ${sparkHtml ? `
    <div class="detail-section">
      <div class="detail-section-title">Condition Trend (min NBI)</div>
      ${sparkHtml}
    </div>` : ""}

    <!-- Inspection year tabs + NBI ratings -->
    <div class="detail-section">
      <div class="detail-section-title">Inspection History</div>
      <div class="year-tabs" id="year-tabs-${bridge.structure_number}">
        ${yearTabsHtml}
      </div>
      ${nbiHtml}
    </div>

    <!-- Defects -->
    <div class="detail-section">
      <div class="detail-section-title">
        Defects (${insp ? insp.defects.length : 0}) — AI Extracted
      </div>
      ${defectsHtml}
    </div>

    <!-- Recommendations -->
    <div class="detail-section">
      <div class="detail-section-title">
        Recommendations (${insp ? insp.recommendations.length : 0}) — AI Extracted
      </div>
      ${recsHtml}
    </div>
  `;

  // Draw the sparkline chart after the DOM is ready
  if (bridge.inspections.length >= 2) {
    drawSparkline(bridge.inspections, `spark-${bridge.structure_number}`);
  }

  // Store bridge on window so switchYear() can re-render without re-fetching
  window._activeBridge = bridge;
}

function switchYear(year) {
  /**
   * When user clicks a year tab, re-render only the dynamic sections
   * (NBI grid, defects, recommendations) without re-fetching the bridge.
   */
  activeInspectionYear = year;
  if (window._activeBridge) renderBridgeDetail(window._activeBridge);
}

// ── Sub-renderers ─────────────────────────────────────────────

function renderImages(images, structureNumber) {
  /**
   * Renders a horizontal scrollable strip of inspection photos.
   * Each thumbnail is a plain <a target="_blank"> link — clicking opens
   * the full-size image in a new browser tab. Simple, reliable, no JS needed.
   */
  if (!images || images.length === 0) return "";

  const thumbs = images.map(img => `
    <a class="photo-thumb" href="${img.url}" target="_blank" rel="noopener"
       data-tip="Page ${img.page_number} — click to open full size">
      <img src="${img.url}" alt="Inspection photo page ${img.page_number}" loading="lazy" />
      <div class="photo-caption">Page ${img.page_number}</div>
    </a>
  `).join("");

  return `<div class="photo-strip" id="photo-strip-${structureNumber}">${thumbs}</div>`;
}

function openLightbox(url, index, images) {
  /**
   * Opens a simple lightbox overlay showing the clicked image full-size.
   * Click outside or press Escape to close.
   */
  // Remove any existing lightbox
  const existing = document.getElementById("photo-lightbox");
  if (existing) existing.remove();

  const lb = document.createElement("div");
  lb.id = "photo-lightbox";
  lb.innerHTML = `
    <div class="lightbox-backdrop" onclick="document.getElementById('photo-lightbox').remove()"></div>
    <div class="lightbox-content">
      <button class="lightbox-close" onclick="document.getElementById('photo-lightbox').remove()">&times;</button>
      <img src="${url}" alt="Inspection photo" />
      <div class="lightbox-caption">Photo ${index + 1} of ${images.length} · Page ${images[index].page_number}</div>
    </div>
  `;
  document.body.appendChild(lb);

  // Keyboard escape to close
  const onKey = (e) => { if (e.key === "Escape") { lb.remove(); document.removeEventListener("keydown", onKey); } };
  document.addEventListener("keydown", onKey);
}
function renderNbiGrid(insp) {
  if (!insp) return `<div class="empty-state">No inspection data.</div>`;

  const nbiTips = {
    "Deck"   : "NBI Item 58: Deck condition (0-9). Covers the riding surface and primary load-carrying deck structure.",
    "Super." : "NBI Item 59: Superstructure condition. Covers beams, girders, trusses — the load-carrying members above the substructure.",
    "Sub."   : "NBI Item 60: Substructure condition. Covers piers, abutments, and foundations.",
    "Channel": "NBI Item 61: Channel & channel protection. Covers scour, bank erosion, and waterway alignment.",
    "Min"    : "The worst (minimum) condition across all rated components — this governs the overall bridge safety rating.",
    "Health" : "Pontis/AASHTOWare health index (0-100): a weighted composite of all element condition states. Higher = better.",
  };

  const nbiField = (val, label) => {
    const color = conditionColor(val);
    const tip   = nbiTips[label] || label;
    return `
      <div class="nbi-pill" data-tip="${tip}">
        <div class="nbi-value" style="color:${color}">${val ?? "—"}</div>
        <div class="nbi-label">${label}</div>
      </div>
    `;
  };

  const sdBadge = insp.structurally_deficient
    ? `<div style="margin-top:10px;padding:5px 10px;background:rgba(239,68,68,0.15);
         color:#f87171;border-radius:6px;font-size:11px;font-weight:600;text-align:center">
         ⚠ Structurally Deficient
       </div>`
    : "";

  return `
    <div class="nbi-grid">
      ${nbiField(insp.deck_condition, "Deck")}
      ${nbiField(insp.superstructure_condition, "Super.")}
      ${nbiField(insp.substructure_condition, "Sub.")}
      ${nbiField(insp.channel_condition, "Channel")}
      ${nbiField(insp.min_condition, "Min")}
      ${nbiField(insp.health_index ? Math.round(insp.health_index) : null, "Health")}
    </div>
    ${sdBadge}
  `;
}

function renderSparkline(inspections, canvasId) {
  /**
   * Returns the HTML wrapper for the sparkline chart.
   * The actual Chart.js chart is drawn by drawSparkline() after the DOM is ready.
   */
  return `<div class="sparkline-wrapper"><canvas id="${canvasId}"></canvas></div>`;
}

function drawSparkline(inspections, canvasId) {
  /**
   * Draws a Chart.js line chart showing the minimum NBI condition rating
   * over inspection years. Called after renderBridgeDetail() inserts the canvas.
   */
  const sorted = [...inspections].sort((a,b) => a.data_year - b.data_year);
  const labels = sorted.map(i => i.data_year);
  const data   = sorted.map(i => i.min_condition);
  const colors = data.map(v => conditionColor(v));

  const canvas = document.getElementById(canvasId);
  if (!canvas) return;

  // Destroy previous chart instance if the canvas was reused
  if (canvas._chartInstance) canvas._chartInstance.destroy();

  canvas._chartInstance = new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [{
        data,
        borderColor       : "#0ea5e9",
        backgroundColor   : "rgba(14,165,233,0.1)",
        pointBackgroundColor : colors,
        pointRadius       : 5,
        pointHoverRadius  : 7,
        tension           : 0.3,
        fill              : true,
      }],
    },
    options: {
      responsive : true,
      maintainAspectRatio: false,
      plugins : { legend: { display: false }, tooltip: { callbacks: {
        label: ctx => `NBI ${ctx.raw} — ${conditionLabel(ctx.raw)}`
      }}},
      scales: {
        y: {
          min: 0, max: 9,
          grid  : { color: "#f1f5f9" },
          ticks : { color: "#94a3b8", stepSize: 1 },
        },
        x: {
          grid  : { display: false },
          ticks : { color: "#94a3b8" },
        },
      },
    },
  });
}

function renderDefects(insp) {
  if (!insp || insp.defects.length === 0)
    return `<div class="empty-state">
      <div style="font-size:20px;margin-bottom:6px">⌛</div>
      AI extraction pending for this inspection year.<br>
      <span style="font-size:10px;color:var(--text-muted)">The defect agent is still processing PDFs — check back soon.</span>
    </div>`;

  // Sort: critical first
  const order = { critical:0, severe:1, moderate:2, minor:3 };
  const sorted = [...insp.defects].sort((a,b) =>
    (order[a.severity]??9) - (order[b.severity]??9)
  );

  const severityTips = {
    minor   : "Minor: cosmetic or early-stage defect, no structural concern. Monitor at next inspection.",
    moderate: "Moderate: defect is progressing and requires scheduled maintenance action.",
    severe  : "Severe: structural integrity is compromised. Prioritize repair within the current cycle.",
    critical: "Critical: imminent structural risk. Requires immediate action or load restriction.",
  };

  const componentTips = {
    deck         : "The riding surface and primary deck slab.",
    superstructure: "Load-carrying members: beams, girders, trusses.",
    substructure : "Piers, abutments, and foundations.",
    joint        : "Expansion joints that allow thermal movement between spans.",
    bearing      : "Bearing devices transferring load from superstructure to substructure.",
    railing      : "Traffic barriers and pedestrian railings.",
    channel      : "Waterway, scour protection, and bank stability.",
  };

  return sorted.map(d => `
    <div class="defect-item ${d.severity}">
      <div class="defect-header">
        <span class="tag tag-severity ${d.severity}"
              data-tip="${severityTips[d.severity] || d.severity}">${d.severity}</span>
        <span class="tag tag-component"
              data-tip="${componentTips[d.component] || 'Bridge component where the defect was observed'}">${d.component}</span>
        <span class="tag tag-type"
              data-tip="Defect classification: ${d.defect_type}">${d.defect_type}</span>
      </div>
      <div class="defect-desc">${d.description}</div>
      ${d.location_on_bridge ? `<div class="defect-loc">📍 ${d.location_on_bridge}</div>` : ""}
    </div>
  `).join("");
}

function renderRecommendations(insp) {
  if (!insp || insp.recommendations.length === 0)
    return `<div class="empty-state">
      <div style="font-size:20px;margin-bottom:6px">⌛</div>
      AI extraction pending for this inspection year.
    </div>`;

  const order = { urgent:0, corrective:1, preventive:2, routine:3 };
  const sorted = [...insp.recommendations].sort((a,b) =>
    (order[a.priority_level]??9) - (order[b.priority_level]??9)
  );

  const priorityTips = {
    routine    : "Routine: scheduled preventive work, no urgency.",
    preventive : "Preventive: action needed before the defect worsens. Plan within 1-2 years.",
    corrective : "Corrective: defect is present and worsening. Action required within current budget cycle.",
    urgent     : "Urgent: immediate action required. Risk of failure or public safety concern.",
  };

  return sorted.map(r => `
    <div class="rec-item">
      <div class="rec-priority ${r.priority_level}"
           data-tip="${priorityTips[r.priority_level] || r.priority_level}" title=""></div>
      <div>
        <div class="rec-text">${r.action_description}</div>
        <div class="rec-cat">${r.priority_level.toUpperCase()} · ${r.category}
          ${r.estimated_cost ? ` · $${r.estimated_cost.toLocaleString()}` : ""}
        </div>
      </div>
    </div>
  `).join("");
}

// ── Tooltip Engine ────────────────────────────────────────────
//
// A lightweight tooltip system that appends a single floating div
// to <body> on hover, so it is never clipped by overflow containers.
//
// Usage: add  data-tip="Your tooltip text"  to any HTML element.
// Works on both static elements and dynamically rendered ones
// (event delegation on document handles the latter).

(function initTooltips() {
  const tip = document.createElement("div");
  tip.className = "ui-tooltip";
  document.body.appendChild(tip);

  let showTimer = null;  // Delay before showing — prevents flicker on fast mouse moves
  let hideTimer = null;

  function showTip(text, rect) {
    clearTimeout(hideTimer);
    clearTimeout(showTimer);
    // 500ms hover delay — feels intentional, not accidental
    showTimer = setTimeout(() => _renderTip(text, rect), 500);
  }

  function _renderTip(text, rect) {
    tip.textContent = text;

    // Position above the target element, horizontally centred
    const tipW = 240;  // max-width from CSS
    let left = rect.left + rect.width / 2;
    let top  = rect.top - 10;  // 10px gap above element

    // Clamp horizontally so it doesn't overflow the viewport
    left = Math.max(8, Math.min(left, window.innerWidth - tipW / 2 - 8));

    tip.style.left      = left + "px";
    tip.style.top       = top + "px";
    tip.style.transform = "translateX(-50%) translateY(-100%)";

    // Use rAF to ensure the initial invisible state is rendered before
    // we add .visible — this triggers the CSS transition correctly
    requestAnimationFrame(() => tip.classList.add("visible"));
  }

  function hideTip() {
    tip.classList.remove("visible");
  }

  // Delegate to document so dynamic elements get tooltips automatically
  document.addEventListener("mouseover", e => {
    const el = e.target.closest("[data-tip]");
    if (!el) return;
    const text = el.getAttribute("data-tip");
    if (!text) return;
    showTip(text, el.getBoundingClientRect());
  });

  document.addEventListener("mouseout", e => {
    if (!e.target.closest("[data-tip]")) return;
    clearTimeout(showTimer);  // Cancel pending show if mouse left before delay elapsed
    hideTip();
  });

  // Also hide if the element scrolls out of view
  document.addEventListener("scroll", hideTip, true);
})();

// ── Static element tooltips ───────────────────────────────────
// Applied after DOM is ready. Dynamic elements get data-tip
// added in their respective render functions below.

document.getElementById("api-status").setAttribute(
  "data-tip", "API connection status — green means the backend is reachable"
);
document.getElementById("api-status-label").setAttribute(
  "data-tip", "API connection status — green means the backend is reachable"
);
document.getElementById("btn-show-all").setAttribute(
  "data-tip", "Show all bridges across all 5 counties"
);
document.getElementById("filter-min").setAttribute(
  "data-tip", "NBI condition scale: 0 = Failed, 9 = Excellent. Filter to show only bridges at or above this rating."
);
document.getElementById("filter-max").setAttribute(
  "data-tip", "Filter to show only bridges at or below this NBI condition rating."
);

// Insight type tab tooltips
const tabTips = {
  ""       : "Show all AI-generated insights",
  "trend"  : "Bridges whose NBI condition rating has declined measurably over multiple inspection years",
  "risk"   : "Bridges ranked by composite risk score: structural condition × traffic volume",
  "pattern": "Defect types recurring across many bridges in a county — systemic infrastructure issues",
};
document.querySelectorAll("#insight-tabs .tab").forEach(tab => {
  const type = tab.dataset.type;
  if (tabTips[type]) tab.setAttribute("data-tip", tabTips[type]);
});

// ── Startup ───────────────────────────────────────────────────

(async function init() {
  /**
   * Boot sequence — runs all initial data fetches in parallel where
   * possible (counties + insights can load independently of bridges).
   */
  try {
    // These can run in parallel — they don't depend on each other
    await Promise.all([
      fetchGlobalStats(),
      fetchCounties(),
      fetchInsights(),
    ]);

    // Bridges take longer (larger payload) — start last
    await fetchBridges();

  } catch (err) {
    console.error("Init error:", err);
    document.getElementById("api-status").className = "status-dot offline";
    document.getElementById("api-status-label").textContent = "API Offline";
  }
})();
