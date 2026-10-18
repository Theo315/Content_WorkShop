# API contract

All endpoints use `/api`. JSON requests/responses. Errors: `{detail: string}` with non-2xx status. Cookies use `credentials: 'same-origin'`. No fake jobs: each render request makes one file; the browser sequentially processes a batch and combines downloads using JSZip.

## Session

- `GET /api/status` → `{authenticated: boolean, auth_required: boolean, storage: 'local'|'s3'|'unconfigured', ready: boolean, max_upload_bytes: number}`.
- `POST /api/session` `{password: string}` → sets HttpOnly session cookie, `{ok:true}`.
- `DELETE /api/session` → logout.

## Files

- `POST /api/files/upload` `{filename, content_type, size}` → `{upload_url, method:'PUT', headers: Record<string,string>, file: FileRef}`. PUT the raw File/Blob to `upload_url` with given headers; no multipart. Then send `file.token` to render endpoints. Local and S3 storage share this contract; large files never pass through a Vercel function in S3 mode.
- `FileRef = {token:string, name:string, url:string, size:number}`. `url` is suitable for image/PDF preview and fetch→Blob→download. Remote URLs expire after one hour; download results promptly.
- `DELETE /api/files` `{token:string}` → `{ok:true}` for temporary uploads/results.
- `POST /api/files/refresh` `{token:string}` → `{file:FileRef}` renews a signed download URL; the original file token must still be valid. Download and ZIP clients call this automatically.

## History

- `GET /api/history/batches?limit=30&cursor=...` → `{items:HistoryBatch[],next_cursor:string|null,retention_days:30}`. Limit 1–50; the UI requests 20. Pagination counts complete groups, never splits one batch across pages. Newest groups first; treat the returned cursor as opaque. Requires owner session.
- `GET /api/history/batches/{id}` → `HistoryBatch` with all completed members and fresh file/preview URLs. A new batch ID is a canonical UUID; legacy standalone batches use the original history entry ID. 400 malformed ID, 404 missing new batch, 410 expired/empty batch.
- `HistoryBatch = {id,kind:'png'|'sds'|'edit',label,created_at:number,expires_at:number,count:number,expected_count:number,partial:boolean,total_size:number,search_text:string,items:HistoryEntry[]}`. `count` and bytes count completed files, `expected_count` is the requested list size, and `partial` indicates fewer completed files. Listing returns at most the first 3 items; fetch detail to download the entire pack. `search_text` includes bounded text for all members, including files not in the first 3 previews.
- `GET /api/history?limit=30&cursor=...` → `{items:HistoryEntry[],next_cursor:string|null,retention_days:30}`. Backwards-compatible per-file listing, limit 1–50.
- `GET /api/history/{id}` → fresh `HistoryEntry` with current signed URLs. 404 missing, 410 expired.
- `HistoryEntry = {id,created_at:number,expires_at:number,kind:'png'|'sds'|'edit',label,detail,file:FileRef,preview:FileRef|null,metadata,warnings,batch_id?,batch_index?,batch_size?,zip_path?,last_written_at?}`. Created/expiry timestamps are Unix seconds; `last_written_at` is ISO datetime, a separate chosen file date.
- Successful PNG/SDS/edit responses additionally return `history_id` and supplied batch/ZIP/date metadata. Saving is automatic on the backend. Result and first preview are archived for 30 days. Input files are not part of the historical record; successfully used custom PNG references are separately saved in the durable template library. No historical token is needed to reopen the result.
- Grouping uses explicit `batch_id` only. Historical records without it remain standalone batches, even if their timestamps match; old list membership cannot be reconstructed reliably. A cancelled/failed list retains its completed members, without recording failed jobs as successes.
- Search/type filters apply to loaded batches; search checks all their member names/codes, not just the 3 previews. Older pages load explicitly. Download the full detail response via the same ZIP exporter used after generation.
- Immutable member objects contain cumulative summary snapshots. Listing validates exact member ID sets and reads one snapshot per group on the normal sequential path. Concurrent/stale snapshots fall back to authoritative manifests; S3 detail/fallback reads use at most 8 workers. These contracts are locally tested; real S3/Vercel deployment is not verified.

## Common batch and file-date metadata

`POST /api/png/render`, `/api/sds/generate` and `/api/sds/edit` accept these optional fields in their existing JSON request:

