#!/usr/bin/env python3
"""Скачать Natural Earth 1:50m и обрезать по театру военных действий.

Запуск (один раз, после заполнения book.config.yml):

    python3 build/clip_geo.py

Читает прямоугольник из book.config.yml → geo:. Скачивает четыре слоя
Natural Earth (страны, реки, озёра, города) в assets/geo/_src/ (кэш),
обрезает по bbox и кладёт компактные assets/geo/clipped_*.geojson, которые
читают build_maps.py и карты. Зависимости — только стандартная библиотека.

Почему bbox должен быть с запасом: соседние страны Natural Earth делят общие
вершины границы; если земля не доходит до края карты, кремовый фон «протекает»
фейковым морем. Объединение всех карт книги + поля. Восточный фронт — дефолт.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "book.config.yml"
SRC = ROOT / "assets" / "geo" / "_src"
OUT = ROOT / "assets" / "geo"

# Natural Earth 1:50m, GeoJSON (репозиторий nvkelso/natural-earth-vector).
BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson"
LAYERS = {
    "ne_50m_admin_0_countries.geojson": "clipped_countries.geojson",
    "ne_50m_rivers_lake_centerlines.geojson": "clipped_rivers.geojson",
    "ne_50m_lakes.geojson": "clipped_lakes.geojson",
    "ne_50m_populated_places.geojson": "clipped_cities.geojson",
}


def read_geo_bbox(cfg_path):
    """Минимальный разбор book.config.yml: только числа из блока geo:.

    Не полноценный YAML — достаточно для плоского блока вида
    `geo:\\n  lon_min: 11.0` и т.п. Если блок не найден — дефолт (Вост. фронт).
    """
    bbox = {"lon_min": 11.0, "lon_max": 49.0, "lat_min": 44.0, "lat_max": 63.0}
    if not cfg_path.exists():
        print(f"⚠ {cfg_path} не найден — bbox по умолчанию {bbox}")
        return bbox
    in_geo = False
    for raw in cfg_path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")):
            in_geo = line.strip().rstrip(":") == "geo"
            continue
        if in_geo:
            key, _, val = line.strip().partition(":")
            key, val = key.strip(), val.strip()
            if key in bbox and val:
                try:
                    bbox[key] = float(val)
                except ValueError:
                    pass
    return bbox


def download(name):
    SRC.mkdir(parents=True, exist_ok=True)
    dst = SRC / name
    if dst.exists() and dst.stat().st_size > 0:
        print(f"  кэш  {name} ({dst.stat().st_size:,} б)")
        return dst
    url = f"{BASE}/{name}"
    print(f"  тяну {name} …")
    req = urllib.request.Request(url, headers={"User-Agent": "memory-book-seed"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dst, "wb") as f:
        f.write(r.read())
    print(f"        {dst.stat().st_size:,} б")
    return dst


def in_bbox(lon, lat, b):
    return b["lon_min"] <= lon <= b["lon_max"] and b["lat_min"] <= lat <= b["lat_max"]


def clip_linestring(coords, b):
    out, seg = [], []
    for lon, lat in coords:
        if in_bbox(lon, lat, b):
            seg.append([round(lon, 4), round(lat, 4)])
        else:
            if seg:
                seg.append([round(lon, 4), round(lat, 4)])
                if len(seg) >= 2:
                    out.append(seg)
                seg = []
    if len(seg) >= 2:
        out.append(seg)
    return out


def clip_polygon(rings, b):
    # БЕЗ децимации: соседние страны делят вершины границы; независимое
    # прореживание разводит границу → «белые щели». Полное разрешение → встык.
    out = []
    for ring in rings:
        if any(in_bbox(lon, lat, b) for lon, lat in ring):
            s = [[round(lon, 5), round(lat, 5)] for lon, lat in ring]
            if s[0] != s[-1]:
                s.append(s[0])
            if len(s) >= 4:
                out.append(s)
    return out


def clip_feature(feat, b):
    geom = feat.get("geometry")
    if not geom:
        return None
    t, coords = geom["type"], geom["coordinates"]
    if t == "Point":
        lon, lat = coords[:2]
        return feat if in_bbox(lon, lat, b) else None
    if t == "LineString":
        c = clip_linestring(coords, b)
        if not c:
            return None
        g = {"type": "LineString", "coordinates": c[0]} if len(c) == 1 else {
            "type": "MultiLineString", "coordinates": c}
        return {**feat, "geometry": g}
    if t == "MultiLineString":
        allc = []
        for line in coords:
            allc.extend(clip_linestring(line, b))
        return {**feat, "geometry": {"type": "MultiLineString", "coordinates": allc}} if allc else None
    if t == "Polygon":
        c = clip_polygon(coords, b)
        return {**feat, "geometry": {"type": "Polygon", "coordinates": c}} if c else None
    if t == "MultiPolygon":
        polys = [cp for poly in coords if (cp := clip_polygon(poly, b))]
        return {**feat, "geometry": {"type": "MultiPolygon", "coordinates": polys}} if polys else None
    return None


def process(infile, outfile, keep_props, b):
    gj = json.loads(Path(infile).read_text(encoding="utf-8"))
    feats = []
    for feat in gj["features"]:
        c = clip_feature(feat, b)
        if c:
            props = c.get("properties", {})
            c["properties"] = {k: props[k] for k in keep_props if k in props}
            feats.append(c)
    Path(outfile).write_text(
        json.dumps({"type": "FeatureCollection", "features": feats},
                   separators=(",", ":")), encoding="utf-8")
    print(f"  {os.path.basename(outfile)}: {len(feats)} объектов")


def process_cities(infile, outfile, b):
    gj = json.loads(Path(infile).read_text(encoding="utf-8"))
    cities = []
    for feat in gj["features"]:
        lon, lat = feat["geometry"]["coordinates"][:2]
        if not in_bbox(lon, lat, b):
            continue
        p = feat["properties"]
        if p.get("SCALERANK", 99) > 6:  # только крупные
            continue
        cities.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lon, 4), round(lat, 4)]},
            "properties": {
                "name": p.get("NAME"), "name_ru": p.get("NAME_RU"),
                "scalerank": p.get("SCALERANK"), "pop_max": p.get("POP_MAX"),
            },
        })
    Path(outfile).write_text(
        json.dumps({"type": "FeatureCollection", "features": cities},
                   separators=(",", ":")), encoding="utf-8")
    print(f"  {os.path.basename(outfile)}: {len(cities)} городов")


def main():
    b = read_geo_bbox(CONFIG)
    print(f"Театр: lon {b['lon_min']}..{b['lon_max']}  lat {b['lat_min']}..{b['lat_max']}")
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        files = {name: download(name) for name in LAYERS}
    except Exception as e:
        print(f"\n✗ Не удалось скачать Natural Earth: {e}\n"
              f"  Проверьте интернет/прокси. Файлы кэшируются в {SRC}.")
        sys.exit(1)
    print("Обрезаю по bbox:")
    process(files["ne_50m_admin_0_countries.geojson"],
            OUT / "clipped_countries.geojson", ["NAME", "NAME_RU", "NAME_LONG"], b)
    process(files["ne_50m_rivers_lake_centerlines.geojson"],
            OUT / "clipped_rivers.geojson", ["name", "name_en", "scalerank"], b)
    process(files["ne_50m_lakes.geojson"],
            OUT / "clipped_lakes.geojson", ["name", "name_en"], b)
    process_cities(files["ne_50m_populated_places.geojson"],
                   OUT / "clipped_cities.geojson", b)
    print(f"\n✓ Готово. Геоданные в {OUT}/clipped_*.geojson — карты можно строить офлайн.")


if __name__ == "__main__":
    main()
