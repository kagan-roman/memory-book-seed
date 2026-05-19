#!/usr/bin/env python3
"""Family мини-tree SVG engine — faithful port of booklets/build_dvory.py.

Draws the light-palette family-yard trees for the book (Vivliostyle light
edition). The SVG drawing engine (cards, badges, veteran lines, edges,
brace connectors, head/foot blocks, headless-Chrome screenshot) is
byte-faithful to the original. ALL family-specific data has been removed:
the branch trees and per-chapter mini-trees now load from
`book/_master/dvory.yml` (documented schema below).

Outputs (light print palette, as in the original):
  build/.out/trees/<branch>_01.html   — full branch tree (all dvory)
  build/.out/trees/<slug>_00.html     — per-chapter inline mini-tree
                                        (one dvor, hero highlighted)
+ PNG previews for the eye-check.

The typesetter weaves <branch>_01 plates and <slug>_00 inline mini-trees
into the book by the <!-- tree --> marker (n==0 inline, no page break).

──────────────────────────────────────────────────────────────────────────────
dvory.yml SCHEMA  (also documented in build/SCHEMA.md)

  book:
    yard_label: "ДВОРЫ <СЕЛО> · <ГОД>"   # banner over branch trees
    foot_legend: "..."                    # optional footer line override

  branches:
    - slug: <branch-slug>          # → build/.out/trees/<slug>_01.html
      founder: "Имя (год)"         # shown as branch subtitle
      title: "ВЕТКА <ИМЯ> · <ГОД>" # banner subtitle for the branch tree
      layout: prokhor | anikita    # which composition (see build_branch)
      root:                        # optional single full-width root dvor
        { qual, head, wife, mutes }
      dvory:                       # 1..N yard cards
        - name: <dvor-id>          # used by chapter_trees → which dvor
          qual: "КОРНЕВОЙ ДВОР · ..."           # small accent caption
          head: "Отец Имя · NN г."             # OR head_vet (см. ниже)
          wife: "ж. Имя · NN г."
          head_vet:                # двор главы ведёт ветеран-глава семьи
            name: "Имя Отчество"
            years: "NN г."
            status: lived | died | unknown | back   # → glyph (back="→ вернулся")
            chapter: <n or slug>   # «гл. N» reference (optional)
            epithet: "повар, ..."  # псевдоним в чужих главах
          members:                 # дети-ветераны
            - { name, years, status, chapter, epithet }
            - { name, years, unknown: true, note: "нет информации" }
          mutes: "+ N детей …"     # серая строка-сводка

  chapter_trees:                   # per-chapter inline mini-tree
    <slug>:
      branch: <branch-slug>
      dvor: <dvor-id>              # which dvor card to render full-width
      focus: "Имя Отчество"        # hero of this chapter (highlighted, no epithet)
      banner: "ВЕТКА … · ДВОР …"   # subtitle line

  epithets:                        # registry (book-director keeps in sync)
    <slug>: "Учитель"
──────────────────────────────────────────────────────────────────────────────
"""
import argparse
import html as H
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "build" / ".out" / "trees"
DVORY_YML = ROOT / "book" / "_master" / "dvory.yml"

# A5-текстблок ≈ 112×172 мм → viewBox шириной W, высота под контент
W = 1000

# светлая печатная палитра (theme.css / recolor_light.py)
PAPER = "#fffdf8"
PAPER2 = "#f5eedd"
INK = "#1a1a1a"
SOFT = "#4a4438"
MUTE = "#8d8472"
RULE = "#bcae93"
ACCENT = "#7a5a32"
KIA = "#b5402f"
SERIF = "'PT Serif', Georgia, 'Times New Roman', serif"
SANS = "'PT Sans', 'Helvetica Neue', Arial, sans-serif"