- `batch_id:string` — UUID shared by every request from one list. Generate a new UUID for each run.
- `batch_index:number` — original zero-based position, 0–199. Requires `batch_id`; defaults to 0 when only the ID is sent.
- `batch_size:number` — requested list size, 1–200; requires `batch_id` and `batch_index < batch_size`. Defaults to 1 with an ID but no size. Reusing an existing batch ID for another kind/size returns 409.
- `zip_path:string` — safe relative ZIP path, at most 1000 characters. Absolute paths, traversal, backslashes, empty/dot segments, control characters and Windows-invalid punctuation are rejected.
- `last_written_at:string` — ISO datetime with explicit timezone, years 1980–2107. Naive dates are rejected; the backend normalizes the ISO representation. If omitted, history records actual generation time without changing retention or filesystem mtime.

The UI plans dates once before the run from a Kyiv wall-clock start (`Europe/Kyiv`) and a minute step or random step range, then sends an ISO instant for each file. JSZip writes a timezone-less DOS timestamp corresponding to the selected Kyiv wall time. ZIP dates have **2-second precision** and year bounds 1980–2107. The report also includes original `last_written_at` and `timestamp_timezone:'Europe/Kyiv'`. Direct browser downloads cannot set Windows LastWriteTime/CreationTime: a dated single-file download is wrapped in ZIP. The ZIP container itself gets a browser/OS download date.

## Preferences and durable template library

- `GET /api/settings` → `{sds:SDSReference,templates:Template[],deleted_templates:Array<{id,name}>}`. Authenticated; refreshed signed URLs.
- `SDSReference = {file:FileRef|null,available:boolean,pages:41|0,custom:boolean,title_template:string,description:string}`. No active PDF means `file:null`, `available:false`, `pages:0`.
- `PUT /api/settings/sds/title` `{title_template:string}` → current SDS reference. Requires a supported placeholder and a valid sample title.
- `PUT /api/settings/sds/reference` `{token:string}` → current SDS reference. Validates the original 41-page structure before saving a durable copy. Invalid uploads leave the previous default unchanged.
- `DELETE /api/settings/sds/reference` → removes the active default PDF, including the builtin. Returns unavailable `SDSReference`; title preference remains unchanged. The builtin packaged file is not physically deleted; a previous custom durable copy is deleted. There is no implicit fallback after removal.
- `POST /api/settings/sds/reference/restore` → restores builtin `SDSReference`; clears the disabled flag and deletes any old custom durable copy. Title preference remains unchanged.
- `POST /api/settings/templates` `{name,image_token,car_mask_token?,marker_mask_token?,config:TemplateConfig}` → `Template`. Up to 100 user templates. PNG/mask dimensions validated; requires body mask or key HEX. PNG and masks get durable private copies.
- `PUT /api/settings/templates/{id}/key` `{key_color:string|null}` → `{ok:true}`. Applies to bundled and user templates; validates HEX. Cannot clear key without a body mask.
- `DELETE /api/settings/templates/{id}` → `{ok:true,restorable?:true}`. User templates: physically delete their durable source PNG/masks. Builtin templates: save a hidden ID in preferences; remove from the catalog and reject render/mask by ID, without deleting packaged assets. Historical output copies are unaffected.
- `POST /api/settings/templates/{id}/restore` → `{ok:true}`. Restores a hidden builtin template, including its saved HEX override. User templates cannot be restored by this endpoint.
- `Template` now includes `builtin:boolean`; `car_mask` and `marker_mask` may be null. PNG catalog includes user templates. Render/mask operations resolve saved templates by ID and use their saved key/config/masks by default. Explicit request values override defaults; omitted config fields do not clear saved values.
- `GET /api/sds/reference` returns the saved default PDF/title, the enabled builtin, or unavailable `SDSReference`. `POST /api/sds/generate` without `reference_token` uses the active default; if removed, returns 422 requiring a PDF. An explicit token remains a one-request override.
- Settings/library have no history retention TTL. Keep `preferences/` and `library/` out of S3 lifecycle rules. Single-owner config, not multi-user concurrent editing.

## PNG operations

