#!/usr/bin/env python3
"""Compile the memorial book to dist/book.pdf via Vivliostyle (CSS Paged
Media). Faithful merge of booklets/book-v2/build.py (the Vivliostyle
pipeline) + the chapter-order/metadata logic from the legacy
booklets/build_book.py — but with ALL family-specific data removed.

What is byte-faithful from book-v2/build.py:
  strip_frontmatter, extract_footnote_defs, refs_to_sup, pandoc_fragment
  (same flags), footnotes/sources sectioning, weave maps at <!-- map -->,
  inline_local_images (base64 data-URI), the
  `npx vivliostyle build … -o dist/book.pdf --timeout …` invocation.

What changed (de-hardcoded):
  - No `import bb` of a Kaganov-specific module. Chapter ORDER comes from
    book/_master/outline.md (a fenced ```book-plan block) or
    book/_master/book-plan.yml; fallback = introduction.md → chapters/*/
    draft.md sorted → conclusion.md → sources.md.
  - CHAPTER_META derived from each book/chapters/<slug>/draft.md YAML
    frontmatter + wiki/people/<slug>.md frontmatter (born/died/family).
    No hardcoded per-person dict.
  - Title/subtitle/footer/cover from book.config.yml → book.
  - Trees woven at a separate <!-- tree --> marker (n==0 inline mini-tree,
    no page break; named branch tree = full plate).

Usage:  python3 build/build_book.py
Prereq: cd build && npm ci   (installs @vivliostyle/cli into build/node_modules)
"""
import base64
import glob
import html as html_lib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
CONFIG = ROOT / "book.config.yml"
MASTER = ROOT / "book" / "_master"
CHAPTERS = ROOT / "book" / "chapters"
WIKI_PEOPLE = ROOT / "wiki" / "people"
MAPS_OUT = BUILD / ".out" / "maps"
TREES_OUT = BUILD / ".out" / "trees"
MANUSCRIPT = BUILD / ".out" / "manuscript.html"
THEME_CSS = BUILD / "theme.css"
PDF_OUT = ROOT / "dist" / "book.pdf"

FOOTNOTE_DEF_RE = re.compile(r"^\[\^([^\]]+)\]:\s*(.*)$")
HEADING_RE = re.compile(r"^#{1,6}\s")
MAP_MARKER_LINE_RE = re.compile(r"^\s*<!--\s*map\s*-->\s*$")
GLAVA_PREFIX_RE = re.compile(r"^\s*(?:Глава|Часть|Глава\s+\d+|Часть\s+[IVXLC]+)\.\s*")


# ─── Minimal YAML reader ─────────────────────────────────────────────────────
# No PyYAML hard dependency. Handles the subset used by book.config.yml,
# book-plan.yml and markdown frontmatter: 2-space-indent nested mappings,
# `- ` block lists, `key: value` scalars, inline `[a, b]` lists, one-level
# flow-mappings `{a: 1, b: x}`. NOT a general parser (no anchors,
# multi-line scalars, tabs). Falls back to PyYAML if importable.
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
        return ""
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


def parse_simple_yaml(text):
    """Recursive indentation parser (see note above)."""
    try:
        import yaml  # optional
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    lines = []
    for raw in text.split("\n"):
        s = _strip_comment(raw)
        if s.strip() == "":
            continue
        indent = len(s) - len(s.lstrip(" "))
        lines.append([indent, s.strip()])

    pos = [0]

    def parse_block(_):
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
            if ind > indent:
                pos[0] += 1
                continue
            key, _, val = body.partition(":")
            key = key.strip()
            val = val.strip()
            pos[0] += 1
            if val == "":
                if pos[0] < len(lines) and (
                        lines[pos[0]][0] > indent
                        or lines[pos[0]][1].startswith("- ")):
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
                pos[0] += 1
                items.append(_flow_map(rest))
            elif ":" in rest and not (rest.startswith("'")
                                      or rest.startswith('"')):
                lines[pos[0]] = [ind + 2, rest]
                items.append(parse_map(ind + 2))
            elif rest == "":
                pos[0] += 1
                items.append(parse_block(ind + 2))
            else:
                pos[0] += 1
                items.append(_scalar(rest))
        return items

    result = parse_block(0)
    return result if isinstance(result, dict) else {}


_CFG = parse_simple_yaml(CONFIG.read_text(encoding="utf-8")
                         if CONFIG.exists() else "")