# status (в YAML человекочитаемо) → внутренний код движка ("st").
# Движок рисует глиф по "st": kia=† missing=БВ back="→ вернулся".
_STATUS = {
    "lived": "back",       # вернулся (по умолчанию для выживших)
    "back": "back",
    "died": "kia",
    "kia": "kia",
    "missing": "missing",
    "bv": "missing",
    "unknown": "",
}


def esc(s):
    return H.escape(str(s))


def txt(x, y, s, size, fill, weight="400", anchor="start", ls="0",
        italic=False, font=SERIF):
    st = ' font-style="italic"' if italic else ""
    return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" '
            f'font-weight="{weight}" text-anchor="{anchor}" '
            f'letter-spacing="{ls}" font-family="{font}"{st}>{esc(s)}</text>')


def _badge(x, y, s, fill):
    bw = 7.6 * len(s) + 16
    return (f'<rect x="{x}" y="{y - 13}" width="{bw}" height="18" rx="3" '
            f'fill="rgba(122,90,50,0.10)" stroke="rgba(122,90,50,0.45)" '
            f'stroke-width="0.8"/>'
            f'<text x="{x + bw/2}" y="{y}" font-size="10.5" fill="{fill}" '
            f'font-weight="700" text-anchor="middle" letter-spacing="0.12em" '
            f'font-family="{SANS}">{esc(s)}</text>'), bw


def _vet_line(cx, cy, v, name_size=16, show_ch=True):
    glyph = {"kia": "†", "missing": "БВ", "back": "→ вернулся"}.get(v["st"], "")
    gcol = KIA if v["st"] in ("kia", "missing") else ACCENT
    ch = (f'<tspan font-size="12.5" fill="{MUTE}" font-family="{SANS}">'
          f'  гл. {v["ch"]}</tspan>') if show_ch and v.get("ch") else ""
    return (f'<text x="{cx}" y="{cy}" font-family="{SERIF}">'
            f'<tspan font-size="{name_size}" fill="{ACCENT}" '
            f'font-weight="700">{esc(v["name"])}</tspan>'
            f'<tspan font-size="13" fill="{SOFT}"> · {esc(v["years"])} </tspan>'
            f'<tspan font-size="13.5" fill="{gcol}" '
            f'font-weight="700">{esc(glyph)}</tspan>{ch}</text>')


def _ep_line(p, cx, cy, v, focus):
    """Строка-эпитет под именем. В своей главе (focus) — «эта глава»
    без псевдонима (dvory §0, правило 3); иначе — псевдоним курсивом."""
    if focus and v.get("name") == focus:
        p.append(f'<rect x="{cx - 12}" y="{cy - 12}" width="3.5" '
                 f'height="16" rx="1.5" fill="{ACCENT}"/>')
        p.append(txt(cx + 6, cy, "— герой этой главы", 13, ACCENT, "700"))
    else:
        p.append(txt(cx + 6, cy, f'«{v["ep"]}»', 13, SOFT, "400",
                     italic=True))


