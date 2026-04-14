# Changelog

All notable changes to **reMark** are documented here. The project follows
[Semantic Versioning](https://semver.org/) and its commits group into the
phases described in the release notes.

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