def cfg_get(path, default=""):
    node = _CFG
    for p in path.split("."):
        if isinstance(node, dict) and p in node:
            node = node[p]
        else:
            return default
    return node if node not in (None, "") else default


def _book_footer():
    footer = cfg_get("book.footer", "")
    if footer:
        return footer
    t, s = cfg_get("book.title", ""), cfg_get("book.subtitle", "")
    if t and s:
        return f"{t} · {s}"
    return t or s or ""


BOOK_TITLE = cfg_get("book.title", "Книга памяти")
BOOK_SUBTITLE = cfg_get("book.subtitle", "")
BOOK_FOOTER = _book_footer()
COMPILER = cfg_get("author.name", "")
COMPILER_YEAR = (f"{COMPILER}, 2026" if COMPILER else "2026")
COLOPHON = cfg_get("book.colophon", "")


def _read_frontmatter(path):
    """Return (frontmatter_dict, body_text) for a markdown file."""
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                fm = parse_simple_yaml("\n".join(lines[1:i]))
                return fm, "\n".join(lines[i + 1:])
    return {}, text


# ─── Markdown препроцессинг (byte-faithful from book-v2/build.py) ────────────
def strip_frontmatter(text: str) -> str:
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                return "\n".join(lines[i + 1:])
    return text


def extract_footnote_defs(md: str):
    """Вынуть строки-определения сносок `[^N]: текст` (с продолжениями).

    Возвращает (тело_без_определений, [(label, markdown_текст), ...]) в
    порядке появления. Заодно срезаем хвостовой заголовок «## Источники»,
    если под ним только определения.
    """
    lines = md.split("\n")
    body, defs = [], []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = FOOTNOTE_DEF_RE.match(line)
        if m:
            label = m.group(1).strip()
            buf = [m.group(2).strip()]
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if (nxt.strip() == "" or FOOTNOTE_DEF_RE.match(nxt)
                        or HEADING_RE.match(nxt)
                        or MAP_MARKER_LINE_RE.match(nxt)):
                    break
                buf.append(nxt.strip())
                i += 1
            defs.append((label, " ".join(b for b in buf if b)))
            continue
        body.append(line)
        i += 1

    # Если есть определения — убрать пустой хвостовой заголовок «Источники»
    if defs:
        while body and body[-1].strip() == "":
            body.pop()
        if body and re.match(r"^#{1,6}\s*Источник", body[-1].strip()):
            body.pop()
    return "\n".join(body), defs


def refs_to_sup(md: str) -> str:
    """Инлайновые ссылки [^N] → надстрочный знак (raw-HTML, pandoc пропустит)."""
    return re.sub(
        r"\[\^([^\]]+)\]",
        lambda m: f'<sup class="footnote-ref">{html_lib.escape(m.group(1))}</sup>',
        md,
    )


