# Субагенты книги памяти

Восемь субагентов, управляемых основным Claude-оркестратором (слэш-команды в
`.claude/commands/`). Конкретные род/село/автор — в `book.config.yml`; правила
и замысел — в `CLAUDE.md`. Хардкода нет.

## Карта ролей

| Агент | Что делает | Когда |
|---|---|---|
| `book-director` | Замысел: outline, введение, заключение, врезки, реестр эпитетов, пост-ревью, проход на динамику | `/conceive`; после каждого `chronicler`; `/review` |
| `researcher` | Факты + голоса соседей (pamyat-naroda через chrome-devtools) | `/research`, на каждого, один раз; перезапуск чтобы докопать |
| `cartographer` | `chapters/<slug>/maps.json` → `build_maps.py` | `/maps`, на каждого; перезапуск при правке карт |
| `chronicler` | Пишет `chapters/<slug>/draft.md` | `/write` после researcher; снова по ревью |
| `reviewer` | Fact-check + style-check + повторы | `/review` на каждый draft |
| `typesetter` | Собирает `dist/book.pdf` (Vivliostyle) | `/typeset`, по явной команде |
| `wiki-curator` | Переносит факты в `wiki/` | `/wiki`, после стабилизации главы |
| `illustrator` | Документ. фото мест; контактный лист → гейт → вставка | `/illustrate`; PROPOSE → автор → APPLY |

## Артефакты

```
book/_master/   outline.md introduction.md conclusion.md
                interlude-*.md sources.md STYLE-RULES.md dvory.yml   ← book-director
book/chapters/<slug>/
  facts.md  context.md          ← researcher
  draft.md                      ← chronicler (ОДИН файл; версии = git)
  review.md                     ← reviewer + book-director (секциями)
  maps.json                     ← cartographer
book/illustration/
  candidates.md  <slug>.md  _contact/   ← illustrator (гейт автора)
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