def card(x, y, w, qual, head, wife, vets, mutes, head_vet=None, focus=None):
    cx = x + 22
    cy = y + 32
    p = ['']
    p.append(txt(cx, cy, qual, 13, ACCENT, "700", ls="0.10em", font=SANS))
    cy += 33
    bsvg, bw = _badge(cx, cy, "ГЛАВА", ACCENT)
    p.append(bsvg)
    if head_vet:
        hf = bool(focus and head_vet.get("name") == focus)
        p.append(_vet_line(cx + bw + 11, cy, head_vet, 16, show_ch=not hf))
        cy += 21
        _ep_line(p, cx, cy, head_vet, focus)
        cy += 23
    else:
        p.append(txt(cx + bw + 11, cy, head, 15.5, INK, "700"))
        cy += 23
    p.append(txt(cx, cy, wife, 13.5, SOFT, "400"))
    cy += 17
    p.append(f'<line x1="{cx}" y1="{cy}" x2="{x + w - 22}" y2="{cy}" '
             f'stroke="{RULE}" stroke-width="1"/>')
    cy += 24
    if vets or mutes:
        dsvg, _ = _badge(cx, cy, "ДЕТИ", "#9a7d4e")
        p.append(dsvg)
        cy += 26
    for v in vets:
        if v.get("unknown"):
            p.append(
                f'<text x="{cx}" y="{cy}" font-family="{SERIF}">'
                f'<tspan font-size="15" fill="{SOFT}" font-weight="700">'
                f'{esc(v["name"])}</tspan>'
                f'<tspan font-size="13" fill="{MUTE}"> · '
                f'{esc(v["years"])}</tspan></text>')
            cy += 19
            p.append(txt(cx + 6, cy, v["note"], 12.5, MUTE, "400",
                         italic=True))
            cy += 24
            continue
        vf = bool(focus and v.get("name") == focus)
        p.append(_vet_line(cx, cy, v, show_ch=not vf))
        cy += 19
        _ep_line(p, cx, cy, v, focus)
        cy += 24
    if mutes:
        p.append(txt(cx, cy, mutes, 12.5, MUTE, "400"))
        cy += 20
    h = cy - y + 8
    p[0] = (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" '
            f'fill="#fffefb" stroke="{RULE}" stroke-width="1.2"/>')
    return "".join(p), (x, y, w, h)


def edge(a, b, label, primary=False):
    ax, ay = a
    bx, by = b
    mx, my = (ax + bx) / 2, (ay + by) / 2
    op = "0.85" if primary else "0.55"
    wdt = "2.4" if primary else "1.4"
    dash = ' stroke-dasharray="9 5"' if primary else ' stroke-dasharray="4 5"'
    lw = 9.5 * len(label) + 22
    return (f'<line x1="{ax}" y1="{ay}" x2="{bx}" y2="{by}" stroke="{ACCENT}" '
            f'stroke-width="{wdt}" opacity="{op}"{dash}/>'
            f'<rect x="{mx - lw/2}" y="{my - 15}" width="{lw}" height="30" '
            f'rx="5" fill="{PAPER}" stroke="rgba(122,90,50,0.5)" '
            f'stroke-width="1"/>'
            + txt(mx, my + 5, label, 13 if primary else 12,
                  ACCENT, "700" if primary else "600", "middle", ls="0.05em",
                  font=SANS))


def svg_doc(parts, h):
    body = "".join(parts)
    return (f'<svg viewBox="0 0 {W} {h}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<defs><linearGradient id="dvBg" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="{PAPER}"/>'
            f'<stop offset="1" stop-color="{PAPER2}"/></linearGradient></defs>'
            f'<rect width="{W}" height="{h}" fill="url(#dvBg)"/>'
            f'{body}</svg>')


def head_block(parts, yard_label, sub):
    parts.append(txt(W / 2, 56, yard_label, 20, INK, "700",
                     "middle", ls="0.06em", font=SANS))
    parts.append(txt(W / 2, 86, sub, 14, ACCENT, "700", "middle",
                     ls="0.16em", font=SANS))
    parts.append(f'<line x1="60" y1="104" x2="{W-60}" y2="104" '
                 f'stroke="{RULE}" stroke-width="1.4"/>')


def foot_note(parts, y, legend):
    parts.append(f'<line x1="60" y1="{y-22}" x2="{W-60}" y2="{y-22}" '
                 f'stroke="{RULE}" stroke-width="1"/>')
    parts.append(txt(W / 2, y, legend,
                     12, MUTE, "400", "middle", ls="0.03em", font=SANS))


def chap_foot(parts, y, legend):
    parts.append(f'<line x1="60" y1="{y-22}" x2="{W-60}" y2="{y-22}" '
                 f'stroke="{RULE}" stroke-width="1"/>')
    parts.append(txt(W / 2, y, legend,
                     11.5, MUTE, "400", "middle", ls="0.02em", font=SANS))


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


