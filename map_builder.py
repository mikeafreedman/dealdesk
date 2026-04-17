"""
map_builder.py — DealDesk Map Image Generator
===============================================
Generates three PNG map images for the PDF report:
    Figure 3.1 — Aerial Location Map       (OpenStreetMap tiles, zoom 13)
    Figure 3.2 — Neighborhood Context Map  (Google Maps Static API, zoom 14)
    Figure 3.3 — FEMA Flood Map            (FEMA NFHL WMS overlay)

Each function returns PNG bytes or None on failure.
The pipeline continues cleanly if any map fails — placeholders remain in report.

Called by word_builder.py after geocoding is complete (lat/lon on deal.address).
"""

from __future__ import annotations

import io
import logging
import math
import urllib.request
import urllib.parse
from typing import Optional, Tuple

from config import GOOGLE_MAPS_API_KEY
from models.models import DealData

logger = logging.getLogger(__name__)

# ── Image dimensions ──────────────────────────────────────────────────────
MAP_WIDTH  = 600
MAP_HEIGHT = 400

# ── OpenStreetMap tile settings ───────────────────────────────────────────
OSM_TILE_SERVER = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_USER_AGENT  = "DealDesk-CRE-Underwriting/1.0 (contact@freedman-properties.com)"


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _lat_lon_valid(lat, lon):
    if lat is None or lon is None:
        return False
    return 24.0 <= lat <= 50.0 and -125.0 <= lon <= -66.0


def _deg_to_tile(lat, lon, zoom):
    lat_r = math.radians(lat)
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def _fetch_url(url, headers=None):
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            data = resp.read()
            logger.debug("HTTP %d — %d bytes from %s", status, len(data), url[:80])
            return data
    except Exception as exc:
        logger.warning("HTTP fetch failed for %s: %s", url[:80], exc)
        return None


def _stitch_tiles(lat, lon, zoom, tiles_wide=3, tiles_tall=3):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("Pillow not installed — cannot stitch OSM tiles")
        return None

    cx, cy = _deg_to_tile(lat, lon, zoom)
    half_w = tiles_wide  // 2
    half_h = tiles_tall  // 2
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
    left  = (total_w - MAP_WIDTH)  // 2
    top   = (total_h - MAP_HEIGHT) // 2
    canvas = canvas.crop((left, top, left + MAP_WIDTH, top + MAP_HEIGHT))

    # Red property pin
    draw = ImageDraw.Draw(canvas)
    cx_px, cy_px = MAP_WIDTH // 2, MAP_HEIGHT // 2
    r = 8
    draw.ellipse((cx_px-r, cy_px-r, cx_px+r, cy_px+r), fill="#CC2200", outline="#FFFFFF", width=2)
    draw.ellipse((cx_px-2, cy_px-2, cx_px+2, cy_px+2), fill="#FFFFFF")

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════════════
# FIGURE 3.1 — AERIAL LOCATION MAP
# ═══════════════════════════════════════════════════════════════════════════

def build_aerial_map(deal):
    lat, lon = deal.address.latitude, deal.address.longitude
    if not _lat_lon_valid(lat, lon):
        logger.info("Aerial map skipped — no geocoordinates")
        return None
    logger.info("Building aerial map — lat=%.5f lon=%.5f zoom=13", lat, lon)
    png = _stitch_tiles(lat, lon, zoom=13, tiles_wide=3, tiles_tall=3)
    if png:
        logger.info("Aerial map built — %d bytes", len(png))
    return png


# ═══════════════════════════════════════════════════════════════════════════
# FIGURE 3.2 — NEIGHBORHOOD CONTEXT MAP
# ═══════════════════════════════════════════════════════════════════════════

