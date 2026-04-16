# Changelog

All notable changes to **reMark** are documented here. The project follows
[Semantic Versioning](https://semver.org/) and its commits group into the
phases described in the release notes.

## [0.8.0] — 2026-04-16

"Offline & Scale." Two themes in one release: a provider-agnostic
LLM layer that lets Ollama replace every cloud call, and a round
of performance work — streaming downloads, cross-document batch
embeddings, a shared httpx pool. Also: the Obsidian plugin is now
in its own repo and the Community Store, and three new bridge API
endpoints let the next plugin version surface note status,
previews, and vault search.

### Added

- **`src/llm/` module.** `LLMClient` ABC with `complete()` +
  `complete_vision()`. `AnthropicClient` wraps the existing SDK;
  `OllamaClient` talks to `/api/chat` and `/api/generate`. A
  factory (`build_llm_client`) picks the right one from
  `llm.provider`. Every consumer (processing, reports, OCR VLM)
  now takes an `LLMClient` by DI — nothing imports `anthropic`
  directly anymore.
- **Ollama embeddings.** New `OllamaEmbeddingBackend` alongside
  Voyage / OpenAI / sentence-transformers. Known-model dimension
  table covers `nomic-embed-text`, `mxbai-embed-large`,
  `snowflake-arctic-embed`, `all-minilm`; unknown models default
  to 768.
- **Ollama OCR.** `VLMOcr` now takes an `LLMClient`, so setting
  `llm.provider: ollama` routes handwriting OCR through `llava`
  (or any configured vision model).
- **Bridge API v2** (all Bearer-token auth like `/api/push`):
    - `GET /api/notes/{path}/status` — per-note sync metadata
    - `GET /api/notes/{path}/preview` — first-page PNG rendered
      from the cached `.rm` with a 24h content-hash cache
    - `POST /api/search` — `{query, mode, limit}` across the
      indexed vault; modes `semantic | bm25 | hybrid`
- **Streaming downloads.** `src/remarkable/streaming.py` spills
  blobs above `sync.streaming_threshold_bytes` (default 5 MB) to
  `sync.temp_dir` instead of buffering in RAM. Small blobs keep
  the in-memory fast path.
- **Batch embeddings.** `reindex_vault()` now collects chunks
  across every note and embeds them in batches sized to the
  backend's `max_batch_size` (default 64). The CLI shows a
  `[reindex] done/total` progress line.
- **Shared httpx pool** (`src/http_pool.py`): auth refresh and
  Teams webhook dispatch now accept a `SharedHttpPool` and reuse
  one keep-alive client instead of spawning per-request clients.
  `RemarkableCloud` and Notion keep their existing long-lived
  pools (they have provider-specific interceptors).
- **`remark-bridge bench`** — synthetic embedding throughput
  measurement: chunks/sec + peak RSS.
- **Settings UI**: `/settings/llm` form auto-rendered from
  `LLMConfig` + `OllamaConfig`.
- **`docs/OLLAMA.md`** — end-to-end offline setup guide.

### Changed

- **Processing + reports + OCR VLM** take `LLMClient` instead of
  `anthropic.AsyncAnthropic` directly. Behavior is identical for
  `provider: anthropic` (the default).
- **Obsidian plugin** moved out of the monorepo. It now lives at
  [github.com/BGGBTAC/obsidian-remark-bridge](https://github.com/BGGBTAC/obsidian-remark-bridge)
  and is listed in the Community Store.
  `contrib/obsidian-plugin/` kept as a pointer README.

### Config

New top-level section:
```yaml
llm:
  provider: anthropic       # anthropic | ollama
  ollama:
    base_url: http://localhost:11434
    text_model: llama3.1
    vision_model: llava
    embedding_model: nomic-embed-text
    timeout_seconds: 120
```

New keys:
- `sync.streaming_threshold_bytes` (default 5 * 1024 * 1024)
- `sync.temp_dir` (default `~/.remark-bridge/tmp`)
- `search.batch_size` (default 64)

### Migration

Fully additive. Installs without an `llm:` block (or with
`provider: anthropic`) behave identically to 0.7.1. Flipping
to `provider: ollama` requires a running Ollama server with the
configured models pulled — see `docs/OLLAMA.md`.

## [0.7.1] — 2026-04-16

Patch release — four fixes discovered during the 0.7.0 rollout.

### Fixed
- **passlib → bcrypt**: passlib 1.7 can't initialise its bcrypt
  backend against bcrypt 4.x (removed `__about__` probe). Replaced
  with direct `bcrypt.hashpw` / `bcrypt.checkpw` — fewer moving
  parts, no version conflict. Dependency changed from
  `passlib[bcrypt]>=1.7.4` to `bcrypt>=4.0`.
- **Pre-0.7 no-auth installs broken**: `_auth_check` required a
  session even when `web.username` / `web.password` were empty. Now
  restores the pre-0.7 open behavior: when neither Basic auth nor a
  session is configured, an anonymous admin context is returned.
  Admin-only routes (`/users`, `/audit`, `/reports`) still require a
  real session login.
- **ISE on cold-start requests**: `current_user()` accessed
  `app.state.sync_state` before any route handler had initialised
  it. Moved the SyncState singleton + admin bootstrap into
  `create_app()` so the attribute exists before any middleware fires.
- **Cross-thread SQLite error**: the eager SyncState init creates
  the connection in the main thread, but TestClient + uvicorn workers
  access it from request threads. Added `check_same_thread=False` —
  safe under WAL mode with FastAPI's sequential request handling.

## [0.7.0] — 2026-04-15

"Multi-user & reporting". Three big additions: per-user accounts with
vault isolation, a structured audit log, and scheduled LLM-backed
summaries pushed to Teams / Notion / vault.

### Added
- **Multi-user web UI**. Session-based login backed by bcrypt
  (passlib) hashes. New `users` table with role (`admin` | `user`),
  optional per-user `vault_path`, last-login tracking. Fresh installs
  auto-seed an `admin` user on first boot — password comes from
  `REMARK_ADMIN_PASSWORD` if set, otherwise a random token-urlsafe
  string printed once to the log. `/users` page (admin-only) for
  creating / toggling / resetting other accounts. Legacy HTTP Basic
  auth kept as an automation fallback.
- **Per-user vault isolation**. `sync_state.user_id` and
  `devices.user_id` columns (additive migration, pre-0.7 rows
  default to the seeded admin). Dashboard / `/notes` / `/devices`
  scope their data to the signed-in user; admins see everything.
- **Audit log**. New `audit_log` table with timestamp, user,
  action, HTTP method + status, resource, IP, user-agent. A FastAPI
  middleware auto-logs every state-mutating web request (GETs stay
  out — uvicorn's access log covers those). CLI helpers
  `remark-bridge audit list` and `remark-bridge audit prune` (default
  90-day retention). `/audit` admin-only web route with
  action / user_id filters, 100/page pagination, and CSV export at
  `/audit.csv`.
- **Scheduled reports** (`src/reports/`). New `reports` state table
  with schedule / prompt / channels / next_run_at bookkeeping. The
  runner builds context from recent synced notes + sync stats, calls
  the configured LLM with a concise system prompt, dispatches the rendered
  Markdown to every configured channel. Three channels implemented:
    - `vault` — dated Markdown note under `Reports/`
    - `teams` — Adaptive Card via `microsoft.teams.webhook_url`
    - `notion` — child page under `notion.vault_mirror_page_id`
  Missing credentials / disabled integrations come back as
  per-channel errors (the other channels still deliver).
- **Report scheduler**. Inline `ReportScheduler` started via FastAPI
  lifespan — the `serve-web` process covers scheduling alongside the
  web UI. Minimal cron-ish grammar: `every <N>m|h|d`,
  `daily HH:MM`, `weekly DAY HH:MM`. Skipped in demo mode.
- **`/reports` web UI** — admin-gated list + create form + per-row
  toggle / delete / "Run now". Schedule validation happens at save
  time, so bad expressions never reach the scheduler loop.
- **CLI**: `remark-bridge report list | run --id N`.
- **Config**: new `ReportsConfig` (under `reports:`) with
  `enabled` + `tick_seconds`. New `web.session_secret` and
  `web.session_https_only`. All surface as editable web forms
  under `/settings/reports` and `/settings/web`.

### Changed
- `_auth_check` is session-first — HTML clients without a session get
  a 303 to `/login`, JSON/API clients get a 401. Bearer tokens
  (`/api/*`) and HTTP Basic continue to work unchanged for automation.
- `SyncEngine.set_device` now takes an optional `user_id` so the CLI
  loop can tag rows per device owner.

### Dependencies
- `passlib[bcrypt] >= 1.7.4` — password hashing
- `itsdangerous >= 2.1` — session cookie signing (pulled via Starlette)

### Tests
- `tests/test_multi_user.py` — users CRUD, bcrypt round-trip,
  authenticate success + failure paths, user-scoped state queries.
- `tests/test_audit.py` — insert shape, filters, limit/offset,
  user-agent truncation, retention prune.
- `tests/test_reports.py` — reports CRUD, schedule parser for every
  accepted form + invalid input, due filter excludes
  disabled / future-scheduled rows, vault-channel end-to-end smoke
  test with no API key (exercises the context-dump fallback).

## [0.6.6] — 2026-04-15

Mini release. Ships the automation behind the wiki screenshots.

### Added
- **`REMARK_DEMO_MODE`** — when the env var is set, `create_app`
  seeds a deterministic state DB + vault (15 notes, 3 devices, a
  failing queue entry, one bridge token) so the UI can render
  without a real reMarkable Cloud pairing. Source lives in
  `src/web/demo.py`. Seeding is idempotent and logs a warning
  rather than crashing if it fails.
- **`scripts/seed_demo_data.py`** — CLI wrapper around the seeder;
  writes a throwaway `config.yaml` + vault + state dir so a
  CI run can point `REMARK_CONFIG` at it.
- **`scripts/screenshots.py`** — Playwright headless-Chromium driver
  that captures 15 screenshots of the dashboard, notes list, queue,
  templates editor, `/settings/<section>` forms, and friends, at
  1440×900 with retina scaling.
- **`.github/workflows/screenshots.yml`** — `workflow_dispatch` + on
  every `v*` tag. Seeds, starts the web server, runs Playwright,
  uploads the PNGs as an artifact, and pushes them into
  `BGGBTAC/reMark.wiki` under `images/` using the
  `WIKI_PUSH_TOKEN` secret.
- **Wiki embeds** — the Home, Web-Dashboard, Multi-Device, Templates,
  and Search pages reference the generated images. Missing images
  degrade gracefully (GitHub renders the alt text).

### Notes
- The `WIKI_PUSH_TOKEN` secret must be a fine-grained PAT scoped to
  the wiki repo with Contents write access. The workflow exits
  cleanly with a log message if the secret is missing — never fails
  the tag push.

## [0.6.5] — 2026-04-15

Service release. No new features — a full security, correctness,
performance, and documentation sweep driven by a three-track audit.

### Security
- **Path traversal** fixed in `/notes/{note_path:path}`. The route now
  resolves the requested path and refuses anything that lands outside
  the configured vault root. Previously
  `GET /notes/../../.remark-bridge/device_token` returned the
  reMarkable device JWT.
- **Bridge token verification** switched to constant-time
  `secrets.compare_digest` in Python. The prior SQLite-level
  `WHERE token_hash = ?` compare leaked prefix information via
  response-time differences.
- **Device token file** is now written through a tempfile opened with
  `O_EXCL` + `0600`, then `os.replace()`'d into place. Closes the
  brief window where the old `write_text` + `chmod` sequence left the
  JWT world-readable.
- **`when:` expression sandbox** caps inputs at 500 chars / 200 AST
  nodes and catches `RecursionError` before it escapes. A malicious
  template YAML with deeply nested boolean ops previously crashed a
  web worker.
- **`/ask` error messages** now show a generic "Search failed" text
  to the browser. The underlying exception (which can carry API keys
  in URLs or token prefixes) stays in the server log only.

### Performance
- **Dashboard "Recent notes"** is served from the state DB via a new
  `state.recent_synced(limit)` helper instead of walking the vault
  with `rglob` and parsing every markdown frontmatter on each request.
- **`/notes` list view** filters server-side via
  `state.list_synced(folder, query)` — no more O(vault) reads per
  keystroke.
- **Notion client** holds a long-lived `httpx.AsyncClient` so pushing
  many pages shares one TLS session.
- **Web Push** exposes an async `send_push_async` wrapper that
  offloads `pywebpush` to a thread so broadcast loops don't block the
  event loop.
- **State DB** gained `IF NOT EXISTS` indexes on `sync_state.status`,
  `sync_state.last_synced_at`, and `external_links(provider, status)`.

### Correctness
- **Multi-device `_sync_once`** deep-copies the AppConfig per device
  via `model_copy(deep=True)`. An exception mid-cycle no longer leaves
  another device's sync / ignore filters leaking into the shared
  config, and concurrent runs (scheduler + manual CLI) stay isolated.
- **Web `SyncState`** is cached on `app.state` as a process-wide
  singleton with `_shared=True`, so every `/api/*` request no longer
  re-runs schema migrations and races the sync daemon for the WAL
  write lock. Route handlers that `finally: state.close()` are now
  no-ops on the shared connection.

### Documentation
- New `remark-bridge auth [--device <id>]` command exists so the
  pairing flow the README has always documented actually works.
- `-c/--config` CLI option now falls back to `$REMARK_CONFIG`. The
  Docker `sync` container previously ignored the mounted
  `/config/config.yaml` because the default was hard-coded.
- `.env.example` trimmed of dead env vars that the code never read
  (`REMARK_WEB_USERNAME/PASSWORD/VAPID_*`, `MS_CLIENT_ID/TENANT_ID`);
  `generate-vapid` renamed to the real command `vapid-keys`;
  `NOTION_TOKEN` + `REMARK_CONFIG` documented.
- README updated with the correct GHCR tag table (0.6.x), the two
  different default ports (8080 pip vs 8000 Docker), the routes
  `/queue` / `/devices` / `/templates`, CLI groups added since 0.3
  (`queue`, `retag`, `bridge-token`, `device remove`, `auth`), and
  the split systemd setup with `/etc/remark-bridge/env`.
- `mark_queue_failed` back-off docstring corrected to reflect the
  actual schedule (5m → 25m → ~2h → cap 6h, not 1m → 5m → 25m).
- New reference pages under `docs/`:
  - `BRIDGE_API.md` — bearer-token auth, endpoint shapes, error model.
  - `TEMPLATES.md` — YAML shape, `when:` grammar, inheritance rules,
    web editor, CLI entrypoints.
  - `upgrading.md` — version-by-version migration notes covering
    0.3.x → 0.6.5.
- `config.example.yaml` now documents `logging.format: text|json` and
  the symmetric `devices[].ignore_folders`.

### Removed
- Legacy combined `systemd/remarkable-bridge.service` and
  `remarkable-bridge.timer` — the split `remark-bridge-sync` +
  `remark-bridge-web` units (shipped since 0.3.1) are now the only
  supported systemd layout.

### Tests
- `tests/test_security_regressions.py`: locks in every audit fix —
  path traversal (plain + encoded + absolute), `secrets.compare_digest`
  usage, `when:` sandbox caps for length / node count / recursion
  depth, multi-device config isolation, and device-token file
  permissions after register_device.

## [0.6.0] — 2026-04-15

"Ecosystem". Conditional templates with inheritance, a bearer-token
HTTP API, and a companion Obsidian plugin that plugs into it.

### Added
- **Smart templates**: per-field ``when:`` expressions are parsed
  through a tiny AST-walker sandbox (equality, membership, boolean
  combinators, literals — no calls, attribute access, or subscripts).
  Templates can ``extends:`` a parent and override named ``blocks:``
  so reusable skeletons stay DRY.
- **Web template editor** under ``/templates``. Tile index of all
  loaded templates, CodeMirror 6 YAML editor (loaded from esm.sh, no
  build step), save validates via the engine's own parser, preview
  renders a one-shot PDF and opens it in a new tab.
- **Bridge HTTP API** for external clients. New ``GET /api/status``
  and ``POST /api/push`` endpoints guarded by
  ``Authorization: Bearer <token>``. Tokens live in a new
  ``bridge_tokens`` table, only the sha256 hash is persisted, the
  plain value is printed once on issue.
- **CLI group** ``remark-bridge bridge-token issue | list | revoke``
  for managing those tokens.
- **Obsidian companion plugin** at ``contrib/obsidian-plugin/``
  (TypeScript, esbuild). Ribbon icon + command to push the active
  note, status-bar widget that polls ``/api/status`` every 60 s,
  settings tab for server URL / token / retry attempts / retry
  delay. Failed requests retry with exponential back-off; each failure
  surfaces as an Obsidian Notice. Ships with
  ``COMMUNITY_STORE.md`` documenting the split-into-own-repo flow
  plus the obsidianmd/obsidian-releases PR checklist.

### Tests
- ``tests/test_templates_smart.py``: when-grammar edge cases
  (rejects ``__import__``, attribute access, subscripting), missing
  identifiers resolve to ``None``, inheritance merges fields,
  child blocks override parent blocks, cycles are detected without
  crashing the loader, ``when: false`` actually drops the field from
  the rendered PDF.
- ``tests/test_bridge_api.py``: missing / wrong-scheme / invalid /
  revoked tokens all 401; valid tokens get the expected payload;
  ``/api/push`` rejects traversal and missing files, queues valid
  paths, bumps ``last_used_at`` on verify.

## [0.5.0] — 2026-04-14

"Integrations & Smart". Web-first configuration plus four new features:
offline queue, hierarchical tags, math/LaTeX plugin, Notion mirror.

### Added
- **Editable /settings** for every config section. Forms render from
  ``AppConfig`` model fields so new keys surface automatically; writes
  round-trip through ruamel.yaml so inline comments survive. Secrets
  (tokens, passwords, client secrets) are masked with a sentinel so
  they're never sent back to the browser; submitting the sentinel
  leaves the existing value untouched. Sections that need a process
  restart (``sync``, ``web``, ``logging``, ``remarkable``) show a
  banner. Every change logs an audit entry in ``sync_log``.
- **Offline / retry queue** (``sync_queue`` table). Transient failures
  during ``process_document`` now enqueue a retry with exponential
  back-off (1m → 5m → 25m, cap 6h). Each sync cycle drains due
  entries before processing fresh work. New CLI group
  ``remark-bridge queue list | retry | clear``, ``/queue`` page in
  the web UI with per-row retry, and a dashboard banner when
  pending/failed > 0.
- **Hierarchical tagger**. New ``processing.hierarchical_tags`` flag
  swaps the tagger prompt to emit slash-separated tags like
  ``project/remark-bridge/multi-device``. Default off so existing
  vaults aren't auto-migrated. Companion CLI
  ``remark-bridge retag [--dry-run] [--limit N]`` backfills vault
  notes after enabling the flag.
- **Example math / LaTeX plugin** (``examples/plugins/math_latex_plugin.py``).
  NoteProcessor wraps bare LaTeX fragments (``\frac``, ``\sum``, greek
  letters, ``\begin{equation}`` blocks) in ``$...$`` / ``$$...$$`` so
  Obsidian renders them. Optional OCR backend stub for pix2text
  (default) or MathPix behind a ``backend`` setting.
- **Notion integration** (``src/integrations/notion/``). Mirror synced
  notes into a Notion workspace via an internal integration token.
  Each note becomes a child page under the configured
  ``vault_mirror_page_id`` with Notion blocks mapped from the source
  Markdown (headings, paragraphs, bulleted / numbered lists, to-do
  items). One-way today; task pull stubbed for a future release.

### Changed
- New dependency: ``ruamel.yaml >= 0.18`` for comment-preserving
  config writes.
- Settings UI indexed alongside existing pages in the top nav; old
  read-only JSON dump replaced with a tile grid linking to each
  section form.

### Tests
- ``tests/test_settings_write.py`` — comment round-trip, secret MASK
  sentinel, bool checkbox parsing, nested subgroup rendering,
  end-to-end POST writes YAML.
- ``tests/test_sync_queue.py`` — enqueue/dequeue, priority ordering,
  back-off skipping, attempt cap, retry reset, clear-by-status.
- ``tests/test_processing.py`` — hierarchical vs flat prompt swap.
- ``tests/test_math_latex_plugin.py`` — inline wrap, existing-math
  skip, code-fence skip, block env ``$$...$$``, disabled no-op.
- ``tests/test_notion.py`` — markdown-to-blocks edge cases, service
  enabled logic, error suppression.

## [0.4.0] — 2026-04-14

"Distribution & Multi-Device". Three significant additions, no breaking
changes for single-tablet installs.

### Added
- **Docker distribution**. Multi-stage `Dockerfile` (python:3.12-slim-bookworm,
  non-root user, `tini` as PID 1, HEALTHCHECK against `/healthz`) and a
  `docker-compose.yml` that ships the dashboard + sync daemon sharing a
  vault/state volume. The release workflow now also builds and publishes
  `ghcr.io/bggbtac/remark-bridge` as multi-arch (linux/amd64 + linux/arm64),
  tagged with `{version}`, `{major}.{minor}`, and `latest`. Compose pulls
  from GHCR by default; `build:` is available as a local fallback. New
  `REMARK_IMAGE_TAG` in `.env.example` for pinning releases.
- **Multi-device sync**. Register multiple reMarkable tablets against the
  same vault. Each device has a stable slug id (e.g. `pro`, `rm2`), its
  own device-token file under `~/.remark-bridge/devices/<id>/`, and an
  optional vault subfolder so notes stay separated. New `devices` table
  in the state DB plus `sync_state.device_id` column (additive migration
  auto-fills `default` for pre-0.4 rows). CLI group
  `remark-bridge device add | list | remove`, new `DeviceConfig` schema
  under `remarkable.devices`, a `/devices` page in the web UI, and a
  `set_device()` hook on `SyncEngine` that the sync loop calls per
  tablet. Single-tablet installs see zero behaviour change.
- **Hybrid search (BM25 + semantic with RRF)**. A new FTS5 virtual table
  `vault_chunks_fts` lives alongside the existing vector index in the
  same SQLite DB. The indexer writes both in one transaction; removal
  and `clear()` keep them in sync. `SearchQuery.ask()` grew a
  `mode: "semantic" | "bm25" | "hybrid"` parameter (default `hybrid`,
  fused via Reciprocal Rank Fusion with k=60). Config key
  `search.mode` threads through the web `/ask` route and the CLI
  `ask` command. BM25 needs no extra dependency — FTS5 ships with
  stock SQLite.

### Changed
- `docker-compose.yml` now defaults to pulling the published GHCR image
  rather than building locally, so users can run the stack without a
  repository checkout. `build:` is retained as a fallback.
- README rewritten around the GHCR-first Docker flow, added a Docker
  badge linking to the packages page and a tag table.

### Tests
- `tests/test_multi_device.py` — device registry CRUD, token-path
  convention, `device_id` column persistence, legacy 0.3 migration.
- `tests/test_search_hybrid.py` — BM25 rare-keyword hits, hybrid
  surfacing of exact matches over tied semantic scores, `bm25` mode
  skipping embedding backend, FTS cleanup on remove and clear.

### Fixed
- Legacy 0.3 database upgrade: the `device_id` index was declared in the
  inline schema and ran before the `ALTER TABLE` migration, blowing up
  pre-existing DBs. Moved into `_apply_migrations`.
- BM25 query was wrapped as an FTS5 *phrase*, requiring contiguous
  matches. Tokenised + OR'd individually-quoted terms so any-match
  scoring works as intended.

## [0.3.1] — 2026-04-14

Reliability patch. No breaking changes.

### Fixed
- CI: `pyproject.toml` now uses the SPDX license expression form
  (`license = "CC-BY-NC-4.0"`) required by setuptools ≥ 77. The legacy
  `{ text = "..." }` form and `License ::` classifiers have been removed.
- License metadata on PyPI now matches the repository license
  (CC BY-NC 4.0). Prior 0.3.0 wheel on PyPI is immutable and still
  shows MIT — install 0.3.1 for correct metadata.

### Added
- Structured JSON logging (`src/log_setup.py`). Opt in via
  `logging.format: json` in config, or `REMARK_LOG_FORMAT=json` env var.
  Rotating file handler replaces the plain `FileHandler`.
- `/healthz` now reports real readiness: state-DB ping, vault-path
  check, installed package version. Returns 503 when degraded so
  systemd / Docker HEALTHCHECK can detect failures.
- Split systemd units: `remark-bridge-sync.service` (+ matching timer)
  and `remark-bridge-web.service`. Old combined unit kept for backward
  compatibility. Includes `EnvironmentFile=` and stricter hardening
  (`ProtectKernel*`, `LockPersonality`, `MemoryDenyWriteExecute`).

### Changed
- Dependency floor bumps: `anthropic>=0.49`, `mcp>=1.2`, `msal>=1.30`,
  `uvicorn>=0.32`, `pydantic>=2.7`, `reportlab>=4.2`, `sqlite-vec>=0.1.5`,
  `websockets>=13`, `numpy>=1.26`, `gitpython>=3.1.43`, `pywebpush>=2.0.3`.
- Python 3.13 added to classifier list (already supported via 3.11 floor).

## [0.3.0] — 2026-04-14

Dashboard, PWA, OneNote, Teams, reverse sync, templates, plugins.

### Added
- **Web dashboard + PWA** (`src/web/`) — FastAPI + Jinja2 + HTMX + Alpine.js
  + Tailwind, no build step. Views for Dashboard, Notes, Note detail, Ask
  (RAG), Actions, Quick-Entry, Settings. Optional HTTP Basic auth.
- **Service worker** + **Web Push** (VAPID via `pywebpush`). Manifest,
  icons, Add-to-Homescreen.
- **Obsidian → reMarkable reverse sync** (`src/sync/reverse_sync.py`) with
  three independent triggers: frontmatter flag, dedicated folder, on-demand
  queue. Renders PDF or native notebook.
- **OneNote mirror** (`src/integrations/microsoft/onenote.py`) — parallel
  vault target with notebook/section management.
- **Microsoft Teams** (`src/integrations/microsoft/teams.py`) — Adaptive
  Card digests (daily/weekly), meeting ↔ note correlation.
- **On-device template engine** (`src/templates/`) — YAML templates,
  built-ins: `meeting`, `daily`, `project-review`. Render to fillable PDF,
  extract fields back into frontmatter on sync.
- **Plugin system** (`src/plugins/`) — `ActionExtractorHook`,
  `OCRBackendHook`, `NoteProcessorHook`, `SyncHook`. Discovery from a plugin
  directory and Python entry points. Example plugin included.
- **State schema additions**: `reverse_push_queue`, `webpush_subscriptions`,
  `plugin_state`, `template_instances`.
- **New CLI commands**: `serve-web`, `vapid-keys`, `push-note`,
  `list-reverse-queue`, `digest`, `template list/push`,
  `plugins list/enable/disable/info`.
- **Dependencies**: `fastapi`, `uvicorn[standard]`, `jinja2`, `pywebpush`.

### Changed
- Sync engine invokes `NoteProcessor` + `SyncHook` plugins at the correct
  pipeline stages.
- `config.example.yaml` extended with `reverse_sync`, `plugins`, `web`,
  `templates` sections plus nested `microsoft.onenote` and
  `microsoft.teams` blocks.

### Tests
- +120 new tests across plugins, reverse_sync, web, onenote, teams,
  templates.
- **408 total**, all green. Lint clean.

## [0.2.0] — 2026-04-14

Response push, semantic search, Outlook integration, foundation fixes.

### Added
- **Response push loop** fully wired — `ResponsePDFGenerator`,
  `NotebookWriter`, and `ResponseUploader` are now invoked from the sync
  engine. Auto-trigger on Q: patterns or blue-ink strokes.
- **ResponseGenerator** (`src/response/generator.py`) — orchestrates Q&A
  generation, analysis, and format selection.
- **Semantic search / RAG** (`src/search/`) — strategy-pattern embedding
  backends (Voyage, OpenAI, local sentence-transformers), `sqlite-vec`
  vector store, Markdown-aware chunker, LLM-synthesized answers with
  wiki-link citations.
- **Microsoft Outlook integration**
  (`src/integrations/microsoft/`) — MSAL device-code flow, Microsoft To
  Do for action items, Outlook Calendar for deadlines.
- **Deleted-document handling** — notes removed on the tablet are archived
  to `Archive/` in the vault and removed from the search index.
- **Cost tracking** — `api_usage` table logs every API call with input /
  output tokens and estimated USD cost. Summary shown in
  `remark-bridge status`.
- **Doctor command** — `remark-bridge doctor` runs health checks for
  config, auth, vault, API keys, libcairo2, search backend, disk.
- **New CLI commands**: `respond`, `ask`, `reindex`, `setup-microsoft`,
  `doctor`.
- **MCP tools**: `remarkable_generate_response`, `remarkable_ask`.

### Tests
- +114 new tests (response flow, search, Microsoft, foundation).
- **288 total**, all green.

## [0.1.0] — 2026-04-13

Initial release. Unidirectional sync from reMarkable → Obsidian.

### Added
- reMarkable Cloud auth (JWT device + user tokens).
- Sync 1.5 protocol client for listing, downloading, uploading.
- Multi-engine OCR pipeline: CRDT text → MyScript → Google Vision → VLM.
- Processing pipeline: structurer, action extractor, tagger, summarizer.
- Obsidian vault integration with YAML frontmatter and action files.
- Git auto-commit + push for the vault.
- Real-time WebSocket watcher.
- CLI with `setup`, `sync`, `watch`, `status`, `process`, `push`, `serve`,
  `migrate`.
- MCP server with 6 tools for any MCP-compatible client.
- systemd service and timer for VPS deployment.

### Tests
- 174 tests, all green.
