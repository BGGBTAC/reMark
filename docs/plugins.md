# Plugin Development Guide

reMark's plugin system lets you extend the sync pipeline without forking the
project. Four hook types cover the most common extension points, and a
plugin can implement any combination of them.

---

## Table of contents

1. [Architecture](#architecture)
2. [Anatomy of a plugin](#anatomy-of-a-plugin)
3. [Hook reference](#hook-reference)
4. [Distribution](#distribution)
5. [Configuration](#configuration)
6. [CLI management](#cli-management)
7. [Testing plugins](#testing-plugins)
8. [Examples](#examples)
9. [Pitfalls & FAQ](#pitfalls--faq)

---

## Architecture

```
                          ┌─────────────────────┐
                          │   SyncEngine        │
                          └─────────────────────┘
                                    │
         ┌──────────────────────────┼──────────────────────────┐
         ▼                          ▼                          ▼
  SyncHook.before_sync        ActionExtractorHook.extract
                              (merged into built-in actions)

                              ┌─────────────────────┐
                              │   write to vault    │
                              └─────────────────────┘
                                    ▲
                          NoteProcessorHook.process
                          (last chance to mutate content + frontmatter)
                                    │
                          ┌─────────────────────┐
                          │   OCRBackendHook    │  ← used by the OCR pipeline
                          │   .recognize_page   │    as a peer to built-in
                          └─────────────────────┘    engines
                                    │
                                    ▼
                          SyncHook.after_sync
```

All hooks live in `src.plugins.hooks`. The registry in
`src.plugins.registry.PluginRegistry` discovers plugins, filters them by
hook type, and hands them to the engine on demand.

---

## Anatomy of a plugin

A minimal plugin is a single class. The only mandatory piece is the
`metadata` property.

```python
from src.plugins.hooks import ActionExtractorHook, PluginMetadata


class MyPlugin(ActionExtractorHook):

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="my-plugin",           # must be globally unique
            version="0.1.0",
            description="One-line summary shown in `plugins list`.",
            author="Your Name",
        )

    async def extract(self, text: str, context: dict) -> list[dict]:
        if "urgent" not in text.lower():
            return []
        return [
            {
                "task": "Review urgent mention",
                "type": "task",
                "priority": "high",
            }
        ]
```

Save it as `~/.config/remark/plugins/my_plugin.py`. Run
`remark-bridge plugins list` and you should see it loaded.

### Rules

- **Module files**: any `.py` file in the plugin directory is scanned.
  Files starting with `_` are ignored (including `__init__.py`).
- **Class discovery**: every top-level class in the module that subclasses
  `Plugin` (directly or via a hook class) is instantiated. The class name
  does not matter — the `metadata.name` is what identifies the plugin.
- **Uniqueness**: if two plugins return the same `metadata.name`, the first
  one wins and subsequent duplicates are skipped with a warning.
- **Error isolation**: exceptions in a plugin are logged and swallowed by
  the engine. A broken plugin never aborts a sync cycle.

---

## Hook reference

All hooks live in [`src/plugins/hooks.py`](../src/plugins/hooks.py).

### `ActionExtractorHook`

Emit additional action items from a note's text.

```python
async def extract(self, text: str, context: dict) -> list[dict]:
    ...
```

- `text` — the structured Markdown content of the note (post-OCR +
  processing, pre-write).
- `context` — currently `{}`, reserved for future metadata (note title,
  frontmatter snapshot, etc.).
- Return a list of dicts; each dict should have at least a `"task"` key.
  Optional keys: `type` (`task` | `question` | `followup`), `priority`,
  `assignee`, `deadline`, `source_context`.

**Note:** as of v0.3 the built-in engine integrates plugin extractors as an
addition, not a replacement. Your items are merged and deduplicated with
those produced by `src.processing.actions.ActionExtractor`.

### `OCRBackendHook`

Provide an additional OCR engine. Currently invoked from the OCR pipeline
as an option next to `remarkable_builtin`, `google_vision`, and `vlm`.

```python
async def recognize_page(self, page_image: bytes) -> dict:
    return {
        "text": "...",
        "confidence": 0.85,       # 0..1
        "engine": self.metadata.name,
    }
```

Register the backend name in `config.ocr.primary` or `config.ocr.fallback`
to make it part of the pipeline.

### `NoteProcessorHook`

Mutate the structured note **after** processing but **before** it is
written to the vault. This is the cleanest extension point for adding
frontmatter fields, rewriting headings, or injecting boilerplate sections.

```python
async def process(
    self,
    content: str,
    frontmatter: dict,
) -> tuple[str, dict]:
    frontmatter["touched_by"] = self.metadata.name
    return content + "\n\n---\n\n*enriched by my-plugin*", frontmatter
```

The return tuple **replaces** the existing content/frontmatter. If you
don't want to change anything, return the original values.

### `SyncHook`

Observe lifecycle events — e.g. send metrics, fire a webhook, warm a cache.
All three methods are optional; override only what you need.

```python
async def before_sync(self, context: dict) -> None:
    ...

async def after_sync(self, context: dict, report: dict) -> None:
    # report keys: total, success, skipped, errors, duration_ms
    ...

async def after_document(
    self, doc_id: str, vault_path: str, result: dict,
) -> None:
    ...
```

`SyncHook` plugins are **read-only by contract** — they should not modify
vault state. Use `NoteProcessorHook` for that.

---

## Distribution

### Drop-in file

Simplest path: a single `.py` file in `~/.config/remark/plugins/`. No
packaging, no `__init__.py`, no pip install. Great for one-off hacks.

### Pip-installed package

For sharing plugins across machines, publish a package to PyPI and expose
an entry point:

```toml
# pyproject.toml of your plugin package
[project]
name = "remark-bridge-mention-plugin"
version = "0.1.0"
dependencies = ["remark-bridge>=0.3"]

[project.entry-points."remark_bridge.plugins"]
at-mention = "remark_plugin.mention:AtMentionExtractor"
```

The entry point can resolve to either:
- a **Plugin subclass** (most common)
- a **module** — reMark will scan it for plugin subclasses

`pip install remark-bridge-mention-plugin` inside the same venv as reMark
is then all that's needed.

---

## Configuration

In `config.yaml`:

```yaml
plugins:
  enabled: true                              # master switch
  plugin_dir: "~/.config/remark/plugins"     # directory scanned on startup
  disabled:                                  # plugin names to skip at load
    - "experimental-plugin"
  settings:                                  # per-plugin settings
    at-mention-extractor:
      default_priority: high
      ignore_users: ["bot"]
```

Your plugin can consume `settings[<name>]` via:

```python
class AtMentionExtractor(ActionExtractorHook):
    def __init__(self) -> None:
        self._priority = "medium"
        self._ignore: set[str] = set()

    def configure(self, settings: dict) -> None:
        self._priority = settings.get("default_priority", self._priority)
        self._ignore = set(settings.get("ignore_users", []))
```

`configure()` is called once after instantiation if settings exist for
your plugin. Failures in `configure()` are logged but do not prevent the
plugin from loading.

---

## CLI management

```bash
remark-bridge plugins list              # discovered plugins + hook types
remark-bridge plugins info <name>       # detailed metadata
remark-bridge plugins enable <name>     # flip enabled=1 in plugin_state
remark-bridge plugins disable <name>    # flip enabled=0
```

Note that `disable`/`enable` persist into the state DB
(`plugin_state` table). For durable config-level disablement, use
`plugins.disabled` in `config.yaml` — those names are rejected at
discovery time.

---

## Testing plugins

Unit tests run fine without reMark's sync engine as long as you import
the hook class and call it directly:

```python
import pytest
from my_plugin import AtMentionExtractor


@pytest.mark.asyncio
async def test_extracts_mentions():
    plugin = AtMentionExtractor()
    actions = await plugin.extract("@alice please review", context={})
    assert len(actions) == 1
    assert actions[0]["assignee"] == "alice"
```

For integration coverage, use the `PluginRegistry` in test:

```python
from src.config import PluginConfig
from src.plugins.registry import PluginRegistry


def test_discovery(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "p.py").write_text(PLUGIN_SOURCE)

    reg = PluginRegistry(PluginConfig(plugin_dir=str(plugin_dir)))
    reg.discover()

    assert reg.get("p-name") is not None
```

See [tests/test_plugins.py](../tests/test_plugins.py) for more patterns.

---

## Examples

### Reference: @-mention extractor

[`src/plugins/examples/at_mention_extractor.py`](../src/plugins/examples/at_mention_extractor.py)
is a ~30-line plugin that promotes `@username` mentions into action items.

### Idea: GitHub issue creator (`NoteProcessorHook`)

Scan the note for `gh:` lines and open GitHub issues, replacing the text
with a link to the created issue. Add the issue URL to the frontmatter.

### Idea: Slack notification (`SyncHook`)

Post a short message to a Slack webhook after each sync cycle with the
count of processed notes and open action items.

### Idea: Domain-specific OCR (`OCRBackendHook`)

Route handwriting containing musical notation to a specialized model, or
route math-heavy pages to a LaTeX-focused transcriber.

---

## Pitfalls & FAQ

### My plugin loads but nothing happens.

Run `remark-bridge plugins info <name>` and check that the correct hook
classes are listed. If the hook is empty, double-check that your class
subclasses one of `ActionExtractorHook`, `OCRBackendHook`,
`NoteProcessorHook`, `SyncHook` — not `Plugin` directly.

### `import src.plugins.hooks` fails.

You must install reMark in the same virtualenv before running plugins
that import from `src.*`. When writing distributable packages, consider
copying the minimal interface types into your package so your plugin
doesn't hard-depend on the private module path.

### Can I read `config.yaml` from a plugin?

Yes — use `src.config.load_config()`. But prefer reading settings through
`configure()` so your plugin works in test environments where there's no
config file.

### Can a plugin replace a built-in engine?

`ActionExtractorHook` results are **merged** with the built-in extractor;
you cannot disable the built-in. If you need a replacement, fork
`src.processing.actions.ActionExtractor` and set
`config.processing.extract_actions: false` to suppress the built-in
version before returning.

### How are plugin errors logged?

They go to the normal reMark log (`logging.file` in config). Grep for
the plugin name — each load failure logs the class or module name and the
traceback summary.

### Can plugins be async?

Yes — all hook methods except `configure()` are async. Use the standard
`asyncio` primitives. Be mindful that `SyncHook.before_sync` runs
sequentially for all registered plugins; don't block for long.

---

For bug reports or proposals about the plugin system itself, open an
issue on the [GitHub repo](https://github.com/BGGBTAC/reMark/issues).