def write(fname, parts, h):
    """Write tree HTML + PNG preview.

    Why --headless=old: the new headless mode with a per-worker
    user-data-dir hangs on macOS after the screenshot and never exits;
    the legacy mode exits promptly (same pin as the original).
    """
    OUT.mkdir(parents=True, exist_ok=True)
    svg = svg_doc(parts, h)
    (OUT / f"{fname}.html").write_text(
        f'<!doctype html><meta charset="utf-8">'
        f'<style>html,body{{margin:0;background:{PAPER}}}'
        f'svg{{display:block;width:{W//2}px;height:auto}}</style>{svg}',
        encoding="utf-8")
    png = OUT / f"{fname}.png"
    if png.exists():
        png.unlink()
    chrome = _find_chrome()
    if not chrome:
        print(f"  HTML {png.with_suffix('.html')} "
              f"(Chrome not found — no PNG)", file=sys.stderr)
        return
    subprocess.run([chrome, "--headless=old", "--disable-gpu", "--no-sandbox",
                    "--hide-scrollbars", "--force-device-scale-factor=2",
                    f"--window-size={W//2},{int(h)//2}",
                    f"--screenshot={png}", f"file://{OUT/(fname+'.html')}"],
                   capture_output=True, text=True, timeout=60)
    print(f"  {'OK ' if png.exists() else 'FAIL'} {png}")


# ─── Minimal YAML reader ─────────────────────────────────────────────────────
# No PyYAML hard dependency. dvory.yml is a nested mapping with lists of
# mappings (2-space indent, `- ` list items, `key: value` scalars, inline
# `[a, b]` lists). This reader handles exactly that subset used by the
# schema above: it does NOT support anchors, flow-mappings `{a: 1}`,
# multi-line scalars, or tabs. Flow-mappings `{a: 1, b: x}` ARE supported
# (one level, used by `edges`). Falls back to PyYAML if importable.
def _split_top(s, sep):
    """Split on `sep` but not inside quotes/brackets/braces."""
    out, depth, in_s, q, buf = [], 0, False, "", []
    for ch in s:
        if in_s:
            buf.append(ch)
            if ch == q:
                in_s = False
            continue
        if ch in ("'", '"'):
            in_s, q = True, ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == sep and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


def _flow_map(v):
    """Parse a one-level flow-mapping `{ k: x, k2: "y" }` → dict."""
    inner = v[1:-1].strip()
    node = {}
    if not inner:
        return node
    for part in _split_top(inner, ","):
        if ":" not in part:
            continue
        k, _, val = part.partition(":")
        node[k.strip()] = _scalar(val.strip())
    return node


def _scalar(v):
    v = v.strip()
    if v == "":
        return ""
    if (v[0] == v[-1]) and v[0] in ("'", '"') and len(v) >= 2:
        return v[1:-1]
    if v.startswith("{") and v.endswith("}"):
        return _flow_map(v)
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [] if not inner else [_scalar(x) for x in _split_top(inner, ",")]
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~", "none"):
        return None
    return v


def _strip_comment(raw):
    in_s = False
    q = ""
    for i, ch in enumerate(raw):
        if ch in ("'", '"'):
            if not in_s:
                in_s, q = True, ch
            elif q == ch:
                in_s = False
        elif ch == "#" and not in_s:
            return raw[:i].rstrip()
    return raw.rstrip()


