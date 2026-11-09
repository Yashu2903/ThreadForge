
Implement **ThreadForge AI, Phase 1: provider and spend foundations, sources with lazy analysis, `@AI` requests in chat, and cited answers**.

This is a change to an existing system. Write working code, migrations, workers, frontend, tests, and documentation. Do not stop after a plan. Do not implement Phase 2 (proposals, drafts, writing into documents).

I am building this to understand how the system works. Explain each checkpoint in simple technical terms (queues, transactions, tokens, embeddings, locks), define each term briefly the first time it appears, and refer to the actual files, queries, and request flow.

## 0. How to work

- Build in the checkpoints of section 12, in order. Do not start one until the previous one passes its tests and lint. One git commit per checkpoint if under git and AGENTS.md does not say otherwise.
- Maintain docs/ai-progress.md after each checkpoint: done, verified, blocked, next.
- Run the existing test suites first and record the baseline. Existing behavior (rooms, chat, events, documents, collaboration, imports, Drive) must keep working.
- Use expand-only migrations (add, never drop). Working downgrades; alembic check clean.
- Never leave a control without backend behavior, never present a stub as working, never claim a mocked provider proves a real integration. If real credentials are unavailable, finish everything else, test with HTTP-level stubs, and list exactly what remains unverified.
- Consult current official documentation for every provider API, SDK, and library you use (OpenAI API, Google Gemini API, pgvector, PostgreSQL full-text search, Tesseract, pypdfium2, Mermaid, SQLAlchemy). Do not invent APIs. List anything you could not verify under "Unverified assumptions".

## 1. Provider facts (supplied by the owner; everything is configuration)

Never hardcode model names, limits, or prices. All of this is configuration with validation, and anything missing leaves the related capability disabled with a clear status (never a silent fallback).

<<FILL IN before running>>
- Generation model: named by the owner as "GPT 6 Luna"; exact API model ID: ____; context window: ____; max output tokens: ____; supports schema-constrained structured output: ____; supports image input (formats, max images, max size): ____; price per million input and output tokens and per image if separate: ____ (with the date checked).
- Embedding model: named by the owner as "Gemini Embedding 2"; exact API model ID: ____; accepted inputs (text, image, interleaved): ____; default and allowed output dimensions: ____; max input tokens or image limits: ____; whether it has query/document task types: ____; whether shortened vectors must be re-normalized: ____; price: ____.
If the owner left blanks, read the official documentation, propose values in docs/ai.md under "Values to confirm", and keep AI disabled until they are confirmed.

## 2. Scope

Phase 1 delivers: provider adapters with startup self-checks; bring-your-own-key (BYOK); spend controls; room AI switch and consent notice; sources (upload, Drive, room list) with lazy analysis (text extraction, Tesseract OCR with a vision-model fallback, figure analysis, chunking, embeddings); the `@AI` composer flow; hybrid retrieval; coverage-based summarize and compare; document review (read-only); cited answers, including Mermaid diagrams, published in chat; a small evaluation harness.

Not in Phase 1: any AI write to a document, edit or draft proposals, a Mermaid editor node, image generation, token streaming, tool or function calling for the model, other providers, languages other than English, per-source privacy toggles. The composer shows only "Answer in chat"; show no control for unbuilt features.

Supported requests:
| Request | Behavior |
|---|---|
| `@AI summarize these three files` | Coverage processing over exactly those source versions, cited answer in chat |
| `@AI compare these other two files` | Only those two, balanced per source, cited |
| `@AI what does the architecture diagram show` | Retrieval over selected sources including figures, real images attached, cited |
| `@AI check the document we have open` | Read the captured document, answer in chat |
| `@AI explain this paragraph` | Selected blocks of the captured document, answer in chat |
| `@AI draw a flowchart of the process in these files` | Cited answer containing a validated Mermaid diagram, rendered in chat |

## 3. Reuse (inspect first, then report what exists)