def pandoc_fragment(md: str) -> str:
    """Markdown → HTML5-фрагмент. smart/auto_identifiers выключены,
    raw_html включён (нужно для <sup> и <!-- map -->/<!-- tree -->)."""
    proc = subprocess.run(
        ["pandoc",
         "-f", "markdown-smart-auto_identifiers+raw_html",
         "-t", "html5", "--wrap=none", "--no-highlight"],
        input=md.encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    return proc.stdout.decode("utf-8")


# ─── Карта-листы / деревья ───────────────────────────────────────────────────
_SVG_RE = re.compile(r"<svg\b.*?</svg>", re.S | re.I)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_WH_RE = re.compile(r'\s(?:width|height)="[^"]*"', re.I)
TREE_MARKER_RE = re.compile(r"<!--\s*tree(?::([^\s>]+))?\s*-->")


def _svg_from(path: Path):
    if not path.exists():
        print(f"  ! отсутствует: {path.name}", file=sys.stderr)
        return None, ""
    raw = path.read_text(encoding="utf-8")
    msvg = _SVG_RE.search(raw)
    if not msvg:
        print(f"  ! <svg> не найден в {path.name}", file=sys.stderr)
        return None, ""
    svg = msvg.group(0)
    open_tag_end = svg.index(">")
    svg = _WH_RE.sub("", svg[:open_tag_end]) + svg[open_tag_end:]
    cap = ""
    mt = _TITLE_RE.search(raw)
    if mt:
        cap = re.sub(r"^\s*Буклет\s*·\s*", "", mt.group(1).strip())
    return svg, cap


def map_figure(path: Path) -> str:
    svg, cap = _svg_from(path)
    if svg is None:
        return ""
    cap_html = (f'<figcaption>{html_lib.escape(cap)}</figcaption>'
                if cap else "")
    return (f'<figure class="map-plate">'
            f'<div class="frame">{svg}</div>{cap_html}</figure>')


def tree_figure(path: Path, inline: bool) -> str:
    """inline=True → мини-дерево двора в зачине (без разрыва страницы,
    .dvor-tree); inline=False → полноразмерный лист ветки (.map-plate)."""
    svg, cap = _svg_from(path)
    if svg is None:
        return ""
    if inline:
        return (f'<figure class="dvor-tree">'
                f'<div class="frame">{svg}</div></figure>')
    cap_html = (f'<figcaption>{html_lib.escape(cap)}</figcaption>'
                if cap else "")
    return (f'<figure class="map-plate">'
            f'<div class="frame">{svg}</div>{cap_html}</figure>')


def chapter_maps(slug: str):
    """build/.out/maps/<slug>_NN.html in numeric order (NN >= 1).
    `<slug>_00` is a tree (handled separately), so skipped here."""
    out = []
    for p in sorted(glob.glob(str(MAPS_OUT / f"{slug}_*.html"))):
        stem = Path(p).stem
        suffix = stem[len(slug) + 1:] if stem.startswith(slug + "_") else ""
        if suffix == "00":
            continue
        out.append(Path(p))
    # also a slug-named single map (e.g. conclusion overview) without _NN
    solo = MAPS_OUT / f"{slug}.html"
    if solo.exists():
        out.append(solo)
    return out


def weave_maps(fragment_html: str, slug: str) -> str:
    """Вставить карты на места <!-- map -->; лишние — в конец секции."""
    maps = chapter_maps(slug)
    if not maps:
        return fragment_html.replace("<!-- map -->", "")
    parts = fragment_html.split("<!-- map -->")
    figs = [map_figure(p) for p in maps]
    out = [parts[0]]
    mi = 0
    for seg in parts[1:]:
        if mi < len(figs):
            out.append(figs[mi])
            mi += 1
        out.append(seg)
    if mi < len(figs):                      # лишние карты — в конец
        out.append("".join(figs[mi:]))
    return "".join(out)


def weave_trees(fragment_html: str, slug: str) -> str:
    """Вставить дерево на место <!-- tree --> или <!-- tree:<id> -->.

    Plain `<!-- tree -->` в главе → инлайн мини-дерево двора героя
    build/.out/trees/<slug>_00.html (n==0, без разрыва страницы).
    `<!-- tree:<id> -->` → именованный лист build/.out/trees/<id>_01.html
    (полный лист ветки; в master/interlude перед группой глав), либо
    build/.out/trees/<id>_00.html если есть только _00.
    """
    def repl(m):
        named = m.group(1)
        if named:
            full = TREES_OUT / f"{named}_01.html"
            if full.exists():
                return tree_figure(full, inline=False)
            mini = TREES_OUT / f"{named}_00.html"
            if mini.exists():
                return tree_figure(mini, inline=True)
            print(f"  ! дерево '{named}' не найдено", file=sys.stderr)
            return ""
        mini = TREES_OUT / f"{slug}_00.html"
        return tree_figure(mini, inline=True) if mini.exists() else ""

    return TREE_MARKER_RE.sub(repl, fragment_html)


def footnotes_section(defs) -> str:
    if not defs:
        return ""
    items = []
    for label, text in defs:
        body = pandoc_fragment(text).strip()
        m = re.fullmatch(r"<p>(.*)</p>", body, re.S)
        if m:
            body = m.group(1)
        items.append(f'<li id="fn-{html_lib.escape(label)}">'
                      f'<span class="fl">{html_lib.escape(label)}.</span> '
                      f'{body}</li>')
    return ('<section class="footnotes"><ol>'
            + "".join(items) + "</ol></section>")


# ─── Сводный раздел «Источники» (в конце книги, по главам) ───────────────────
SRC_MARKER_RE = re.compile(r"^\s*<!--\s*src:\s*(\S+)\s*-->\s*$")


def _parse_def_block(lines):
    defs, rest, i = [], [], 0
    while i < len(lines):
        line = lines[i]
        m = FOOTNOTE_DEF_RE.match(line)
        if m:
            label = m.group(1).strip()
            buf = [m.group(2).strip()]
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if (nxt.strip() == "" or FOOTNOTE_DEF_RE.match(nxt)
                        or HEADING_RE.match(nxt)
                        or nxt.lstrip().startswith("<!--")):
                    break
                buf.append(nxt.strip())
                i += 1
            defs.append((label, " ".join(b for b in buf if b)))
            continue
        rest.append(line)
        i += 1
    return defs, rest


def _li_html(label, md_text):
    body = pandoc_fragment(md_text).strip()
    m = re.fullmatch(r"<p>(.*)</p>", body, re.S)
    if m:
        body = m.group(1)
    return (f'<li><span class="fl">{html_lib.escape(label)}.</span> '
            f'{body}</li>')


def render_sources_section(slug, eyebrow, md_text, chapter_order,
                           chapter_titles, chapter_defs):
    """Собрать единый раздел «Источники» в конце книги (byte-faithful)."""
    text = strip_frontmatter(md_text)
    lines = text.split("\n")

    preamble, blocks, cur_slug, buf = [], {}, None, []
    for line in lines:
        m = SRC_MARKER_RE.match(line)
        if m:
            if cur_slug is not None:
                blocks[cur_slug] = buf
            cur_slug, buf = m.group(1), []
            continue
        if cur_slug is None:
            preamble.append(line)
        else:
            buf.append(line)
    if cur_slug is not None:
        blocks[cur_slug] = buf

    pre_md = "\n".join(l for l in preamble
                       if "chapter-sources" not in l).strip()
    pre_frag = pandoc_fragment(pre_md) if pre_md else ""
    h1m = re.search(r"<h1[^>]*>(.*?)</h1>", pre_frag, re.S)
    if h1m:
        title = clean_title(re.sub(r"<[^>]+>", "", h1m.group(1)).strip())
        pre_frag = (pre_frag[:h1m.start()]
                    + f'<h1>{html_lib.escape(title)}</h1>'
                    + pre_frag[h1m.end():])
    else:
        title = "Источники"

    chap_html = []
    for cslug in chapter_order:
        if cslug in chapter_defs and chapter_defs[cslug]:
            defs, rest = chapter_defs[cslug], []
        else:
            defs, rest = _parse_def_block(blocks.get(cslug, []))
        rest_md = "\n".join(l for l in rest
                            if not l.lstrip().startswith("<!--")
                            and not HEADING_RE.match(l)).strip()
        if not defs and not rest_md:
            continue
        ctitle = chapter_titles.get(cslug, cslug)
        ol = (f'<ol class="src-list">'
              + "".join(_li_html(lbl, t) for lbl, t in defs)
              + "</ol>") if defs else ""
        extra = pandoc_fragment(rest_md) if rest_md else ""
        chap_html.append(
            f'<div class="src-chap">'
            f'<h2 class="src-h">{html_lib.escape(ctitle)}</h2>'
            f'{ol}{extra}</div>'
        )

    general_md = "\n".join(blocks.get("GENERAL", [])).strip()
    general_frag = pandoc_fragment(general_md) if general_md else ""

    section = (
        f'<section class="chapter cont sources" id="sec-{slug}">'
        f'<p class="eyebrow">{html_lib.escape(eyebrow)}</p>'
        f'{pre_frag}'
        f'<div class="footnotes consolidated">{"".join(chap_html)}</div>'
        f'{general_frag}'
        f'</section>'
    )
    return section, title


# ─── Сборка раздела ──────────────────────────────────────────────────────────
def clean_title(raw_title: str) -> str:
    return GLAVA_PREFIX_RE.sub("", raw_title).strip()


def render_section(slug, eyebrow, md_text, is_chapter):
    """Возвращает (html_раздела, заголовок, defs).

    Сноски-определения главы НЕ печатаются в теле — они уходят в сводный
    раздел «Источники». В тексте остаётся надстрочный номер.
    """
    md_text = strip_frontmatter(md_text)
    body_md, defs = extract_footnote_defs(md_text)
    body_md = refs_to_sup(body_md)
    frag = pandoc_fragment(body_md)

    h1m = re.search(r"<h1[^>]*>(.*?)</h1>", frag, re.S)
    if h1m:
        raw_title = re.sub(r"<[^>]+>", "", h1m.group(1)).strip()
        title = clean_title(raw_title)
        new_h1 = f'<h1>{html_lib.escape(title)}</h1>'
        frag = frag[:h1m.start()] + new_h1 + frag[h1m.end():]
    else:
        title = slug
        frag = f'<h1>{html_lib.escape(title)}</h1>' + frag

    frag = weave_maps(frag, slug)
    frag = weave_trees(frag, slug)

    cls = "chapter" if is_chapter else "chapter cont"
    section = (
        f'<section class="{cls}" id="sec-{slug}">'
        f'<p class="eyebrow">{html_lib.escape(eyebrow)}</p>'
        f'{frag}'
        f'</section>'
    )
    return section, title, defs


# ─── Встраивание локальных фото (data-URI) ───────────────────────────────────
_IMG_SRC_RE = re.compile(r'<img\s+([^>]*?)src="([^"]+)"([^>]*?)>', re.I)
_IMG_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".png": "image/png", ".gif": "image/gif"}