def _parse_yaml(text):
    """Indentation-based parser for the dvory.yml subset (see note above)."""
    lines = []
    for raw in text.split("\n"):
        s = _strip_comment(raw)
        if s.strip() == "":
            continue
        indent = len(s) - len(s.lstrip(" "))
        lines.append((indent, s.strip()))

    pos = [0]

    def parse_block(min_indent):
        if pos[0] >= len(lines):
            return None
        indent, body = lines[pos[0]]
        if body.startswith("- "):
            return parse_list(indent)
        return parse_map(indent)

    def parse_map(indent):
        node = {}
        while pos[0] < len(lines):
            ind, body = lines[pos[0]]
            if ind < indent or body.startswith("- "):
                break
            if ind > indent:                       # malformed; skip
                pos[0] += 1
                continue
            key, _, val = body.partition(":")
            key = key.strip()
            val = val.strip()
            pos[0] += 1
            if val == "":
                # nested map or list, or empty
                if pos[0] < len(lines) and lines[pos[0]][0] > indent:
                    node[key] = parse_block(ind + 1)
                else:
                    node[key] = {}
            else:
                node[key] = _scalar(val)
        return node

    def parse_list(indent):
        items = []
        while pos[0] < len(lines):
            ind, body = lines[pos[0]]
            if ind < indent or not body.startswith("- "):
                break
            if ind > indent:
                pos[0] += 1
                continue
            rest = body[2:].strip()
            if rest.startswith("{") and rest.endswith("}"):
                # flow-mapping list item: `- { k: v, … }`
                pos[0] += 1
                items.append(_flow_map(rest))
            elif ":" in rest and not (rest.startswith("'")
                                      or rest.startswith('"')):
                # inline first key of a block-mapping item: rewrite as a
                # map line at indent+2 then parse the mapping block.
                lines[pos[0]] = (ind + 2, rest)
                items.append(parse_map(ind + 2))
            elif rest == "":
                pos[0] += 1
                items.append(parse_block(ind + 2))
            else:
                pos[0] += 1
                items.append(_scalar(rest))
        return items

    result = parse_block(0)
    return result or {}


def load_dvory():
    if not DVORY_YML.exists():
        print(f"  ! {DVORY_YML} missing — nothing to render",
              file=sys.stderr)
        return {}
    text = DVORY_YML.read_text(encoding="utf-8")
    try:
        import yaml  # optional
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return _parse_yaml(text)


# ─── YAML dvor dict → engine card spec ───────────────────────────────────────
def _vet_spec(m):
    """Member/head_vet YAML dict → engine vet dict (name/years/st/ch/ep)."""
    if m.get("unknown"):
        return {"name": m.get("name", ""), "years": m.get("years", ""),
                "unknown": True, "note": m.get("note", "")}
    raw_st = str(m.get("status", "lived")).lower()
    return {
        "name": m.get("name", ""),
        "years": m.get("years", ""),
        "st": _STATUS.get(raw_st, "back"),
        "ch": m.get("chapter", ""),
        "ep": m.get("epithet", ""),
    }


def _dvor_spec(d):
    """YAML dvor dict → engine card(...) kwargs."""
    spec = {
        "qual": d.get("qual", ""),
        "head": d.get("head", ""),
        "wife": d.get("wife", ""),
        "mutes": d.get("mutes", ""),
        "vets": [_vet_spec(m) for m in d.get("members", []) or []],
    }
    if d.get("head_vet"):
        spec["head_vet"] = _vet_spec(d["head_vet"])
    return spec


def _find_dvor(branch, dvor_id):
    for d in branch.get("dvory", []) or []:
        if d.get("name") == dvor_id:
            return d
    return None


# ─── Branch tree compositions ────────────────────────────────────────────────
def col(specs, x, w, y0, gap=34):
    out, rects, cy = [], [], y0
    for s in specs:
        sv, r = card(x, cy, w, s["qual"], s.get("head", ""), s["wife"],
                      s.get("vets", []), s.get("mutes"),
                      head_vet=s.get("head_vet"))
        out.append(sv)
        rects.append(r)
        cy += r[3] + gap
    return out, rects


def topc(r):
    return (r[0] + r[2] / 2, r[1])


def botc(r):
    return (r[0] + r[2] / 2, r[1] + r[3])


