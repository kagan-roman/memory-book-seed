---
description: Состояние конвейера — что на какой стадии
---

Стадия **status**. Состояние читается **с диска**, не из памяти чата.

1. Прочитай `book.config.yml` (заполнен ли; режим; глубина).
2. Состояние источников: есть ли `sources/<pdf>`, сколько
   `sources/document/page_*.md`.
3. Родословная: есть ли `book/_master/dvory.yml` (заполнен реально, не
   плейсхолдеры), `wiki/families/*`, реестр эпитетов; зафиксированные
   конфликты дерева.
4. По `wiki/people/*` / `book/chapters/*/` построй таблицу людей и стадий:

| Человек (slug) | facts | context | docs | maps.json | draft | review | в wiki |

   Помечай: ✓ есть / — нет / ⚠ пробелы или `revise`. `docs` =
   `assets/documents/<slug>/`.
5. Мастер: есть ли `outline.md`, `introduction.md`, `conclusion.md`,
   врезки, `sources.md`; `EDIT-PASS.md` (сквозная редактура пройдена?).
6. Сборка: есть ли `assets/geo/clipped_*`, `build/.out/maps|trees/`,
   `dist/book.pdf` (дата/размер).
7. Иллюстрации: есть ли `candidates.md`, сколько строк отмечено автором.
8. Вычитка: есть ли `dist/book.annotated.pdf`, файлы
   `book/_master/PROOF-*.md` (сколько, разобраны ли — секции «Вычитка
   автора» в `review.md` глав).

Выведи компактную сводку и **предложи следующий разумный шаг** (какую команду
звать). Ничего не запускай и не пересобирай — только отчёт.