- `GET /api/png/templates` → `{templates: Array<{id,name,builtin:boolean,width,height,image:FileRef,car_mask:FileRef|null,marker_mask:FileRef|null,config:TemplateConfig}>,deleted_templates:Array<{id,name}>}`. Enabled builtin and user templates only. Packaged originals: Ford Kuga, Ford Kuga chroma, Skoda Kodiaq, demo. Each selection should apply its config and use its mask tokens unless user provides new masks.
- `POST /api/png/colors` `{text:string, format:'list'|'csv'}` → `{colors:Array<{code,hex,metallic,error:string|null}>}`. Show row errors before generation.
- `POST /api/png/render` `{template_id?:string, template_token?:string, car_mask_token?:string, marker_mask_token?:string, config?:TemplateConfig, color:{code,hex,metallic}, lightness_mode:'match'|'keep', feather:number, draw_plate:boolean}` → `{file:FileRef, preview:FileRef, metrics:{body_delta_e:number,marker_delta_e:number|null}, warnings:string[]}`. Custom template requires car mask or `config.key_color` for automatic chroma mask. Existing bundled masks used by default. `preview` is reduced-size PNG; `file` is original-size PNG. Alpha preserved.
- PNG results also return human-readable `template_name`, used in history metadata/group labels instead of a saved car UUID. All generation responses include the common history/batch metadata described above.
- A successful render with `template_token` automatically saves the original PNG, supplied masks and config into the durable car/reference library. Returns `auto_saved_template:{template:Template,created:boolean}`; `created:false` means exact PNG SHA-256 identity already exists (including manually saved references). Repeated colours/reuploads reuse the existing entry without overwriting its name/config/masks. Name is trimmed `config.model_name`, falling back to the upload filename stem. Saved/builtin `template_id` renders do not create another reference. Uploads, mask creation and failed renders never save a reference.
- The original source bytes are shared by render and autosave, not downloaded again per colour. A library copy/commit failure rolls back new objects and returns the generated PNG/history with `reference_save_warning:string` also present in `warnings`; it never claims `auto_saved_template` success. The library keeps its existing 100-user-template limit and single-owner concurrency limitations. Automatically saved inputs do not expire with temporary uploads or 30-day history.
- `plate_box` is an approximate search region, not an opaque wipe rectangle. Background/text replacement is clipped to the detected inner plate contour; rounded corners, perspective, frame and saturated EU strip remain outside it. Dark enclosed glyph holes are filled without a convex hull or morphology that joins bumper reflections. Failed/ambiguous detection leaves the plate unchanged and adds a warning; no unmasked rectangle fallback. Chroma-key recolouring also guards the detected plate interior. Input regions should surround the plate, not the entire vehicle.
- `POST /api/png/mask` `{template_id?:string, template_token?:string, kind:'car'|'marker', backend:'flood'|'grabcut'|'chroma', key_color?:string, positives:[[x,y]], negatives:[[x,y]], rects:[[x1,y1,x2,y2]], tolerance:number}` → `{file:FileRef}` (full-size grayscale PNG). Browser painting/upload also supported via file API.
- TemplateConfig: `{brand_name:string,model_name?:string,plate_box?:[number,number,number,number]|null,key_color?:string|null,font_size?:number,auto_font_size?:boolean,text_color?:[number,number,number],plate_bg_color?:[number,number,number]|'auto'|null}`. Omit font_path: deployed font is bundled.
- `auto_font_size` defaults to `true`: the full brand/code is fitted to the plate and the selected `plate_box`, including tilt. With `false`, `font_size` is an upper limit; smaller text is still selected when necessary. Very small or unreadable text produces a warning, not intentional cropping.

## SDS

- `GET /api/sds/reference` → `SDSReference` (nullable active reference PDF).
- `POST /api/sds/parse` `{mode:'template'|'exact',brand:string,title_template:string,values_text:string,exact_text:string}` → `{items:Array<{code,brand,color,title,filename}>}`.
- `POST /api/sds/generate` `{item:{code,brand,color,title,filename},reference_token?:string,render_previews:boolean}` → `{file:FileRef,previews:FileRef[],validation:{pages:number,paint_title_regions_exact:number,clear_varnish_regions_exact:number,dates_preserved:boolean,protected_text_preserved:boolean}}`. Always validate. Only product titles are replaced; no hazard/composition generation.
- `POST /api/sds/find` `{file_token:string,search_text:string,pages_spec:string,exact_case:boolean,replace_all:boolean}` → `{matches:Array<{page:number,text:string,rect:[number,number,number,number]}>}`.
- `POST /api/sds/edit` `{file_token:string,search_text:string,replacement:string,pages_spec:string,exact_case:boolean,replace_all:boolean}` → `{file:FileRef,preview:FileRef,replacements:number,pages:number}`.

Limits: one color/PDF per render call, images ≤8M pixels, upload ≤25 MiB, ≤200 items in one parsed list. Frontend must stop on cancellation/error and retain completed downloads. Browser ZIP rejects packages above 250 MiB and offers individual downloads. LastWriteTime is implemented through ZIP entry dates and persistent ISO metadata, not direct browser OS timestamp manipulation.