def build_branch(branch, yard_label, foot_legend):
    """Render one branch's full tree.

    `layout` selects the composition (faithful to the two originals):
      - "prokhor": optional full-width root dvor on top, then exactly two
        child dvory side by side, "отец → сын" edges.
      - "anikita": two columns of three dvory, top brace connecting the
        two head dvory, plus per-column local edges from `edges`.
    Generic single-column fallback when layout is unset/unknown.
    """
    p = []
    layout = branch.get("layout", "")
    title = branch.get("title", "")
    head_block(p, yard_label, title)
    dvory = branch.get("dvory", []) or []
    slug = branch.get("slug", "branch")

    if layout == "prokhor":
        specs = [_dvor_spec(d) for d in dvory]
        croot = None
        root = branch.get("root")
        if root:
            croot, rroot = card(60, 140, W - 120, root.get("qual", ""),
                                 root.get("head", ""), root.get("wife", ""),
                                 [], root.get("mutes", ""))
            p.append(croot)
            y2 = rroot[1] + rroot[3] + 96
        else:
            y2 = 140
        cI, rI = card(60, y2, 430, specs[0]["qual"], "", specs[0]["wife"],
                      specs[0]["vets"], specs[0]["mutes"],
                      head_vet=specs[0].get("head_vet"))
        cS, rS = card(510, y2, 430, specs[1]["qual"], "", specs[1]["wife"],
                      specs[1]["vets"], specs[1]["mutes"],
                      head_vet=specs[1].get("head_vet"))
        p += [cI, cS]
        if root:
            p.append(edge(botc(rroot), (topc(rI)[0], rI[1]), "отец → сын"))
            p.append(edge(botc(rroot), (topc(rS)[0], rS[1]), "отец → сын"))
        h = max(rI[1] + rI[3], rS[1] + rS[3]) + 92
        foot_note(p, h - 30, foot_legend)
        write(f"{slug}_01", p, h)
        return

    if layout == "anikita":
        LX, RX, CW = 60, 510, 430
        YTOP = 188
        left = [_dvor_spec(d) for d in dvory[:3]]
        right = [_dvor_spec(d) for d in dvory[3:6]]
        sl, rl = col(left, LX, CW, YTOP, gap=30)
        sr, rr = col(right, RX, CW, YTOP, gap=30)
        p += sl + sr
        # верхняя скобка-связь двух главных дворов (общий предок)
        brace = branch.get("brace", {})
        msx, pax = LX + CW / 2, RX + CW / 2
        yb = 150
        for seg in (f'<line x1="{msx}" y1="{YTOP}" x2="{msx}" y2="{yb}"/>',
                    f'<line x1="{pax}" y1="{YTOP}" x2="{pax}" y2="{yb}"/>',
                    f'<line x1="{msx}" y1="{yb}" x2="{pax}" y2="{yb}"/>'):
            p.append(seg.replace("/>", f' stroke="{ACCENT}" stroke-width="1.5" '
                                  f'opacity="0.55" stroke-dasharray="4 5"/>'))
        lbl = brace.get("label", "")
        if lbl:
            lw = 9.5 * len(lbl) + 22
            p.append(f'<rect x="{(msx+pax)/2 - lw/2}" y="{yb-15}" width="{lw}" '
                     f'height="30" rx="5" fill="{PAPER}" '
                     f'stroke="rgba(122,90,50,0.5)" stroke-width="1"/>')
            p.append(txt((msx + pax) / 2, yb + 5, lbl, 12, ACCENT, "600",
                         "middle", ls="0.04em", font=SANS))
        # локальные связи: edges = [{col:"L"|"R", a:0, b:1, label:"…"}]
        for e in branch.get("edges", []) or []:
            rects = rl if e.get("col", "L") == "L" else rr
            ai, bi = int(e.get("a", 0)), int(e.get("b", 1))
            cw = CW
            p.append(edge((rects[ai][0] + cw / 2,
                           rects[ai][1] + rects[ai][3]),
                          (rects[bi][0] + cw / 2, rects[bi][1]),
                          e.get("label", "")))
        h = max(rl[-1][1] + rl[-1][3], rr[-1][1] + rr[-1][3]) + 92
        foot_note(p, h - 30, foot_legend)
        write(f"{slug}_01", p, h)
        return

    # generic single column
    specs = [_dvor_spec(d) for d in dvory]
    s, rects = col(specs, 60, W - 120, 140, gap=34)
    p += s
    for i in range(len(rects) - 1):
        p.append(edge(botc(rects[i]), (topc(rects[i + 1])[0],
                      rects[i + 1][1]), ""))
    h = (rects[-1][1] + rects[-1][3] if rects else 220) + 92
    foot_note(p, h - 30, foot_legend)
    write(f"{slug}_01", p, h)