def inline_local_images(html_doc: str):
    """Вшить локальные растровые фото в manuscript как data-URI.

    Vivliostyle поднимает dev-сервер с корнем в каталоге manuscript.html,
    поэтому относительные ссылки выше корня сервера не отдаются (404).
    Кадры встраиваются прямо в manuscript.html на сборке. Пути ищутся:
    относительно каталога manuscript (.out/) и относительно корня репо.
    """
    n = [0]

    def repl(m):
        pre, src, post = m.group(1), m.group(2), m.group(3)
        if src.startswith(("data:", "http:", "https:")):
            return m.group(0)
        cand = [(MANUSCRIPT.parent / src).resolve(),
                (ROOT / src).resolve(),
                (ROOT / src.lstrip("/")).resolve()]
        path = next((p for p in cand if p.exists()), None)
        if path is None:
            print(f"  ! фото отсутствует: {src}", file=sys.stderr)
            return m.group(0)
        mime = _IMG_MIME.get(path.suffix.lower(), "image/jpeg")
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        n[0] += 1
        return f'<img {pre}src="data:{mime};base64,{b64}"{post}>'

    return _IMG_SRC_RE.sub(repl, html_doc), n[0]


# ─── Обложка / титул / задняя сторонка / оглавление ──────────────────────────
def prep_cover_image():
    """Cover frame from book.config.yml → book.cover_image (assets/cover/).

    Bakes the tone into pixels (grayscale+sepia+contrast+brightness) so
    Chromium (Vivliostyle) emits a plain JPEG instead of a huge alpha
    raster — byte-faithful to book-v2/build.py's prep_cover_image.
    Returns (front_src, back_src) relative to MANUSCRIPT (.out/) or "".
    """
    name = cfg_get("book.cover_image", "")
    if not name:
        return "", ""
    src = (ROOT / "assets" / "cover" / name)
    if not src.exists():
        print(f"  ! обложка: фото отсутствует {src}", file=sys.stderr)
        return "", ""
    front_out = MANUSCRIPT.parent / "_cover.jpg"
    back_out = MANUSCRIPT.parent / "_cover_back.jpg"
    try:
        from PIL import Image, ImageOps, ImageEnhance

        im = Image.open(src)
        im = ImageOps.exif_transpose(im).convert("RGB")
        target_ar = 148.0 / 210.0
        w, h = im.size
        if w / h > target_ar:
            new_w = int(round(h * target_ar))
            left = (w - new_w) // 2
            im = im.crop((left, 0, left + new_w, h))
        else:
            new_h = int(round(w / target_ar))
            top = (h - new_h) // 2
            im = im.crop((0, top, w, top + new_h))
        out_w = 1750
        im = im.resize((out_w, int(round(out_w / target_ar))),
                       Image.LANCZOS)
        gray = ImageOps.grayscale(im)
        r = gray.point(lambda v: min(255, int(v * 1.15542)))
        g = gray.point(lambda v: min(255, int(v * 1.08526)))
        b = gray.point(lambda v: min(255, int(v * 0.97354)))
        toned = Image.merge("RGB", (r, g, b))
        toned = ImageEnhance.Contrast(toned).enhance(1.05)

        def _save(img, path, brightness):
            img = ImageEnhance.Brightness(img).enhance(brightness)
            img.save(path, "JPEG", quality=86, optimize=True,
                     progressive=False, subsampling=2)

        _save(toned, front_out, 0.84)
        _save(toned, back_out, 0.70)
        return "_cover.jpg", "_cover_back.jpg"
    except Exception as e:
        print(f"  ! обложка: Pillow не сработал ({e}); копирую оригинал",
              file=sys.stderr)
        import shutil
        shutil.copy(src, front_out)
        shutil.copy(src, back_out)
        return "_cover.jpg", "_cover_back.jpg"


