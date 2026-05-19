#!/usr/bin/env python3
"""Вытащить комментарии-аннотации из вычитанного PDF и сопоставить с исходным
markdown глав/мастер-кусков.

Сценарий вычитки:
  1. `/pdf` собрал `dist/book.pdf`.
  2. Человек открывает его в любой читалке, выделяет текст и оставляет
     заметки (highlight + sticky note / комментарий), сохраняет копию в
     `dist/book.annotated.pdf` (или передаёт путь аргументом).
  3. `python3 build/extract_annots.py [path.pdf]` →
     `book/_master/PROOF-<дата>.md`: по каждому комментарию — что выделено,
     что сказал рецензент, в каком файле/разделе это место.
  4. Стадия `/proof` разносит пункты в `book/chapters/<slug>/review.md`
     (секция «## Вычитка автора»), `chronicler` правит обычным revise-loop
     (точечно, без потери фактуры — CLAUDE.md §10/§3), `/pdf` пересобрать.

Зависимость: `pypdf` (чистый Python). Нет — скрипт честно скажет, как
поставить. Только для стадии /proof; основной конвейер от неё не зависит.
"""
import datetime
import glob
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PDF = ROOT / "dist" / "book.annotated.pdf"
OUT = ROOT / "book" / "_master" / f"PROOF-{datetime.date.today().isoformat()}.md"

# Где искать исходный текст под цитатой аннотации.
SOURCE_GLOBS = [
    ROOT / "book" / "chapters" / "*" / "draft.md",
    ROOT / "book" / "_master" / "introduction.md",
    ROOT / "book" / "_master" / "conclusion.md",
    ROOT / "book" / "_master" / "interlude-*.md",
]

MARKUP = {"/Highlight", "/Underline", "/StrikeOut", "/Squiggly"}


def _txt(v):
    """/Contents и /T бывают str, bytes, UTF-16 — привести к строке."""
    if v is None:
        return ""
    if isinstance(v, bytes):
        for enc in ("utf-16", "utf-8", "latin-1"):
            try:
                return v.decode(enc).strip("\x00 ").strip()
            except Exception:
                pass
        return ""
    return str(v).strip()


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def load_sources():
    """{path: (raw_text, normalized_text, [line_offsets])}."""
    src = {}
    for g in SOURCE_GLOBS:
        for p in sorted(glob.glob(str(g))):
            p = Path(p)
            raw = p.read_text(encoding="utf-8", errors="replace")
            src[p] = (raw, _norm(raw))
    return src


def locate(quote, sources):
    """Найти файл+раздел+строку по нормализованной цитате (>=12 симв.)."""
    q = _norm(quote)
    if len(q) < 12:
        return None
    for p, (raw, norm) in sources.items():
        if q in norm:
            # приблизительная строка: ищем первые ~6 слов в сыром тексте
            head = re.escape(" ".join(q.split()[:6]))
            m = re.search(head.replace(r"\ ", r"\s+"), raw)
            line = raw[: m.start()].count("\n") + 1 if m else 1
            sect = ""
            for hm in re.finditer(r"(?m)^#{1,3}\s+(.+)$", raw):
                if hm.start() <= (m.start() if m else 0):
                    sect = hm.group(1).strip()
                else:
                    break
            rel = p.relative_to(ROOT)
            return f"{rel}", line, sect
    return None


def quad_bbox(qp):
    xs = [qp[i] for i in range(0, len(qp), 2)]
    ys = [qp[i] for i in range(1, len(qp), 2)]
    return min(xs), min(ys), max(xs), max(ys)


def page_quote(page, rect):
    """Текст страницы внутри bbox аннотации (visitor по позиции)."""
    frags = []

    def visit(text, cm, tm, font, size):
        if not text.strip():
            return
        x, y = tm[4], tm[5]
        x0, y0, x1, y1 = rect
        if x0 - 2 <= x <= x1 + 2 and y0 - 4 <= y <= y1 + 4:
            frags.append(text)

    try:
        page.extract_text(visitor_text=visit)
    except Exception:
        return ""
    return _norm("".join(frags))


def main():
    try:
        from pypdf import PdfReader
    except ImportError:
        sys.exit("Нужен pypdf:  pip install pypdf   (только для /proof)")

    pdf = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PDF
    if not pdf.exists():
        sys.exit(f"Нет файла {pdf}\n"
                 f"Откройте dist/book.pdf в читалке, оставьте заметки и "
                 f"сохраните как {DEFAULT_PDF} (или укажите путь аргументом).")

    reader = PdfReader(str(pdf))
    sources = load_sources()
    items = []
    for pno, page in enumerate(reader.pages, 1):
        annots = page.get("/Annots") or []
        for a in annots:
            try:
                o = a.get_object()
            except Exception:
                continue
            sub = str(o.get("/Subtype", ""))
            comment = _txt(o.get("/Contents"))
            author = _txt(o.get("/T"))
            if sub not in MARKUP and not comment:
                continue  # не разметка и без текста — пропустить
            quote = ""
            if "/QuadPoints" in o:
                try:
                    quote = page_quote(page, quad_bbox(
                        [float(x) for x in o["/QuadPoints"]]))
                except Exception:
                    quote = ""
            if not quote and "/Rect" in o:
                try:
                    r = [float(x) for x in o["/Rect"]]
                    quote = page_quote(page, (r[0], r[1], r[2], r[3]))
                except Exception:
                    quote = ""
            loc = locate(quote, sources)
            items.append({
                "page": pno, "sub": sub.lstrip("/"), "author": author,
                "comment": comment, "quote": quote, "loc": loc,
            })

    if not items:
        sys.exit("В PDF не найдено аннотаций/комментариев.")

    # Сгруппировать по файлу (нераспознанные — отдельно).
    by_file = {}
    for it in items:
        key = it["loc"][0] if it["loc"] else "— не сопоставлено (вручную) —"
        by_file.setdefault(key, []).append(it)

    lines = [
        f"---\ntype: proof\nsource_pdf: {pdf.name}\n"
        f"date: {datetime.date.today().isoformat()}\n"
        f"items: {len(items)}\n---\n",
        f"# Вычитка автора — {datetime.date.today().isoformat()}\n",
        "Извлечено из аннотаций PDF. Стадия `/proof` разносит пункты в "
        "`review.md` глав; правит `chronicler` (точечно, без потери "
        "фактуры — CLAUDE.md §10/§3). Непонятный комментарий — спросить "
        "автора, не угадывать (§1).\n",
    ]
    for key in sorted(by_file):
        lines.append(f"\n## {key}\n")
        for it in by_file[key]:
            where = ""
            if it["loc"]:
                _, ln, sect = it["loc"]
                where = f" · стр.{it['page']} · строка ~{ln}" + (
                    f" · «{sect}»" if sect else "")
            else:
                where = f" · стр.{it['page']} PDF"
            lines.append(f"- **Комментарий**: {it['comment'] or '(выделение без текста)'}")
            if it["quote"]:
                q = it["quote"][:300] + ("…" if len(it["quote"]) > 300 else "")
                lines.append(f"  - выделено: «{q}»")
            lines.append(f"  - где: {it['sub']}{where}"
                         + (f" · {it['author']}" if it["author"] else ""))
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    n_loc = sum(1 for i in items if i["loc"])
    print(f"✓ {OUT.relative_to(ROOT)}: {len(items)} комментариев, "
          f"{n_loc} сопоставлено с текстом, "
          f"{len(items) - n_loc} — вручную.")


if __name__ == "__main__":
    main()
