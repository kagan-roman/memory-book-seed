#!/usr/bin/env python3
"""A5 map-sheet SVG engine — faithful port of booklets/build_booklet.py.

Renders battle-path map sheets for each chapter. The engine math
(projection with cos(lat), Catmull-Rom → Bezier, arc paths, shorten,
arrow markers, timeline layout, fact key, headless-Chrome screenshot +
crop) is byte-faithful to the original. ALL family-specific data has been
removed: slide configs now load from per-chapter JSON files
(`book/chapters/<slug>/maps.json`), and the footer/title come from
`book.config.yml`.

Military-map notation (RULES.md §2 — engine, do not "improve"):
  RED ARROW          — combat advance inside the unit
  RED DASHED ARC     — unit redeployment (no combat)
  GREY DASHED        — personal/administrative travel
  GREY THIN + cross  — wounded evacuation
  GREEN DASHED       — post-death continuation of his unit
  RED DASHED SORTIE  — aviation sortie direction (long arc)

Front line is NEVER drawn (RULES.md §2): no reliable vector data exists.

Usage:
  python3 build/build_maps.py            # render every chapters/*/maps.json
  python3 build/build_maps.py --slug ivan-petrov   # only that chapter
"""
import argparse
import glob
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

# Paths are derived, never hardcoded to a user home.
ROOT = Path(__file__).resolve().parent.parent
GEO = ROOT / "assets" / "geo"
OUT = ROOT / "build" / ".out" / "maps"
CHAPTERS = ROOT / "book" / "chapters"
CONFIG = ROOT / "book.config.yml"

W, H = 874, 1240   # A5 portrait (210/148 ≈ 874/1240): map fits the leaf upright

# Vertical layout: header → map → numbered key → war timeline
MAP_X, MAP_Y, MAP_W, MAP_H = 30, 104, 814, 540
# Numbered-point key — wide strip under the map (height by content)
KEY_X, KEY_Y, KEY_W = 30, 664, 814

# Colors
RED_COMBAT = "#b5402f"
TAN_PERSONAL = "#6b6357"
TAN_HOME = "#7a5a32"
DEATH = "#b23a2b"
POST_GREEN = "#5f7a45"
WOUND = "#c07a2e"


# ─── Minimal YAML reader ─────────────────────────────────────────────────────
# The repo cannot assume PyYAML. book.config.yml is a flat, nested-by-indent
# mapping with scalar values (no flow-mapping objects, no anchors, no
# multi-line scalars). This reader handles exactly that subset: nested
# `key:` blocks by two-space indent, `key: value` scalars, and `[a, b]`
# inline lists. It is NOT a general YAML parser — limits noted here.
def _yaml_scalar(v):
    v = v.strip()
    if not v:
        return ""
    if (v[0] == v[-1]) and v[0] in ("'", '"') and len(v) >= 2:
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_yaml_scalar(x) for x in inner.split(",")]
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    return v