def title_page(cover_src: str) -> str:
    img = f'<img class="ph" src="{cover_src}" alt="">' if cover_src else ""
    title_html = html_lib.escape(BOOK_TITLE).replace(" ", "<br>", 1) \
        if " " in BOOK_TITLE else html_lib.escape(BOOK_TITLE)
    return (
        '<section class="bookcover">'
        + img +
        '<div class="veil"></div>'
        '<p class="eyebrow">Семейная память</p>'
        '<div class="blk">'
        f'<div class="title">{html_lib.escape(BOOK_TITLE)}</div>'
        '<div class="rule"></div>'
        f'<div class="sub">{html_lib.escape(BOOK_SUBTITLE)}</div>'
        '</div>'
        + (f'<div class="by">{html_lib.escape(COMPILER)}</div>'
           if COMPILER else "") +
        '</section>'
    )


def back_cover(cover_src: str, names) -> str:
    img = f'<img class="ph" src="{cover_src}" alt="">' if cover_src else ""
    name_spans = "".join(f"<span>{html_lib.escape(n)}</span>" for n in names)
    colo = COLOPHON or (
        f'{html_lib.escape(BOOK_TITLE)}. {html_lib.escape(COMPILER_YEAR)}.'
        '<br>По документам Центрального архива Министерства обороны.')
    return (
        '<section class="backcover">'
        + img +
        '<div class="veil"></div>'
        '<div class="inner">'
        + (f'<div class="names">{name_spans}</div>' if name_spans else "") +
        '</div>'
        f'<div class="colophon">{colo}</div>'
        '</section>'
    )


