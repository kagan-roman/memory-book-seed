# Субагенты книги памяти

Десять субагентов, управляемых основным Claude-оркестратором (слэш-команды в
`.claude/commands/`). Конкретные род/село/автор — в `book.config.yml`; правила
и замысел — в `CLAUDE.md`. Хардкода нет.

## Карта ролей

| Агент | Что делает | Когда |
|---|---|---|
| `genealogist` | Ветки, дворы (`dvory.yml`), `wiki/families/*`, реестр эпитетов, конфликты дерева | `/genealogy`, после `/ingest`; перезапуск при новых данных |
| `book-director` | Замысел: outline, введение, заключение, врезки, пост-ревью, динамика (берёт дерево+эпитеты от genealogist) | `/conceive`; после каждого `chronicler`; `/review` |
| `researcher` | Факты + голоса соседей + сканы документов (pamyat-naroda, chrome-devtools) | `/research`, на каждого; перезапуск чтобы докопать |
| `cartographer` | `chapters/<slug>/maps.json` → `build_maps.py` | `/maps`, на каждого; перезапуск при правке карт |
| `chronicler` | Пишет `chapters/<slug>/draft.md` | `/write` после researcher; снова по ревью/edit |
| `reviewer` | Fact-check + style-check + **внутриглавные** повторы + регресс | `/review` на каждый draft |
| `editor` | **Сквозная** редактура всей рукописи (межглавные повторы, согласованность) | `/edit`, когда все главы прошли `/review` |
| `typesetter` | Собирает `dist/book.pdf` (Vivliostyle) | `/pdf`, по явной команде |
| `wiki-curator` | Переносит факты в `wiki/` | `/wiki`, после стабилизации главы |
| `illustrator` | Документ. фото мест; контактный лист → гейт → вставка | `/illustrate`; PROPOSE → автор → APPLY |

Разграничение ревью: **внутри одной главы** (факты/стиль/повторы/регресс) —
`reviewer`; **замысел и динамика** — `book-director`; **между главами и по
всей книге** (повторы, согласованность фактов/эпитетов/заголовков) — `editor`.

## Артефакты

```
book/_master/   outline.md introduction.md conclusion.md
                interlude-*.md sources.md STYLE-RULES.md       ← book-director
                dvory.yml                                      ← genealogist
                EDIT-PASS.md                                   ← editor
wiki/families/*                 ← genealogist
book/chapters/<slug>/
  facts.md  context.md          ← researcher
  draft.md                      ← chronicler (ОДИН файл; версии = git)
  review.md                     ← reviewer + book-director (секциями)
  maps.json                     ← cartographer
book/illustration/
  candidates.md  <slug>.md  _contact/   ← illustrator (гейт автора)
assets/documents/<slug>/        ← researcher (сканы ЦАМО + MANIFEST)
build/.out/maps|trees/          ← build_maps.py / build_trees.py
assets/photos/ portraits/       ← illustrator (APPLY) / ingest
dist/book.pdf                   ← typesetter
```

## Контракт взаимодействия

1. **Каждый агент перечитывает входы с диска.** Никакого state в памяти
   оркестратора — только файлы. Reply — сводка для оркестратора, не источник
   истины.
2. **Полный артефакт — в файл; в reply — резюме 1–2 экрана.** Экономия
   context-окна.
3. **Frontmatter:** `facts.md`/`context.md` — без; `draft.md` — `revision,
   word_count, sources_used, status`; `review.md` — `verdict, blockers_count`.
4. **Не получилось — отметить явно, не выдумывать.** «По периоду X источников
   не найдено» — нормальная запись.
5. **17 железных правил `CLAUDE.md §2`** — над любым другим соображением.
6. Батчи субагентов — по `run.parallel_batch` (3–4, лимиты), один файл на
   агента (без write-конфликтов). Доверять выводу субагента только после
   проверки (класс ошибок «справка рядом с приказом → чужая часть»).

## Запуск

Из основного Claude (обычно через слэш-команду):
`Agent({subagent_type: "researcher", prompt: "<slug> <Имя> …"})`. Файлы
агентов рядом, подхватываются автоматически.