def build_chapter_tree(slug, spec, focus, banner, foot_legend):
    """Мини-дерево двора в зачин главы → build/.out/trees/<slug>_00.html.

    Один двор-карточка во всю ширину; герой главы помечен полосой и без
    псевдонима (dvory §0). Присоединяется к главе как inline по
    маркеру <!-- tree --> (n==0, без разрыва страницы), сразу под H1."""
    p = []
    p.append(txt(W / 2, 50, "СЕМЬЯ ГЕРОЯ — К НАЧАЛУ ВОЙНЫ", 17, INK, "700",
                 "middle", ls="0.05em", font=SANS))
    p.append(txt(W / 2, 76, banner, 13, ACCENT, "700", "middle",
                 ls="0.16em", font=SANS))
    p.append(f'<line x1="60" y1="92" x2="{W-60}" y2="92" '
             f'stroke="{RULE}" stroke-width="1.4"/>')
    cv, r = card(60, 120, W - 120, spec["qual"], spec.get("head", ""),
                 spec["wife"], spec.get("vets", []), spec.get("mutes"),
                 head_vet=spec.get("head_vet"), focus=focus)
    p.append(cv)
    h = r[1] + r[3] + 84
    chap_foot(p, h - 30, foot_legend)
    write(f"{slug}_00", p, h)


# ─── Main ────────────────────────────────────────────────────────────────────
DEFAULT_FOOT = ("золотом — ушедшие на фронт  ·  † убит  ·  "
                "БВ без вести  ·  → вернулся  ·  возраст — к началу войны")
DEFAULT_CHAP_FOOT = ("│ — герой этой главы  ·  золотом — ушедшие на фронт  ·  "
                     "† убит  ·  БВ без вести  ·  → вернулся")


def main():
    ap = argparse.ArgumentParser(description="Render family yard trees.")
    ap.add_argument("--slug", default=None,
                    help="render only this chapter mini-tree (default: all)")
    args = ap.parse_args()

    data = load_dvory()
    if not data:
        return
    book = data.get("book", {}) or {}
    yard_label = book.get("yard_label", "ДВОРЫ")
    foot_legend = book.get("foot_legend", DEFAULT_FOOT)
    chap_legend = book.get("chap_foot_legend", DEFAULT_CHAP_FOOT)

    branches = {b.get("slug"): b for b in data.get("branches", []) or []}

    if not args.slug:
        for b in branches.values():
            build_branch(b, yard_label, foot_legend)

    chapter_trees = data.get("chapter_trees", {}) or {}
    for cslug, ct in chapter_trees.items():
        if args.slug and cslug != args.slug:
            continue
        br = branches.get(ct.get("branch"))
        if not br:
            print(f"  ! {cslug}: branch '{ct.get('branch')}' not found",
                  file=sys.stderr)
            continue
        dvor = _find_dvor(br, ct.get("dvor"))
        if not dvor:
            print(f"  ! {cslug}: dvor '{ct.get('dvor')}' not found in "
                  f"branch '{ct.get('branch')}'", file=sys.stderr)
            continue
        spec = _dvor_spec(dvor)
        build_chapter_tree(cslug, spec, ct.get("focus"),
                           ct.get("banner", ""), chap_legend)


if __name__ == "__main__":
    main()