Existing authentication and CSRF; rooms, memberships, roles; the message service (room lock, event sequence, outbox, client_message_id idempotency, rate-limit buckets); room events and the Part 5 client (cursor, replay, resync); the PostgreSQL claim-token job pattern (outbox dispatcher, import worker); storage adapter and opaque keys; the sandboxed child-process conversion with rlimits; room_assets and the image re-encoding pipeline; the Google Drive connection, Picker, and import code; the AES-GCM key-set encryption helper used for Drive tokens (reuse it for API keys); the collaboration service (Hocuspocus) and its internal API; the shared editor-schema package and block IDs; Docker Compose; test isolation; cleanup command; the error envelope (lowercase snake_case codes). Report anything missing and how you adapt.

## 4. Permissions and policy

**can_write(user_id, document_id):** one shared service function: user active; active room member with role owner or editor; the document belongs to the room and exists; the room is not archived; the document's collab_state is ready (not needs_repair) and not size-locked read-only. The collaboration service's authorize endpoint must use the same function. Call it: when a request is accepted, when a worker claims a job (before any model or document access), before publishing a result, and on retry. Background jobs call it with the requester's user_id (a service credential never bypasses it). A live click uses the live session.

| Action | Owner | Editor | Viewer |
|---|---|---|---|
| Invoke `@AI`, attach sources, cancel or retry own request | needs can_write on the anchor document | same | No |
| Cancel any request in the room | Yes | No | No |
| Upload or replace a source | Yes | Yes | No |
| Remove a source | Any | Own uploads | No |
| List sources and statuses | Yes | Yes | Yes |
| Download source original (SOURCE_DOWNLOAD_ROLES, default owner,editor,viewer) | per setting | per setting | per setting |
| Inspect extracted passages | Yes | Yes | No |
| Read published AI chat replies | Yes | Yes | Yes |
| Resolve citations on a published reply | per SOURCE_DOWNLOAD_ROLES | same | same |
| See request progress, context summary, manifest | requester, plus owners and editors with can_write | | No |
| Toggle the room AI switch, view usage page | Owner | No | No |
| Save and delete own provider keys | Any user, for their own account | | |

Removed members have no access. Return 404 for rooms a caller cannot access, 403 insufficient_role for active members lacking the role, and 403 ai_write_access_required when can_write fails. Viewers see AI controls hidden with a short explanation, still see published answers, and see AI requests only as the ordinary chat message.

**Anchor rule:** every AI request has an anchor document for the permission check. In sources-only mode the anchor's content is never sent to the model. If no writable document exists, return a clear error asking the user to open or create one first.

**Room AI switch:** rooms.ai_enabled (default true). Owner toggles it (emits an event). While off: AI controls disabled, no new requests, no new analysis, uploads still allowed (stored, not analyzed). Also a global kill switch (a database flag plus operator command, no redeploy needed).

**Consent:** before a user's first attach or request, show a notice naming exactly what is sent where: source text and images are sent to the embedding provider when a file is first analyzed; retrieved evidence, selected images, document text and chat context are sent to the generation provider; scanned pages that fail local OCR are sent to the vision provider; which key (yours or the server's) will be used; and that other room members can already read the files you attach. Record acceptance (user_id, notice_version, accepted_at). Requests without current-version consent fail with consent_required.

## 5. Checkpoint 1: providers, keys, spend

**Adapters:** LLMProvider (generate_structured with a JSON schema, text and image inputs, usage reporting, token counting, health) and EmbeddingProvider (embed_texts, embed_query, embed_images, health), with OpenAI and Google implementations behind interfaces, and HTTP-level stubs for tests. Prefer official SDKs only if they expose usage and retry control; verify. Provider calls happen only in worker processes (and the single key-validation call below), never inside a database transaction, never from the browser or the collaboration service.

**Self-check:** on worker start and by operator command, run small test calls: text generation, schema-valid structured output, an image input, a text embedding, an image embedding (check dimension and that a matching text/image pair scores higher than a mismatched pair), Tesseract installed with English data. Store per-capability results; expose GET /ai/status (booleans and codes only, no secrets). A failed capability disables only that capability (for example no vision means no figure analysis and no images at answer time, stated clearly in results). A failed embedding or generation check disables AI with a clear message.

