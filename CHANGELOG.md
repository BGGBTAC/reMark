# Changelog

All notable changes to **reMark** are documented here. The project follows
[Semantic Versioning](https://semver.org/) and its commits group into the
phases described in the release notes.

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
  vector store, Markdown-aware chunker, Claude-synthesized answers with
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
- MCP server with 6 tools for Claude Desktop / Code.
- systemd service and timer for VPS deployment.

### Tests
- 174 tests, all green.
