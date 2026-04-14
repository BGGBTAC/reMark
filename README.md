# reMark

[![CI](https://github.com/BGGBTAC/reMark/actions/workflows/ci.yml/badge.svg)](https://github.com/BGGBTAC/reMark/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/remark-bridge)](https://pypi.org/project/remark-bridge/)
[![Downloads](https://img.shields.io/pypi/dm/remark-bridge)](https://pypi.org/project/remark-bridge/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: CC BY-NC 4.0](https://img.shields.io/badge/license-CC%20BY--NC%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by-nc/4.0/)
[![Non-Commercial](https://img.shields.io/badge/use-non--commercial-red.svg)](LICENSE)

**Bidirectional sync between reMarkable tablets and an Obsidian knowledge base** with multi-engine OCR, intelligent note processing, semantic search, and automatic action item extraction.

Write on your reMarkable. reMark handles the rest — your handwritten notes become structured, searchable Markdown in Obsidian (optionally mirrored to OneNote), complete with tags, summaries, and action items. Push responses back to the tablet, query your vault in natural language, drive Microsoft To Do / Calendar / Teams, and run the whole thing with a web dashboard + mobile PWA.

> **Latest:** v0.3.0 — Web Dashboard + PWA, OneNote & Teams integration, Obsidian → reMarkable reverse sync, on-device templates, and a plugin system. See [CHANGELOG.md](CHANGELOG.md) for the full history.

---

## Features

### Core sync & processing
- **Multi-engine OCR pipeline** — reMarkable built-in (MyScript), Google Cloud Vision, or VLM-based recognition with automatic fallback
- **Intelligent structuring** — raw handwriting becomes clean Markdown with inferred headings, lists, and formatting
- **Action item extraction** — tasks, questions, follow-ups from both text patterns and pen color annotations
- **Auto-tagging** and **summarization** — categorizes and condenses notes
- **Real-time sync** — WebSocket notifications or configurable cron schedule

### Knowledge management
- **Obsidian vault integration** — YAML frontmatter, wiki-links, Git sync
- **Semantic search (RAG)** — embed the entire vault and query it with natural language. Backends for Voyage, OpenAI, or local sentence-transformers
- **OneNote mirror** _(v0.3)_ — write notes in parallel to Microsoft OneNote
- **Deletion-aware** — notes removed from the tablet are archived in the vault automatically

### Response + reverse push
- **Response push** — auto-generates PDFs or native reMarkable notebooks that answer questions in your notes
- **Reverse sync** _(v0.3)_ — push Obsidian notes back to the tablet (frontmatter flag, folder-based, or on-demand)
- **On-device templates** _(v0.3)_ — push structured templates (meeting, daily, project review) to the tablet and extract the filled fields back into frontmatter

### Integrations
- **Microsoft Outlook** — action items → Microsoft To Do, deadlines → Outlook Calendar
- **Microsoft Teams** _(v0.3)_ — daily/weekly digest Adaptive Cards, meeting ↔ note correlation
- **MCP server** — interact with your notes directly from Claude Desktop or Claude Code

### Web + PWA
- **Web dashboard** _(v0.3)_ — FastAPI + HTMX + Alpine.js + Tailwind, no build step
- **Mobile PWA** _(v0.3)_ — installable app with Web Push notifications and quick-entry from your phone
- **Start on demand** via `remark-bridge serve-web`

### Extensibility
- **Plugin system** _(v0.3)_ — custom action extractors, OCR backends, note post-processors, sync hooks. Load from a local directory or pip-installed packages.

### Operations
- **Cost tracking** — token usage and USD cost logged per API call
- **Health check** — `remark-bridge doctor` verifies config, auth, vault, API keys, and system deps
- **Idempotent** — SQLite state tracking with WAL mode; notes are never processed twice

## Architecture

```
┌──────────────────┐    reMarkable Cloud API    ┌──────────────────────────┐
│  reMarkable       │◄─────────────────────────►│  reMark                  │
│  Paper Pro / rM2  │    (sync 1.5 / JWT)        │                          │
└──────────────────┘                              │  ┌────────────────────┐ │
        ▲                                         │  │ sync_engine        │ │
        │                                         │  │  ├─ ocr_pipeline   │ │
   (PDF│/notebook                                 │  │  ├─ processing     │ │
    push│back)                                    │  │  ├─ vault_writer   │ │
        │                                         │  │  ├─ search_indexer │ │
        │                                         │  │  ├─ response_push  │ │
        │                                         │  │  ├─ reverse_sync   │ │
        │                                         │  │  ├─ plugins        │ │
        │                                         │  │  └─ integrations   │ │
        │                                         │  └────────────────────┘ │
        │                                         │          │              │
        └───────response / reverse-push───────────┤          ▼              │
                                                  │  ┌────────────────────┐ │
                                                  │  │ Obsidian Vault     │ │
                                                  │  │  (Git-synced)      │ │
                                                  │  │ + OneNote (opt.)   │ │
                                                  │  └────────────────────┘ │
                                                  │          │              │
                                                  │          ▼              │
                                                  │  MCP · Web UI · PWA     │
                                                  │  Outlook · Teams        │
                                                  └──────────────────────────┘
```

### Data Flow

```
1. DETECT    → New/changed notebook on reMarkable Cloud (poll or WebSocket)
2. DOWNLOAD  → Fetch .rm files + metadata via sync 1.5 protocol
3. CONVERT   → Extract text (CRDT → MyScript → primary OCR → fallback)
4. PROCESS   → Structure, actions, tags, summary, optional Q&A
5. STORE     → Write Markdown + YAML frontmatter to Obsidian vault
               (optional parallel write to OneNote)
6. INDEX     → Chunk and embed for semantic search (if enabled)
7. RESPOND   → Auto-generate response PDF / notebook; push back to tablet
8. INTEGRATE → Push action items to Microsoft To Do, deadlines to Calendar,
               post digest to Teams (if enabled)
9. REVERSE   → Pick up flagged vault notes and push them to the tablet
10. TRACK    → Update SQLite state DB, log API usage, commit vault to Git
```

## Requirements

- Python 3.11+
- A reMarkable tablet (Paper Pro or reMarkable 2)
- An Obsidian vault (local or Git-synced)
- An [Anthropic API key](https://console.anthropic.com/) for note processing
- (Optional) Google Cloud Vision credentials for OCR fallback

### VPS Deployment

- Ubuntu 22.04+ or Debian 12+
- 1 GB RAM minimum (2 GB recommended if using VLM OCR)
- 10 GB disk (vault + state + downloaded .rm files)
- Outbound HTTPS to `*.remarkable.com`, `api.anthropic.com`, `vision.googleapis.com`

## Installation

### From PyPI (recommended)

```bash
pip install remark-bridge
```

### From source

```bash
git clone https://github.com/BGGBTAC/reMark.git
cd reMark
pip install .
```

> **Note:** reMark requires `libcairo2` for SVG→PNG rendering. On Debian/Ubuntu: `sudo apt install libcairo2-dev`. On macOS: `brew install cairo`.

## Setup

```bash
# Interactive setup — authenticates with reMarkable Cloud,
# creates config, initializes vault structure and state DB
remark-bridge setup
```

During setup you'll need a one-time code from [my.remarkable.com/device/browser/connect](https://my.remarkable.com/device/browser/connect).

## Configuration

Copy and edit the example config:

```bash
cp config.example.yaml config.yaml
```

Key sections:

| Section | What it controls |
|---------|-----------------|
| `remarkable` | Cloud auth, folder filters, response folder |
| `ocr` | Primary/fallback OCR engines, confidence threshold |
| `processing` | Model selection, what to extract (actions, tags, summaries) |
| `obsidian` | Vault path, folder mapping, Git sync settings |
| `sync` | Trigger mode (realtime/scheduled/manual), WebSocket config |
| `response` | Response format (PDF/notebook), auto-trigger rules |
| `search` | Semantic search — backend, chunking, synthesis |
| `microsoft` | Outlook Tasks + Calendar + OneNote + Teams integration |
| `reverse_sync` | Obsidian → reMarkable push-back triggers |
| `plugins` | Plugin discovery + settings |
| `web` | Dashboard host/port, auth, VAPID keys |
| `templates` | On-device template engine |

See [config.example.yaml](config.example.yaml) for the full reference with comments.

## Usage

```bash
# One-shot sync
remark-bridge sync --once

# Continuous sync (scheduled + realtime based on config)
remark-bridge sync

# Real-time WebSocket watcher only
remark-bridge watch

# Process a specific notebook
remark-bridge process "Meeting Notes 2026-04-13"

# Push a generated response back to the tablet
remark-bridge respond "Meeting Notes 2026-04-13" --format pdf

# Ask your vault a question (semantic search)
remark-bridge ask "what did I decide about the API migration"

# Rebuild the semantic search index
remark-bridge reindex

# Authenticate Microsoft for Outlook/To Do
remark-bridge setup-microsoft

# Run a health check
remark-bridge doctor

# Upload a PDF to your reMarkable
remark-bridge push report.pdf --folder "Work"

# Check sync status + API cost summary
remark-bridge status

# Start MCP server (for Claude Desktop / Claude Code)
remark-bridge serve

# Start the web dashboard + PWA (v0.3)
remark-bridge serve-web

# Push a vault note back to your tablet (v0.3)
remark-bridge push-note "My Vault Note"

# Post a Teams digest (v0.3)
remark-bridge digest --period weekly --teams

# Push an on-device template (v0.3)
remark-bridge template push meeting

# Plugin management (v0.3)
remark-bridge plugins list

# Generate VAPID keys for Web Push (v0.3)
remark-bridge vapid-keys

# One-time import of all existing notebooks
remark-bridge migrate
```

### Web Dashboard + PWA

```bash
remark-bridge serve-web  # http://localhost:8080
```

Routes include `/notes`, `/actions`, `/ask`, `/quick-entry`, and `/settings`. When you generate VAPID keys and install the app to your phone's homescreen, Web Push notifications fire for high-priority action items.

### Plugin System

reMark can be extended with user-written plugins. See **[docs/plugins.md](docs/plugins.md)** for the full developer guide.

**Quick overview:**

1. Create a `.py` file in `~/.config/remark/plugins/` (configurable via `plugins.plugin_dir`), or distribute a package exposing a `remark_bridge.plugins` entry point.
2. Subclass one or more of the four hook types in [src/plugins/hooks.py](src/plugins/hooks.py):
   - `ActionExtractorHook` — emit extra action items from note text.
   - `OCRBackendHook` — provide an additional OCR engine.
   - `NoteProcessorHook` — post-process the structured note before it's written to the vault.
   - `SyncHook` — observe sync lifecycle events (`before_sync`, `after_sync`, `after_document`).
3. Each plugin must expose a `metadata` property returning a `PluginMetadata` dataclass with at least a unique `name`.
4. Optional: implement `configure(settings)` — receives `config.plugins.settings[<plugin-name>]` from `config.yaml`.

Manage plugins from the CLI:

```bash
remark-bridge plugins list              # discover + list with hooks
remark-bridge plugins info <name>       # show detailed metadata
remark-bridge plugins enable <name>
remark-bridge plugins disable <name>
```

Permanent disable list and per-plugin settings in `config.yaml`:

```yaml
plugins:
  enabled: true
  plugin_dir: "~/.config/remark/plugins"
  disabled: ["plugin-name-to-skip"]
  settings:
    at-mention-extractor:
      default_priority: high
```

Reference plugin: [src/plugins/examples/at_mention_extractor.py](src/plugins/examples/at_mention_extractor.py) — turns `@-mentions` into follow-up action items in ~30 lines.

### Semantic Search

Enable semantic search by setting `search.enabled: true` in `config.yaml`. Three backends are supported:

- **local** (default) — offline via `sentence-transformers`, no API costs. Install with `pip install 'remark-bridge[local-embeddings]'`.
- **voyage** — highest quality, requires `VOYAGE_API_KEY`. Install with `pip install 'remark-bridge[voyage]'`.
- **openai** — solid quality, requires `OPENAI_API_KEY`. Install with `pip install 'remark-bridge[openai]'`.

Embeddings are stored in the same SQLite database as the sync state (via `sqlite-vec`), so no extra infrastructure is needed.

### Microsoft Outlook Integration

Register an app at [entra.microsoft.com](https://entra.microsoft.com) (Public client, redirect URI `https://login.microsoftonline.com/common/oauth2/nativeclient`), add its client ID to `config.yaml` under `microsoft.client_id`, and run `remark-bridge setup-microsoft`. Action items will flow into Microsoft To Do; items with deadlines become Outlook Calendar events.

## reMarkable Marking Conventions

Use pen colors and text patterns to mark up your notes. reMark detects both, so it works on the monochrome rM2 too (just use text patterns).

| Mark | Pen Color | Meaning | Detection |
|------|-----------|---------|-----------|
| Action item | Red | Task to do | Color filter |
| Question | Blue | Needs follow-up | Color filter |
| Important | Yellow highlight | Key info | Color filter |
| Done | Green | Completed | Color filter |
| `TODO:` text | Any | Task | Text pattern |
| `Q:` text | Any | Question | Text pattern |
| `!` prefix | Any | Priority flag | Text pattern |
| `[ ]` checkbox | Any | Task checkbox | Text pattern |

## Deployment (systemd)

```bash
# Install on VPS
sudo apt install libcairo2-dev
pip install remark-bridge

# Run setup
remark-bridge setup

# Copy service files
sudo cp systemd/remarkable-bridge.service /etc/systemd/system/
sudo cp systemd/remarkable-bridge.timer /etc/systemd/system/

# Set your API key in the service file
sudo systemctl edit remarkable-bridge
# Add: Environment=ANTHROPIC_API_KEY=sk-ant-...

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now remarkable-bridge
```

For timer-based mode (instead of continuous):

```bash
sudo systemctl enable --now remarkable-bridge.timer
```

## Project Structure

```
reMark/
├── src/
│   ├── main.py                     # CLI entry point (Click)
│   ├── config.py                   # Config loading + Pydantic validation
│   │
│   ├── remarkable/                 # reMarkable Cloud interaction
│   │   ├── auth.py                 # JWT auth flow
│   │   ├── cloud.py                # Cloud API client (sync 1.5)
│   │   ├── documents.py            # Document listing, download, upload
│   │   ├── websocket.py            # Real-time change notifications
│   │   └── formats.py              # .rm file parsing (wraps rmscene)
│   │
│   ├── ocr/                        # Handwriting recognition pipeline
│   │   ├── pipeline.py             # OCR orchestrator (strategy pattern)
│   │   ├── remarkable_builtin.py   # MyScript conversion reader
│   │   ├── google_vision.py        # Google Cloud Vision backend
│   │   ├── vlm.py                  # Claude / GPT-4o vision backend
│   │   └── renderer.py             # .rm → PNG/SVG rendering
│   │
│   ├── processing/                 # Intelligent note processing
│   │   ├── structurer.py           # Raw text → structured Markdown
│   │   ├── actions.py              # Action item extraction
│   │   ├── tagger.py               # Auto-tagging
│   │   ├── summarizer.py           # Note summarization
│   │   └── usage.py                # Token accounting + cost tracking
│   │
│   ├── obsidian/                   # Obsidian vault management
│   │   ├── vault.py                # Read/write, archive, action items
│   │   ├── frontmatter.py          # YAML frontmatter generation
│   │   ├── templates.py            # Note content templates
│   │   └── git_sync.py             # Git commit + push
│   │
│   ├── response/                   # Push-back to reMarkable
│   │   ├── pdf_generator.py        # E-ink optimized PDF
│   │   ├── notebook_writer.py      # Native .rm notebook
│   │   ├── generator.py            # Response orchestrator (Q&A + analysis)
│   │   └── uploader.py             # Upload to Cloud (PDF + notebook zip)
│   │
│   ├── sync/                       # Sync orchestration
│   │   ├── engine.py               # Main sync loop
│   │   ├── state.py                # SQLite state tracking (WAL)
│   │   ├── scheduler.py            # Cron / interval scheduling
│   │   ├── watcher.py              # WebSocket real-time watcher
│   │   └── reverse_sync.py         # Obsidian → reMarkable (v0.3)
│   │
│   ├── search/                     # Semantic search / RAG (v0.2)
│   │   ├── backends.py             # Voyage / OpenAI / local embeddings
│   │   ├── chunker.py              # Markdown-aware chunking
│   │   ├── index.py                # sqlite-vec VectorIndex
│   │   ├── indexer.py              # Chunking + embedding orchestrator
│   │   └── query.py                # Semantic query + Claude synthesis
│   │
│   ├── integrations/               # External integrations
│   │   └── microsoft/              # Microsoft Graph
│   │       ├── auth.py             # MSAL device-code flow
│   │       ├── graph.py            # Async Graph client
│   │       ├── todo.py             # Microsoft To Do (tasks)
│   │       ├── calendar.py         # Outlook Calendar (events)
│   │       ├── onenote.py          # OneNote mirror (v0.3)
│   │       ├── teams.py            # Teams digest + meeting corr. (v0.3)
│   │       └── service.py          # High-level facade
│   │
│   ├── plugins/                    # Plugin system (v0.3)
│   │   ├── hooks.py                # ActionExtractor / OCR / Processor / Sync
│   │   ├── registry.py             # Discovery + enable/disable
│   │   └── examples/               # Reference plugins
│   │
│   ├── web/                        # Web dashboard + PWA (v0.3)
│   │   ├── app.py                  # FastAPI app, routes
│   │   ├── push.py                 # VAPID Web Push helper
│   │   ├── templates/              # Jinja2 HTML templates
│   │   └── static/                 # Service worker, manifest, JS, icons
│   │
│   ├── templates/                  # On-device template engine (v0.3)
│   │   ├── engine.py               # Render PDF + extract fields
│   │   └── builtin/                # meeting / daily / project-review
│   │
│   └── mcp/                        # MCP server
│       └── server.py               # Tools for Claude Desktop / Code
│
├── tests/                          # 408 tests across all modules
├── scripts/                        # Setup + connection test helpers
├── systemd/                        # VPS service + timer units
├── vault_template/                 # Initial Obsidian vault structure
├── config.example.yaml             # Full reference config
├── pyproject.toml
├── CHANGELOG.md
└── LICENSE
```

## Development

```bash
git clone https://github.com/BGGBTAC/reMark.git
cd reMark

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Lint
ruff check src/ tests/
```

## License

**Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)** — see [LICENSE](LICENSE).

You are free to use, modify, share, and fork this project (in whole or in part) for personal, academic, research, educational, and non-commercial purposes, as long as you give appropriate credit to **BGGBTAC**.

**Commercial use — in whole or in part — requires prior written permission.** This includes paid SaaS, paid products or services that incorporate this code, and revenue-generating internal use by for-profit organizations.

To request a commercial license, open an issue or contact the author via [GitHub](https://github.com/BGGBTAC).