**Call policy:** classify errors: retry 429 (respect Retry-After), 5xx, and timeouts with capped exponential backoff and jitter; do not retry 400, 401, 403, or content-policy refusals (fail with a safe code). Per-key concurrency limiter in the worker. Explicit timeouts. Scrub provider error text before storing or showing it (keys can appear in errors). Token counting: use the tokenizer matching the model if verified, otherwise a conservative estimate with a safety margin; enforce a hard pre-send input limit.

**Structured output:** use schema-constrained decoding if the model supports it (verify), otherwise parse plus one bounded repair attempt. The model gets no tools and no network.

**BYOK:** user_provider_keys (user_id, provider openai or google, encrypted_key BYTEA, key_id, last4, state, created_at, last_validated_at; UNIQUE (user_id, provider)). Routes (CSRF, no-store): save, delete, list metadata only (provider, last4, dates). Encrypt with the existing AES-GCM key-set helper, binding user_id and provider as additional authenticated data. Validate with one cheap provider call at save time (the plaintext lives only in memory for that call); rate-limit validation attempts (per user and per IP, to stop key testing). Never return, log, or store a plaintext key; only workers decrypt. Policy AI_KEY_POLICY: server_only, user_only, or user_if_present (default: use the requester's key for a provider if saved, otherwise the server key). The key source per provider is decided at request admission and recorded; if that key is missing or rejected at execution, fail with key_unavailable or key_rejected; never switch silently. A user key may be supplied for one or both providers.

**Spend controls (all units integer micro-dollars and tokens; prices come from configuration with a checked-on date; if prices are unset, enforce token caps only):**
- usage_ledger: created_at, request_id or analysis_job_id, user_id, charged_user_id, room_id, provider, model, operation (embed_text, embed_image, vision_describe, vision_ocr, classify, map, reduce, answer, key_validate), input_tokens, output_tokens, image_count, est_cost_micros, key_source (server, user), status (ok, failed, cancelled). Record failed calls and retries too.
- Budgets (server key only): global monthly, per-user daily, per-room daily. User-key calls are exempt from server budgets but not from per-request caps. Atomic reservation: before each call, reserve estimated cost against the budget window with a guarded upsert (like the rate-limit bucket); settle with actual usage; release on failure. At 100 percent, new server-key requests stop with budget_exhausted.
- Per-request caps (before sending): max input tokens, max output tokens, max images, max provider calls (map-reduce), max new-analysis pages and figures triggered by the request.
- Estimate and confirmation: POST /rooms/{id}/ai-requests/estimate returns approximate tokens, calls, and cost, and which key will be charged. If the estimate exceeds AI_CONFIRM_THRESHOLD, the send route requires confirm_large=true or returns 409 confirmation_required.
- Usage page for owners: spend by day, user, room, operation, key source. Warnings at 50, 80, and 100 percent of each budget, once per threshold per period, shown to owners and logged.
- Operator commands: ai_disable/ai_enable (global flag), ai_status (queue depth, oldest age, failures by code, spend today; no content), spend report.

## 6. Checkpoint 2: sources

**Tables (adapt names):** sources (id, room_id, created_by, display_name, current_version_id, state active or removed, created_at, removed_at); source_versions (id, source_id, room_id, version_no, original_name sanitized, media_type, byte_size, sha256, storage_key opaque, analysis_state not_analyzed, queued, extracting, indexing, ready, failed, removed; page_count, text_page_count, likely_scanned_pages, image_count, error_code, created_by, created_at, removed_at; UNIQUE (source_id, version_no); UNIQUE (room_id, id)). Immutable versions: replacing a file creates a new version.

**Upload paths (owner or editor; CSRF; client_request_id idempotency with a keyed fingerprint, like imports):** from device (intent, streamed bytes, finalize; actual byte counting; 25 MiB limit; no resumable-upload claim), from room sources (select existing), from Google Drive (reuse the connection, Picker, server-side metadata verification; PDF, DOCX, TXT, Markdown, and native Google Docs exported to DOCX; a ThreadForge copy; never arbitrary URLs). Accept PDF, DOCX, TXT, Markdown only; verify real signatures, reject encrypted PDFs, macro or invalid DOCX packages (reuse the existing package-safety checks), binary or NUL-containing text, wrong extension or type. Sanitize display names (path separators, control and bidi characters). Per-room source count and total-byte quotas, per-user upload rate.

**Upload does not analyze.** In the sandboxed child process, do a cheap inspection only: validity, page count, pages with a text layer, likely scanned pages (no extractable text), embedded image count, DOCX image count. Store the counts for estimates. No extraction, OCR, embedding, or provider call happens at upload; no data leaves the server. A corrupt or encrypted file fails immediately; an unreadable scan may only be discovered at first use.

APIs: list sources with analysis_state and counts; inspect (versions, status, passages for analyzed versions); download original (setting-controlled, attachment, nosniff, sanitized name); replace; remove. Removal: state removed; delete original bytes, chunks, embeddings, figure images immediately; redact excerpts in stored results and manifests (keep citation stubs: source version id, locator, "source removed"); already-published chat text stays (document this). Abandoned uploads expire and their bytes are deleted. A source upload never creates or replaces a document, and an editable-document import never becomes evidence automatically.

## 7. Checkpoint 3: lazy analysis and retrieval

**Trigger:** analysis starts only when a source version is attached to an AI request (or the owner presses "Analyze now"). analysis_jobs (source_version_id, config fingerprint of parser version, chunk policy, embedding model and dimension, vision model; state; claim token and lease; attempts; charged_user_id and key sources; caps snapshot; error code), UNIQUE per active (source_version_id, config fingerprint) so two requests attaching the same file share one job. Reuse the claim-token worker pattern in a separate worker entry point (python -m app.workers.ai_worker), with lease renewal between stages, cancellation checked at stage boundaries (cancel an unattended job whose last waiting request is gone; keep it if another request waits), bounded retries. Once a version is ready, later attaches reuse it with no provider calls.

**Pipeline, per source version:**
1. Extract (sandboxed child: scrubbed environment, rlimits, wall-clock timeout, no network): PDF text and page rendering with pypdfium2 (verify license; do NOT use PyMuPDF, it is AGPL), DOCX via the existing converter pieces keeping heading paths, Markdown and TXT. Embedded images extracted and re-encoded through the existing image pipeline. Limits: max pages (default 400), chunks per source, extracted text size.
2. OCR, decided page by page: use the text layer when present. Otherwise render at about 300 DPI and run Tesseract (English data) in the child process. A page fails quality if mean word confidence is below OCR_MIN_CONFIDENCE (default 60), too few words for the page size, a high share of non-dictionary tokens, or Tesseract crashed or timed out. Failed pages go to the vision fallback (transcription-only prompt: transcribe exactly, mark illegible text as [illegible], never summarize or correct; structured output, one text field per page), capped by VISION_FALLBACK_MAX_PAGES (default 20) and the daily vision budget. Cross-check the transcription against Tesseract's vocabulary and flag low agreement. Pages still unreadable are recorded as gaps. If more than 30 percent of pages are unreadable the version fails with a clear error (never marked ready); otherwise it is ready with a visible gap warning. Limits: OCR pages per source (default 100) and per-room caps.
3. Figures: skip decorative images (tiny, repeated by hash, near-blank); render pages that are mostly drawings (low text, many vector or image objects; verify what pypdfium2 exposes) as figure candidates. Cap figures per source (default 40) with a visible "12 of 40 figures analyzed" notice. For each figure call the vision model with a fixed structured prompt: description, visible text (transcription), and for diagrams the list of elements and which connect to which. Store the image (re-encoded, metadata stripped) and the description as a figure chunk (extraction: vision). Embedding per FIGURE_EMBEDDING_MODE (image, description, or image_plus_description; follow what the embedding model's documentation supports and evaluate it on the gold set).
4. Chunk with a versioned policy (chunk-v1): token-aware (target about 400 tokens, hard max under the embedding limit, small overlap), structure-aware (respect headings, pages, list items; keep a table as readable text with its header row repeated per chunk). Each chunk records ordinal, text, text_hash, token_count, kind (text, figure), extraction (text_layer, tesseract, vision_ocr, docx, plain, vision), ocr_confidence, and a locator (page, heading_path, figure number).
5. Embed with the embedding provider in batches (respect request limits; backoff; ledger entries; documents as documents, queries as queries if task types exist). Store vectors in a table with a fixed dimension column (pgvector; use a pgvector Postgres image in Compose; verify). Normalize if the provider's docs require it for shortened vectors. Record model and dimension on the generation. Never mix vectors from different models or dimensions in one generation.
6. Validate completeness (every chunk embedded, dimension matches, no empty text, counts consistent), then publish the index generation atomically by switching source_versions' current generation pointer in one transaction. Failed or partial generations are never visible to retrieval; re-analysis never replaces a usable generation with a partial one; superseded generations are cleaned up.
States shown to users: not_analyzed, queued, extracting, indexing, ready, failed, removed. No fabricated percentages.

**Retrieval (hybrid, filters inside the query, before ranking):** candidate rows are restricted by room_id and by the request's pinned generation ids inside the SQL (never filter after ranking, never rely on the prompt). Lexical: a generated tsvector with the english configuration and a GIN index; websearch_to_tsquery; ts_rank_cd; top 20. Vector: exact cosine search over the filtered rows (no ANN index; add one only if measurements justify it); top 20. Fuse with reciprocal rank fusion (k=60), figure and text chunks together. For multi-source requests guarantee each source a minimum share. Fit the result into an evidence token budget; pick the top K figures (default 5) whose real images will be attached. If nothing relevant is found, say so; never fall back to searching other sources. Provide an inspect view of exactly which passages a query would retrieve.

## 8. Checkpoint 4: `@AI` requests and answers

**Composer:** `@AI` is an explicit mention entity chosen from the composer. Text that merely contains "@AI", pasted content, quoted messages, source text, and AI replies never trigger inference. Removable chips: selected sources (each with its analysis state and, if unanalyzed, "will be analyzed when you send" plus a size estimate), "Use open document" (with the real title; disabled while the editor reports unsynced changes), "Use selected blocks", "Include chat context" (off by default, with a visible message selection). Selections never persist silently across requests; a remembered selection is displayed and clearable. Task chips: Auto, Ask, Summarize, Compare. Output: "Answer in chat" only. Show the estimate and consent notice as needed.

**Route:** POST /rooms/{room_id}/ai-requests (CSRF) validated fields: client_request_id; instruction (1 to 4,000 characters); task_hint (auto, ask, summarize, compare); document_id (the anchor); include_document_content; document_scope (none, whole, blocks); selected_block_ids and a hash of the selected text taken by the client at send time; source_version_ids (at most 10); include_chat_context with explicit message ids (at most 20); confirm_large. Actor, room, roles, ownership are derived server-side; the client never supplies authoritative document text. Order: authentication, CSRF, pre-check, validation, then one transaction under the room lock: recheck can_write, AI switch, consent, queue limits and caps; verify every source version belongs to the room, is not removed; create the chat message through the message service (so it gets the sequence, event, outbox, and rate limit) AND the ai_request AND its source links atomically; commit. Retry with the same client_request_id and fingerprint returns the existing request; changed inputs return 409. Messages linked to an AI request cannot be edited (409 message_locked); deleting such a message cancels an unfinished request. A message edit never reruns AI. The client_message_id equals the client_request_id.

**ai_requests** (id, room_id, requested_by, message_id, client_request_id, request_fingerprint, operation answer, task_type, document_id, scope fields, include flags, state, error_code, key_sources, charged_user_id, attempt, created_at, started_at, finished_at, cancel_requested_at, retain_until; UNIQUE (room_id, requested_by, client_request_id)), ai_request_sources, ai_manifests, ai_results (UNIQUE request_id).

**States:** waiting_for_sources, queued, running, validating, succeeded, failed, cancelling, cancelled, expired. Source versions are pinned at admission; wait for those exact versions (and their analysis) and freeze their ready generations before retrieval. A failed or removed source stops the request with an actionable error (never skipped, never replaced by a newer version). A request waiting or queued longer than AI_REQUEST_TTL (default 30 minutes) expires.

**Worker execution:** claim with the lease pattern; before any model or document access run can_write for the requester; no database transaction during provider calls; cancellation checked at every stage and in flight; late results discarded; usage still recorded. A worker crash may repeat computation (and spend); it never duplicates a published result (UNIQUE ai_results.request_id, deterministic reply client_message_id derived from the request id). Capacity: per-user active requests (default 3), per-room (default 10), global worker concurrency, fair claim order across rooms, a displayed queue position, provider rate-limit backoff.

**Task classification:** if task_hint is auto, one small structured call using only the instruction text (no evidence) returns qa, summarize, compare, or document_review; failure defaults to qa. Classification can choose a processing strategy but can never widen scope, sources, or context.

**Context modes:** "Selected sources only" (instruction plus selected source evidence; no document text, no chat, no earlier AI output) and "Include additional context" (plus only the document scope and chat messages explicitly enabled). No sources selected means no retrieval. Document-only requests are valid.

**Document capture (read-only):** when document content is included, the worker asks the collaboration service (internal route, user-authorized) to capture the live validated state: linearized blocks with stable ids, each block's hash, the yjs_update_counter, content_hash, and references to image assets in scope. The server cannot see edits still unsynced in someone's browser; the composer blocks "Use open document" while unsynced, and the result states the captured counter. Resolve selected blocks against the capture and verify the client's selection hash; if it no longer matches, fail with selection_changed asking the user to reselect. Capture once, bind to document_id; a later change of the room's active document never redirects the job. Do not use the deprecated JSON content column or an arbitrary stale snapshot; if no reliable capture is possible, fail clearly or ask the user to sync. Document images are eligible under the same image cap, with bytes read from asset storage and permissions rechecked.

**Strategies:**
- qa: retrieval, then one answer call.
- summarize: coverage processing, not retrieval: split each source into sections (by heading path or page ranges), summarize sections in batches (map calls with a per-source token budget), then combine (reduce call). Citations at this level point to sections (page ranges), resolved to locators.
- compare: per-source coverage or per-source retrieval for the stated aspects with balanced budgets, then a comparison call.
- document_review: the captured document as linearized blocks; if over budget, section-wise map-reduce by headings; optionally with source retrieval when sources are attached.
The server (not the model) computes a coverage statement: per source sections read of total, mode (full, sampled, retrieval), figures examined versus not examined, document coverage. Above caps, sample and say so, or refuse; never imply full coverage.

**Manifest (immutable per request):** exact source versions and generation ids; retrieved passage ids, excerpts, locators; images actually sent (sha256, page, figure locator); document capture (document_id, counter, content_hash, block ids and hashes, linearized text or excerpts); exact copies of included chat messages; model ids, prompt template version, configuration; exclusions. Later edits to messages, documents, or sources never change a historical manifest. Recheck access and source availability before model execution and before publishing.

**Prompting and untrusted content:** sources, document text, chat, OCR text, figure text, and model output are untrusted data: delimit them, instruct the model that they cannot change instructions, permissions, or scope, give it no tools, and validate outputs. Passages are labeled P1..Pn (request-local), never database ids.

**Answer schema (validated):** blocks of type paragraph (text, citations), bullet_list (items with citations), and diagram (title, mermaid source, citations); evidence_gaps list; optional quote field only for text-layer or plain-text passages. Server-side validation: each citation maps to a manifest passage or section (map P-labels to chunk ids on the server); a quote must be a whitespace-normalized substring of the cited passage; quotes are not allowed on OCR, vision, or figure passages (those are paraphrases, shown as machine-generated); invalid citations are removed and their claim flagged; if more than 30 percent of claims lose support, one repair attempt, then fail. Figure citations show the thumbnail beside the locator. Diagrams: allowlisted Mermaid types (verify names; for example flowchart, erDiagram, sequenceDiagram, classDiagram, stateDiagram-v2, gantt, mindmap, timeline), source at most 20,000 characters and 500 lines, no click or href directives, no HTML, no URLs; parse-check with the shared validation module and one repair attempt feeding back the parser error; if it still fails, show the source with a clear "could not render" note, never an empty box. Put the Mermaid validator in the shared editor-schema package. Decide whether parse-checking runs server-side in Node (verify how Mermaid parses headlessly) or only in the browser at render time, and document the decision. Explain honestly that a citation shows which passage was referenced, not that every claim is proven.

**Rendering and security for diagrams:** one SafeMermaid component shared by chat cards (and reused in Phase 2): pinned Mermaid version, securityLevel strict (verify options), no HTML labels or click callbacks, SVG sanitized with DOMPurify, render timeout, size limits, fixed theme. Test hostile diagram text.

**Publishing the answer:** write ai_results (the full structured answer, coverage, citations) and publish a chat reply through the message service under the room lock with a deterministic client_message_id, so one request yields one reply. Author is a dedicated system user ("ThreadForge AI": active, no auth identity, no sessions, no membership, excluded from member lists, presence, and invitations; verify the schema impact and choose the smallest change, for example a message kind column with values user, ai_request, ai_answer). The reply's body is a plain-text rendering of the answer (at most 4,000 characters, with a pointer to the full card); the client renders a card from the result. AI messages cannot be edited; the owner can delete one (this redacts the stored result content, like a tombstone). AI replies have their own quotas and do not consume human write buckets.

**Events and client:** add event types ai_request_updated {request_id, state}, source_updated {source_id, source_version_id, state} (terminal states and new versions only), room_ai_settings_changed {ai_enabled}. Requires: extending the room_events type CHECK, payload schemas, bootstrap and replay handling, the Part 5 client, and the completeness audit. Events carry ids and states only; details come from authorized endpoints; stage labels (Waiting for files, Analyzing files, Gathering context, Generating, Validating) come from polling the status endpoint with backoff while a card is visible. No per-token events. Non-writers see the ordinary chat messages only.

**APIs:** status, authorized context summary, cancel (requester or owner), retry (requester, up to 3 attempts; re-checks everything and re-pins nothing new), read result, resolve citations (respecting the source-visibility setting), room AI settings, keys, usage, consent. Existing authentication, CSRF, pagination, and error conventions; internal collaboration routes stay private and secret-protected.

**Frontend:** composer and chips; attachment control (upload, choose room sources, Drive); source panel (list, status, inspect passages, replace, remove, download); request card with stage labels, cancel and retry; answer card with citations, figure thumbnails, coverage statement, evidence gaps, and safe Mermaid diagrams; AI settings (keys, consent, usage, owner switch); clear unavailable states (AI disabled, no key, budget exhausted, capability down). Chat and everything else stay usable during AI outages.

## 9. Retention and cleanup

AI_RESULT_RETENTION_DAYS (default 30): after that, delete manifests (retrieved passages, document captures, chat copies, images-sent records); keep published answers and their short citation excerpts (at most 300 characters). Cleanup command: abandoned uploads, stale temp directories, superseded and failed generations, expired requests, orphan storage files, expired budget windows. Phase 2 will pin captures referenced by pending proposals.

## 10. Security checklist to implement and test

Keys: encrypted, never logged or returned, decrypted only in workers, errors scrubbed. No document, source, or chat content, filenames, or prompts in logs; log ids and codes. Internal routes private. Provider calls only in workers. All retrieval filters inside SQL. Prompt-injection containment (instructions inside sources, documents, chat, OCR text, figure text, and diagram text cannot expand scope, trigger requests, or cause actions; output validated; links in model output pass the existing scheme policy; never fetch URLs or images named by model output). Quotas and caps cannot be bypassed by retries or parallel requests.

## 11. Tests and evaluation

Real PostgreSQL (with pgvector), Redis, storage, worker, and collaboration integration tests; HTTP-level provider stubs for deterministic cases; real-provider runs separately when credentials exist.

Cover: the permission table on every route (direct calls and modified clients; viewer, outsider, removed user, read-only editor, downgrade while queued, running, and before publishing); key storage (encryption with AAD, no leakage, validation rate limit, rejected key, key deleted mid-job, no silent fallback); self-checks per capability; ledger rows for ok, failed, retried, cancelled calls; budget reservation under concurrency (no overshoot); caps; estimate and confirmation; warnings; source upload (all types, hostile files, wrong types, encrypted PDF, quotas, idempotent retry, abandoned upload cleanup, Drive with stubs, no analysis at upload and no provider calls); lazy analysis (one shared job for two requests, reuse on second attach, cancellation, crash and retry yields one generation, partial generation invisible, failed source stops the request, removal during a job, version pinning); OCR (clean scan, bad scan falling back to vision, low-agreement flag, gaps, over-30-percent failure, caps); figures (decorative skipped, cap notice, descriptions, images attached at answer time within the cap, vision unavailable fallback); retrieval (filters inside the query: five sources, select three then two, assert the exact passages and that the second bundle contains nothing from the first, no fallback to other sources, RRF behavior, per-source balance, deleted source never retrievable); requests (one job per structured request; plain text, quotes, pasted content, and AI replies never trigger; retry creates no duplicate; message edit does not rerun; deleting the message cancels; document-only, sources-only, mixed; no leakage in sources-only mode; document capture with unsynced warning, selection hash mismatch, active-document change does not redirect; coverage statements; summarize and compare really cover all sections or say they sampled); output validation (bogus citations, quote substring check, OCR quote rejection, malformed JSON and one repair, invalid Mermaid and one repair, hostile Mermaid and SVG, over-limit diagrams); publication (one reply per request despite retries, system author, deleted reply redacts result); events and Part 5 client handling; capacity and fairness; kill switches; regressions of chat, events, documents, collaboration, imports.

**Evaluation harness (tests/eval/, not CI):** create small synthetic sources (a text PDF, a scanned PDF page, a DOCX with a figure, a Markdown file, a diagram image) and a gold set of questions with expected citation locators: factual questions, summaries, comparisons, figure and chart questions, document-review questions, and draw-a-diagram requests. Report: retrieval recall at k, citation validity rate, share of schema-valid outputs without repair, share of diagrams that render without repair, OCR character accuracy on the scan, latency p50 and p95, tokens and estimated cost per task type. Record results in docs/ai.md. The owner will judge quality from these numbers; Phase 2 depends on them.

Run all tests, lint, type checks, and builds. Fix failures you introduced and report exact commands and results, separating automated evidence from real-provider checks.

## 12. Checkpoints (each gated)

1. Providers, self-checks, BYOK, consent, AI switch, can_write, spend ledger, budgets, caps, usage page, operator commands.
2. Sources: tables, upload paths (device, room, Drive), inspection, policy, removal, cleanup.
3. Lazy analysis worker, OCR with vision fallback, figures, chunking, embeddings, generations, hybrid retrieval, inspect view.
4. `@AI` composer and route, states, events, worker execution, strategies, document capture, manifest, answer validation, Mermaid, publication, UI cards.
5. Evaluation harness and results, security and regression tests, docs/ai.md, final report.

At each checkpoint explain the files that matter, the request and job flow, database writes and transactions, permission checks, failure behavior, what was verified, and what remains unverified.

## 13. Final deliverables

Working code, migrations, Compose changes (AI worker, pgvector image, Tesseract in the worker image), settings with validation and .env.example, operator commands, docs/ai.md (architecture, tables, API and error-code reference, settings, privacy notice text, evaluation results, limits, values to confirm), suggested file reading order, and unverified assumptions. Known limits to state plainly: lost spend control if prices are misconfigured, OCR and figure descriptions can be wrong, citations are references not proofs, answers cannot see edits unsynced in a browser, published answers keep their text after a source is removed, chunk and figure caps can leave parts of a large file unanalyzed (always stated in the result), diagrams are not exportable yet.

Phase 1 is complete when a user with document write access can attach files in chat, have them analyzed once, invoke `@AI`, and receive a grounded, cited answer (including diagrams and figure-aware answers) from exactly the selected sources and explicitly included context, within the spend controls, with no content written to any document.

Do not begin Phase 2.

