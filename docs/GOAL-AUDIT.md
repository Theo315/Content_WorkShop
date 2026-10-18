# Аудит мети та ТЗ — 2026-10-03

Актуальний handoff 2026-10-18: [джерела для Git та інструкція Vercel/R2/B2](DEPLOY-VERCEL.md). Новий запит — підготовка без фактичного push/deploy. Наведені нижче оцінки — попередній аудит реалізації. Cloud-реліз не виконаний; Hobby не підходить для бізнес-використання, R2 має платіжні умови.

Доповнення 2026-10-04: [LastWriteTime, пакетна історія, видалення всіх шаблонів, хвиля теми й компактний експорт](REQUEST-FIXES.md). Локальні перевірки пройдені; статус хмарного релізу не змінився.

## Результат

Локально два desktop-пайплайни об’єднані в один React + FastAPI сервіс, підготовлений до Vercel. Є серверна історія й постійні налаштування. Актуальний UI — радикальний графітово-білий мінімалізм із темною/світлою темами й анімованим перемикачем Фото/SDS; цей напрям замінив попередній Liquid Glass. Публічний хмарний реліз не виконаний.

## Відповідність

| Вимога | Статус | Доказ |
|---|---|---|
| HEX → PNG збережено | Виконано | Інтеграційні тести, реальна генерація Ford Kuga 2048×2048, ΔE, alpha, маски, ZIP |
| SDS збережено | Виконано | 41 сторінка, 24 title-зони, dates/protected text, UI-генерація |
| PDF find/replace | Виконано | Реальний UI: 2 `Seat → Ford` на page 1, інші сторінки не змінені |
| Маски / chroma / plate | Виконано | API + Canvas brush/eraser/zoom/plate rectangle; UI перевірений |
| Власні PNG/PDF | Виконано | Direct upload contract, size/signature checks, custom SDS reference |
| Batch/progress/cancel | Виконано | Послідовні браузерні запити, фактичний `completed/total`, stop after current |
| ZIP/report | Виконано | Перевірений browser download: 2 PNG + `generation-report.json` |
| Auth/security | Виконано | HttpOnly session, CSRF origin guard, signed tokens, production fail-closed |
| Актуальний мінімалістичний UI | Виконано | `minimal.css`, темна/світла теми, центральний switch, короткі анімації, reduced-motion; `docs/UI-REDESIGN.md` |
| History | Виконано | `/api/history`, immutable manifests + copies, search/filter/pagination, fresh URLs, 30 days |
| Налаштування і бібліотека машин | Виконано локально | Типова назва SDS/PDF, upload/delete машин, постійний HEX; 19 тестів, перевірка UI 360/1440 px |
| Responsive | Виконано | Фото/SDS: 360/768/1280/1600 у двох темах без горизонтального переповнення; налаштування/історія: 360/1280 у двох темах |
| Vercel publish | Не виконано | Немає доступу до Vercel project/account |
| Real S3/R2 cloud | Не виконано | Немає bucket credentials/domain CORS; SDK is stub-tested |

## Частково підтверджено

- Повний keyboard-only сценарій генерації/скачування і всі screenshot states з початкового ТЗ не проходилися вичерпно. Login focus trap, wrong password, mask modal focus, responsive states і ключові production paths перевірені.
- Історія не архівує input PNG/PDF/masks і не відновлює незавершений batch. Вона зберігає завершений output + preview; це навмисно дешевша модель без queue/database.
- Вихідний PDF має власний дефект на page 9 (текст за правим краєм); генератор його зберігає і не маскує під виправлений документ.

## Що потрібно для повного релізу

1. Створити Vercel project і додати production variables.
2. Створити приватний S3/R2 bucket, CORS, permissions і lifecycle за `docs/DEPLOY-VERCEL.md`: політики R2 та B2 різні.
3. Deploy з root repository, перевірити production login, один PNG, один SDS, custom upload >4.5 MB, маску, ZIP та History після нового входу.
4. Визначити ліцензійну модель PyMuPDF перед комерційним розповсюдженням.
