# Git → Vercel + приватне сховище

Перевірено за офіційною документацією 18.10.2026. Це інструкція ручного деплою, не підтвердження вже виконаного розгортання. Репозиторій, bucket і deployment автоматично не створюються.

## 0. Що справді можна безкоштовно

| Варіант | Безкоштовна квота | Обмеження |
|---|---|---|
| Cloudflare R2 **Standard** | 10 GB-month, 1 млн Class A, 10 млн Class B на місяць; вихідний трафік без плати | Активація через checkout/платіжний профіль; перевищення платні |
| Backblaze B2 — **S3-сумісна альтернатива**, не R2 | Перші 10 ГБ; поточні A/B/C API-операції без плати; egress до 3× середнього збереженого обсягу | Почати можна без картки; обсяг, версії та трафік треба контролювати |

Джерела: [R2 pricing](https://developers.cloudflare.com/r2/pricing/), [R2 activation](https://developers.cloudflare.com/r2/get-started/), [платіжний профіль Cloudflare](https://developers.cloudflare.com/billing/get-started/create-billing-profile/), [B2 pricing](https://www.backblaze.com/cloud-storage/pricing), [B2 transaction pricing](https://www.backblaze.com/cloud-storage/transaction-pricing), [B2 без картки](https://www.backblaze.com/blog/?p=106414).

Для вимоги «без картки» обирай **B2**. Якщо потрібен саме продукт **Cloudflare R2**, використовуй розділ 3А і перевір checkout перед підтвердженням. Це не необмежений free-only bucket; жорстка гарантія рахунку $0 для R2 не підтверджена. Infrequent Access не підходить під зазначену безкоштовну квоту.

**Блокер бізнес-використання:** Vercel Hobby дозволений лише для особистих некомерційних проєктів. Генерація контенту для бізнесу не стає дозволеною на Hobby через малий трафік чи приватний Git. Для такого продакшену потрібен відповідний тариф або інша платформа. Нижче описана технічна процедура, а не обхід умов. Платний план тут не замовляється. [Умови Hobby](https://vercel.com/docs/plans/hobby).

## 1. Підготувати чисті джерела

Робочий проєкт на цьому ПК: `C:\Users\sakal\Desktop\HEX+PNG GENERATOR`.

Найпростіше: розпакуй **content-workshop-git.zip** у нову окрему папку, наприклад `C:\Users\sakal\Desktop\content-workshop-git`. У її корені мають бути `vercel.json`, `requirements.txt`, `package.json`, `api/`, `backend/`, `frontend/`, `assets/`.

ZIP містить джерела й необхідні шаблони, але не `.git`, `.env.local`, ключі, `node_modules`, `.venv`, локальну історію, результати генерації чи QA-скриншоти. `GIT-PACKAGE-MANIFEST.json` описує SHA256 байтів кожного включеного файла саме в архіві; подальша Git-нормалізація кінців рядків може змінити ці хеші. Вибіркова перевірка секретів не замінює перегляд перед публікацією. Використовуй **Private** repository: у комплекті є твої шаблони та SDS.

За потреби перевипустити ZIP з робочого проєкту:

```powershell
Set-Location -LiteralPath 'C:\Users\sakal\Desktop\HEX+PNG GENERATOR'
.\.venv\Scripts\python tools/prepare_git.py --check
.\.venv\Scripts\python tools/prepare_git.py --output tmp/releases/content-workshop-git-v2.zip
```

Існуючий архів скрипт не перезаписує. `--check` нічого не публікує і не перевіряє хмару.

## 2. Завантажити в GitHub

1. У своєму GitHub: **New repository → content-workshop → Private**. Не додавай README, license або `.gitignore` через GitHub — вони вже є у джерелах.
2. Відкрий PowerShell у **розпакованій чистій папці**, не в старому проєкті з локальними файлами.
3. Перевір авторство: `git config user.name` та `git config user.email`. Якщо порожньо — задай власне ім’я й адресу з GitHub, за потреби GitHub noreply. Не використовуй чужу чи вигадану identity.
4. Виконай команди, замінивши `YOUR_GITHUB_LOGIN`:

```powershell
git init
git branch -M main
git add .
git diff --cached --stat
git diff --cached --name-only
git commit -m "Prepare Content Workshop for Vercel"
git remote add origin https://github.com/YOUR_GITHUB_LOGIN/content-workshop.git
git push -u origin main
```

Перед commit у списку не повинно бути `.env.local`, токенів, приватних ключів, `tmp/`, `.venv/`, `node_modules/`. `.env.example` із порожніми секретами має бути. Якщо Git просить авторизацію — увійди у власний GitHub через браузер/Git Credential Manager; не вписуй PAT у URL remote і не надсилай його в чат.

Альтернатива без термінала: GitHub Desktop → додати розпаковану папку → створити repository → переглянути Changes → commit → Publish repository із ввімкненим **Keep this code private**. Не завантажуй ZIP одним файлом у GitHub: Vercel потрібні розпаковані джерела.

## 3А. Сховище Cloudflare R2

Виконуй лише якщо погоджуєшся з його платіжними умовами. Інакше переходь до 3Б.

1. Cloudflare Dashboard → **R2 Object Storage**. Якщо є checkout, уважно перевір суму й платіжні умови; за вимоги «жодних платіжних даних» зупинись і обери B2.
2. Створи bucket з унікальною назвою, наприклад `content-workshop-private-<суфікс>`, клас **Standard**. Регіон обери близько до майбутнього backend. Залиш bucket приватним: не вмикай `r2.dev` або публічний custom domain.
3. У керуванні R2 API tokens створи токен **Object Read & Write**, обмежений **лише цим bucket**. Backend не потребує Admin. CORS/lifecycle налаштовуються власником у Dashboard.
4. Збережи **Access Key ID**, **Secret Access Key** і **S3 API endpoint** в менеджері паролів. Потрібні саме S3 credentials, не звичайний Cloudflare API token і не Global API Key.
5. Endpoint копіюй із Dashboard: звичайний вигляд `https://ACCOUNT_ID.r2.cloudflarestorage.com`; для юрисдикційного bucket він може відрізнятися. Bucket name не дописувати до endpoint. `S3_REGION=auto`.

[Офіційне створення R2 credentials](https://developers.cloudflare.com/r2/api/tokens/), [S3-сумісність потрібних операцій](https://developers.cloudflare.com/r2/api/s3/api/).

## 3Б. Сховище Backblaze B2 без картки

1. Створи акаунт **B2 Cloud Storage**, не Computer Backup. Почни без додавання платіжної картки; якщо конкретний екран вимагає оплату — не підтверджуй її автоматично.
2. **Buckets → Create a Bucket**: унікальна назва, **Private**. Для простого видалення не вмикай Object Lock/retention. Запиши bucket name та його **S3 Endpoint**, наприклад `s3.eu-central-003.backblazeb2.com`.
3. **Application Keys → Add a New Application Key**: доступ до конкретного bucket, **Read and Write**. Потрібні list/read/write/delete files; не обмежуй доступ одним префіксом, бо сервіс використовує кілька. Не передавай master key застосунку. Доступ List All Bucket Names цьому коду не потрібний.
4. Запиши **keyID** → `S3_ACCESS_KEY_ID`, **applicationKey** → `S3_SECRET_ACCESS_KEY`.
5. `S3_ENDPOINT_URL=https://` + S3 Endpoint із панелі; `S3_REGION` — регіон із цього endpoint, наприклад `eu-central-003`. Не копіюй адресу завантаження `f00x.backblazeb2.com/file/...`.
6. Перевір **Caps & Alerts**. Увімкни сповіщення та доступні обмеження витрат. Застосування caps може тривати до 10 хвилин. Можливість задати саме `$0` у твоєму акаунті не перевірена; не вважай alerts жорстким фінансовим лімітом. Безкартковий старт не означає необмежену квоту. [Caps & Alerts](https://www.backblaze.com/docs/cloud-storage-create-and-manage-caps-and-alerts).

[Створення application key](https://www.backblaze.com/docs/cloud-storage-create-and-manage-app-keys), [S3 API і presigned upload](https://www.backblaze.com/docs/cloud-storage-s3-compatible-api).

## 4. Імпортувати Git у Vercel

1. Vercel → **Add New → Project → Import Git Repository**. Підключи свій GitHub і надай доступ лише до потрібного приватного repository.
2. Обери `content-workshop`. Для Hobby використовуй власний personal Git repository, не організацію. Умови бізнес-використання з розділу 0 все одно діють.
3. **Root Directory: `./`**, **Framework Preset: Other**. Не обирай `frontend/` і не перемикай на Vite/FastAPI preset: це один комбінований проєкт.
4. Node.js: **22.x**; Python **3.12** задає `.python-version`. Не оновлюй Python навмання: залежності зафіксовані під поточний runtime.
5. `vercel.json` уже задає:

| Поле | Значення |
|---|---|
| Install Command | `npm --prefix frontend ci` |
| Build Command | `npm --prefix frontend run build` |
| Output Directory | `frontend/dist` |
| Python entry point | `api/index.py` |
| Fluid Compute | `true` |
| Максимум одного запиту | `300` секунд |

Не додавай legacy `builds` і не прописуй `memory` у JSON: для Fluid пам’ять налаштовується через Dashboard. Це верхня межа одного запиту, не безкоштовний безліміт обчислень. [Конфігурація Vercel](https://vercel.com/docs/project-configuration/vercel-json), [пам’ять Fluid](https://vercel.com/docs/functions/configuring-functions/memory).

## 5. Додати environment variables і зробити перший Deploy

Перед Deploy розкрий **Environment Variables**. Якщо deployment вже створено — **Project → Settings → Environment Variables**, потім **Redeploy**.

| Змінна | Cloudflare R2 | Backblaze B2 |
|---|---|---|
| `APP_PASSWORD` | Власний випадковий пароль, щонайменше 16 символів | Так само |
| `SESSION_SECRET` | Інший випадковий секрет, щонайменше 32 символи | Так само |
| `S3_ENDPOINT_URL` | Скопійований R2 S3 endpoint з `https://` | Скопійований B2 S3 endpoint з `https://` |
| `S3_REGION` | `auto` | Наприклад `eu-central-003`, точне значення bucket |
| `S3_BUCKET` | Точна назва приватного bucket | Точна назва приватного bucket |
| `S3_ACCESS_KEY_ID` | Access Key ID R2 object token | B2 keyID |
| `S3_SECRET_ACCESS_KEY` | Secret Access Key R2 | B2 applicationKey |

Згенеруй пароль і секрет у менеджері паролів. Не копіюй сюди значення-приклади як справжні ключі. Жодна змінна не повинна мати `VITE_`: секрети належать лише серверу. Не задавай `VERCEL`, `LOCAL_STORAGE_DIR` чи шлях до локального диска.

Для першого запуску задай значення **Production**. Preview не повинен автоматично отримувати production-ключі: для нього використовуй окремий bucket або взагалі не налаштовуй робочий API. Після зміни env обов’язковий новий deployment.

Натисни **Deploy**. Запиши стабільний production domain виду `https://content-workshop-....vercel.app` із **Project → Domains**, не випадковий URL окремого deployment. Поки не налаштовано CORS, великі upload/preview/ZIP можуть не працювати.

## 6. Дозволити браузеру доступ: CORS

Заміни `https://YOUR-PROJECT.vercel.app` на **точний production origin**: схема + hostname, без кінцевого `/`, path чи query. Після додавання власного домену додай його окремо. Не використовуй `*` або `https://*.vercel.app` для origins.

### R2

Bucket → **Settings → CORS policy**. Встав масив із [STORAGE-CORS.json](STORAGE-CORS.json), замінивши origin, та збережи:

```json
[
  {
    "AllowedOrigins": ["https://YOUR-PROJECT.vercel.app"],
    "AllowedMethods": ["GET", "PUT", "HEAD"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag", "Content-Length", "Content-Type"],
    "MaxAgeSeconds": 3600
  }
]
```

Dashboard приймає саме масив. AWS SDK `put_bucket_cors` потребує обгортку `{"CORSRules": [...]}` — не плутай формати. OPTIONS обробляється сховищем, додавати його до AllowedMethods не треба. Публічний доступ до bucket для CORS не потрібний. [R2 CORS](https://developers.cloudflare.com/r2/buckets/cors/).

### B2

Не вмикай download-only preset у B2 UI. Custom JSON editor у звичайній панелі не підтверджений; для точного правила в комплекті є `tools/configure_b2_cors.py`. Він використовує офіційний S3 PutBucketCors: GET/PUT/HEAD, точні origins, потрібні headers. **`--apply` замінює S3 CORS тільки в названому bucket**. Виконуй для нового окремого bucket; на існуючому спочатку збережи старі правила. Native CORS, вже створені через UI, можуть вимагати окремого видалення власником — S3 API їх не редагує. [B2 CORS](https://www.backblaze.com/docs/cloud-storage-cross-origin-resource-sharing-rules), [PutBucketCors](https://www.backblaze.com/apidocs/s3-put-bucket-cors).

Потрібен короткочасний **setup key** із `readBuckets,writeBuckets`. Звичайний bucket-scoped runtime Read and Write key не слід розширювати. Master key не підтримується в S3 API; owner master дозволений лише для авторизації **Native B2 CLI**, щоб створити окремий standard setup key. За суперечливих обмежень bucket-scoped `writeBuckets` використовуємо одноразовий account-scoped ключ на 1 годину: він має ширші права керування bucket, тому **ніколи не додавай його у Vercel/Git**. [S3 app-key capabilities](https://www.backblaze.com/docs/cloud-storage-s3-compatible-app-keys).

У локальній папці проєкту встанови Python-залежності; якщо `.venv` вже є, пропусти її створення:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m pip install b2
.\.venv\Scripts\b2 account authorize
.\.venv\Scripts\b2 key create --duration 3600 workshop-cors-setup readBuckets,writeBuckets
```

`account authorize` запитає owner keyID і key інтерактивно. Не передавай їх аргументами команд. Якщо використовується master key, візьми наявний: створення нового master відкликає попередній і може зламати інші інтеграції. Отримані **нові** setup keyID і applicationKey з’являться локально один раз — збережи приватно, не додавай в логи/чат/Git.

Заміни 4 значення команди на свої; endpoint і region мають відповідати bucket:

```powershell
# Лише показує майбутню політику, без мережі:
.\.venv\Scripts\python tools/configure_b2_cors.py --bucket YOUR_BUCKET --endpoint https://s3.eu-central-003.backblazeb2.com --region eu-central-003 --origin https://YOUR-PROJECT.vercel.app

# Свідомо застосовує її; обидва setup credentials вводяться приховано:
.\.venv\Scripts\python tools/configure_b2_cors.py --bucket YOUR_BUCKET --endpoint https://s3.eu-central-003.backblazeb2.com --region eu-central-003 --origin https://YOUR-PROJECT.vercel.app --apply

# Відкликати setup key, використовуючи його keyID, та прибрати кеш owner-входу:
.\.venv\Scripts\b2 key delete SETUP_KEY_ID
.\.venv\Scripts\b2 account clear
```

Додатковий дозволений домен передавай ще одним `--origin`. Не використовуй master/runtime key у CORS helper. Якщо setup не завершився, все одно відклич setup key і очисти owner-кеш; створений сайт має використовувати тільки обмежений runtime key із розділу 3Б.

Ці CLI-синтаксиси перевірені за документацією: [authorize](https://b2-command-line-tool.readthedocs.io/en/master/subcommands/account_authorize.html), [create key](https://b2-command-line-tool.readthedocs.io/en/master/subcommands/key_create.html), [delete key](https://b2-command-line-tool.readthedocs.io/en/master/subcommands/key_delete.html), [clear local auth](https://b2-command-line-tool.readthedocs.io/en/master/subcommands/account_clear.html). Реальний запуск у твоєму акаунті не виконаний.

Передача паролів/ключів браузеру не потрібна: API підписує короткочасні URL. B2 helper налаштовує тільки production HTTPS origins; локальна Python-діагностика з розділу 8 CORS не потребує.

## 7. Налаштувати автоматичне очищення

Ці правила **видаляють результати за віком**. Для нового окремого bucket це очікувана поведінка. Не застосовуй їх до bucket з чужими даними. Важливі результати завантажуй окремо: історія не є резервною копією.

### R2

Bucket → **Settings → Object lifecycle rules**: створи enabled-правила expiration:

- prefix `temporary/`: 1 день;
- prefix `history/`: 30 днів.

[STORAGE-LIFECYCLE.json](STORAGE-LIFECYCLE.json) містить S3 API-формат цих правил; у UI заповнюй відповідні поля, не вставляй цей JSON у CORS. Не додавай загального expiration без prefix. `assets/`, `preferences/`, `library/` повинні зберігатися. [R2 lifecycle](https://developers.cloudflare.com/r2/buckets/object-lifecycles/).

### B2 — окрема політика через Native UI

B2 зберігає старі/приховані версії: звичайний DeleteObject не гарантує миттєвого звільнення їхнього місця. У **Buckets → Lifecycle Settings → Use custom lifecycle rules** налаштуй три правила, потім **Update Bucket**:

| Prefix | Days from uploading to hiding | Days from hiding to deleting |
|---|---|---|
| Порожній: усі файли | Не задавати / `null` | `1` |
| `temporary/` | `1` | `1` |
| `history/` | `30` | `1` |

Перше правило прибирає тільки старі/приховані версії, не актуальну бібліотеку. Не став `1` у uploading-to-hiding для порожнього prefix: це сховає всі шаблони й налаштування. Очищення відбувається не миттєво, додаткові дні враховуй у квоті. [B2 lifecycle](https://www.backblaze.com/docs/en/cloud-storage-lifecycle-rules).

Не застосовуй R2 lifecycle JSON до B2 без адаптації: [B2 S3 lifecycle](https://www.backblaze.com/apidocs/s3-put-lifecycle-configuration) має іншу семантику expiration/версій. Для цього сценарію використовуй один спосіб керування — Native UI.

## 8. Перевірити сховище і сайт

Необов’язкова локальна діагностика: встанови залежності за README, скопіюй `.env.example` у `.env.local`, заповни свої credentials. `.env.local` не потрапляє у Git.

```powershell
# Лише offline-перевірка env, без мережі чи записів:
.\.venv\Scripts\python tools/check_cloud_storage.py

# ЛИШЕ коли свідомо дозволяєш реальні операції у своєму bucket:
.\.venv\Scripts\python tools/check_cloud_storage.py --cloud
```

Другий режим створює невеликі тестові об’єкти тільки у власному `temporary/deploy-check/<uuid>/`, перевіряє signed PUT 6 MiB та базові S3-операції і намагається прибрати свої об’єкти. Може спожити квоту; на B2 старі версії дочищає lifecycle. Він **не перевіряє браузерний CORS або Vercel runtime**. Помилка cleanup означає, що тестові дані могли залишитися; не очищуй весь bucket навмання.

Обов’язковий release checklist на реальному домені:

1. Відкрити `/api/status`: конфігурація production готова, немає повідомлення про відсутні secrets/storage. `ready:true` перевіряє лише env, **не реальні права bucket**.
2. У приватному вікні спробувати генерацію без входу: доступ має бути закритий. Увійти своїм паролем.
3. Вибрати вбудований шаблон → згенерувати один PNG → відкрити preview → скачати файл/ZIP.
4. Завантажити власний валідний PNG **понад 4,5 МБ**, але до 25 MiB і 8 млн пікселів. Намалювати маску, згенерувати результат. Це перевіряє прямий upload, який не проходить через ліміт тіла Vercel Functions.
5. Перезавантажити сайт: референс після успішної генерації має лишитися в бібліотеці. Змінити й повторно перевірити збережений HEX у налаштуваннях.
6. Створити SDS, відкрити PDF. Створити пакет із двох PNG/SDS → перевірити групування в історії, ZIP пакета та LastWriteTime після розпакування.
7. Вийти й зайти знову/з іншого браузера: історія та налаштування повинні лишитися. Перевірити видалення **тестового власного** шаблону; не використовувати важливий оригінал для цієї перевірки.
8. Переглянути Vercel Runtime Logs та storage usage: немає 403/500, timeout або швидкого неконтрольованого накопичення версій.

Без цих пунктів deployment не вважати перевіреним. У поточній підготовці реальний bucket/Vercel/CORS не тестувався: credentials не надані, ресурси не створювалися.

## 9. Оновлення, витрати та обмеження

- Оновлення: тести → `git add` конкретних змінених файлів → commit → `git push origin main`. Git integration створює новий deployment; перевір його статус і smoke test. Rollback коду не відновлює видалені дані bucket.
- Зміна env вимагає Redeploy. Заміна `SESSION_SECRET` робить старі сесії/токени недійсними; це не видаляє самі файли. Ротацію storage keys завершуй відкликанням старого ключа після перевірки нового.
- Git/ZIP не мігрує локальні `tmp/storage`, історію чи власні шаблони. За потреби завантаж їх у вебверсію через UI. Вбудовані assets уже входять до джерел.
- Приватний Git не захищає сам сайт. Тут один пароль/одна спільна бібліотека, не мультитенантний сервіс. Окремого rate limiter для входу немає; потрібен довгий випадковий пароль і доступні засоби захисту Vercel. Не публікуй signed URLs або логи з query signatures.
- Вкладку генерації залишай відкритою: незалежної черги background workers немає. Один запит має serverless timeout; великі пакети не є однією необмеженою задачею.
- Слідкуй за **всіма** лімітами Vercel: compute, requests, transfer, bundle. Вхідний файл до 25 MiB, PNG до 8 млн пікселів, список до 200, ZIP у браузері до 250 MiB. Прямий upload оминає тільки 4,5 МБ payload API, а не решту квот. [Vercel function limits](https://vercel.com/docs/functions/limitations).
- Історія доступна 30 днів; для тимчасових файлів налаштовано expiration/hiding після 1 дня. Фізичне видалення пізніше, за циклом провайдера: у B2 після приховання ще має пройти строк deletion, а приховані версії продовжують займати квоту. Бібліотека/налаштування без строку. У сховищі можуть бути тимчасова й архівна копії, previews і старі версії: оцінюй фактичний usage, не тільки суму завантажених оригіналів. Робочий запас нижче 10 ГБ доцільний, але це рекомендація, не гарантований hard cap застосунку. [Строки очищення B2](https://www.backblaze.com/docs/cloud-storage-configure-and-manage-lifecycle-rules).
- PDF використовує PyMuPDF з AGPL/комерційною моделлю. Для закритого сервісу третім особам спочатку визнач відповідний спосіб ліцензування. [Умови PyMuPDF](https://pymupdf.readthedocs.io/en/latest/about.html).

## 10. Типові помилки

| Симптом | Що перевірити |
|---|---|
| UI відкривається, `/api/...` — 404 або HTML | Root `./`, preset Other, `api/index.py` у Git, rewrites із `vercel.json` |
| API закритий після Deploy | Усі 7 env задані для Production; пароль ≥16, session secret ≥32; зроблено Redeploy |
| Upload/ZIP: CORS error | Точний поточний origin у bucket; PUT/GET/HEAD; headers; не URL старого deployment. Після зміни перезапустити запит/оновити вкладку |
| `SignatureDoesNotMatch` | Правильні S3 key/secret, endpoint і region, без зайвих пробілів; B2 не `auto`; час ПК; Content-Type не змінювався після підпису |
| Upload працює, історія — ні | Дозволи ListObjectsV2, Get/Put/Copy/Delete саме цього bucket; не Read Only key |
| Файли зникли після доби | Чи не застосований TTL до всього bucket; не видаляти `library/`, `preferences/`, `assets/` |
| B2 usage не зменшується після видалення | Приховані/старі версії і native lifecycle; почекати виконання правила, перевірити версії |
| Timeout/500 під час генерації | Vercel runtime logs, вхідні ліміти, час одного файла, memory/bundle; не підвищувати тариф без свідомого рішення |
| ZIP без потрібного часу | Увімкнути LastWriteTime, скачати ZIP і розпакувати; браузер не може задати Windows mtime прямому PNG/PDF |

## Статус передачі

Підготовлені джерела, конфігурація, приватне S3/R2-підключення через env, чистий Git ZIP і ця інструкція. **Push у GitHub, створення bucket, оплата, прив’язка картки й реальний Vercel deployment цією підготовкою не виконані.**