def toc(entries) -> str:
    rows, cur_part = [], None
    for e in entries:
        eb = e["eyebrow"]
        part = None
        first = eb.split("·")[0].strip() if eb else ""
        if first.startswith("ЧАСТЬ"):
            part = first
        if part and part != cur_part:
            rows.append(f'<li class="part">{html_lib.escape(part)}</li>')
            cur_part = part
        rows.append(
            f'<li><a href="#sec-{e["slug"]}">'
            f'<span class="t">{html_lib.escape(e["title"])}</span>'
            f'<span class="leader"></span></a></li>'
        )
    return ('<section class="toc"><h2>Содержание</h2><ol>'
            + "".join(rows) + "</ol></section>")


# ─── Book-plan: chapter order from outline.md (or fallback) ───────────────────
BOOK_PLAN_FENCE_RE = re.compile(
    r"```book-plan\s*\n(.*?)\n```", re.S)


def read_book_plan():
    """Ordered list of (kind, name): kind ∈ intro|interlude|chapter|
    conclusion|sources. Contract documented in build/SCHEMA.md.

    Source priority:
      1. book/_master/book-plan.yml  (a `plan:` list of the same strings)
      2. ```book-plan fenced block in book/_master/outline.md
      3. Fallback: intro → all chapters (slug-sorted) → conclusion → sources
    """
    plan = []

    bp_yml = MASTER / "book-plan.yml"
    if bp_yml.exists():
        # Read the `plan:` list lines directly as raw strings. Items may
        # contain ':' (e.g. `- interlude:smirnovo`), so the generic YAML
        # list parser is bypassed here on purpose.
        in_plan = False
        for raw in bp_yml.read_text(encoding="utf-8").split("\n"):
            line = _strip_comment(raw)
            stripped = line.strip()
            if not stripped:
                continue
            if not in_plan:
                if re.match(r"^plan\s*:", stripped):
                    in_plan = True
                continue
            if stripped.startswith("- "):
                tok = stripped[2:].strip().strip("'\"")
                it = _plan_item(tok)
                if it:
                    plan.append(it)
            elif not raw.startswith((" ", "\t", "-")):
                break  # next top-level key ends the plan list
        if plan:
            return plan

    outline = MASTER / "outline.md"
    if outline.exists():
        m = BOOK_PLAN_FENCE_RE.search(outline.read_text(encoding="utf-8"))
        if m:
            for raw in m.group(1).split("\n"):
                s = raw.strip()
                if not s or s.startswith("#"):
                    continue
                it = _plan_item(s)
                if it:
                    plan.append(it)
        if plan:
            return plan

    # Fallback
    plan = [("intro", "introduction")]
    for d in sorted(glob.glob(str(CHAPTERS / "*"))):
        slug = Path(d).name
        if (Path(d) / "draft.md").exists():
            plan.append(("chapter", slug))
    if (MASTER / "conclusion.md").exists():
        plan.append(("conclusion", "conclusion"))
    if (MASTER / "sources.md").exists():
        plan.append(("sources", "sources"))
    return plan