def _load_yaml_config(path):
    """Parse the flat/nested book.config.yml into a dict. Falls back to
    PyYAML if available (some users have it); otherwise the hand reader.
    Comments (`#`) and blank lines are skipped."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        import yaml  # optional, not a hard dependency
        data = yaml.safe_load(text)
        return data or {}
    except Exception:
        pass
    root = {}
    stack = [(-1, root)]
    for raw in text.split("\n"):
        line = raw.split("#", 1)[0].rstrip() if "#" not in raw[:1] else raw
        # keep '#' that is inside quotes; cheap heuristic: only strip a
        # trailing comment when not inside an obvious quoted value
        if "#" in raw:
            # strip comment only if the '#' is not within quotes
            in_s = False
            cut = None
            q = ""
            for i, ch in enumerate(raw):
                if ch in ("'", '"'):
                    if not in_s:
                        in_s, q = True, ch
                    elif q == ch:
                        in_s = False
                elif ch == "#" and not in_s:
                    cut = i
                    break
            line = (raw[:cut] if cut is not None else raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        if ":" not in body:
            continue
        key, _, val = body.partition(":")
        key = key.strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if val.strip() == "":
            node = {}
            parent[key] = node
            stack.append((indent, node))
        else:
            parent[key] = _yaml_scalar(val)
    return root


_CFG = _load_yaml_config(CONFIG)


def _cfg_get(path, default=""):
    node = _CFG
    for p in path.split("."):
        if isinstance(node, dict) and p in node:
            node = node[p]
        else:
            return default
    return node if node not in (None, "") else default


# Footer line of the timeline strip — replaces the original hardcoded
# "КАГАНОВЫ ИЗ СМИРНОВО · ВЕЛИКАЯ ОТЕЧЕСТВЕННАЯ" with the config value.
def _footer_line():
    footer = _cfg_get("book.footer", "")
    if footer:
        return footer
    title = _cfg_get("book.title", "")
    subtitle = _cfg_get("book.subtitle", "")
    if title and subtitle:
        return f"{title} · {subtitle}"
    return title or subtitle or ""


BOOK_FOOTER = _footer_line()


# ─── Load shared geo data ────────────────────────────────────────────────────
def _geo(name):
    p = GEO / name
    if not p.exists():
        print(f"  ! geo missing: {p} (run build/clip_geo.py first)",
              file=sys.stderr)
        return {"features": []}
    return json.loads(p.read_text())


countries = _geo("clipped_countries.geojson")
rivers = _geo("clipped_rivers.geojson")
lakes = _geo("clipped_lakes.geojson")
cities_ne = _geo("clipped_cities.geojson")


# ─── Catmull-Rom path (for front lines) ─────────────────────────────────────
def catmull_to_bezier(pts, tension=0.5):
    if len(pts) < 2:
        return ""
    if len(pts) == 2:
        return f"M{pts[0][0]:.1f},{pts[0][1]:.1f} L{pts[1][0]:.1f},{pts[1][1]:.1f}"
    d = [f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"]
    n = len(pts)
    for i in range(n - 1):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1 = pts[i]
        p2 = pts[i + 1]
        p3 = pts[i + 2] if i + 2 < n else p2
        c1x = p1[0] + (p2[0] - p0[0]) * tension / 3
        c1y = p1[1] + (p2[1] - p0[1]) * tension / 3
        c2x = p2[0] - (p3[0] - p1[0]) * tension / 3
        c2y = p2[1] - (p3[1] - p1[1]) * tension / 3
        d.append(f"C{c1x:.1f},{c1y:.1f} {c2x:.1f},{c2y:.1f} {p2[0]:.1f},{p2[1]:.1f}")
    return " ".join(d)


def arc_path(from_pt, to_pt, curve=0.15):
    dx = to_pt[0] - from_pt[0]
    dy = to_pt[1] - from_pt[1]
    mx = (from_pt[0] + to_pt[0]) / 2 + (-dy) * curve
    my = (from_pt[1] + to_pt[1]) / 2 + dx * curve
    return f"M{from_pt[0]:.1f},{from_pt[1]:.1f} Q{mx:.1f},{my:.1f} {to_pt[0]:.1f},{to_pt[1]:.1f}"


def shorten(from_pt, to_pt, pad_start=10, pad_end=12):
    dx = to_pt[0] - from_pt[0]
    dy = to_pt[1] - from_pt[1]
    L = math.hypot(dx, dy) or 1
    return (
        (from_pt[0] + dx / L * pad_start, from_pt[1] + dy / L * pad_start),
        (to_pt[0] - dx / L * pad_end, to_pt[1] - dy / L * pad_end),
    )


# Линия фронта НЕ рисуется — см. RULES.md §2.
# Надёжных векторных данных нет, попытка «на глаз» искажает картину.


# ─── Slide-config loader (replaces the hardcoded SLIDE_CONFIGS literal) ──────
# Each book/chapters/<slug>/maps.json is a JSON list of slide-config dicts
# using the SAME keys the engine consumes. Two normalisations are applied so
# the cartographer agent can use the documented friendly schema:
#   - "bbox": [lon_min, lon_max, lat_min, lat_max]  → tuple
#   - segments[].arrow / from_idx / to_idx / dates  → type / from / to / label
#     (the engine's native keys type/from/to/label are also accepted as-is)
#   - timeline / post / overview roads carrying ISO date strings
#     ("YYYY-MM-DD") → datetime.date (the engine does date math).
_ARROW_TO_TYPE = {
    "combat": "combat",
    "redeploy": "redeploy",
    "personal": "personal",
    "post": "post",
    "evac": "evac",
    "possible": "possible",
    "sortie": "sortie",
    "zone": "zone",
}
_COLOR_NAMES = {
    "RED_COMBAT": RED_COMBAT, "TAN_PERSONAL": TAN_PERSONAL,
    "TAN_HOME": TAN_HOME, "DEATH": DEATH, "POST_GREEN": POST_GREEN,
    "WOUND": WOUND,
}


def _to_date(v):
    """Accept 'YYYY-MM-DD', [Y,M,D], or pass through date."""
    if isinstance(v, date):
        return v
    if isinstance(v, (list, tuple)) and len(v) == 3:
        return date(int(v[0]), int(v[1]), int(v[2]))
    if isinstance(v, str) and len(v) >= 8 and v[4] == "-":
        y, m, d = v.split("-")[:3]
        return date(int(y), int(m), int(d))
    return v


def _norm_color(v):
    return _COLOR_NAMES.get(v, v) if isinstance(v, str) else v


def _norm_timeline(events):
    out = []
    for e in events or []:
        e = dict(e)
        if "d" in e:
            e["d"] = _to_date(e["d"])
        if "start" in e:
            e["start"] = _to_date(e["start"])
        if "end" in e:
            e["end"] = _to_date(e["end"])
        if "color" in e:
            e["color"] = _norm_color(e["color"])
        out.append(e)
    return out


def _norm_segment(seg):
    seg = dict(seg)
    if "type" not in seg and "arrow" in seg:
        seg["type"] = _ARROW_TO_TYPE.get(seg.pop("arrow"), "combat")
    if "from" not in seg and "from_idx" in seg:
        seg["from"] = seg.pop("from_idx")
    if "to" not in seg and "to_idx" in seg:
        seg["to"] = seg.pop("to_idx")
    if "label" not in seg and "dates" in seg:
        seg["label"] = seg.pop("dates")
    seg.setdefault("label", "")
    return seg


def _norm_config(cfg):
    cfg = dict(cfg)
    if "bbox" in cfg and isinstance(cfg["bbox"], (list, tuple)):
        cfg["bbox"] = tuple(cfg["bbox"])
    if "segments" in cfg:
        cfg["segments"] = [_norm_segment(s) for s in cfg["segments"]]
    # panel_sections: [title, items, color] — color may be a name
    if "panel_sections" in cfg:
        ps = []
        for sec in cfg["panel_sections"]:
            if isinstance(sec, (list, tuple)) and len(sec) == 3:
                ps.append((sec[0], sec[1], _norm_color(sec[2])))
            else:
                ps.append(sec)
        cfg["panel_sections"] = ps
    if "timeline_events" in cfg:
        cfg["timeline_events"] = _norm_timeline(cfg["timeline_events"])
    # overview roads / zones pass through unchanged (geometry only)
    return cfg


def load_slide_configs(slug_filter=None):
    """Glob book/chapters/*/maps.json and return a flat ordered list of
    normalised slide-config dicts. `slug_filter` restricts to one chapter."""
    configs = []
    pattern = str(CHAPTERS / "*" / "maps.json")
    for jp in sorted(glob.glob(pattern)):
        slug = Path(jp).parent.name
        if slug_filter and slug != slug_filter:
            continue
        try:
            raw = json.loads(Path(jp).read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ! {jp}: bad JSON ({e})", file=sys.stderr)
            continue
        if isinstance(raw, dict):
            raw = [raw]
        for item in raw:
            configs.append(_norm_config(item))
    return configs


# ─── Build one slide ────────────────────────────────────────────────────────
def build_slide(cfg):
    lon_min, lon_max, lat_min, lat_max = cfg["bbox"]
    lat_mid = (lat_min + lat_max) / 2
    cos_mid = math.cos(math.radians(lat_mid))
    lon_span_corr = (lon_max - lon_min) * cos_mid
    lat_span = lat_max - lat_min
    scale = min(MAP_W / lon_span_corr, MAP_H / lat_span)
    draw_w = lon_span_corr * scale
    draw_h = lat_span * scale
    off_x = MAP_X + (MAP_W - draw_w) / 2
    off_y = MAP_Y + (MAP_H - draw_h) / 2

    def project(lon, lat):
        return (
            off_x + (lon - lon_min) * cos_mid * scale,
            off_y + (lat_max - lat) * scale,
        )

    def path_for_ring(ring):
        pts = [project(lon, lat) for lon, lat in ring]
        return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + "Z"

    def path_for_line(coords):
        pts = [project(lon, lat) for lon, lat in coords]
        return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)

    def feature_path(feat):
        geom = feat["geometry"]
        t = geom["type"]
        c = geom["coordinates"]
        if t == "Polygon":
            return " ".join(path_for_ring(r) for r in c)
        if t == "MultiPolygon":
            return " ".join(" ".join(path_for_ring(r) for r in poly) for poly in c)
        if t == "LineString":
            return path_for_line(c)
        if t == "MultiLineString":
            return " ".join(path_for_line(line) for line in c)
        return ""

    def in_bbox(lon, lat):
        return lon_min <= lon <= lon_max and lat_min <= lat <= lat_max

    parts = []
    parts.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="url(#bgGrad)"/>')

    # Map frame
    parts.append(
        f'<rect x="{MAP_X}" y="{MAP_Y}" width="{MAP_W}" height="{MAP_H}" '
        f'fill="#e9eef0" stroke="rgba(122,90,50,0.30)" stroke-width="1"/>'
    )

    parts.append(
        f'<defs><clipPath id="mapClip">'
        f'<rect x="{MAP_X}" y="{MAP_Y}" width="{MAP_W}" height="{MAP_H}"/>'
        f'</clipPath>'
        f'<marker id="aCombat" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="6.5" markerHeight="6.5" orient="auto">'
        f'<path d="M0,0 L10,5 L0,10 Z" fill="{RED_COMBAT}"/></marker>'
        f'<marker id="aCombatSm" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="5" markerHeight="5" orient="auto">'
        f'<path d="M0,0 L10,5 L0,10 Z" fill="{RED_COMBAT}"/></marker>'
        f'<marker id="aRedeploy" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="5" markerHeight="5" orient="auto">'
        f'<path d="M0,0 L10,5 L0,10 L3,5 Z" fill="{RED_COMBAT}" opacity="0.85"/></marker>'
        f'<marker id="aSortie" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="6" markerHeight="6" orient="auto">'
        f'<path d="M0,0 L10,5 L0,10 Z" fill="{RED_COMBAT}" opacity="0.9"/></marker>'
        f'<marker id="aPost" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="5" markerHeight="5" orient="auto">'
        f'<path d="M0,0 L10,5 L0,10 Z" fill="{POST_GREEN}"/></marker>'
        # Hatch pattern для заштрихованных зон вероятных событий
        # (RULES.md §2: «заштрихованные зоны без жирных границ»).
        f'<pattern id="zoneHatch" patternUnits="userSpaceOnUse" '
        f'width="6" height="6" patternTransform="rotate(45)">'
        f'<line x1="0" y1="0" x2="0" y2="6" stroke="{RED_COMBAT}" '
        f'stroke-width="1" opacity="0.35"/>'
        f'</pattern>'
        f'</defs>'
    )

    parts.append('<g clip-path="url(#mapClip)">')

    # Base layers
    for f in countries["features"]:
        d = feature_path(f)
        if d:
            parts.append(f'<path d="{d}" fill="#efe7d5" stroke="#cdbfa3" stroke-width="0.7"/>')
    for f in lakes["features"]:
        d = feature_path(f)
        if d:
            parts.append(f'<path d="{d}" fill="#e9eef0" stroke="#bcccd0" stroke-width="0.5"/>')
    for f in rivers["features"]:
        d = feature_path(f)
        if d:
            parts.append(
                f'<path d="{d}" fill="none" stroke="#9bb2c0" stroke-width="0.9" opacity="0.75"/>'
            )

    # Линия фронта НЕ рисуется (RULES.md §2).

    # Заштрихованные зоны вероятных секторов фронта.
    # Используются в коротких главах (БВ декабря 1941), когда точное место
    # неизвестно и нужно дать читателю общий географический сектор без
    # обмана точкой «погиб». Без жирных границ — только лёгкий пунктир.
    for z in cfg.get("zones", []):
        ring = [project(lon, lat) for lon, lat in z["polygon"]]
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in ring) + "Z"
        parts.append(
            f'<path d="{d}" fill="url(#zoneHatch)" '
            f'stroke="{RED_COMBAT}" stroke-width="0.8" stroke-dasharray="3 3" '
            f'opacity="0.55"/>'
        )
        if z.get("label") and z.get("label_pos"):
            lx, ly = project(*z["label_pos"])
            parts.append(
                f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="10.5" '
                f'fill="#9a4632" text-anchor="middle" font-weight="600" '
                f'letter-spacing="0.04em" '
                f'paint-order="stroke" stroke="#fffdf8" stroke-width="3.2" stroke-linejoin="round" '
                f'font-family="-apple-system, Helvetica, Arial">{z["label"]}</text>'
            )

    # Precompute waypoint pixel positions for label suppression
    wp_pts_for_filter = [project(w["lon"], w["lat"]) for w in cfg["waypoints"]]

    def near_waypoint(x, y, threshold=22):
        return any(math.hypot(x - wx, y - wy) < threshold for wx, wy in wp_pts_for_filter)

    # Cities (Natural Earth + extras) — filtered to bbox; suppress labels near waypoints
    for c in cities_ne["features"]:
        p = c["properties"]
        lon, lat = c["geometry"]["coordinates"]
        if not in_bbox(lon, lat):
            continue
        x, y = project(lon, lat)
        sr = p.get("scalerank", 5)
        r = 3 if sr <= 1 else 2.4 if sr <= 3 else 1.8
        name = p.get("name_ru") or p.get("name") or ""
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="#8a8478"/>')
        if not near_waypoint(x, y):
            parts.append(
                f'<text x="{x + 5:.1f}" y="{y + 3.5:.1f}" font-size="11" fill="#5a5448" '
                f'paint-order="stroke" stroke="#fffdf8" stroke-width="3.2" stroke-linejoin="round" '
                f'font-family="-apple-system, Helvetica, Arial">{name}</text>'
            )
    for c in cfg.get("extra_cities", []):
        if not in_bbox(c["lon"], c["lat"]):
            continue
        x, y = project(c["lon"], c["lat"])
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.4" fill="#8a8478"/>')
        # extra_cities — кураторский список ключевых городов (Берлин, Варшава …):
        # имя показываем ВСЕГДА. Если город совпал с waypoint — раньше подпись
        # просто подавлялась и ключевой город исчезал с карты (а соседний
        # мелкий — оставался). Теперь при совпадении уводим подпись под
        # номерной диск (по центру, ниже), чтобы не легла на цифру.
        if near_waypoint(x, y):
            tx, ty, anc = x, y + 25, "middle"
        else:
            tx, ty, anc = x + 5, y + 3.5, "start"
        parts.append(
            f'<text x="{tx:.1f}" y="{ty:.1f}" font-size="11" fill="#6b6357" '
            f'text-anchor="{anc}" paint-order="stroke" stroke="#fffdf8" stroke-width="3.2" '
            f'stroke-linejoin="round" font-family="-apple-system, Helvetica, Arial">{c["name"]}</text>'
        )

    # Movement segments
    waypoints = cfg["waypoints"]
    wp_pts = [project(w["lon"], w["lat"]) for w in waypoints]
    for seg in cfg["segments"]:
        a = wp_pts[seg["from"]]
        b = wp_pts[seg["to"]]
        a2, b2 = shorten(a, b, pad_start=14, pad_end=16)
        if seg["type"] == "combat":
            d = arc_path(a2, b2, curve=0.06)
            parts.append(
                f'<path d="{d}" fill="none" stroke="{RED_COMBAT}" stroke-width="3.0" '
                f'stroke-linejoin="round" stroke-linecap="butt" '
                f'marker-end="url(#aCombat)"/>'
            )
        elif seg["type"] == "redeploy":
            d = arc_path(a2, b2, curve=0.18)
            parts.append(
                f'<path d="{d}" fill="none" stroke="{RED_COMBAT}" stroke-width="1.8" '
                f'stroke-dasharray="6 4" opacity="0.7" '
                f'marker-end="url(#aRedeploy)"/>'
            )
        elif seg["type"] == "evac":
            d = arc_path(a2, b2, curve=0.18)
            parts.append(
                f'<path d="{d}" fill="none" stroke="{TAN_PERSONAL}" stroke-width="1.6" '
                f'stroke-dasharray="3 3" opacity="0.85"/>'
            )
            mx = (a[0] + b[0]) / 2 + 6
            my = (a[1] + b[1]) / 2
            parts.append(
                f'<text x="{mx:.1f}" y="{my:.1f}" font-size="10" fill="#6b6357" '
                f'paint-order="stroke" stroke="#fffdf8" stroke-width="3.2" stroke-linejoin="round" '
                f'opacity="0.9" font-family="-apple-system, Helvetica, Arial">эвак. ✚</text>'
            )
        elif seg["type"] == "personal":
            # Личное/админ перемещение — серая пунктирная дуга, без стрелки
            d = arc_path(a2, b2, curve=0.10)
            parts.append(
                f'<path d="{d}" fill="none" stroke="{TAN_PERSONAL}" stroke-width="1.4" '
                f'stroke-dasharray="3 3" opacity="0.6"/>'
            )
        elif seg["type"] == "possible":
            # Возможное направление отправки (часть и место не установлены).
            # Длинная тонкая красная пунктирная дуга с небольшим наконечником
            # и пониженной непрозрачностью — отличается от "redeploy"
            # (подтверждённая передислокация) и от "combat".
            curve = seg.get("curve", 0.14)
            d = arc_path(a2, b2, curve=curve)
            parts.append(
                f'<path d="{d}" fill="none" stroke="{RED_COMBAT}" stroke-width="1.4" '
                f'stroke-dasharray="4 4" opacity="0.45" '
                f'marker-end="url(#aRedeploy)"/>'
            )
        elif seg["type"] == "sortie":
            # Авиационный боевой вылет: длинная пунктирная дуга красного цвета
            # с наконечником у цели. Не «движение части», а «направление удара».
            # Кривая — небольшая дуга, чтобы две стрелки от одной точки
            # не сливались в одну линию.
            curve = seg.get("curve", 0.10)
            d = arc_path(a2, b2, curve=curve)
            parts.append(
                f'<path d="{d}" fill="none" stroke="{RED_COMBAT}" stroke-width="2.2" '
                f'stroke-dasharray="10 5" opacity="0.85" '
                f'marker-end="url(#aSortie)"/>'
            )

        # Segment date label — perpendicular offset from path so it doesn't sit on cities
        if seg.get("label") and seg["type"] in ("combat", "redeploy", "sortie", "possible"):
            seg_len = math.hypot(b[0] - a[0], b[1] - a[1])
            if seg_len > 40:
                dx, dy = b[0] - a[0], b[1] - a[1]
                # Perpendicular unit vector (rotated 90° CCW)
                px, py = -dy / seg_len, dx / seg_len
                # Bias the offset upward so labels prefer the upper side
                offset = 14 if py < 0 else -14
                mx = (a[0] + b[0]) / 2 + px * offset
                my = (a[1] + b[1]) / 2 + py * offset
                col = RED_COMBAT
                parts.append(
                    f'<text x="{mx:.1f}" y="{my:.1f}" font-size="10" fill="{col}" '
                    f'text-anchor="middle" opacity="0.95" font-weight="600" '
                    f'paint-order="stroke" stroke="#fffdf8" stroke-width="3.2" stroke-linejoin="round" '
                    f'font-family="-apple-system, Helvetica, Arial">{seg["label"]}</text>'
                )

    # Post-death continuation (green dashed)
    if cfg.get("post_waypoints"):
        post_pts = [project(w["lon"], w["lat"]) for w in cfg["post_waypoints"]]
        chain = [wp_pts[cfg["post_after"]]] + post_pts
        for i in range(len(chain) - 1):
            a, b = shorten(chain[i], chain[i + 1], pad_start=13, pad_end=12)
            d = arc_path(a, b, curve=0.10)
            parts.append(
                f'<path d="{d}" fill="none" stroke="{POST_GREEN}" stroke-width="1.8" '
                f'stroke-dasharray="5 3" opacity="0.85" marker-end="url(#aPost)"/>'
            )
        # Post-death waypoint markers
        for c, (x, y) in zip(cfg["post_waypoints"], post_pts):
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="none" stroke="{POST_GREEN}" '
                f'stroke-width="1.5"/>'
                f'<text x="{x:.1f}" y="{y - 10:.1f}" font-size="10" fill="{POST_GREEN}" '
                f'text-anchor="middle" font-weight="600" '
                f'paint-order="stroke" stroke="#fffdf8" stroke-width="3.2" stroke-linejoin="round" '
                f'font-family="-apple-system, Helvetica, Arial">{c["date"]}</text>'
            )

    # Waypoint markers
    for w, (x, y) in zip(waypoints, wp_pts):
        is_death = "†" in w["name"]
        fill = DEATH if is_death else "#7a5a32"
        stroke = "#7a1818" if is_death else "#5e451f"
        # Bigger markers since map is more zoomed
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="11" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
            f'<text x="{x:.1f}" y="{y + 4.5:.1f}" font-size="13" font-weight="700" '
            f'fill="#fffdf8" text-anchor="middle">{w["n"]}</text>'
        )

    parts.append("</g>")  # end mapClip

    # Заголовок карты теперь в шапке (строка охвата) — внутри карты только легенда
    # Legend
    legend_x = MAP_X + 12
    legend_y = MAP_Y + 20
    for i, (kind, text) in enumerate(cfg["legend"]):
        ly = legend_y + i * 16
        x1, x2 = legend_x, legend_x + 32
        if kind == "combat":
            parts.append(
                f'<line x1="{x1}" y1="{ly}" x2="{x2 - 4}" y2="{ly}" '
                f'stroke="{RED_COMBAT}" stroke-width="2.8" marker-end="url(#aCombatSm)"/>'
            )
        elif kind == "redeploy":
            parts.append(
                f'<line x1="{x1}" y1="{ly}" x2="{x2 - 4}" y2="{ly}" '
                f'stroke="{RED_COMBAT}" stroke-width="1.8" stroke-dasharray="4 3" opacity="0.7" '
                f'marker-end="url(#aRedeploy)"/>'
            )
        elif kind == "evac":
            parts.append(
                f'<line x1="{x1}" y1="{ly}" x2="{x2}" y2="{ly}" '
                f'stroke="{TAN_PERSONAL}" stroke-width="1.6" stroke-dasharray="3 3" opacity="0.85"/>'
            )
        elif kind == "post":
            parts.append(
                f'<line x1="{x1}" y1="{ly}" x2="{x2 - 4}" y2="{ly}" '
                f'stroke="{POST_GREEN}" stroke-width="1.8" stroke-dasharray="5 3" opacity="0.85" '
                f'marker-end="url(#aPost)"/>'
            )
        elif kind == "sortie":
            parts.append(
                f'<line x1="{x1}" y1="{ly}" x2="{x2 - 4}" y2="{ly}" '
                f'stroke="{RED_COMBAT}" stroke-width="2.2" stroke-dasharray="10 5" opacity="0.85" '
                f'marker-end="url(#aSortie)"/>'
            )
        elif kind == "zone":
            # Заштрихованный прямоугольник — образец заливки зон вероятных событий
            parts.append(
                f'<rect x="{x1}" y="{ly - 5}" width="{x2 - x1}" height="10" '
                f'fill="url(#zoneHatch)" stroke="{RED_COMBAT}" stroke-width="0.6" '
                f'stroke-dasharray="3 3" opacity="0.55"/>'
            )
        elif kind == "possible":
            parts.append(
                f'<line x1="{x1}" y1="{ly}" x2="{x2 - 4}" y2="{ly}" '
                f'stroke="{RED_COMBAT}" stroke-width="1.4" stroke-dasharray="4 4" '
                f'opacity="0.5" marker-end="url(#aRedeploy)"/>'
            )
        elif kind == "personal":
            parts.append(
                f'<line x1="{x1}" y1="{ly}" x2="{x2}" y2="{ly}" '
                f'stroke="{TAN_PERSONAL}" stroke-width="1.4" stroke-dasharray="3 3" '
                f'opacity="0.6"/>'
            )
        # «front» legend kind удалён — линия фронта не рисуется (RULES.md §2)
        parts.append(
            f'<text x="{x2 + 8}" y="{ly + 3.5}" font-size="10" fill="#6b6357" '
            f'font-family="-apple-system, Helvetica, Arial">{text}</text>'
        )

    # ─── Ключ нумерованных точек: широкой полосой под картой ────────────────
    # (бывшая правая панель. Убраны: блок «КОНТЕКСТ»/биография, кросс-ссылка
    #  «→ лист N», кредит источников — всё это есть в самой главе книги.)
    pad = 20
    row_h = 32
    key_h = 20
    for _t, _items, _c in cfg["panel_sections"]:
        _nc = 2 if len(_items) > 4 else 1
        key_h += 22 + math.ceil(len(_items) / _nc) * row_h + 16
    parts.append(
        f'<rect x="{KEY_X}" y="{KEY_Y}" width="{KEY_W}" height="{key_h}" '
        f'fill="rgba(122,90,50,0.05)" stroke="rgba(122,90,50,0.24)" stroke-width="1"/>'
    )
    ky = KEY_Y + 28
    for title, items, color in cfg["panel_sections"]:
        parts.append(
            f'<text x="{KEY_X + pad}" y="{ky}" font-size="11" fill="{color}" '
            f'letter-spacing="0.12em" font-weight="600" '
            f'font-family="-apple-system, Helvetica, Arial">{title}</text>'
        )
        ky += 22
        n_items = len(items)
        two_col = n_items > 4
        ncol = 2 if two_col else 1
        rows = math.ceil(n_items / ncol)
        gap = 28
        col_w = (KEY_W - pad * 2 - gap * (ncol - 1)) / ncol
        row_h = 32
        for idx, w in enumerate(items):
            col = idx // rows
            row = idx % rows
            ex = KEY_X + pad + col * (col_w + gap)
            ey = ky + row * row_h
            is_death = "†" in w.get("name", "")
            n = w.get("n", "●")
            circle_fill = DEATH if is_death else color
            circle_stroke = "#7a1818" if is_death else "#5e451f"
            text_color = "#9a2f23" if is_death else "#7a5a32"
            parts.append(
                f'<circle cx="{ex + 10:.1f}" cy="{ey:.1f}" r="10" fill="{circle_fill}" '
                f'stroke="{circle_stroke}" stroke-width="1.2"/>'
                f'<text x="{ex + 10:.1f}" y="{ey + 4:.1f}" font-size="12" font-weight="700" '
                f'fill="#fffdf8" text-anchor="middle">{n}</text>'
                f'<text x="{ex + 30:.1f}" y="{ey - 1:.1f}" font-size="13" fill="{text_color}" '
                f'font-weight="600" font-family="-apple-system, Helvetica, Arial">{w.get("name", "")}</text>'
                f'<text x="{ex + 30:.1f}" y="{ey + 15:.1f}" font-size="10.5" fill="#6b6357" '
                f'font-family="-apple-system, Helvetica, Arial">{w.get("date", "")} · {w.get("label", "")}</text>'
            )
        ky += rows * row_h + 16

    # ─── Шапка: имя + строка охвата карты ───────────────────────────────────
    # Даты/родитель/«лист N» убраны — это есть в открытии главы книги.
    parts.append(f'<rect x="0" y="0" width="{W}" height="92" fill="rgba(122,90,50,0.06)"/>')
    display_name = cfg.get("display_name", "")
    # "†" по умолчанию (погиб); пусто если ветеран вернулся
    name_mark = cfg.get("name_mark", "†")
    name_mark_tspan = (
        f' <tspan fill="#7a5a32" font-weight="300">{name_mark}</tspan>'
        if name_mark else ""
    )
    parts.append(
        f'<text x="30" y="46" font-size="25" fill="#1a1a1a" font-weight="800" '
        f'letter-spacing="-0.01em" '
        f'font-family="-apple-system, Helvetica, Arial">{display_name}{name_mark_tspan}</text>'
    )
    parts.append(
        f'<text x="30" y="74" font-size="12.5" fill="#7a5a32" letter-spacing="0.14em" '
        f'font-weight="600" font-family="-apple-system, Helvetica, Arial">{cfg["map_title"]}</text>'
    )
    parts.append(
        f'<line x1="30" y1="92" x2="{W - 30}" y2="92" stroke="rgba(122,90,50,0.32)" stroke-width="1"/>'
    )

    # ─── Timeline ───────────────────────────────────────────────────────────
    # Layout (top→bottom):
    #   - Title
    #   - Personal POINT labels (with subs above), connected to rail by leader lines
    #   - Personal SPAN bars — flush to the rail (touching from above)
    #   - RAIL
    #   - Year tick marks (short, below rail)
    #   - Year labels
    #   - Macro war labels (above boxes)
    #   - Macro war boxes (at the bottom)
    TL_Y = 1150          # рельса таймлайна; заголовок −70, годы +36 (запас до 1240)
    TL_X1, TL_X2 = 60, W - 60
    TL_W = TL_X2 - TL_X1
    T0 = date(1941, 6, 22)
    T1 = date(1945, 5, 9)
    TOTAL_DAYS = (T1 - T0).days

    def t_to_x(d):
        return TL_X1 + ((d - T0).days / TOTAL_DAYS) * TL_W

    # Title
    parts.append(
        f'<text x="{TL_X1}" y="{TL_Y - 70}" font-size="11" fill="#7a5a32" letter-spacing="0.18em" '
        f'font-weight="600" font-family="-apple-system, Helvetica, Arial">'
        f'ВЕЛИКАЯ ОТЕЧЕСТВЕННАЯ · 22.06.1941 — 09.05.1945</text>'
    )

    # Rail
    parts.append(
        f'<line x1="{TL_X1}" y1="{TL_Y}" x2="{TL_X2}" y2="{TL_Y}" '
        f'stroke="#7a5a32" stroke-width="1.6" opacity="0.85"/>'
    )

    # ── Macro war boxes flush to rail (from below) ──────────────────────────
    # Symmetric with personal spans above. Labels inside boxes.
    # Year ticks and labels move below the boxes.
    MACRO_BOX_Y = TL_Y           # flush to rail
    MACRO_BOX_H = 14
    MACRO_BOX_BOTTOM = TL_Y + MACRO_BOX_H
    YEAR_TICK_TOP = MACRO_BOX_BOTTOM + 4
    YEAR_TICK_BOTTOM = MACRO_BOX_BOTTOM + 10
    YEAR_LABEL_Y = MACRO_BOX_BOTTOM + 22

    macro = [
        (date(1941, 6, 22), date(1941, 12, 5), "Барбаросса", "#cdb8a8"),
        (date(1941, 12, 5), date(1942, 4, 20), "Москва", "#cdbfa0"),
        (date(1942, 7, 17), date(1943, 2, 2), "Сталинград", "#ccbd9c"),
        (date(1943, 7, 5), date(1943, 8, 23), "Курск", "#c2c4a4"),
        (date(1944, 1, 14), date(1944, 3, 1), "Лен.-Новг.", "#b8c6b6"),
        (date(1944, 6, 22), date(1944, 8, 19), "БАГРАТИОН", "#bcc6a8"),
        (date(1945, 4, 16), date(1945, 5, 8), "Берлин", "#b4bcc2"),
    ]
    for start, end, name, color in macro:
        x1 = t_to_x(start)
        x2 = t_to_x(end)
        # Box flush to rail (from below)
        parts.append(
            f'<rect x="{x1:.1f}" y="{MACRO_BOX_Y}" width="{x2 - x1:.1f}" '
            f'height="{MACRO_BOX_H}" fill="{color}" opacity="0.78" rx="2"/>'
            # Label inside the box (left-aligned with small padding, like personal spans)
            f'<text x="{x1 + 5:.1f}" y="{MACRO_BOX_BOTTOM - 4:.1f}" '
            f'font-size="9" fill="#2a261f" font-weight="500" '
            f'font-family="-apple-system, Helvetica, Arial">{name}</text>'
        )

    # Year ticks below the macro boxes (small vertical marks)
    for y in [1941, 1942, 1943, 1944, 1945]:
        d = date(y, 1, 1) if y > 1941 else date(1941, 6, 22)
        x = t_to_x(d)
        parts.append(
            f'<line x1="{x:.1f}" y1="{YEAR_TICK_TOP}" x2="{x:.1f}" y2="{YEAR_TICK_BOTTOM}" '
            f'stroke="#9c917c" stroke-width="0.8"/>'
            f'<text x="{x:.1f}" y="{YEAR_LABEL_Y}" font-size="10.5" fill="#6b6357" '
            f'text-anchor="middle" '
            f'font-family="-apple-system, Helvetica, Arial">{y}</text>'
        )

    # ── Personal events ABOVE the rail ─────────────────────────────────────
    # Spans flush to the rail (touching from above, all in one lane).
    # Points have labels stacked higher with leader lines down to dots on rail.
    SPAN_Y_TOP = TL_Y - 14
    SPAN_Y_BOTTOM = TL_Y     # bottom edge touches rail
    SPAN_H = SPAN_Y_BOTTOM - SPAN_Y_TOP

    # Point lanes — above spans
    POINT_LANE_Y = [TL_Y - 26, TL_Y - 46]  # label baseline; sub goes ABOVE label

    # Без хардкод-таймлайна конкретного человека: если в конфиге листа нет
    # timeline_events — таймлайн рисуется без личных событий (рельса +
    # макробоксы войны). Личные точки/спаны задаются в maps.json.
    personal = cfg.get("timeline_events", [])

    # Грубая оценка ширины текста в px (Helvetica ≈ 0.56·кегль на символ).
    def _txt_w(s, fs):
        return len(s) * fs * 0.56

    # Подпись с зажимом в края рельсы: у правого края — anchor=end,
    # у левого — start, иначе центр. Так ничего не вылезает за TL_X1/TL_X2.
    def _clamped_text(s, fs, x, y, fill, weight):
        if not s:
            return ""
        half = _txt_w(s, fs) / 2
        if x - half < TL_X1 + 2:
            tx, anc = TL_X1 + 2, "start"
        elif x + half > TL_X2 - 2:
            tx, anc = TL_X2 - 2, "end"
        else:
            tx, anc = x, "middle"
        return (
            f'<text x="{tx:.1f}" y="{y:.1f}" font-size="{fs}" fill="{fill}" '
            f'text-anchor="{anc}" font-weight="{weight}" '
            f'font-family="-apple-system, Helvetica, Arial">{s}</text>'
        )

    # Сначала спаны (под точечными), чтобы leader-линии точек шли поверх.
    # Бар зажат в [TL_X1, TL_X2]; подпись рисуется только если влезает в бар,
    # иначе обрезается с «…» (раньше длинные подписи спанов лезли за край).
    for ev in personal:
        if ev["kind"] != "span":
            continue
        x1 = max(TL_X1, t_to_x(ev["start"]))
        x2 = min(TL_X2, t_to_x(ev["end"]))
        w = x2 - x1
        if w < 5:
            continue
        parts.append(
            f'<rect x="{x1:.1f}" y="{SPAN_Y_TOP}" width="{w:.1f}" height="{SPAN_H}" '
            f'fill="{ev["color"]}" opacity="0.35" stroke="{ev["color"]}" stroke-width="1.0" rx="3"/>'
        )
        lbl, fs = ev["label"], 9.5
        avail = w - 10
        if _txt_w(lbl, fs) > avail:
            nmax = int(avail / (fs * 0.56)) - 1
            lbl = (lbl[:nmax].rstrip() + "…") if nmax >= 2 else ""
        if lbl:
            parts.append(
                f'<text x="{x1 + 6:.1f}" y="{SPAN_Y_BOTTOM - 4:.1f}" font-size="{fs}" '
                f'fill="{ev["color"]}" font-weight="600" '
                f'font-family="-apple-system, Helvetica, Arial">{lbl}</text>'
            )

    # Точечные события — выше спанов. Авто-прореживание: на каждой полосе
    # держим точку только если она дальше MIN_GAP px от предыдущей удержанной;
    # перегруженные подписи (как в 1944—45) просто не рисуем — их меньше.
    MIN_GAP = 68
    pts = [(t_to_x(e["d"]), e) for e in personal if e["kind"] == "point"]
    kept, last_x = [], {}
    for x, ev in sorted(pts, key=lambda p: p[0]):
        ln = ev.get("lane", 0)
        if ln in last_x and x - last_x[ln] < MIN_GAP:
            continue
        last_x[ln] = x
        kept.append((x, ev))
    for x, ev in kept:
        label_y = POINT_LANE_Y[ev.get("lane", 0)]
        sub_y = label_y - 12
        parts.append(
            f'<line x1="{x:.1f}" y1="{TL_Y}" x2="{x:.1f}" y2="{label_y + 2}" '
            f'stroke="{ev["color"]}" stroke-width="0.9" opacity="0.55"/>'
            f'<circle cx="{x:.1f}" cy="{TL_Y}" r="3.5" fill="{ev["color"]}" '
            f'stroke="#fffdf8" stroke-width="1.1"/>'
            + _clamped_text(ev["label"], 10.5, x, label_y, ev["color"], 700)
            + _clamped_text(ev.get("sub", ""), 8.5, x, sub_y, "#6b6357", 400)
        )

    # Футер (кредит + «ЛИСТ N · A5») убран — книга сама нумерует страницы.

    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Буклет · {cfg["map_title"]}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  html, body {{
    width: {W}px; height: {H}px; overflow: hidden;
    -webkit-font-smoothing: antialiased; text-rendering: geometricPrecision;
    font-family: -apple-system, 'SF Pro Display', 'Helvetica Neue', 'Arial', sans-serif;
  }}
</style></head>
<body>
<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%"  stop-color="#fffdf8"/>
      <stop offset="55%" stop-color="#fbf5ea"/>
      <stop offset="100%" stop-color="#f5eedd"/>
    </linearGradient>
  </defs>
  {''.join(parts)}
</svg>
</body></html>
"""
    return html


# ─── Narrative slide (text + timeline only, no map) ─────────────────────────
def build_narrative_slide(cfg):
    """Build the 'history in plain Russian' slide: header + two-column narrative
    + the shared war timeline + footer. No map, no waypoints, no right panel.
    """
    parts = []
    parts.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="url(#bgGrad)"/>')

    # ── Body: subtitle + two-column text via foreignObject ──────────────────
    BODY_X, BODY_Y = 30, 100
    BODY_W = W - 60
    SUBTITLE_Y = BODY_Y + 18

    parts.append(
        f'<text x="{BODY_X}" y="{SUBTITLE_Y}" font-size="13" fill="#7a5a32" '
        f'letter-spacing="0.04em" font-weight="500" font-style="italic" '
        f'font-family="-apple-system, Helvetica, Arial">{cfg["body_subtitle"]}</text>'
    )

    text_x = BODY_X + 6
    text_y = SUBTITLE_Y + 22
    text_w = BODY_W - 12
    text_h = 555  # до начала верха ленты времени (~y=690)

    paragraphs_html = "".join(f"<p>{p}</p>" for p in cfg["narrative"])

    parts.append(
        f'<foreignObject x="{text_x}" y="{text_y}" width="{text_w}" height="{text_h}">'
        f'<div xmlns="http://www.w3.org/1999/xhtml" style="'
        f'column-count: 2; column-gap: 36px; column-rule: 1px solid rgba(122,90,50,0.16);'
        f'font-family: -apple-system, &quot;SF Pro Display&quot;, &quot;Helvetica Neue&quot;, Arial, sans-serif;'
        f'font-size: 13.5px; line-height: 1.6; color: #4a4438; '
        f'text-align: justify; hyphens: auto; -webkit-hyphens: auto;'
        f'">'
        f'<style>p {{ margin: 0 0 11px 0; text-indent: 1.1em; }} '
        f'p:first-child {{ text-indent: 0; }} '
        f'p:last-child {{ margin-bottom: 0; }}</style>'
        f"{paragraphs_html}"
        f"</div>"
        f"</foreignObject>"
    )

    # ── Header ──────────────────────────────────────────────────────────────
    parts.append(f'<rect x="0" y="0" width="{W}" height="80" fill="rgba(122,90,50,0.06)"/>')
    parts.append(
        f'<text x="30" y="34" font-size="11.5" fill="#7a5a32" letter-spacing="0.2em" '
        f'font-weight="500" font-family="-apple-system, Helvetica, Arial">{cfg["eyebrow"]}</text>'
    )
    display_name = cfg.get("display_name", "")
    dates_label = cfg.get("dates_label", "")
    parent_label = cfg.get("parent_label", "")
    # "†" по умолчанию (погиб); пусто если ветеран вернулся
    name_mark = cfg.get("name_mark", "†")
    name_mark_tspan = (
        f' <tspan fill="#7a5a32" font-weight="300">{name_mark}</tspan>'
        if name_mark else ""
    )
    parts.append(
        f'<text x="30" y="64" font-size="26" fill="#1a1a1a" font-weight="800" '
        f'letter-spacing="-0.01em" '
        f'font-family="-apple-system, Helvetica, Arial">{display_name}{name_mark_tspan}</text>'
    )
    parts.append(
        f'<text x="{W - 30}" y="34" font-size="11.5" fill="#6b6357" letter-spacing="0.18em" '
        f'text-anchor="end" font-family="-apple-system, Helvetica, Arial">{dates_label}</text>'
    )
    parts.append(
        f'<text x="{W - 30}" y="62" font-size="13.5" fill="#7a5a32" letter-spacing="0.04em" '
        f'text-anchor="end" font-family="-apple-system, Helvetica, Arial">'
        f'{parent_label} · лист {cfg["slide_indicator"]}</text>'
    )
    parts.append(
        f'<line x1="30" y1="84" x2="{W - 30}" y2="84" stroke="rgba(122,90,50,0.32)" stroke-width="1"/>'
    )

    # ── Timeline (same as map slides) ───────────────────────────────────────
    TL_Y = 760
    TL_X1, TL_X2 = 60, W - 60
    TL_W = TL_X2 - TL_X1
    T0 = date(1941, 6, 22)
    T1 = date(1945, 5, 9)
    TOTAL_DAYS = (T1 - T0).days

    def t_to_x(d):
        return TL_X1 + ((d - T0).days / TOTAL_DAYS) * TL_W

    parts.append(
        f'<text x="{TL_X1}" y="{TL_Y - 70}" font-size="11" fill="#7a5a32" letter-spacing="0.18em" '
        f'font-weight="600" font-family="-apple-system, Helvetica, Arial">'
        f'ВЕЛИКАЯ ОТЕЧЕСТВЕННАЯ · 22.06.1941 — 09.05.1945</text>'
    )
    parts.append(
        f'<line x1="{TL_X1}" y1="{TL_Y}" x2="{TL_X2}" y2="{TL_Y}" '
        f'stroke="#7a5a32" stroke-width="1.6" opacity="0.85"/>'
    )

    MACRO_BOX_Y = TL_Y
    MACRO_BOX_H = 14
    MACRO_BOX_BOTTOM = TL_Y + MACRO_BOX_H
    YEAR_TICK_TOP = MACRO_BOX_BOTTOM + 4
    YEAR_TICK_BOTTOM = MACRO_BOX_BOTTOM + 10
    YEAR_LABEL_Y = MACRO_BOX_BOTTOM + 22
    macro = [
        (date(1941, 6, 22), date(1941, 12, 5), "Барбаросса", "#cdb8a8"),
        (date(1941, 12, 5), date(1942, 4, 20), "Москва", "#cdbfa0"),
        (date(1942, 7, 17), date(1943, 2, 2), "Сталинград", "#ccbd9c"),
        (date(1943, 7, 5), date(1943, 8, 23), "Курск", "#c2c4a4"),
        (date(1944, 1, 14), date(1944, 3, 1), "Лен.-Новг.", "#b8c6b6"),
        (date(1944, 6, 22), date(1944, 8, 19), "БАГРАТИОН", "#bcc6a8"),
        (date(1945, 4, 16), date(1945, 5, 8), "Берлин", "#b4bcc2"),
    ]
    for start, end, name, color in macro:
        x1 = t_to_x(start)
        x2 = t_to_x(end)
        parts.append(
            f'<rect x="{x1:.1f}" y="{MACRO_BOX_Y}" width="{x2 - x1:.1f}" '
            f'height="{MACRO_BOX_H}" fill="{color}" opacity="0.78" rx="2"/>'
            f'<text x="{x1 + 5:.1f}" y="{MACRO_BOX_BOTTOM - 4:.1f}" '
            f'font-size="9" fill="#2a261f" font-weight="500" '
            f'font-family="-apple-system, Helvetica, Arial">{name}</text>'
        )

    for y in [1941, 1942, 1943, 1944, 1945]:
        d = date(y, 1, 1) if y > 1941 else date(1941, 6, 22)
        x = t_to_x(d)
        parts.append(
            f'<line x1="{x:.1f}" y1="{YEAR_TICK_TOP}" x2="{x:.1f}" y2="{YEAR_TICK_BOTTOM}" '
            f'stroke="#9c917c" stroke-width="0.8"/>'
            f'<text x="{x:.1f}" y="{YEAR_LABEL_Y}" font-size="10.5" fill="#6b6357" '
            f'text-anchor="middle" '
            f'font-family="-apple-system, Helvetica, Arial">{y}</text>'
        )

    SPAN_Y_TOP = TL_Y - 14
    SPAN_Y_BOTTOM = TL_Y
    SPAN_H = SPAN_Y_BOTTOM - SPAN_Y_TOP
    POINT_LANE_Y = [TL_Y - 26, TL_Y - 46]

    personal = cfg.get("timeline_events", [])
    for ev in personal:
        if ev["kind"] != "span":
            continue
        x1 = t_to_x(ev["start"])
        x2 = t_to_x(ev["end"])
        parts.append(
            f'<rect x="{x1:.1f}" y="{SPAN_Y_TOP}" width="{x2 - x1:.1f}" height="{SPAN_H}" '
            f'fill="{ev["color"]}" opacity="0.35" stroke="{ev["color"]}" stroke-width="1.0" rx="3"/>'
            f'<text x="{x1 + 6:.1f}" y="{SPAN_Y_BOTTOM - 4:.1f}" font-size="9.5" '
            f'fill="{ev["color"]}" font-weight="600" '
            f'font-family="-apple-system, Helvetica, Arial">{ev["label"]}</text>'
        )
    for ev in personal:
        if ev["kind"] != "point":
            continue
        x = t_to_x(ev["d"])
        label_y = POINT_LANE_Y[ev.get("lane", 0)]
        sub_y = label_y - 12
        parts.append(
            f'<line x1="{x:.1f}" y1="{TL_Y}" x2="{x:.1f}" y2="{label_y + 2}" '
            f'stroke="{ev["color"]}" stroke-width="0.9" opacity="0.55"/>'
            f'<circle cx="{x:.1f}" cy="{TL_Y}" r="3.5" fill="{ev["color"]}" '
            f'stroke="#fffdf8" stroke-width="1.1"/>'
            f'<text x="{x:.1f}" y="{label_y}" font-size="10.5" fill="{ev["color"]}" '
            f'text-anchor="middle" font-weight="700" '
            f'font-family="-apple-system, Helvetica, Arial">{ev["label"]}</text>'
            f'<text x="{x:.1f}" y="{sub_y}" font-size="8.5" fill="#6b6357" '
            f'text-anchor="middle" '
            f'font-family="-apple-system, Helvetica, Arial">{ev.get("sub", "")}</text>'
        )

    # ── Footer ──────────────────────────────────────────────────────────────
    parts.append(
        f'<line x1="30" y1="{H - 38}" x2="{W - 30}" y2="{H - 38}" '
        f'stroke="rgba(122,90,50,0.24)" stroke-width="1"/>'
    )
    parts.append(
        f'<text x="30" y="{H - 18}" font-size="10" fill="#9c917c" letter-spacing="0.15em" '
        f'font-family="-apple-system, Helvetica, Arial">{BOOK_FOOTER}</text>'
    )
    parts.append(
        f'<text x="{W - 30}" y="{H - 18}" font-size="10" fill="#9c917c" letter-spacing="0.12em" '
        f'text-anchor="end" font-family="-apple-system, Helvetica, Arial">'
        f'ЛИСТ {cfg["slide_indicator"]} · A5</text>'
    )

    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Лист · {cfg["eyebrow"]}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  html, body {{
    width: {W}px; height: {H}px; overflow: hidden;
    -webkit-font-smoothing: antialiased; text-rendering: geometricPrecision;
    font-family: -apple-system, 'SF Pro Display', 'Helvetica Neue', 'Arial', sans-serif;
  }}
</style></head>
<body>
<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%"  stop-color="#fffdf8"/>
      <stop offset="55%" stop-color="#fbf5ea"/>
      <stop offset="100%" stop-color="#f5eedd"/>
    </linearGradient>
  </defs>
  {''.join(parts)}
</svg>
</body></html>
"""
    return html


# ─── Сводная карта «Дороги, которые сошлись» ────────────────────────────────
def build_overview_slide(cfg):
    """Карта-вывод книги: одно село-исток → N дорог.

    Не тактическая карта одного бойца, а «геометрия» всей книги. Дуги
    расходятся из одной точки. Где есть документ — дорога доходит до
    конкретного места († если погиб, кольцо если вернулся, звезда Победы
    если дошёл). Где документа нет — дорога обрывается блёклым
    пунктиром в «?» (без вымысла). Линия фронта НЕ рисуется (RULES.md §2).

    `origin` и `victory` — точки {name,lon,lat} из конфига (бывшие
    SMIRNOVO/BERLIN_VICTORY: вынесены в данные, не зашиты в движок).
    """
    lon_min, lon_max, lat_min, lat_max = cfg["bbox"]
    lat_mid = (lat_min + lat_max) / 2
    cos_mid = math.cos(math.radians(lat_mid))
    lon_span_corr = (lon_max - lon_min) * cos_mid
    lat_span = lat_max - lat_min

    MAP_X0, MAP_Y0, MAP_W0, MAP_H0 = 30, 104, 814, 410
    # Холст этого листа НЕ во всю A5-высоту (1240): сводная карта —
    # широкая, под ней только легенда + ключ-список имён. Берём высоту
    # по содержимому (≈920), чтобы в книге не было пустой нижней полосы
    # (карта-плита масштабируется по ширине, аспект = HOV/W).
    HOV = 920
    scale = min(MAP_W0 / lon_span_corr, MAP_H0 / lat_span)
    draw_w = lon_span_corr * scale
    draw_h = lat_span * scale
    off_x = MAP_X0 + (MAP_W0 - draw_w) / 2
    off_y = MAP_Y0 + (MAP_H0 - draw_h) / 2

    def project(lon, lat):
        return (
            off_x + (lon - lon_min) * cos_mid * scale,
            off_y + (lat_max - lat) * scale,
        )

    def path_for_ring(ring):
        pts = [project(lon, lat) for lon, lat in ring]
        return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + "Z"

    def path_for_line(coords):
        pts = [project(lon, lat) for lon, lat in coords]
        return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)

    def feature_path(feat):
        geom = feat["geometry"]
        t, c = geom["type"], geom["coordinates"]
        if t == "Polygon":
            return " ".join(path_for_ring(r) for r in c)
        if t == "MultiPolygon":
            return " ".join(" ".join(path_for_ring(r) for r in poly) for poly in c)
        if t == "LineString":
            return path_for_line(c)
        if t == "MultiLineString":
            return " ".join(path_for_line(line) for line in c)
        return ""

    def in_bbox(lon, lat):
        return lon_min <= lon <= lon_max and lat_min <= lat <= lat_max

    parts = []
    parts.append(f'<rect x="0" y="0" width="{W}" height="{HOV}" fill="url(#bgGrad)"/>')
    parts.append(
        f'<rect x="{MAP_X0}" y="{MAP_Y0}" width="{MAP_W0}" height="{MAP_H0}" '
        f'fill="#e9eef0" stroke="rgba(122,90,50,0.30)" stroke-width="1"/>'
    )
    parts.append(
        f'<defs><clipPath id="ovClip">'
        f'<rect x="{MAP_X0}" y="{MAP_Y0}" width="{MAP_W0}" height="{MAP_H0}"/>'
        f'</clipPath>'
        f'<marker id="ovDeath" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="5.5" markerHeight="5.5" orient="auto">'
        f'<path d="M0,0 L10,5 L0,10 Z" fill="{DEATH}"/></marker>'
        f'<marker id="ovBack" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="5" markerHeight="5" orient="auto">'
        f'<path d="M0,0 L10,5 L0,10 Z" fill="{TAN_HOME}"/></marker>'
        f'</defs>'
    )
    parts.append('<g clip-path="url(#ovClip)">')

    # Базовые слои
    for f in countries["features"]:
        d = feature_path(f)
        if d:
            parts.append(f'<path d="{d}" fill="#efe7d5" stroke="#cdbfa3" stroke-width="0.7"/>')
    for f in lakes["features"]:
        d = feature_path(f)
        if d:
            parts.append(f'<path d="{d}" fill="#e9eef0" stroke="#bcccd0" stroke-width="0.5"/>')
    for f in rivers["features"]:
        d = feature_path(f)
        if d:
            parts.append(
                f'<path d="{d}" fill="none" stroke="#9bb2c0" stroke-width="0.8" opacity="0.7"/>'
            )

    # Минимум опорных городов: карта не про города, а про геометрию дорог.
    # Список — кураторский, из конфига (anchor_cities), не зашит в движок.
    ANCHOR_CITIES = cfg.get("anchor_cities", [])
    origin = cfg["origin"]
    victory = cfg["victory"]
    smx, smy = project(origin["lon"], origin["lat"])
    for c in ANCHOR_CITIES:
        if not in_bbox(c["lon"], c["lat"]):
            continue
        x, y = project(c["lon"], c["lat"])
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" fill="#a59c8a"/>')
        parts.append(
            f'<text x="{x + 5:.1f}" y="{y + 3.5:.1f}" font-size="10.5" fill="#9a917c" '
            f'paint-order="stroke" stroke="#fffdf8" stroke-width="3" stroke-linejoin="round" '
            f'font-family="-apple-system, Helvetica, Arial">{c["name"]}</text>'
        )

    # Звезда Победы — общая точка (рисуем до дорог, чтобы дуги к ней)
    bx, by = project(victory["lon"], victory["lat"])

    def star_path(cx, cy, ro, ri, n=5, rot=-math.pi / 2):
        pts = []
        for k in range(n * 2):
            r = ro if k % 2 == 0 else ri
            a = rot + k * math.pi / n
            pts.append(f"{cx + r * math.cos(a):.1f},{cy + r * math.sin(a):.1f}")
        return "M" + " L".join(pts) + "Z"

    # Дороги: дуги из истока
    DIED, BACK = DEATH, TAN_HOME
    end_markers = []   # (x, y, n, outcome, place, lpos) — рисуем поверх линий
    faint_tips = []    # неизвестные обрывы
    for rd in cfg["roads"]:
        out = rd["outcome"]
        if out == "unknown":
            tx, ty = project(*rd["tip"])
            a2, b2 = shorten((smx, smy), (tx, ty), pad_start=14, pad_end=4)
            d = arc_path(a2, b2, curve=0.08)
            parts.append(
                f'<path d="{d}" fill="none" stroke="{TAN_PERSONAL}" stroke-width="1.3" '
                f'stroke-dasharray="2 5" opacity="0.5"/>'
            )
            faint_tips.append((tx, ty, rd["n"]))
            continue
        if out == "returned_berlin":
            dx, dy = bx, by
            color, marker = BACK, "url(#ovBack)"
        else:
            dx, dy = project(*rd["dest"])
            color = DIED if out == "died" else BACK
            marker = "url(#ovDeath)" if out == "died" else "url(#ovBack)"
        a2, b2 = shorten((smx, smy), (dx, dy), pad_start=15,
                          pad_end=(20 if out == "returned_berlin" else 15))
        d = arc_path(a2, b2, curve=rd.get("curve", 0.0))
        sw = "2.0" if out == "died" else "1.7"
        parts.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{sw}" '
            f'opacity="0.78" stroke-linecap="round" marker-end="{marker}"/>'
        )
        end_markers.append((dx, dy, rd["n"], out,
                            rd.get("place", ""), rd.get("lpos", "L")))

    # Блёклые «?» на обрывах неизвестных дорог
    for tx, ty, n in faint_tips:
        parts.append(
            f'<text x="{tx:.1f}" y="{ty + 4:.1f}" font-size="15" fill="{TAN_PERSONAL}" '
            f'text-anchor="middle" opacity="0.7" font-weight="700" '
            f'paint-order="stroke" stroke="#fffdf8" stroke-width="3" stroke-linejoin="round" '
            f'font-family="-apple-system, Helvetica, Arial">?</text>'
            f'<text x="{tx:.1f}" y="{ty - 12:.1f}" font-size="10" fill="{TAN_PERSONAL}" '
            f'text-anchor="middle" opacity="0.85" font-weight="700" '
            f'paint-order="stroke" stroke="#fffdf8" stroke-width="3" stroke-linejoin="round" '
            f'font-family="-apple-system, Helvetica, Arial">{n}</text>'
        )

    # Маркеры в конечных точках известных дорог: номерной диск + короткая
    # подпись места. Подпись уводится в сторону от пучка дуг по `lpos`
    # ("L" влево / "T" вверх / "B" вниз / "R" вправо). Полная расшифровка —
    # в ключе-списке под картой.
    berlin_ns = []
    for dx, dy, n, out, place, lpos in end_markers:
        if out == "returned_berlin":
            berlin_ns.append(n)
            continue
        is_d = out == "died"
        fill = DIED if is_d else BACK
        strk = "#7a1818" if is_d else "#5e451f"
        parts.append(
            f'<circle cx="{dx:.1f}" cy="{dy:.1f}" r="9.5" fill="{fill}" '
            f'stroke="{strk}" stroke-width="1.4"/>'
            f'<text x="{dx:.1f}" y="{dy + 4:.1f}" font-size="12" font-weight="700" '
            f'fill="#fffdf8" text-anchor="middle">{n}</text>'
        )
        if place:
            if lpos == "T":
                lxp, lyp, anc = dx, dy - 15, "middle"
            elif lpos == "B":
                lxp, lyp, anc = dx, dy + 24, "middle"
            elif lpos == "R":
                lxp, lyp, anc = dx + 14, dy + 4, "start"
            else:  # "L"
                lxp, lyp, anc = dx - 14, dy + 4, "end"
            txt = (("† " if is_d else "") + place)
            parts.append(
                f'<text x="{lxp:.1f}" y="{lyp:.1f}" font-size="10.5" '
                f'fill="{"#9a2f23" if is_d else "#5e451f"}" text-anchor="{anc}" '
                f'font-weight="600" paint-order="stroke" stroke="#fffdf8" '
                f'stroke-width="3.2" stroke-linejoin="round" '
                f'font-family="-apple-system, Helvetica, Arial">{txt}</text>'
            )

    # Звезда Победы (поверх дуг, с номерами дошедших)
    parts.append(
        f'<path d="{star_path(bx, by, 15, 6.2)}" fill="{RED_COMBAT}" '
        f'stroke="#7a1818" stroke-width="1.2"/>'
    )
    bn = " · ".join(str(n) for n in sorted(berlin_ns))
    victory_label = cfg.get("victory_label", victory.get("name", "").upper())
    victory_sub = cfg.get("victory_sub", "")
    parts.append(
        f'<text x="{bx + 19:.1f}" y="{by - 21:.1f}" font-size="12.5" fill="#9a2f23" '
        f'text-anchor="start" font-weight="800" letter-spacing="0.04em" '
        f'paint-order="stroke" stroke="#fffdf8" stroke-width="3.4" stroke-linejoin="round" '
        f'font-family="-apple-system, Helvetica, Arial">{victory_label}</text>'
        f'<text x="{bx + 19:.1f}" y="{by - 6:.1f}" font-size="10" fill="#6b6357" '
        f'text-anchor="start" font-weight="600" '
        f'paint-order="stroke" stroke="#fffdf8" stroke-width="3" stroke-linejoin="round" '
        f'font-family="-apple-system, Helvetica, Arial">{victory_sub} {bn}</text>'
    )

    # Дом — исток всех дорог, рисуем последним: поверх всего
    parts.append(
        f'<circle cx="{smx:.1f}" cy="{smy:.1f}" r="20" fill="none" '
        f'stroke="{TAN_HOME}" stroke-width="1" opacity="0.45"/>'
        f'<path d="{star_path(smx, smy, 13, 5.4)}" fill="{TAN_HOME}" '
        f'stroke="#5e451f" stroke-width="1.2"/>'
    )
    origin_label = cfg.get("origin_label", origin.get("name", "").upper())
    origin_sub = cfg.get("origin_sub", "")
    parts.append(
        f'<text x="{smx:.1f}" y="{smy - 25:.1f}" font-size="13" fill="#5e451f" '
        f'text-anchor="end" font-weight="800" letter-spacing="0.04em" '
        f'paint-order="stroke" stroke="#fffdf8" stroke-width="3.6" stroke-linejoin="round" '
        f'font-family="-apple-system, Helvetica, Arial">{origin_label}</text>'
        f'<text x="{smx:.1f}" y="{smy + 31:.1f}" font-size="10" fill="#6b6357" '
        f'text-anchor="end" font-weight="600" '
        f'paint-order="stroke" stroke="#fffdf8" stroke-width="3" stroke-linejoin="round" '
        f'font-family="-apple-system, Helvetica, Arial">{origin_sub}</text>'
    )

    parts.append("</g>")  # end ovClip

    # ─── Шапка ──────────────────────────────────────────────────────────────
    parts.append(f'<rect x="0" y="0" width="{W}" height="92" fill="rgba(122,90,50,0.06)"/>')
    parts.append(
        f'<text x="30" y="46" font-size="26" fill="#1a1a1a" font-weight="800" '
        f'letter-spacing="-0.01em" '
        f'font-family="-apple-system, Helvetica, Arial">{cfg["title"]}</text>'
    )
    parts.append(
        f'<text x="30" y="74" font-size="12.5" fill="#7a5a32" letter-spacing="0.06em" '
        f'font-weight="600" font-family="-apple-system, Helvetica, Arial">{cfg["subtitle"]}</text>'
    )
    parts.append(
        f'<line x1="30" y1="92" x2="{W - 30}" y2="92" stroke="rgba(122,90,50,0.32)" stroke-width="1"/>'
    )

    # ─── Легенда символов ───────────────────────────────────────────────────
    LEG_Y = MAP_Y0 + MAP_H0 + 30
    lx = 42
    parts.append(
        f'<path d="{star_path(lx, LEG_Y, 9, 3.8)}" fill="{TAN_HOME}" stroke="#5e451f" stroke-width="1"/>'
        f'<text x="{lx + 16}" y="{LEG_Y + 4}" font-size="11" fill="#6b6357" '
        f'font-family="-apple-system, Helvetica, Arial">{cfg.get("legend_origin", "исток")}</text>'
    )
    lx2 = 230
    parts.append(
        f'<circle cx="{lx2}" cy="{LEG_Y}" r="8" fill="{DIED}" stroke="#7a1818" stroke-width="1.2"/>'
        f'<text x="{lx2 + 15}" y="{LEG_Y + 4}" font-size="11" fill="#6b6357" '
        f'font-family="-apple-system, Helvetica, Arial">погиб (†)</text>'
    )
    lx3 = 370
    parts.append(
        f'<circle cx="{lx3}" cy="{LEG_Y}" r="8" fill="{BACK}" stroke="#5e451f" stroke-width="1.2"/>'
        f'<text x="{lx3 + 15}" y="{LEG_Y + 4}" font-size="11" fill="#6b6357" '
        f'font-family="-apple-system, Helvetica, Arial">вернулся</text>'
    )
    lx4 = 510
    parts.append(
        f'<line x1="{lx4 - 8}" y1="{LEG_Y}" x2="{lx4 + 14}" y2="{LEG_Y}" '
        f'stroke="{TAN_PERSONAL}" stroke-width="1.3" stroke-dasharray="2 5" opacity="0.7"/>'
        f'<text x="{lx4 + 22}" y="{LEG_Y + 4}" font-size="11" fill="#6b6357" '
        f'font-family="-apple-system, Helvetica, Arial">путь не установлен</text>'
    )
    lx5 = 700
    parts.append(
        f'<path d="{star_path(lx5, LEG_Y, 9, 3.8)}" fill="{RED_COMBAT}" stroke="#7a1818" stroke-width="1"/>'
        f'<text x="{lx5 + 16}" y="{LEG_Y + 4}" font-size="11" fill="#6b6357" '
        f'font-family="-apple-system, Helvetica, Arial">{cfg.get("legend_victory", "Победа")}</text>'
    )

    # ─── Ключ: имена (2 колонки) ────────────────────────────────────────────
    KEY_Y0 = LEG_Y + 26
    roads = cfg["roads"]
    ncol, pad, gap = 2, 22, 30
    rows = math.ceil(len(roads) / ncol)
    col_w = (W - 60 - pad * 2 - gap * (ncol - 1)) / ncol
    row_h = 40
    key_box_h = 30 + rows * row_h + 14   # по содержимому, без пустого хвоста
    parts.append(
        f'<rect x="30" y="{KEY_Y0}" width="{W - 60}" height="{key_box_h}" '
        f'fill="rgba(122,90,50,0.05)" stroke="rgba(122,90,50,0.24)" stroke-width="1"/>'
    )
    ky = KEY_Y0 + 30
    for idx, rd in enumerate(roads):
        col = idx // rows
        row = idx % rows
        ex = 30 + pad + col * (col_w + gap)
        ey = ky + row * row_h
        out = rd["outcome"]
        if out == "died":
            cfill, cstrk, tcol = DIED, "#7a1818", "#9a2f23"
        elif out == "unknown":
            cfill, cstrk, tcol = TAN_PERSONAL, "#5a5448", "#6b6357"
        else:  # returned / returned_berlin
            cfill, cstrk, tcol = BACK, "#5e451f", "#5e451f"
        parts.append(
            f'<circle cx="{ex + 11:.1f}" cy="{ey:.1f}" r="11" fill="{cfill}" '
            f'stroke="{cstrk}" stroke-width="1.2"/>'
            f'<text x="{ex + 11:.1f}" y="{ey + 4:.1f}" font-size="12" font-weight="700" '
            f'fill="#fffdf8" text-anchor="middle">{rd["n"]}</text>'
            f'<text x="{ex + 32:.1f}" y="{ey - 2:.1f}" font-size="13.5" fill="{tcol}" '
            f'font-weight="700" font-family="-apple-system, Helvetica, Arial">'
            f'{rd["name"]} <tspan font-size="10.5" fill="#9c917c" font-weight="400">'
            f'· {rd["branch"]}</tspan></text>'
            f'<text x="{ex + 32:.1f}" y="{ey + 15:.1f}" font-size="11" fill="#6b6357" '
            f'font-family="-apple-system, Helvetica, Arial">{rd["note"]}</text>'
        )

    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Буклет · {cfg["title"]}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  html, body {{
    width: {W}px; height: {HOV}px; overflow: hidden;
    -webkit-font-smoothing: antialiased; text-rendering: geometricPrecision;
    font-family: -apple-system, 'SF Pro Display', 'Helvetica Neue', 'Arial', sans-serif;
  }}
</style></head>
<body>
<svg width="{W}" height="{HOV}" viewBox="0 0 {W} {HOV}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%"  stop-color="#fffdf8"/>
      <stop offset="55%" stop-color="#fbf5ea"/>
      <stop offset="100%" stop-color="#f5eedd"/>
    </linearGradient>
  </defs>
  {''.join(parts)}
</svg>
</body></html>
"""
    return html


# ─── Headless-Chrome screenshot + crop ───────────────────────────────────────
# Why --headless=old: on macOS the new headless mode with a per-worker
# user-data-dir hangs after writing the screenshot and never exits, so the
# subprocess times out. The legacy headless mode exits promptly. (Same
# reason the legacy book.py and build_dvory.py pin --headless=old.)
def _find_chrome():
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if os.path.exists(mac):
        return mac
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _screenshot(html_path, png_path, w, h):
    chrome = _find_chrome()
    if not chrome:
        print("  ! Chrome/Chromium not found on PATH — skipping PNG preview",
              file=sys.stderr)
        return False
    if png_path.exists():
        png_path.unlink()
    subprocess.run(
        [chrome, "--headless=old", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", "--force-device-scale-factor=2",
         f"--window-size={w},{h}",
         f"--screenshot={png_path}", f"file://{html_path}"],
        capture_output=True, text=True, timeout=60,
    )
    if not png_path.exists():
        return False
    # Crop: trim a uniform paper-colored border so the preview is tight
    # (faithful to the legacy crop step; pure-Pillow, no hard dep beyond
    # what the original already used).
    try:
        from PIL import Image, ImageChops
        im = Image.open(png_path).convert("RGB")
        bg = Image.new("RGB", im.size, im.getpixel((0, 0)))
        diff = ImageChops.difference(im, bg)
        bbox = diff.getbbox()
        if bbox:
            pad = 4
            x0 = max(0, bbox[0] - pad)
            y0 = max(0, bbox[1] - pad)
            x1 = min(im.width, bbox[2] + pad)
            y1 = min(im.height, bbox[3] + pad)
            im.crop((x0, y0, x1, y1)).save(png_path)
    except Exception as e:
        print(f"  ! crop skipped ({e})", file=sys.stderr)
    return True


def write_slide(cfg):
    """Render one config: write HTML, screenshot to PNG, sanity-check size."""
    kind = cfg.get("kind")
    if kind == "narrative":
        html = build_narrative_slide(cfg)
        w, h = W, H
    elif kind == "overview":
        html = build_overview_slide(cfg)
        w, h = W, 920
    else:
        html = build_slide(cfg)
        w, h = W, H
    OUT.mkdir(parents=True, exist_ok=True)
    fname = cfg["filename"]
    out_html = OUT / fname
    out_html.write_text(html, encoding="utf-8")
    png = OUT / (Path(fname).stem + ".png")
    ok = _screenshot(out_html, png, w, h)
    status = "OK "
    if ok and png.exists():
        size = png.stat().st_size
        # ≥50 KB sanity check: a tiny PNG means the render is broken
        # (blank/white page) — flag it loudly.
        if size < 50 * 1024:
            status = "SMALL"
            print(f"  ! {png.name}: {size} bytes (<50 KB — render likely "
                  f"broken)", file=sys.stderr)
    elif not ok:
        status = "NOPNG"
    print(f"  {status} {out_html}  ({len(html)} bytes)")
    return out_html


def main():
    ap = argparse.ArgumentParser(description="Render A5 map sheets.")
    ap.add_argument("--slug", default=None,
                    help="render only this chapter's maps.json (default: all)")
    args = ap.parse_args()
    configs = load_slide_configs(slug_filter=args.slug)
    if not configs:
        scope = f" for slug '{args.slug}'" if args.slug else ""
        print(f"No slide configs found{scope} "
              f"(book/chapters/*/maps.json).", file=sys.stderr)
        return
    for cfg in configs:
        write_slide(cfg)


if __name__ == "__main__":
    main()
