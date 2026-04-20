"""
map_builder.py — DealDesk Map & Street View Image Generator
============================================================
Generates all map and street view images for the PDF report:

    Figure 2.1 — Street View (property exterior, 3 angles)
    Figure 3.1 — Aerial Location Map     (Aerial View API → Static satellite → OSM)
    Figure 3.2 — Neighborhood Context    (Static Maps + Places API POI pins)
    Figure 3.3 — FEMA Flood Map          (FEMA NFHL REST /export — unchanged)

Each function returns PNG bytes or None on failure.
Pipeline continues cleanly on any individual failure.

Dependencies: requests, Pillow (PIL)
"""

from __future__ import annotations

import io
import logging
import math
import urllib.parse
import urllib.request
from typing import List, Optional, Tuple

import requests

from config import (
    AERIAL_VIEW_API_URL,
    ELEVATION_API_URL,
    GOOGLE_MAPS_API_KEY,
    MAP_HEIGHT,
    MAP_TILES_FETCH_URL,
    MAP_TILES_SESSION_URL,
    MAP_WIDTH,
    PLACES_NEARBY_URL,
    POI_TYPES,
    SV_HEIGHT,
    SV_WIDTH,
)
from models.models import DealData

logger = logging.getLogger(__name__)

# ── Brand colors (DealDesk design system) ─────────────────────────────────
SAGE_DEEP   = "#4A6E50"
DARK_WALNUT = "#2C1F14"
PARCHMENT   = "#F5EFE4"
SAGE_LIGHT  = "#B2C9B4"

TIMEOUT = 12

# ── OpenStreetMap tile settings (fallback) ────────────────────────────────
OSM_TILE_SERVER = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_USER_AGENT  = "DealDesk-CRE-Underwriting/1.0 (contact@freedman-properties.com)"


# ═══════════════════════════════════════════════════════════════════════════
# SHARED HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _lat_lon_valid(lat, lon) -> bool:
    """Return True only when lat/lon are real numeric coordinates."""
    try:
        if lat is None or lon is None:
            return False
        return -90 <= float(lat) <= 90 and -180 <= float(lon) <= 180
    except (TypeError, ValueError):
        return False


def _img_to_bytes(img, fmt: str = "PNG") -> bytes:
    """Convert a PIL Image to bytes in the given format."""
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _deg_to_tile(lat: float, lon: float, zoom: int) -> Tuple[int, int]:
    """Convert lat/lon to OpenStreetMap tile x/y at the given zoom level."""
    lat_r = math.radians(lat)
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return x, y


# Alias kept for any legacy callers importing _deg2tile
_deg2tile = _deg_to_tile


def _fetch_url(url, headers=None):
    """urllib-based fetch that matches the legacy implementation's contract.
    Kept so FEMA and other existing callers keep working."""
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = resp.read()
            logger.debug("HTTP %d — %d bytes from %s",
                         resp.status, len(data), url[:80])
            return data
    except Exception as exc:
        logger.warning("HTTP fetch failed for %s: %s", url[:80], exc)
        return None


