# AI phase 1 progress

One section per checkpoint, one commit each. Design and reference: `docs/ai.md`.

## Baseline (before any AI change)

Last full runs on this repository state (collaboration checkpoint 2, commit `616220f`):

| Suite | Result |
|---|---|
| backend pytest | 1284 passed (four sequential batches; one single full run is reaped by the host for low memory) |
| collab service vitest | 28 passed |
| editor-schema package vitest | 91 passed |
| frontend vitest | 192 at collaboration checkpoint 1 (not re-run since; nothing it covers changed) |
| `client/` vitest | 177 (not re-run since) |
| Playwright | 69 passed, 2 skipped (not re-run since) |

Not re-run for AI checkpoint 1 because no frontend or `client/` code changed.

## Checkpoint 1: providers, keys, consent, switches, can_write, spend

**Done**
* Migration `0007_ai_foundations` (expand only; working downgrade; `alembic check` clean): `rooms.ai_enabled`,
  `ai_global_flags`, `ai_consents`, `user_provider_keys`, `ai_capability_status`, `ai_usage_ledger`,
  `ai_budget_windows`, `ai_budget_warnings`, `ai_rate_buckets`, and the `room_ai_settings_changed` event type.
* `app/ai/`: adapters (OpenAI Responses generation, Gemini embeddings, plain HTTP), call policy with retry and
  per-key concurrency, ledger, atomic budget reservation, warnings, caps, AES-GCM key sets with AAD, BYOK
  service, key policy, consent notice, self-check, capability status, estimate, usage aggregation.
* `app/services/permissions.py` (`decide_write`) used by the AI gate and the collaboration authorize endpoint.
* Routes: status, consent, keys, room AI settings, usage, estimate. Event `room_ai_settings_changed`.
* Operator commands: `ai_disable`, `ai_enable`, `ai_status`, `ai_spend_report`, `ai_selfcheck`;
  worker entry `python -m app.workers.ai_worker` (self-check only in this checkpoint).
* Settings with startup validation, `.env.example`, `docs/ai.md`.

**Verified (automated)**
* backend 1390 passed in sequential batches: 272 + 691 + 423 + 4 (baseline 1284; **106 new**: 41 core, 40 API,
  23 config, 2 migration), 0 failed in the final state. One existing guard test
  (`test_only_the_message_service_and_cleanup_touch_the_message_tables`) failed twice on my code and caught real
  layering mistakes (a direct read of `messages` from the estimate, then a docstring mention); fixed by
  moving the read into `services/messages.py`.
* Existing tests changed: `test_room_events.py` (13 event types; the completeness audit lists
  `set_room_ai_enabled`), `tests/db_utils.py` (AI tables truncated between tests).
* `ruff check`, `ruff format --check` clean on `app scripts tests`.
* Behaviour proven against real PostgreSQL and the real adapters over HTTP-level stubs: retry/no-retry
  classification, `Retry-After`, ledger rows for ok/failed/retried/cancelled, reservation under concurrency
  (no overshoot), rollback of partial reservations, warnings once, user-key exemption, caps, key encryption
  with AAD, no plaintext in DB/logs/responses, rate limiting of key validation, key policies without silent
  fallback, the permission table, `decide_write` reasons, shared collab authorize, kill switches and commands,
  self-check per capability, usage page, estimate.

**Not verified**: anything against the real OpenAI or Google APIs (see `docs/ai.md` section 8). The
self-check has not been run with real credentials.

**Blocked / adapted**: Google Drive, import jobs and the sandboxed converter do not exist in the repository
(see `docs/ai.md` section 1). The collaboration browser editor (collab checkpoint 3) is still pending.

**Next**: checkpoint 2 (sources: tables, device and room-source upload, inspection in a sandboxed child
process, policy, removal, cleanup).
