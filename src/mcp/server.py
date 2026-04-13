"""MCP server exposing reMark tools for Claude Desktop / Claude Code.

Provides tools to trigger sync, search notes, manage actions,
and interact with the reMarkable vault directly from Claude.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Resource, TextContent, Tool

from src.config import AppConfig, load_config, resolve_path
from src.obsidian.vault import ObsidianVault
from src.sync.state import SyncState

logger = logging.getLogger(__name__)

app = Server("remark-bridge")


def _get_config() -> AppConfig:
    return load_config()


def _get_vault(config: AppConfig) -> ObsidianVault:
    return ObsidianVault(
        Path(config.obsidian.vault_path).expanduser(),
        config.obsidian.folder_map,
    )


def _get_state(config: AppConfig) -> SyncState:
    return SyncState(resolve_path(config.sync.state_db))


# -- Tools --

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="remarkable_sync_now",
            description="Trigger an immediate sync cycle with the reMarkable Cloud",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="remarkable_search",
            description="Search across all synced notes in the Obsidian vault",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="remarkable_get_actions",
            description="List all extracted action items, optionally filtered by status",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter: 'open', 'all'",
                        "default": "open",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="remarkable_get_note",
            description="Read the full content of a specific note from the vault",
            inputSchema={
                "type": "object",
                "properties": {
                    "note_path": {
                        "type": "string",
                        "description": "Path relative to vault root, e.g. 'Notes/Work/Meeting.md'",
                    },
                },
                "required": ["note_path"],
            },
        ),
        Tool(
            name="remarkable_status",
            description="Get current sync status: last sync time, pending items, errors, stats",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="remarkable_list_notes",
            description="List all synced notes from reMarkable",
            inputSchema={
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": "Filter by vault folder (optional)",
                    },
                },
                "required": [],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    config = _get_config()

    if name == "remarkable_sync_now":
        return await _tool_sync_now(config)
    elif name == "remarkable_search":
        return _tool_search(config, arguments.get("query", ""))
    elif name == "remarkable_get_actions":
        return _tool_get_actions(config, arguments.get("status", "open"))
    elif name == "remarkable_get_note":
        return _tool_get_note(config, arguments.get("note_path", ""))
    elif name == "remarkable_status":
        return _tool_status(config)
    elif name == "remarkable_list_notes":
        return _tool_list_notes(config, arguments.get("folder"))
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _tool_sync_now(config: AppConfig) -> list[TextContent]:
    """Trigger an immediate sync cycle."""
    # Importing here to avoid circular deps and heavy init at startup
    from src.ocr.pipeline import OCRPipeline
    from src.remarkable.auth import AuthManager
    from src.remarkable.cloud import RemarkableCloud
    from src.remarkable.documents import DocumentManager
    from src.sync.engine import SyncEngine

    try:
        auth = AuthManager(resolve_path(config.remarkable.device_token_path))
        engine = SyncEngine(config)
        ocr_pipeline = OCRPipeline(config.ocr)
        download_dir = resolve_path(config.sync.state_db).parent / "downloads"

        async with RemarkableCloud(auth) as cloud:
            doc_manager = DocumentManager(cloud, download_dir)
            report = await engine.sync_once(cloud, doc_manager, ocr_pipeline)

        result = (
            f"Sync complete: {report.success_count} processed, "
            f"{report.skipped} skipped, {report.errors} errors "
            f"({report.duration_ms}ms)"
        )
        return [TextContent(type="text", text=result)]

    except Exception as e:
        return [TextContent(type="text", text=f"Sync failed: {e}")]


def _tool_search(config: AppConfig, query: str) -> list[TextContent]:
    """Search across all synced notes."""
    vault = _get_vault(config)
    vault_path = vault.path
    query_lower = query.lower()
    results = []

    for md_file in vault_path.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
            if query_lower in content.lower():
                # Find matching lines for context
                rel_path = md_file.relative_to(vault_path)
                matches = []
                for i, line in enumerate(content.split("\n"), 1):
                    if query_lower in line.lower():
                        matches.append(f"  L{i}: {line.strip()[:100]}")

                results.append(f"**{rel_path}**\n" + "\n".join(matches[:5]))
        except Exception:
            continue

    if not results:
        return [TextContent(type="text", text=f"No results for '{query}'")]

    header = f"Found {len(results)} notes matching '{query}':\n\n"
    return [TextContent(type="text", text=header + "\n\n".join(results[:20]))]


def _tool_get_actions(config: AppConfig, status: str) -> list[TextContent]:
    """List action items from the vault."""
    vault = _get_vault(config)
    actions_dir = vault.path / "Actions"

    if not actions_dir.exists():
        return [TextContent(type="text", text="No action items found.")]

    all_items = []
    for action_file in actions_dir.glob("*-actions.md"):
        content = action_file.read_text(encoding="utf-8")
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("- ["):
                is_open = line.startswith("- [ ]") or line.startswith("- [?]")
                if status == "all" or (status == "open" and is_open):
                    source = action_file.stem.replace("-actions", "")
                    all_items.append(f"{line}  ← {source}")

    if not all_items:
        return [TextContent(type="text", text=f"No {status} action items found.")]

    header = f"{len(all_items)} {status} action items:\n\n"
    return [TextContent(type="text", text=header + "\n".join(all_items))]


def _tool_get_note(config: AppConfig, note_path: str) -> list[TextContent]:
    """Read a specific note."""
    vault = _get_vault(config)
    full_path = vault.path / note_path

    result = vault.read_note(full_path)
    if result is None:
        return [TextContent(type="text", text=f"Note not found: {note_path}")]

    fm, content = result
    fm_str = "\n".join(f"{k}: {v}" for k, v in fm.items())
    return [TextContent(type="text", text=f"---\n{fm_str}\n---\n\n{content}")]


def _tool_status(config: AppConfig) -> list[TextContent]:
    """Get sync status."""
    state = _get_state(config)
    stats = state.get_sync_stats()
    state.close()

    lines = [
        f"Total documents: {stats.total_docs}",
        f"Synced: {stats.synced}",
        f"Errors: {stats.errors}",
        f"Pending responses: {stats.pending}",
        f"Total pages: {stats.total_pages}",
        f"Total action items: {stats.total_actions}",
        f"Last sync: {stats.last_sync or 'never'}",
    ]

    return [TextContent(type="text", text="\n".join(lines))]


def _tool_list_notes(config: AppConfig, folder: str | None) -> list[TextContent]:
    """List all synced notes."""
    vault = _get_vault(config)
    notes = vault.list_notes_by_source("remarkable")

    if folder:
        notes = [n for n in notes if folder.lower() in str(n).lower()]

    if not notes:
        return [TextContent(type="text", text="No synced notes found.")]

    lines = []
    for note in sorted(notes):
        rel = note.relative_to(vault.path)
        lines.append(str(rel))

    header = f"{len(lines)} synced notes:\n\n"
    return [TextContent(type="text", text=header + "\n".join(lines))]


# -- Resources --

@app.list_resources()
async def list_resources() -> list[Resource]:
    return [
        Resource(
            uri="remarkable://sync-status",
            name="Sync Status",
            description="Current sync state and statistics",
            mimeType="text/plain",
        ),
    ]


@app.read_resource()
async def read_resource(uri: str) -> str:
    config = _get_config()

    if uri == "remarkable://sync-status":
        state = _get_state(config)
        stats = state.get_sync_stats()
        state.close()
        return json.dumps({
            "total_docs": stats.total_docs,
            "synced": stats.synced,
            "errors": stats.errors,
            "pending": stats.pending,
            "last_sync": stats.last_sync,
        })

    return f"Unknown resource: {uri}"


async def run_server() -> None:
    """Start the MCP server on stdio."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())