def _stitch_tiles(lat, lon, zoom, tiles_wide=3, tiles_tall=3):
    """Legacy OSM tile stitcher — crops to MAP_WIDTH×MAP_HEIGHT and drops a
    red property pin. Kept as-is for FEMA base map compatibility."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("Pillow not installed — cannot stitch OSM tiles")
        return None

    cx, cy = _deg_to_tile(lat, lon, zoom)
    half_w = tiles_wide // 2
    half_h = tiles_tall // 2
    tile_size = 256
    canvas = Image.new("RGB", (tiles_wide * tile_size, tiles_tall * tile_size))

    for dy in range(-half_h, half_h + 1):
        for dx in range(-half_w, half_w + 1):
            tx, ty = cx + dx, cy + dy
            url = OSM_TILE_SERVER.format(z=zoom, x=tx, y=ty)
            data = _fetch_url(url, headers={"User-Agent": OSM_USER_AGENT})
            if not data:
                logger.warning("OSM tile missing: z=%d x=%d y=%d", zoom, tx, ty)
                return None
            tile_img = Image.open(io.BytesIO(data)).convert("RGB")
            px = (dx + half_w) * tile_size
            py = (dy + half_h) * tile_size
            canvas.paste(tile_img, (px, py))

    total_w = tiles_wide * tile_size
    total_h = tiles_tall * tile_size
    left = (total_w - MAP_WIDTH) // 2
    top  = (total_h - MAP_HEIGHT) // 2
    canvas = canvas.crop((left, top, left + MAP_WIDTH, top + MAP_HEIGHT))

    # Red property pin
    draw = ImageDraw.Draw(canvas)
    cx_px, cy_px = MAP_WIDTH // 2, MAP_HEIGHT // 2
    r = 8
    draw.ellipse((cx_px - r, cy_px - r, cx_px + r, cy_px + r),
                 fill="#CC2200", outline="#FFFFFF", width=2)
    draw.ellipse((cx_px - 2, cy_px - 2, cx_px + 2, cy_px + 2),
                 fill="#FFFFFF")

    return _img_to_bytes(canvas)


def _stitch_osm_tiles(lat: float, lon: float,
                      zoom: int = 16, grid: int = 3) -> Optional[bytes]:
    """Modern tile-stitching fallback — delegates to the existing
    _stitch_tiles implementation so both path names resolve to the same
    code. Kept for API symmetry with the integration spec."""
    return _stitch_tiles(lat, lon, zoom=zoom, tiles_wide=grid, tiles_tall=grid)


# ═══════════════════════════════════════════════════════════════════════════
# MAP TILES API — Session helper (for styled tile fallback)
# ═══════════════════════════════════════════════════════════════════════════

_TILES_SESSION: Optional[str] = None


def _get_map_tiles_session() -> Optional[str]:
    """Create a Map Tiles API session token for styled tile fetching.
    Session tokens are valid for 2 weeks. Returns None if the API key is
    missing or the request fails."""
    if not GOOGLE_MAPS_API_KEY:
        return None

    payload = {
        "mapType":    "roadmap",
        "language":   "en-US",
        "region":     "US",
        "layerTypes": [],
        "scale":      "scaleFactor2x",
        "highDpi":    True,
        "styles": [
            {"stylers": [{"saturation": -35}, {"lightness": 5}]},
            {"featureType": "water",        "stylers": [{"color": "#C9D8E8"}]},
            {"featureType": "landscape",    "stylers": [{"color": "#EEE8DC"}]},
            {"featureType": "road.highway", "stylers": [{"color": "#D4C4A8"}]},
        ],
    }
    try:
        r = requests.post(
            MAP_TILES_SESSION_URL,
            params={"key": GOOGLE_MAPS_API_KEY},
            json=payload,
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        session = r.json().get("session")
        logger.info("Map Tiles API: session token created")
        return session
    except Exception as exc:
        logger.warning("Map Tiles API session creation failed: %s", exc)
        return None


def _get_or_create_tiles_session() -> Optional[str]:
    """Return the cached session token or create a new one."""
    global _TILES_SESSION
    if not _TILES_SESSION:
        _TILES_SESSION = _get_map_tiles_session()
    return _TILES_SESSION


# ═══════════════════════════════════════════════════════════════════════════
# FIGURE 2.1 — STREET VIEW (property exterior, 3 angles)
# ═══════════════════════════════════════════════════════════════════════════

def _fetch_street_view_image(lat: float, lon: float,
                             heading: int = 0,
                             pitch: int = 10,
                             fov: int = 90) -> Optional[bytes]:
    """Fetch a single Street View Static API image. Returns JPEG bytes or
    None if no imagery is available at the heading."""
    if not GOOGLE_MAPS_API_KEY:
        logger.warning("Street View skipped — no GOOGLE_MAPS_API_KEY")
        return None

    params = {
        "size":     f"{SV_WIDTH}x{SV_HEIGHT}",
        "location": f"{lat},{lon}",
        "heading":  str(heading),
        "pitch":    str(pitch),
        "fov":      str(fov),
        "source":   "outdoor",
        "return_error_code": "true",
        "key":      GOOGLE_MAPS_API_KEY,
    }
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/streetview",
            params=params,
            timeout=TIMEOUT,
        )
        if r.status_code == 404:
            logger.info("Street View: no imagery at heading=%s (404)", heading)
            return None
        r.raise_for_status()
        # Quick sanity check that this is a real image payload.
        if len(r.content) < 1000:
            logger.info("Street View: response too small (%d bytes) — skipping", len(r.content))
            return None
        logger.info("Street View: fetched heading=%s (%d bytes)", heading, len(r.content))
        return r.content
    except Exception as exc:
        logger.error("Street View fetch error (heading=%s): %s", heading, exc)
        return None


def build_street_view(deal) -> Tuple[Optional[bytes], Optional[bytes], Optional[bytes]]:
    """Figure 2.1 — property exterior Street View at three angles.

    Returns (primary_bytes, alt1_bytes, alt2_bytes). Any element may be None
    when imagery is unavailable at that heading."""
    lat = deal.address.latitude
    lon = deal.address.longitude
    if not _lat_lon_valid(lat, lon):
        logger.info("Street View skipped — no geocoordinates")
        return None, None, None

    logger.info("Street View: fetching 3 angles for %s", deal.address.full_address)
    primary = _fetch_street_view_image(lat, lon, heading=0,   pitch=10)
    alt1    = _fetch_street_view_image(lat, lon, heading=90,  pitch=5)
    alt2    = _fetch_street_view_image(lat, lon, heading=270, pitch=5)
    fetched = sum(1 for x in [primary, alt1, alt2] if x)
    logger.info("Street View: %d/3 angles fetched", fetched)
    return primary, alt1, alt2


# ═══════════════════════════════════════════════════════════════════════════
# FIGURE 3.1 — AERIAL LOCATION MAP
# ═══════════════════════════════════════════════════════════════════════════

def _fetch_aerial_view_still(lat: float, lon: float) -> Optional[bytes]:
    """Attempt a photographic aerial still from the Aerial View API.
    The API is in preview; returns None on any failure (no coverage,
    PROCESSING state, or unusable response shape)."""
    if not GOOGLE_MAPS_API_KEY:
        return None
    try:
        r = requests.post(
            AERIAL_VIEW_API_URL,
            params={"key": GOOGLE_MAPS_API_KEY},
            json={"address": f"{lat},{lon}"},
            timeout=TIMEOUT,
        )
        if r.status_code in (403, 404):
            logger.info("Aerial View API: no coverage at %s,%s (status %s)",
                        lat, lon, r.status_code)
            return None
        r.raise_for_status()
        data = r.json()
        if data.get("state") == "PROCESSING":
            logger.info("Aerial View API: video still PROCESSING — skipping")
            return None
        uris = data.get("uris") or {}
        image_url = (uris.get("LANDSCAPE_IMAGE")
                     or uris.get("PORTRAIT_IMAGE")
                     or uris.get("LANDSCAPE_VIDEO"))
        if not image_url:
            logger.info("Aerial View API: no image URI in response — skipping")
            return None
        img_r = requests.get(image_url, timeout=TIMEOUT)
        img_r.raise_for_status()
        from PIL import Image
        img = Image.open(io.BytesIO(img_r.content)).convert("RGB")
        img = img.resize((MAP_WIDTH, MAP_HEIGHT), Image.LANCZOS)
        logger.info("Aerial View API: fetched still — %dx%d", img.width, img.height)
        return _img_to_bytes(img)
    except Exception as exc:
        logger.warning("Aerial View API error: %s — falling back", exc)
        return None


def build_aerial_map(deal) -> Optional[bytes]:
    """Figure 3.1 — aerial / satellite location map.

    Framed to a ~30-minute driving radius (~20–25 mi) for regional context.
    Aerial View API is skipped — it only returns building-level stills and
    cannot zoom out to a regional frame.

    Priority: Static Maps satellite zoom 10 → OSM tiles.
    Always returns PNG bytes or None."""
    lat, lon = deal.address.latitude, deal.address.longitude
    if not _lat_lon_valid(lat, lon):
        logger.info("Aerial map skipped — no geocoordinates")
        return None

    logger.info("Aerial map: building for lat=%.5f lon=%.5f", lat, lon)

    # Priority 1 — Maps Static API satellite at regional zoom (~30-min drive)
    if GOOGLE_MAPS_API_KEY:
        try:
            params = {
                "center":  f"{lat},{lon}",
                "zoom":    "10",
                "size":    f"{MAP_WIDTH}x{MAP_HEIGHT}",
                "maptype": "satellite",
                "scale":   "2",
                "markers": f"color:0x4A6E50|label:P|{lat},{lon}",
                "key":     GOOGLE_MAPS_API_KEY,
            }
            r = requests.get(
                "https://maps.googleapis.com/maps/api/staticmap",
                params=params,
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            logger.info("Aerial map: using Maps Static API satellite view (zoom 10)")
            return r.content
        except Exception as exc:
            logger.warning("Maps Static aerial fallback failed: %s", exc)

    # Priority 2 — OSM tile stitching at matching regional zoom
    logger.info("Aerial map: falling back to OSM tile stitching")
    return _stitch_osm_tiles(lat, lon, zoom=10, grid=3)


# ═══════════════════════════════════════════════════════════════════════════
# FIGURE 3.2 — NEIGHBORHOOD CONTEXT MAP
# ═══════════════════════════════════════════════════════════════════════════

def _build_poi_marker_string(pois: List[dict]) -> List[str]:
    """Convert Places API POI dicts to Static Maps marker strings.
    Caps at 5 pins per category, color-coded by type."""
    category_colors = {
        "transit_station":        "0x4A6E50",
        "subway_station":         "0x4A6E50",
        "bus_station":            "0x4A6E50",
        "grocery_or_supermarket": "0xC4A882",
        "school":                 "0x5C3D26",
        "park":                   "0xB2C9B4",
        "restaurant":             "0x8B6914",
        "hospital":               "0x8B2020",
    }
    markers: List[str] = []
    seen_types: dict = {}
    for poi in pois:
        ptype = poi.get("type", "other")
        if seen_types.get(ptype, 0) >= 5:
            continue
        color = category_colors.get(ptype, "0x888888")
        p_lat = poi.get("lat")
        p_lon = poi.get("lon")
        if p_lat and p_lon:
            markers.append(f"color:{color}|size:tiny|{p_lat},{p_lon}")
            seen_types[ptype] = seen_types.get(ptype, 0) + 1
    return markers


def build_neighborhood_map(deal) -> Optional[bytes]:
    """Figure 3.2 — zoom-14 roadmap with property pin and POI pins
    (from deal.nearby_pois when available). Falls back to OSM zoom 14."""
    lat, lon = deal.address.latitude, deal.address.longitude
    if not _lat_lon_valid(lat, lon):
        logger.info("Neighborhood map skipped — no geocoordinates")
        return None

    if not GOOGLE_MAPS_API_KEY:
        logger.warning("Neighborhood map: no API key — OSM fallback")
        return _stitch_osm_tiles(lat, lon, zoom=14, grid=3)

    # Property pin (red) plus color-coded POI pins
    marker_params = [f"color:red|label:P|{lat},{lon}"]
    pois = getattr(deal, "nearby_pois", None) or []
    if pois:
        poi_markers = _build_poi_marker_string(pois)
        marker_params.extend(poi_markers)
        logger.info("Neighborhood map: adding %d POI pins", len(poi_markers))

    styles = [
        "feature:all|element:labels.text.fill|color:0x4A4A3A",
        "feature:water|element:geometry|color:0xC9D8E8",
        "feature:landscape|element:geometry|color:0xEEE8DC",
        "feature:road.highway|element:geometry|color:0xD4C4A8",
        "feature:road.arterial|element:geometry|color:0xDDD5C4",
        "feature:poi|element:geometry|color:0xDDD8CC",
        "feature:transit|element:geometry|color:0xC8C0B0",
    ]

    base = "https://maps.googleapis.com/maps/api/staticmap"
    parts = [
        f"center={lat},{lon}",
        "zoom=14",
        f"size={MAP_WIDTH}x{MAP_HEIGHT}",
        "maptype=roadmap",
        "scale=2",
        f"key={GOOGLE_MAPS_API_KEY}",
    ]
    for m in marker_params:
        parts.append(f"markers={urllib.parse.quote(m, safe='|:,')}")
    for s in styles:
        parts.append(f"style={urllib.parse.quote(s, safe='|:')}")

    url = base + "?" + "&".join(parts)
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        if len(r.content) < 1000:
            raise RuntimeError(f"Static Maps response too small: {len(r.content)} bytes")
        logger.info("Neighborhood map: fetched from Maps Static API (%d bytes)",
                    len(r.content))
        return r.content
    except Exception as exc:
        logger.error("Neighborhood Static API error: %s — OSM fallback", exc)
        return _stitch_osm_tiles(lat, lon, zoom=14, grid=3)


# ═══════════════════════════════════════════════════════════════════════════
# FIGURE 3.3 — FEMA FLOOD MAP (preserved verbatim from prior implementation)
# ═══════════════════════════════════════════════════════════════════════════

def build_fema_map(deal):
    lat, lon = deal.address.latitude, deal.address.longitude
    if not _lat_lon_valid(lat, lon):
        logger.info("FEMA map skipped — no geocoordinates")
        return None

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("Pillow not installed — cannot build FEMA map")
        return None

    logger.info("Building FEMA flood map — lat=%.5f lon=%.5f", lat, lon)
    base_png = _stitch_tiles(lat, lon, zoom=14, tiles_wide=3, tiles_tall=3)
    if not base_png:
        logger.warning("FEMA map — OSM base tiles failed")
        return None

    base_img = Image.open(io.BytesIO(base_png)).convert("RGBA")

    # Calculate WMS bounding box
    zoom = 14
    cx, cy = _deg_to_tile(lat, lon, zoom)
    half = 1

    def tile_to_lon(x, z):
        return x / (2 ** z) * 360.0 - 180.0

    def tile_to_lat(y, z):
        n = math.pi - 2.0 * math.pi * y / (2 ** z)
        return math.degrees(math.atan(math.sinh(n)))

    west  = tile_to_lon(cx - half, zoom)
    east  = tile_to_lon(cx + half + 1, zoom)
    north = tile_to_lat(cy - half, zoom)
    south = tile_to_lat(cy + half + 1, zoom)

    # FEMA moved the public NFHL endpoint. The legacy WMS path
    # (hazards.fema.gov/gis/nfhl/...) returns 404 in 2026. The current
    # working endpoint is the ArcGIS REST /export, which accepts a
    # layers=show:28 (Flood Hazard Zones) parameter and returns a PNG
    # directly. Use that and fall back to the group layer if needed.
    rest_base = (
        "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/"
        "MapServer/export?"
        f"bbox={west},{south},{east},{north}&bboxSR=4326"
        f"&size={MAP_WIDTH},{MAP_HEIGHT}"
        "&format=png&transparent=true&f=image"
    )
    overlay_data = None
    for layer_spec in ("show:28", "show:14,28", ""):
        candidate = rest_base + (f"&layers={layer_spec}" if layer_spec else "")
        overlay_data = _fetch_url(candidate)
        if overlay_data and len(overlay_data) > 500:
            logger.info("FEMA overlay fetched via layers=%s (%d bytes)",
                        layer_spec or "default", len(overlay_data))
            break
        logger.info("FEMA overlay attempt layers=%s returned %d bytes",
                    layer_spec or "default",
                    len(overlay_data) if overlay_data else 0)
    if overlay_data and len(overlay_data) > 500:
        try:
            overlay_img = Image.open(io.BytesIO(overlay_data)).convert("RGBA")
            overlay_img = overlay_img.resize((MAP_WIDTH, MAP_HEIGHT), Image.LANCZOS)
            pixels = overlay_img.load()
            # Keep only colored flood-zone polygons; drop the near-white
            # background so the OSM base shows through. Preserve the service's
            # native alpha on the zones themselves.
            for y_px in range(overlay_img.height):
                for x_px in range(overlay_img.width):
                    r, g, b, a = pixels[x_px, y_px]
                    if r > 248 and g > 248 and b > 248:
                        pixels[x_px, y_px] = (r, g, b, 0)
            base_img = Image.alpha_composite(base_img, overlay_img)
            logger.info("FEMA NFHL overlay applied (layer 28)")
        except Exception as exc:
            logger.warning("FEMA overlay compositing failed: %s", exc)
    else:
        logger.warning("FEMA WMS overlay unavailable — returning OSM base only")

    # Property pin + flood zone label
    draw = ImageDraw.Draw(base_img)
    cx_px, cy_px = MAP_WIDTH // 2, MAP_HEIGHT // 2
    r = 8
    draw.ellipse((cx_px-r, cy_px-r, cx_px+r, cy_px+r), fill="#CC2200", outline="#FFFFFF", width=2)
    draw.ellipse((cx_px-2, cy_px-2, cx_px+2, cy_px+2), fill="#FFFFFF")

    flood_zone = getattr(deal.market_data, "fema_flood_zone", None)
    if flood_zone:
        label = f"Zone {flood_zone}"
        draw.rectangle((8, MAP_HEIGHT-30, 8+len(label)*8+12, MAP_HEIGHT-8), fill="#1A3A5C")
        draw.text((14, MAP_HEIGHT-26), label, fill="#FFFFFF")

    buf = io.BytesIO()
    base_img.convert("RGB").save(buf, format="PNG", optimize=True)
    result = buf.getvalue()
    logger.info("FEMA flood map built — %d bytes", len(result))
    return result


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

class MapImages:
    """Container for all map and street view images used in the report.
    Every field is Optional[bytes] — None means unavailable."""
    def __init__(
        self,
        aerial:           Optional[bytes] = None,
        neighborhood:     Optional[bytes] = None,
        fema:             Optional[bytes] = None,
        street_view:      Optional[bytes] = None,
        street_view_alt1: Optional[bytes] = None,
        street_view_alt2: Optional[bytes] = None,
    ):
        self.aerial           = aerial
        self.neighborhood     = neighborhood
        self.fema             = fema
        self.street_view      = street_view
        self.street_view_alt1 = street_view_alt1
        self.street_view_alt2 = street_view_alt2

    @property
    def any_available(self) -> bool:
        return any([self.aerial, self.neighborhood, self.fema,
                    self.street_view, self.street_view_alt1, self.street_view_alt2])


def build_all_maps(deal) -> MapImages:
    """Build all map and street view images for the deal.
    Each image type is attempted independently — one failure does not block
    the others. Returns a MapImages container."""
    logger.info("map_builder: starting for %s", deal.address.full_address)
    logger.info("Maps API key present: %s", bool(GOOGLE_MAPS_API_KEY))
    logger.info("map_builder: coordinates lat=%s lon=%s",
                deal.address.latitude, deal.address.longitude)

    if not GOOGLE_MAPS_API_KEY:
        logger.warning("GOOGLE_MAPS_API_KEY not set — Google images "
                       "will fall back to OSM")

    aerial = neighborhood = fema = None
    sv_primary = sv_alt1 = sv_alt2 = None

    try:
        aerial = build_aerial_map(deal)
    except Exception as exc:
        logger.error("Aerial map error: %s", exc)

    try:
        neighborhood = build_neighborhood_map(deal)
    except Exception as exc:
        logger.error("Neighborhood map error: %s", exc)

    try:
        fema = build_fema_map(deal)
    except Exception as exc:
        logger.error("FEMA map error: %s", exc)

    try:
        sv_primary, sv_alt1, sv_alt2 = build_street_view(deal)
    except Exception as exc:
        logger.error("Street View error: %s", exc)

    map_count = sum(1 for m in [aerial, neighborhood, fema] if m)
    sv_count  = sum(1 for m in [sv_primary, sv_alt1, sv_alt2] if m)
    logger.info("map_builder: %d/3 maps, %d/3 street views generated",
                map_count, sv_count)

    return MapImages(
        aerial=aerial,
        neighborhood=neighborhood,
        fema=fema,
        street_view=sv_primary,
        street_view_alt1=sv_alt1,
        street_view_alt2=sv_alt2,
    )
