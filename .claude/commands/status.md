---
description: Состояние конвейера — что на какой стадии
---

Стадия **status**. Состояние читается **с диска**, не из памяти чата.

1. Прочитай `book.config.yml` (заполнен ли; режим; глубина).
2. Состояние источников: есть ли `sources/<pdf>`, сколько
   `sources/document/page_*.md`.
3. По `wiki/people/*` / `book/chapters/*/` построй таблицу людей и стадий:

| Человек (slug) | facts | context | maps.json | draft | review (verdict) | в wiki |

   Помечай: ✓ есть / — нет / ⚠ пробелы или `revise`.
4. Мастер: есть ли `outline.md`, `introduction.md`, `conclusion.md`,
   врезки, `sources.md`, `dvory.yml`.
5. Сборка: есть ли `assets/geo/clipped_*`, `build/.out/maps|trees/`,
   `dist/book.pdf` (дата/размер).
6. Иллюстрации: есть ли `candidates.md`, сколько строк отмечено автором.

Выведи компактную сводку и **предложи следующий разумный шаг** (какую команду
звать). Ничего не запускай и не пересобирай — только отчёт.