def build_neighborhood_map(deal):
    lat, lon = deal.address.latitude, deal.address.longitude
    if not _lat_lon_valid(lat, lon):
        logger.info("Neighborhood map skipped — no geocoordinates")
        return None

    if not GOOGLE_MAPS_API_KEY:
        logger.warning("No Google Maps API key — falling back to OSM")
        return _stitch_tiles(lat, lon, zoom=14, tiles_wide=3, tiles_tall=3)

    base = "https://maps.googleapis.com/maps/api/staticmap?"
    parts = [
        f"center={lat},{lon}",
        f"zoom=14",
        f"size={MAP_WIDTH}x{MAP_HEIGHT}",
        "maptype=roadmap",
        "scale=1",
        f"markers=color:red|label:P|{lat},{lon}",
        "style=feature:poi|visibility:simplified",
        "style=feature:transit|visibility:simplified",
        f"key={GOOGLE_MAPS_API_KEY}",
    ]
    url = base + "&".join(parts)

    logger.info("Google Maps Static API URL: %s",
                url.replace(GOOGLE_MAPS_API_KEY, "KEY_REDACTED"))
    data = _fetch_url(url)
    logger.info("Google Maps Static API response: %d bytes, non-empty=%s",
                len(data) if data else 0, bool(data and len(data) > 1000))
    if data and len(data) > 1000:
        logger.info("Neighborhood map built — %d bytes", len(data))
        return data
    logger.warning("Google Maps Static API failed (got %d bytes) — falling back to OSM",
                   len(data) if data else 0)
    return _stitch_tiles(lat, lon, zoom=14, tiles_wide=3, tiles_tall=3)


# ═══════════════════════════════════════════════════════════════════════════
# FIGURE 3.3 — FEMA FLOOD MAP
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

    wms_url = (
        "https://hazards.fema.gov/gis/nfhl/services/public/NFHL/MapServer/WMSServer?"
        f"SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=NFHL&STYLES=&CRS=EPSG:4326"
        f"&BBOX={south},{west},{north},{east}&WIDTH={MAP_WIDTH}&HEIGHT={MAP_HEIGHT}"
        f"&FORMAT=image/png&TRANSPARENT=TRUE"
    )

    overlay_data = _fetch_url(wms_url)
    if overlay_data and len(overlay_data) > 500:
        try:
            overlay_img = Image.open(io.BytesIO(overlay_data)).convert("RGBA")
            overlay_img = overlay_img.resize((MAP_WIDTH, MAP_HEIGHT), Image.LANCZOS)
            pixels = overlay_img.load()
            for y_px in range(overlay_img.height):
                for x_px in range(overlay_img.width):
                    r, g, b, a = pixels[x_px, y_px]
                    pixels[x_px, y_px] = (r, g, b, 0) if (r > 240 and g > 240 and b > 240) else (r, g, b, min(180, a))
            base_img = Image.alpha_composite(base_img, overlay_img)
            logger.info("FEMA NFHL overlay applied")
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
    """Container for the three map PNG byte strings."""
    def __init__(self, aerial=None, neighborhood=None, fema=None):
        self.aerial       = aerial
        self.neighborhood = neighborhood
        self.fema         = fema

    @property
    def any_available(self):
        return any([self.aerial, self.neighborhood, self.fema])


def build_all_maps(deal):
    """
    Build all three map images. Each is attempted independently.
    Returns MapImages container with PNG bytes or None for each map.
    """
    logger.info("map_builder: starting for %s", deal.address.full_address)
    logger.info("Maps API key present: %s", bool(GOOGLE_MAPS_API_KEY))
    logger.info("map_builder: coordinates lat=%s lon=%s",
                deal.address.latitude, deal.address.longitude)

    if not GOOGLE_MAPS_API_KEY:
        logger.warning("GOOGLE_MAPS_API_KEY not set — Google map images "
                        "will fall back to OSM")

    aerial = neighborhood = fema = None

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

    available = sum(1 for m in [aerial, neighborhood, fema] if m)
    logger.info("map_builder: %d/3 maps generated", available)
    return MapImages(aerial=aerial, neighborhood=neighborhood, fema=fema)
