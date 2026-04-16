"""MCP server exposing reMark tools for Claude Desktop / Claude Code.

Provides tools to trigger sync, search notes, manage actions,
and interact with the reMarkable vault directly from Claude.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from mcp.server.stdio import stdio_server
from mcp.types import Resource, TextContent, Tool

from mcp.server import Server
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
        Tool(
            name="remarkable_ask",
            description=(
                "Ask a natural-language question against your synced notes. "
                "Uses semantic search to find relevant passages and optionally "
                "synthesizes a grounded answer with wiki-link citations."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The question to ask",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of source chunks to retrieve",
                        "default": 5,
                    },
                    "with_answer": {
                        "type": "boolean",
                        "description": "Synthesize an answer from retrieved chunks",
                        "default": True,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="remarkable_generate_response",
            description=(
                "Generate a response document (PDF or native notebook) for a specific "
                "synced note and push it back to the reMarkable tablet. Optionally "
                "uses Claude to answer questions found in the note."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "note_path": {
                        "type": "string",
                        "description": "Path relative to vault root, e.g. 'Notes/Work/Meeting.md'",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["pdf", "notebook"],
                        "description": "Response format. Defaults to config.response.format.",
                    },
                },
                "required": ["note_path"],
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
    elif name == "remarkable_ask":
        return await _tool_ask(
            config,
            arguments.get("query", ""),
            arguments.get("top_k", 5),
            arguments.get("with_answer", True),
        )
    elif name == "remarkable_generate_response":
        return await _tool_generate_response(
            config,
            arguments.get("note_path", ""),
            arguments.get("format"),
        )
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _tool_ask(
    config: AppConfig,
    query: str,
    top_k: int,
    with_answer: bool,
) -> list[TextContent]:
    """Run a semantic search with optional synthesis."""
    if not query:
        return [TextContent(type="text", text="Missing required parameter: query")]

    if not config.search.enabled:
        return [TextContent(
            type="text",
            text="Search is disabled. Enable in config.yaml under search.enabled: true.",
        )]

    from src.search.backends import build_backend
    from src.search.index import VectorIndex
    from src.search.query import SearchQuery
    from src.sync.engine import SyncEngine

    try:
        backend = build_backend(
            config.search.backend,
            model=config.search.model,
            api_key_env=config.search.api_key_env,
        )
        index = VectorIndex(
            db_path=resolve_path(config.sync.state_db),
            dimension=backend.dimension,
        )

        if index.stats()["total_chunks"] == 0:
            return [TextContent(
                type="text",
                text="Index is empty. Run `remark-bridge reindex` first.",
            )]

        client = None
        synthesize = with_answer and config.search.synthesize_answer
        if synthesize:
            engine = SyncEngine(config)
            client = engine._get_anthropic()

        searcher = SearchQuery(
            backend=backend,
            index=index,
            anthropic_client=client,
            synthesis_model=config.search.synthesis_model,
        )

        result = await searcher.ask(
            query,
            top_k=top_k,
            min_score=config.search.min_score,
            synthesize=synthesize,
        )

        if not result.has_results:
            return [TextContent(type="text", text=f"No notes matched '{query}'.")]

        parts = []
        if result.answer:
            parts.append("## Answer\n\n" + result.answer)

        parts.append(f"\n## Sources ({len(result.hits)})\n")
        for i, hit in enumerate(result.hits, 1):
            from pathlib import Path
            note_name = Path(hit.vault_path).stem
            heading = hit.heading_context
            header = f"**[{i}] [[{note_name}]]** (score: {hit.score:.2f})"
            if heading:
                header += f" — {heading}"
            parts.append(header)
            preview = hit.content.strip()
            if len(preview) > 400:
                preview = preview[:400] + "..."
            parts.append(f"> {preview}\n")

        return [TextContent(type="text", text="\n".join(parts))]

    except Exception as e:
        return [TextContent(type="text", text=f"Query failed: {e}")]


async def _tool_generate_response(
    config: AppConfig,
    note_path: str,
    format_: str | None,
) -> list[TextContent]:
    """Generate a response document and push it to reMarkable."""
    from src.remarkable.auth import AuthManager
    from src.remarkable.cloud import RemarkableCloud
    from src.sync.engine import SyncEngine

    if not note_path:
        return [TextContent(type="text", text="Missing required parameter: note_path")]

    if format_ in ("pdf", "notebook"):
        config.response.format = format_

    vault = _get_vault(config)
    full_path = vault.path / note_path
    if not full_path.exists():
        return [TextContent(type="text", text=f"Note not found: {note_path}")]

    try:
        auth = AuthManager(resolve_path(config.remarkable.device_token_path))
        engine = SyncEngine(config)

        async with RemarkableCloud(auth) as cloud:
            success = await engine.generate_response_for_note(full_path, cloud)

        if success:
            return [TextContent(
                type="text",
                text=(
                    f"Response pushed to reMarkable folder "
                    f"'{config.response.response_folder}' for '{note_path}'."
                ),
            )]
        return [TextContent(
            type="text",
            text=f"Response generation failed for '{note_path}' (see server logs).",
        )]

    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def _tool_sync_now(config: AppConfig) -> list[TextContent]:
    """Trigger an immediate sync cycle."""
    # Importing here to avoid circular deps and heavy init at startup
    from src.ocr.pipeline import build_pipeline
    from src.remarkable.auth import AuthManager
    from src.remarkable.cloud import RemarkableCloud
    from src.remarkable.documents import DocumentManager
    from src.sync.engine import SyncEngine

    try:
        auth = AuthManager(resolve_path(config.remarkable.device_token_path))
        engine = SyncEngine(config)
        ocr_pipeline = build_pipeline(config, llm_client=engine._get_llm_client())
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