def _plan_item(s):
    """One book-plan line → (kind, name). Forms:
       intro | conclusion | sources | interlude:<topic> | chapter:<slug>
    """
    s = s.strip()
    if s in ("intro", "introduction"):
        return ("intro", "introduction")
    if s in ("conclusion", "outro"):
        return ("conclusion", "conclusion")
    if s == "sources":
        return ("sources", "sources")
    if s.startswith("interlude:"):
        return ("interlude", "interlude-" + s.split(":", 1)[1].strip())
    if s.startswith("interlude-"):
        return ("interlude", s)
    if s.startswith("chapter:"):
        return ("chapter", s.split(":", 1)[1].strip())
    # bare token = chapter slug
    return ("chapter", s)


# ─── Chapter / master metadata (derived, not hardcoded) ──────────────────────
def chapter_meta(slug):
    """Derive header metadata from draft.md frontmatter + wiki/people/<slug>.

    Recognised draft.md frontmatter keys (all optional):
      eyebrow, display_name, dates_label, parent_label, name_mark
    Falls back to wiki/people/<slug>.md frontmatter (name, born, died,
    family) when the draft does not state them.
    """
    fm, _ = _read_frontmatter(CHAPTERS / slug / "draft.md")
    pfm, _ = _read_frontmatter(WIKI_PEOPLE / f"{slug}.md")

    name = fm.get("display_name") or pfm.get("name") or slug
    born = str(pfm.get("born", "") or "")
    died = str(pfm.get("died", "") or "")
    family = pfm.get("family", "") or fm.get("family", "")

    dates = fm.get("dates_label", "")
    if not dates and (born or died):
        dates = f"{born} — {died}".strip(" —")

    name_mark = fm.get("name_mark", None)
    if name_mark is None:
        # default: † if the person has a death year, else empty
        name_mark = "†" if died else ""

    return {
        "eyebrow": fm.get("eyebrow", ""),
        "display_name": name,
        "dates_label": dates,
        "parent_label": fm.get("parent_label", family),
        "name_mark": name_mark,
    }


def master_meta(kind, name):
    fm, _ = _read_frontmatter(MASTER / f"{name}.md")
    label = {"intro": "ВВЕДЕНИЕ", "conclusion": "ЗАКЛЮЧЕНИЕ",
             "sources": "ИСТОЧНИКИ", "interlude": "ВРЕЗКА"}.get(kind, "")
    return {
        "eyebrow": fm.get("eyebrow", label),
        "display_name": fm.get("name", name),
    }


