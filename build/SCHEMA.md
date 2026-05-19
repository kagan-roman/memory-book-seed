# Data contracts — `build/SCHEMA.md`

Three data contracts feed the rendering engines. The cartographer writes
`maps.json`, the book-director maintains `dvory.yml` + the `book-plan`
block in `outline.md`, the typesetter runs `build_book.py`. The Python
engines never carry person/place data — everything is in these files.

---

## 1. `book/chapters/<slug>/maps.json` — read by `build/build_maps.py`

A JSON **list** of slide-config objects (one per distinct operation, 1–3
typical). `build_maps.py` globs every `book/chapters/*/maps.json`, renders
each to `build/.out/maps/<filename>` + a `.png` preview.

```json
[
  {
    "filename": "ivan-petrov_01.html",
    "slide_indicator": "1 / 2",
    "display_name": "Иван Петрович <Фамилия>",
    "name_mark": "",
    "eyebrow": "<полк> · <дивизия> · <армия> · <фронт>",
    "map_title": "ОТ <A> К <B> · <ГОД>",
    "bbox": [25.5, 33.5, 57.4, 60.4],
    "waypoints": [
      {"n": 1, "name": "Порошки", "lon": 31.05, "lat": 57.95,
       "date": "1943", "label": "382 СД, оборона на Волхове"}
    ],
    "segments": [
      {"from_idx": 0, "to_idx": 1, "arrow": "combat", "dates": "01.1944"}
    ],
    "post_waypoints": [],
    "post_after": null,
    "panel_sections": [
      ["382 СД · ВОЛХОВ → НАРВА", [/* same shape as waypoints */], "TAN_HOME"]
    ],
    "panel_footer": "",
    "legend": [["combat", "Боевое наступление 382 СД"]],
    "extra_cities": [{"name": "Нарва", "lon": 28.19, "lat": 59.38}],
    "timeline_events": [
      {"kind": "point", "d": "1942-03-27", "label": "призван",
       "sub": "<место>, 18 лет", "color": "TAN_HOME", "lane": 0},
      {"kind": "span", "start": "1943-06-01", "end": "1944-01-13",
       "label": "382 СД · оборона", "color": "TAN_HOME"}
    ]
  }
]
```

### Key reference

| Key | Type | Notes |
|---|---|---|
| `filename` | str | output HTML name in `build/.out/maps/` |
| `slide_indicator` | str | "N / M" |
| `display_name` | str | header name (engine no longer hardcodes one) |
| `name_mark` | str | "†"/"БВ"/""; absent ⇒ defaults to "†" |
| `eyebrow` | str | regiment · division · army · front |
| `map_title` | str | coverage line in the header |
| `bbox` | `[lon_min, lon_max, lat_min, lat_max]` | JSON array → tuple internally |
| `waypoints` | list | `{n,name,lon,lat,date,label}`; `†` in `name` ⇒ death marker |
| `segments` | list | see arrow enum below |
| `post_waypoints` | list | green post-death chain `{name,lon,lat,date}` |
| `post_after` | int/null | waypoint index the green chain starts from |
| `zones` | list | `{polygon:[[lon,lat]…], label, label_pos:[lon,lat]}` hatched sectors |
| `panel_sections` | list | `[title, items, color]`; items same shape as waypoints |
| `panel_footer` | str | (kept for compatibility; not drawn in current key strip) |
| `legend` | list | `[kind, text]`; kind = arrow enum or `zone` |
| `extra_cities` | list | `{name,lon,lat}` — curated small towns, own set per slide |
| `timeline_events` | list | `point`/`span` (see below); omitted ⇒ rail + war boxes only |

### `segments[]` — friendly OR native keys

Friendly (cartographer): `from_idx`, `to_idx`, `arrow`, `dates`.
Native (engine): `from`, `to`, `type`, `label`. Either is accepted; an
optional `curve` (float) tweaks arc bend for `sortie`/`possible`.

### `arrow` enum (RULES.md §2)

| `arrow` | Meaning | Drawn as |
|---|---|---|
| `combat` | combat advance in unit | solid red arrow |
| `redeploy` | unit redeployment, no combat | red dashed arc + small head |
| `personal` | personal/admin travel | grey dashed, no head |
| `evac` | wounded evacuation | thin grey dashed + "эвак. ✚" |
| `post` | post-death continuation of his unit | green dashed (legend only) |
| `possible` | unestablished likely direction | faint thin red dashed |
| `sortie` | aviation sortie direction | long red dashed + head |
| `zone` | (legend only) probable-sector hatch swatch | hatched rect |

### `timeline_events[]`

`{"kind":"point","d":"YYYY-MM-DD","label":"…","sub":"…","color":"…","lane":0|1}`
or `{"kind":"span","start":"YYYY-MM-DD","end":"YYYY-MM-DD","label":"…","color":"…"}`.
Dates accept `"YYYY-MM-DD"` or `[Y,M,D]`. `color` accepts a hex string or
a name: `RED_COMBAT TAN_PERSONAL TAN_HOME DEATH POST_GREEN WOUND`.

### Special slide kinds

- `"kind": "narrative"` — text-only sheet: `eyebrow`, `body_subtitle`,
  `narrative` (list of paragraph strings), `slide_indicator`,
  `display_name`, `dates_label`, `parent_label`, `name_mark`,
  `timeline_events`.
