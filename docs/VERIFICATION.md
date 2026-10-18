# Перевірка реалізації — 2026-10-03

## Актуальна перевірка Git-handoff — 2026-10-18

- Повний Python-набір: **173 passed, 3 skipped**. Пропущено native symlink tests через відсутній дозвіл Windows; окремі simulated Windows reparse-point tests пройшли. Є одне deprecation-попередження Starlette/httpx, без падіння тестів.
- Frontend: **19 passed**; TypeScript + production Vite build успішні.
- Source package: allowlist, SHA256 manifest, необхідні assets/lockfile/tests; секретні env, залежності, локальна історія та screenshots виключені. Перевірка credential patterns обмежена, не повний аудит секретів.
- Локальне SDK-підписання окремо перевірене для R2 `auto` та B2 `eu-central-003`, path-style та підписаних Content-Length/Content-Type; мережа не використовувалася.
- `check_cloud_storage.py`: default offline, явний `--cloud`, scoped cleanup і безпечний вивід помилок перевірені mocks. Offline запуск без credentials очікувано повідомив про відсутні поля; cloud-режим не запускався.
- `configure_b2_cors.py`: default preview без credentials/network, explicit apply, окремий setup key, обмеження target/origins, CORS readback і safe errors перевірені 33 тестами. Фактичного застосування до bucket не було.
- `vercel.json`: Fluid Compute, 300 секунд, без несумісного `memory`. Інструкція й офіційні обмеження тарифів: [DEPLOY-VERCEL.md](DEPLOY-VERCEL.md).
- **Git push, реальний bucket, Vercel build/runtime і browser CORS не виконані/не перевірені.** Це не production acceptance. Нижче збережено попередні локальні UI-перевірки.

Актуальні доповнення 2026-10-04: [сім виправлень, 40 backend + 3 timestamp-тести та реальні ZIP/Windows дати](REQUEST-FIXES.md). Нижче — попередній контрольний прохід.

## Виконано локально

- `pytest -q`: **19 passed**. Є попередження про майбутню зміну HTTP-клієнта Starlette TestClient; тестових помилок немає.
- `npm run build`: TypeScript і production Vite build успішні.
- Налаштування: нова сесія зберігає типову назву; PDF має постійну копію й справді використовується генерацією (перевірено metadata marker). Несумісний PDF не замінює поточний. Типовий PDF можна повернути до вбудованого.
- Бібліотека машин: власний PNG переживає видалення temporary upload та застаріння токена через 2 доби; повторне відкриття дає свіжий токен. Render і chroma mask використовують збережений HEX без введення його в запиті. Маски перевіряються на розміри; власні шаблони видаляються разом із PNG/масками, історичний output лишається.
- S3 SDK-контракт налаштувань: відсутній/існуючий JSON, запис preferences, durable library upload і scoped delete перевірені через Stubber, без мережі.
- Налаштування через реальний UI: збереження типової назви і HEX, нормалізація пробілів/регістру, автоматична підстановка HEX після вибору машини. Семантичні значення вбудованих налаштувань не змінені. Viewport 360/1440 px без горизонтального переповнення.
- `npm --prefix frontend audit`: **0 vulnerabilities** на момент перевірки.
- Через реальний браузер: вибір Ford Kuga, відкриття/застосування маски кузова, генерація `436 #4D778A` і `644 #A1382C`. Перший результат: ΔE кузова 0,40, маркера 0,40.
- ZIP справді збережений браузером; перевірено вміст: два PNG по ~5,65 МБ та `generation-report.json` у структурі `Asturcolor/Ford Kuga/`.
- SDS через UI: Seat / LB9A CANDY WHITE, 41 сторінка, перевірено 24 зони назв.
- PDF-редактор через UI: upload, пошук Seat лише на сторінці 1, два збіги, заміна на Ford, скачування. У завантаженому PDF: 41 сторінка, на першій Ford ×2 і Seat ×0, на другій Seat ×1.
- Результати PNG не втрачаються при переході в SDS і назад.
- Історія через браузер: згенерований PNG залишився після перезавантаження сторінки, знайшовся пошуком, відкрився у preview і повторно скачався; SDS відобразився у фільтрі `SDS`. Backend-тести також перевіряють пагінацію, 30-денне завершення, повторні signed URLs, auth та S3 `CopyObject`/`ListObjectsV2` контракти.
- PNG і PDF-редактор: ширина viewport 360 px, ширина документа дорівнює доступній ширині без горизонтального переповнення. PDF-редактор також перевірений на 1440 px. Це не повний accessibility-аудит усіх станів.
- Попередній темний Liquid Glass UI був перевірений на desktop/mobile; login focus trap і помилка неправильного пароля перевірені в цій ітерації. Його замінив актуальний мінімалістичний dark/light UI; свіжі перевірки й screenshots наведені у `UI-REDESIGN.md`.
- Візуально оглянуто контрольні PDF-сторінки 1/9/24/30. На сторінці 9 є дефект оригінального еталона: вихід тексту за правий край. Піксельна рівність основної області з еталоном підтверджена; дефект не виправлявся.

## Не перевірено в production

Деплой Vercel, cold start, RAM/час функції на Linux у хмарі, реальний S3/R2 і CORS домену, production login та прямий великий upload. Для цього потрібні підключені Vercel і bucket. S3 SDK-тести використовують підписання та stub-відповіді, а не живе сховище.

Конфігурацію routing перевірено офіційним `@vercel/routing-utils`; Python dependency discovery — `@vercel/python-analysis`. Це перевірка конфігурації, не доказ успішного хмарного запуску.