# ─── Главный проход ──────────────────────────────────────────────────────────
def build():
    plan = read_book_plan()
    sections, entries = [], []
    chapter_order, chapter_titles, chapter_defs = [], {}, {}
    chapter_names = []

    for kind, name in plan:
        if kind == "sources":
            meta = master_meta(kind, name)
            md = (MASTER / f"{name}.md").read_text(encoding="utf-8")
            sec, title = render_sources_section(
                name, meta["eyebrow"], md, chapter_order,
                chapter_titles, chapter_defs)
            sections.append(sec)
            entries.append({"slug": name, "title": title,
                            "eyebrow": meta["eyebrow"], "is_chapter": False})
            continue

        if kind in ("intro", "conclusion", "interlude"):
            meta = master_meta(kind, name)
            mp = MASTER / f"{name}.md"
            if not mp.exists():
                print(f"  ! master отсутствует: {name}.md", file=sys.stderr)
                continue
            md = mp.read_text(encoding="utf-8")
            sec, title, defs = render_section(
                name, meta["eyebrow"], md, is_chapter=False)
            sections.append(sec)
            if kind == "interlude":
                chapter_order.append(name)
                chapter_titles[name] = f"Врезка. {title}"
                if defs:
                    chapter_defs[name] = defs
            entries.append({"slug": name, "title": title,
                            "eyebrow": meta["eyebrow"], "is_chapter": False})
            print(f"  · {name}: «{title}»")
            continue

        # chapter
        slug = name
        meta = chapter_meta(slug)
        dp = CHAPTERS / slug / "draft.md"
        if not dp.exists():
            print(f"  ! глава без draft.md: {slug}", file=sys.stderr)
            continue
        md = dp.read_text(encoding="utf-8")
        sec, title, defs = render_section(
            slug, meta["eyebrow"], md, is_chapter=True)
        sections.append(sec)
        chapter_order.append(slug)
        chapter_titles[slug] = title
        chapter_names.append(meta["display_name"])
        if defs:
            chapter_defs[slug] = defs
        entries.append({"slug": slug, "title": title,
                        "eyebrow": meta["eyebrow"], "is_chapter": True})
        print(f"  · {slug}: «{title}» ({len(defs)} сносок)")

    MANUSCRIPT.parent.mkdir(parents=True, exist_ok=True)
    cover_src, cover_back_src = prep_cover_image()

    html_doc = (
        '<!DOCTYPE html>\n<html lang="ru">\n<head>\n'
        '<meta charset="utf-8">\n'
        f'<title>{html_lib.escape(BOOK_TITLE)}</title>\n'
        '<link rel="stylesheet" href="theme.css">\n'
        '</head>\n<body>\n'
        + title_page(cover_src)
        + toc(entries)
        + "\n".join(sections)
        + back_cover(cover_back_src, chapter_names)
        + '\n</body>\n</html>\n'
    )
    html_doc, n_img = inline_local_images(html_doc)
    MANUSCRIPT.write_text(html_doc, encoding="utf-8")

    # theme.css must sit next to manuscript.html (Vivliostyle dev-server
    # root). Copy build/theme.css into .out/ alongside the manuscript.
    import shutil
    if THEME_CSS.exists():
        # build/theme.css references fonts as ../assets/fonts/ (correct
        # relative to its own location). When relocated next to the
        # manuscript in .out/, rewrite that to fonts/ and copy the woff2
        # there. Also inject the per-book running-header title from config
        # (theme.css ships a neutral default).
        css = THEME_CSS.read_text(encoding="utf-8")
        css = css.replace('url("../assets/fonts/', 'url("fonts/')
        bt = cfg_get("book.title", "") or "Книга памяти"
        css += ('\n/* per-book override (build_book.py ← book.config.yml) */\n'
                f'body{{ string-set:booktitle "{bt}"; }}\n')
        (MANUSCRIPT.parent / "theme.css").write_text(css, encoding="utf-8")
        fonts_src = BUILD / "fonts"
        fonts_alt = ROOT / "assets" / "fonts"
        fonts_dst = MANUSCRIPT.parent / "fonts"
        src_dir = fonts_src if fonts_src.exists() else (
            fonts_alt if fonts_alt.exists() else None)
        if src_dir and not fonts_dst.exists():
            shutil.copytree(src_dir, fonts_dst)

    print(f"\nmanuscript.html: {len(html_doc)//1024} КБ, "
          f"{len(sections)} разделов, {n_img} фото встроено")

    PDF_OUT.parent.mkdir(parents=True, exist_ok=True)
    print("Vivliostyle: вёрстка PDF (может занять минуту)…")
    r = subprocess.run(
        # крупные inline-SVG карты — типографике нужен запас по времени.
        ["npx", "vivliostyle", "build", str(MANUSCRIPT),
         "-o", str(PDF_OUT), "--timeout", "1800"],
        cwd=BUILD, capture_output=True, text=True,
    )
    sys.stdout.write(r.stdout[-2000:] if r.stdout else "")
    sys.stderr.write(r.stderr[-3000:] if r.stderr else "")
    if r.returncode != 0:
        print(f"\nVivliostyle FAILED (rc={r.returncode})", file=sys.stderr)
        sys.exit(1)
    size = PDF_OUT.stat().st_size / 1024
    print(f"\n✓ {PDF_OUT}  ({size:.0f} КБ)")


if __name__ == "__main__":
    build()