- `"kind": "overview"` — book-conclusion roads map: `title`, `subtitle`,
  `bbox`, `origin {name,lon,lat}`, `victory {name,lon,lat}`,
  `origin_label/origin_sub`, `victory_label/victory_sub`,
  `anchor_cities [{name,lon,lat}]`, `roads`:
  `{n,name,branch,outcome,note,...}` where `outcome ∈
  died|returned|returned_berlin|unknown`; `died/returned` need
  `dest:[lon,lat]`, `place`, `lpos:L|T|R|B`, optional `curve`; `unknown`
  needs `tip:[lon,lat]`.

`--slug <slug>` renders only that chapter's `maps.json`. PNG previews are
sanity-checked: < 50 KB ⇒ render likely broken (flagged on stderr).
Front line is never drawn (RULES.md §2): there is no `front_line` key.

---

## 2. `book/_master/dvory.yml` — read by `build/build_trees.py`

Family-yard data for branch trees and per-chapter inline mini-trees. A
shipped, clearly-marked **template** lives at this path (placeholders
`<ФАМИЛИЯ>`, `<Имя>`, `<NN>` — not real people).

```yaml
book:
  yard_label: "ДВОРЫ <СЕЛО> · <ГОД>"   # banner over branch trees
  foot_legend: "…"                      # optional footer override

branches:
  - slug: branch-a            # → build/.out/trees/branch-a_01.html
    founder: "Имя (год)"
    title: "ВЕТКА <ИМЯ> · <ГОД>"
    layout: prokhor | anikita | (unset → single column)
    root: { qual, head, wife, mutes }   # prokhor: optional full-width root
    brace: { label: "общий прадед …" }  # anikita: top connector
    edges:                              # anikita: per-column local edges
      - { col: L|R, a: 0, b: 1, label: "родные братья" }
    dvory:
      - name: dvor-a1                   # dvor id (chapter_trees → this)
        qual: "КОРНЕВОЙ ДВОР · …"       # small accent caption
        head: "Отец Имя · NN г."        # OR head_vet (mutually exclusive)
        wife: "ж. Имя · NN г."
        head_vet: { name, years, status, chapter, epithet }
        members:
          - { name, years, status, chapter, epithet }
          - { name, years, unknown: true, note: "нет информации" }
        mutes: "+ N детей …"

chapter_trees:               # one per chapter slug → <slug>_00.html (inline)
  <slug>:
    branch: branch-a
    dvor: dvor-a1
    focus: "Имя Отчество"     # hero of this chapter (highlighted, no epithet)
    banner: "ВЕТКА … · ДВОР …"

epithets:                     # registry, kept in sync with outline.md
  <slug>: "Учитель"
```

`status` ∈ `lived|back` (→ "→ вернулся"), `died|kia` (→ "†"),
`missing|bv` (→ "БВ"), `unknown`. `layout`:

- **prokhor** — optional full-width `root` dvor on top, then exactly two
  child dvory side by side, "отец → сын" edges.
- **anikita** — two columns of three dvory, top `brace` connecting the
  two head dvory, plus the local `edges`.
- unset/other — single column, generic vertical edges.

Outputs: `build/.out/trees/<branch>_01.html` (+ `.png`),
`build/.out/trees/<slug>_00.html` (+ `.png`). `--slug <slug>` renders only
that chapter mini-tree.

---

## 3. `book/_master/outline.md` `book-plan` block — read by `build/build_book.py`

Chapter order + section list. Two accepted forms (priority order):

1. `book/_master/book-plan.yml` with a `plan:` list of the strings below.
2. A fenced ```book-plan block inside `book/_master/outline.md`:

````
```book-plan
intro
interlude:smirnovo
chapter:ivan-petrov
chapter:nikolay-petrov
interlude:bv
chapter:aleksey-petrov
conclusion
sources
```
````

Line grammar (one per line, `#` lines ignored):

| Token | Resolves to | File |
|---|---|---|
| `intro` / `introduction` | introduction | `book/_master/introduction.md` |
| `interlude:<topic>` | interlude | `book/_master/interlude-<topic>.md` |
| `chapter:<slug>` or bare `<slug>` | chapter | `book/chapters/<slug>/draft.md` |
| `conclusion` / `outro` | conclusion | `book/_master/conclusion.md` |
| `sources` | sources | `book/_master/sources.md` |

**Fallback** (no book-plan.yml, no fenced block): `introduction.md` →
every `book/chapters/*/draft.md` (slug-sorted) → `conclusion.md` →
`sources.md`.

### Markers inside markdown

- `<!-- map -->` — insert the next `build/.out/maps/<slug>_NN.html` here
  (extra maps appended at section end). `<slug>_00` is a tree, not a map.
- `<!-- tree -->` — in a chapter: inline mini-tree
  `build/.out/trees/<slug>_00.html` (n==0, **no page break**).
- `<!-- tree:<id> -->` — named full branch plate
  `build/.out/trees/<id>_01.html` (or `<id>_00.html` inline if only that
  exists). Use in an interlude before a group of chapters.
- `<!-- src: <slug> -->` / `<!-- src: GENERAL -->` — section markers
  inside `sources.md` for the consolidated end-of-book Sources.

### Derived metadata (no hardcoded per-person dict)

Header meta per chapter = `book/chapters/<slug>/draft.md` frontmatter
(`eyebrow`, `display_name`, `dates_label`, `parent_label`, `name_mark` —
all optional) overlaid on `wiki/people/<slug>.md` frontmatter (`name`,
`born`, `died`, `family`). `name_mark` defaults to `†` when a death year
is present, else empty. Title/subtitle/footer/cover/author come from
`book.config.yml → book` / `→ author`.
