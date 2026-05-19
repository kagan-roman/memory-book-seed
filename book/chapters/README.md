# book/chapters/

Одна папка на человека (slug по `CLAUDE.md §4`). Файлы — контракт агентов
(см. `.claude/agents/README.md`):

| Файл | Кто пишет | Что |
|---|---|---|
| `facts.md` | `researcher` | таблица фактов по дате (Дата \| Событие \| Место \| Часть \| Источник) |
| `context.md` | `researcher` | голоса соседей по фронту, с атрибуцией |
| `maps.json` | `cartographer` | массив конфигов карта-листов (схема — `build/SCHEMA.md`) |
| `draft.md` | `chronicler` | глава. **ОДИН файл** — версии = git-история, не `draft-vN` |
| `review.md` | `reviewer` + `book-director` | вердикт фактов/стиля/повторов + ревью замысла + динамика |

Создаётся `/ingest`. Пустые папки в git держит `.gitkeep`. Папка `_TEMPLATE/`
ниже — пример структуры, в книгу не идёт.
