"""CLI entry point for reMark.

All user-facing commands go through this Click-based CLI.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC
from pathlib import Path

import click

from src.config import AppConfig, load_config, resolve_path


def _setup_logging(config: AppConfig) -> None:
    """Configure logging from config settings.

    Honours ``REMARK_LOG_FORMAT`` env var so operators can flip to JSON
    in production without touching config files.
    """
    import os

    from src.log_setup import configure

    log_config = config.logging
    log_file = resolve_path(log_config.file)
    fmt = os.environ.get("REMARK_LOG_FORMAT", log_config.format)
    configure(
        level=log_config.level,
        file=log_file,
        fmt=fmt,
        max_size_mb=log_config.max_size_mb,
        backup_count=log_config.backup_count,
    )


def _get_auth(config: AppConfig, device_id: str = "default"):
    """Return an AuthManager for a specific device.

    For the implicit ``default`` device we keep using the legacy
    single-token path so existing installs keep working. Named devices
    get their own token file under ``devices/<id>/`` alongside.
    """
    from src.remarkable.auth import AuthManager, device_token_path_for

    if device_id == "default":
        return AuthManager(resolve_path(config.remarkable.device_token_path))
    base_dir = Path(config.remarkable.device_token_path).expanduser().parent
    return AuthManager(device_token_path_for(device_id, base_dir))


def _get_ocr_pipeline(config: AppConfig, llm_client=None):
    from src.ocr.pipeline import build_pipeline
    return build_pipeline(config, llm_client=llm_client)


@click.group()
@click.option(
    "--config", "-c", "config_path",
    default=None,
    help="Path to config file (defaults to $REMARK_CONFIG or ./config.yaml)",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None) -> None:
    """reMark — reMarkable ↔ Obsidian sync with intelligent processing."""
    import os

    if config_path is None:
        config_path = os.environ.get("REMARK_CONFIG", "config.yaml")
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(config_path)


@cli.command()
@click.pass_context
def setup(ctx: click.Context) -> None:
    """Interactive setup — authenticate, create config, init vault."""
    config = ctx.obj["config"]

    click.echo("=== reMark Setup ===\n")

    # Step 1: Auth
    token_path = resolve_path(config.remarkable.device_token_path)
    auth = _get_auth(config)

    if auth.has_device_token():
        click.echo(f"Device token found at {token_path}")
        if not click.confirm("Re-register device?", default=False):
            click.echo("Keeping existing token.\n")
        else:
            _do_register(auth)
    else:
        _do_register(auth)

    # Step 2: Config
    if not Path("config.yaml").exists():
        click.echo("Creating config.yaml from template...")
        import shutil
        shutil.copy("config.example.yaml", "config.yaml")
        click.echo("Edit config.yaml to set your vault path and preferences.\n")
    else:
        click.echo("config.yaml already exists.\n")

    # Step 3: Vault structure
    from src.obsidian.vault import ObsidianVault
    vault = ObsidianVault(
        Path(config.obsidian.vault_path).expanduser(),
        config.obsidian.folder_map,
    )
    vault.ensure_structure()
    click.echo(f"Vault structure created at {config.obsidian.vault_path}\n")

    # Step 4: State DB
    state_path = resolve_path(config.sync.state_db)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    click.echo(f"State DB will be at {state_path}\n")

    click.echo("Setup complete. Run `remark-bridge sync --once` to test.")


def _do_register(auth) -> None:
    click.echo("\nGo to: https://my.remarkable.com/device/browser/connect")
    code = click.prompt("Enter the one-time code")

    try:
        asyncio.run(auth.register_device(code))
        click.echo("Device registered successfully!\n")
    except Exception as e:
        click.echo(f"Registration failed: {e}")
        sys.exit(1)


@cli.command()
@click.option(
    "--device", "device_id", default="default",
    help="Pair a specific registered device slug (for multi-device setups)",
)
@click.pass_context
def auth(ctx: click.Context, device_id: str) -> None:
    """Pair with reMarkable Cloud.

    Prompts for the one-time code from https://my.remarkable.com/device/browser/connect
    and stores the resulting device token. Use ``--device <id>`` when
    you've registered multiple tablets via ``remark-bridge device add``
    (the single-device default writes to the legacy token path).
    """
    config: AppConfig = ctx.obj["config"]
    _setup_logging(config)
    manager = _get_auth(config, device_id)
    if manager.has_device_token():
        click.echo(f"Device token already present for '{device_id}'.")
        if not click.confirm("Re-pair?", default=False):
            click.echo("Keeping existing token.")
            return
    _do_register(manager)


@cli.command()
@click.option("--once", is_flag=True, help="Run a single sync cycle then exit")
@click.pass_context
def sync(ctx: click.Context, once: bool) -> None:
    """Sync notes from reMarkable to Obsidian vault."""
    config = ctx.obj["config"]
    _setup_logging(config)

    if once:
        asyncio.run(_sync_once(config))
    else:
        asyncio.run(_sync_continuous(config))


async def _sync_once(config: AppConfig) -> None:
    from src.remarkable.auth import device_token_path_for
    from src.remarkable.cloud import RemarkableCloud
    from src.remarkable.documents import DocumentManager
    from src.sync.engine import SyncEngine

    engine = SyncEngine(config)
    ocr_pipeline = _get_ocr_pipeline(config, llm_client=engine._get_llm_client())
    download_dir = resolve_path(config.sync.state_db).parent / "downloads"

    # Legacy single-device path: no ``devices`` list configured, run once.
    devices = config.remarkable.devices
    if not devices:
        auth = _get_auth(config)
        async with RemarkableCloud(auth) as cloud:
            doc_manager = DocumentManager(cloud, download_dir)
            report = await engine.sync_once(cloud, doc_manager, ocr_pipeline)
        click.echo(
            f"Sync complete: {report.success_count} processed, "
            f"{report.skipped} skipped, {report.errors} errors "
            f"({report.duration_ms}ms)"
        )
        if report.errors > 0:
            for r in report.processed:
                if not r.success:
                    click.echo(f"  Error: {r.doc_name} — {r.error}")
        return

    # Multi-device: iterate configured tablets. Each device has its own
    # token file + vault subfolder so notes stay separated.
    base_dir = Path(config.remarkable.device_token_path).expanduser().parent
    for device in devices:
        click.echo(f"--- device: {device.label} ({device.id}) ---")
        engine.set_device(device.id, device.vault_subfolder)
        # Register (or refresh) the device row so the UI picker sees it.
        engine.state.register_device(
            device.id,
            device.label,
            str(device_token_path_for(device.id, base_dir)),
            device.vault_subfolder,
        )
        auth = _get_auth(config, device.id)
        # Per-device sync_folders / ignore_folders override the top
        # level only when the device has its own lists set. Use a deep
        # copy so a second concurrent cycle (scheduler vs. manual run)
        # can't see another device's filters leak through, and so a
        # mid-flight exception can't leave the shared config mutated.
        device_config = config.model_copy(deep=True)
        if device.sync_folders:
            device_config.remarkable.sync_folders = device.sync_folders
        if device.ignore_folders:
            device_config.remarkable.ignore_folders = device.ignore_folders
        device_engine = SyncEngine(device_config)
        device_engine.set_device(device.id, device.vault_subfolder)

        async with RemarkableCloud(auth) as cloud:
            doc_manager = DocumentManager(cloud, download_dir)
            report = await device_engine.sync_once(cloud, doc_manager, ocr_pipeline)
        device_engine.state.touch_device(device.id)
        click.echo(
            f"  {device.label}: {report.success_count} processed, "
            f"{report.skipped} skipped, {report.errors} errors "
            f"({report.duration_ms}ms)"
        )
        if report.errors > 0:
            for r in report.processed:
                if not r.success:
                    click.echo(f"    Error: {r.doc_name} — {r.error}")


async def _sync_continuous(config: AppConfig) -> None:
    from src.sync.engine import SyncEngine
    from src.sync.scheduler import SyncScheduler

    auth = _get_auth(config)
    engine = SyncEngine(config)
    ocr_pipeline = _get_ocr_pipeline(config, llm_client=engine._get_llm_client())
    scheduler = SyncScheduler(engine, config)

    click.echo("Starting continuous sync (Ctrl+C to stop)...")

    try:
        await scheduler.run(auth, ocr_pipeline)
    except KeyboardInterrupt:
        click.echo("\nStopping...")
        scheduler.stop()


@cli.command()
@click.pass_context
def watch(ctx: click.Context) -> None:
    """Start real-time WebSocket watcher."""
    config = ctx.obj["config"]
    _setup_logging(config)

    asyncio.run(_watch(config))


async def _watch(config: AppConfig) -> None:
    from src.sync.engine import SyncEngine
    from src.sync.watcher import RealtimeWatcher

    auth = _get_auth(config)
    engine = SyncEngine(config)
    ocr_pipeline = _get_ocr_pipeline(config, llm_client=engine._get_llm_client())
    watcher = RealtimeWatcher(engine, auth, config)

    click.echo("Starting real-time watcher (Ctrl+C to stop)...")

    try:
        await watcher.watch(ocr_pipeline)
    except KeyboardInterrupt:
        click.echo("\nStopping...")
        watcher.stop()


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show sync status and statistics."""
    config = ctx.obj["config"]
    state = None

    try:
        from src.sync.state import SyncState
        state = SyncState(resolve_path(config.sync.state_db))
        stats = state.get_sync_stats()

        click.echo("=== reMark Status ===\n")
        click.echo(f"Documents:     {stats.total_docs}")
        click.echo(f"  Synced:      {stats.synced}")
        click.echo(f"  Errors:      {stats.errors}")
        click.echo(f"  Pending:     {stats.pending}")
        click.echo(f"Total pages:   {stats.total_pages}")
        click.echo(f"Action items:  {stats.total_actions}")
        click.echo(f"Last sync:     {stats.last_sync or 'never'}")

        # Git status
        if config.obsidian.git.enabled:
            from src.obsidian.git_sync import GitSync
            git = GitSync(config.obsidian.vault_path)
            if git.is_git_repo():
                gs = git.status()
                click.echo(f"\nGit branch:    {gs['branch']}")
                click.echo(f"  Dirty:       {gs['dirty']}")
                click.echo(f"  Ahead:       {gs['ahead']}")
                click.echo(f"  Last commit: {gs['last_commit_msg'] or 'none'}")

        # API usage
        usage = state.get_api_usage_summary(days=30)
        if usage["total_calls"] > 0:
            click.echo(
                f"\nAPI usage (last 30 days):   "
                f"{usage['total_calls']} calls, "
                f"${usage['total_cost_usd']:.2f}"
            )
            for provider, stats in usage["providers"].items():
                tokens = stats["input_tokens"] + stats["output_tokens"]
                click.echo(
                    f"  {provider}:{'':<12}"
                    f"{stats['calls']} calls, "
                    f"{tokens} tokens, "
                    f"${stats['cost_usd']:.2f}"
                )

    except Exception as e:
        click.echo(f"Error reading status: {e}")
    finally:
        if state:
            state.close()


@cli.command()
@click.argument("notebook_name")
@click.pass_context
def process(ctx: click.Context, notebook_name: str) -> None:
    """Manually process a specific notebook by name."""
    config = ctx.obj["config"]
    _setup_logging(config)

    click.echo(f"Processing: {notebook_name}")
    asyncio.run(_process_single(config, notebook_name))


@cli.command()
@click.argument("notebook_name")
@click.option(
    "--format", "format_",
    type=click.Choice(["pdf", "notebook"]),
    default=None,
    help="Response format. Defaults to config.response.format.",
)
@click.pass_context
def respond(ctx: click.Context, notebook_name: str, format_: str | None) -> None:
    """Generate and push a response back to the tablet for a specific note."""
    config = ctx.obj["config"]
    _setup_logging(config)

    if format_:
        config.response.format = format_

    click.echo(f"Generating response for: {notebook_name}")
    asyncio.run(_respond(config, notebook_name))


async def _respond(config: AppConfig, notebook_name: str) -> None:
    from pathlib import Path

    from src.remarkable.cloud import RemarkableCloud
    from src.sync.engine import SyncEngine

    engine = SyncEngine(config)
    auth = _get_auth(config)

    note_path = None
    vault_path = Path(config.obsidian.vault_path).expanduser()
    for md_file in vault_path.rglob("*.md"):
        result = engine.vault.read_note(md_file)
        if result is None:
            continue
        fm, _ = result
        if fm.get("title") == notebook_name or md_file.stem == notebook_name:
            note_path = md_file
            break

    if note_path is None:
        click.echo(f"Note '{notebook_name}' not found in vault.")
        return

    async with RemarkableCloud(auth) as cloud:
        success = await engine.generate_response_for_note(note_path, cloud)

    if success:
        click.echo(f"Response uploaded to reMarkable '{config.response.response_folder}' folder.")
    else:
        click.echo("Response generation failed (see logs).")


async def _process_single(config: AppConfig, notebook_name: str) -> None:
    from src.remarkable.cloud import RemarkableCloud
    from src.remarkable.documents import DocumentManager
    from src.sync.engine import SyncEngine

    auth = _get_auth(config)
    engine = SyncEngine(config)
    ocr_pipeline = _get_ocr_pipeline(config, llm_client=engine._get_llm_client())
    download_dir = resolve_path(config.sync.state_db).parent / "downloads"

    async with RemarkableCloud(auth) as cloud:
        doc_manager = DocumentManager(cloud, download_dir)
        docs = await doc_manager.list_documents()

        target = None
        for doc in docs:
            if doc.name == notebook_name:
                target = doc
                break

        if target is None:
            click.echo(f"Notebook '{notebook_name}' not found on reMarkable.")
            return

        result = await engine.process_document(target, doc_manager, ocr_pipeline)

    if result.success:
        click.echo(f"Done: {result.page_count} pages, {result.action_count} actions")
        click.echo(f"Written to: {result.vault_path}")
    else:
        click.echo(f"Failed: {result.error}")


@cli.command()
@click.argument("file_path", type=click.Path(exists=True))
@click.option("--folder", "-f", default="", help="Target folder on reMarkable")
@click.pass_context
def push(ctx: click.Context, file_path: str, folder: str) -> None:
    """Upload a file (PDF/EPUB) to reMarkable."""
    config = ctx.obj["config"]
    _setup_logging(config)

    asyncio.run(_push_file(config, Path(file_path), folder))


@cli.command()
@click.argument("query")
@click.option("--top-k", "-k", default=5, help="Number of results to return")
@click.option("--with-answer/--no-answer", default=True,
              help="Synthesize a grounded answer from hits")
@click.pass_context
def ask(ctx: click.Context, query: str, top_k: int, with_answer: bool) -> None:
    """Ask a natural-language question against your synced notes."""
    config = ctx.obj["config"]
    _setup_logging(config)

    if not config.search.enabled:
        click.echo("Search is disabled. Enable it in config.yaml under `search.enabled: true`.")
        return

    asyncio.run(_ask(config, query, top_k, with_answer))


async def _ask(config: AppConfig, query: str, top_k: int, with_answer: bool) -> None:
    from src.search.backends import build_backend
    from src.search.index import VectorIndex
    from src.search.query import SearchQuery
    from src.sync.engine import SyncEngine

    backend = build_backend(
        config.search.backend,
        model=config.search.model,
        api_key_env=config.search.api_key_env,
    )
    index = VectorIndex(
        db_path=resolve_path(config.sync.state_db),
        dimension=backend.dimension,
    )

    stats = index.stats()
    if stats["total_chunks"] == 0:
        click.echo("Index is empty. Run `remark-bridge reindex` first.")
        return

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
        mode=config.search.mode,
    )

    if not result.has_results:
        click.echo(f"No notes matched '{query}'.")
        return

    if result.answer:
        click.echo("\n=== Answer ===\n")
        click.echo(result.answer)
        click.echo()

    click.echo(f"=== Top {len(result.hits)} sources ===\n")
    for i, hit in enumerate(result.hits, 1):
        note_name = Path(hit.vault_path).stem
        heading = hit.heading_context
        header = f"[{i}] {note_name} (score: {hit.score:.2f})"
        if heading:
            header += f" — {heading}"
        click.echo(header)
        preview = hit.content.replace("\n", " ").strip()
        if len(preview) > 200:
            preview = preview[:200] + "..."
        click.echo(f"    {preview}\n")


@cli.command(name="push-note")
@click.argument("note_name")
@click.option(
    "--format", "format_",
    type=click.Choice(["pdf", "notebook"]),
    default=None,
    help="Output format. Defaults to config.reverse_sync.format.",
)
@click.pass_context
def push_note(ctx: click.Context, note_name: str, format_: str | None) -> None:
    """Push a vault note to the reMarkable tablet."""
    config = ctx.obj["config"]
    _setup_logging(config)

    if format_:
        config.reverse_sync.format = format_

    asyncio.run(_push_note(config, note_name))


async def _push_note(config: AppConfig, note_name: str) -> None:
    from src.remarkable.cloud import RemarkableCloud
    from src.sync.engine import SyncEngine
    from src.sync.reverse_sync import ReverseSyncer

    engine = SyncEngine(config)
    auth = _get_auth(config)

    vault_path = Path(config.obsidian.vault_path).expanduser()
    target = None
    for md in vault_path.rglob("*.md"):
        result = engine.vault.read_note(md)
        if result is None:
            continue
        fm, _ = result
        if fm.get("title") == note_name or md.stem == note_name:
            target = md
            break

    if target is None:
        click.echo(f"Note '{note_name}' not found in vault.")
        return

    syncer = ReverseSyncer(config.reverse_sync, engine.vault, engine.state)
    async with RemarkableCloud(auth) as cloud:
        rm_doc_id = await syncer.push_single(target, cloud)

    if rm_doc_id:
        click.echo(f"Pushed '{note_name}' to tablet '{config.reverse_sync.target_folder}' folder.")
    else:
        click.echo("Push failed (see logs).")


@cli.command(name="list-reverse-queue")
@click.option("--status", default="pending", help="pending | pushed | error")
@click.pass_context
def list_reverse_queue(ctx: click.Context, status: str) -> None:
    """Show the reverse-sync queue."""
    config = ctx.obj["config"]
    state = SyncState(resolve_path(config.sync.state_db))
    entries = state.get_reverse_queue(status=status)

    if not entries:
        click.echo(f"No entries with status '{status}'.")
        state.close()
        return

    click.echo(f"\n{len(entries)} entries:\n")
    for e in entries:
        click.echo(f"  {e['vault_path']}")
        click.echo(f"    Queued: {e['queued_at']}  Status: {e['status']}")
        if e.get("remarkable_doc_id"):
            click.echo(f"    Pushed: {e['pushed_at']} → {e['remarkable_doc_id'][:8]}")
        if e.get("error"):
            click.echo(f"    Error: {e['error']}")
        click.echo()
    state.close()


@cli.group()
def template() -> None:
    """Manage on-device note templates."""


@template.command("list")
@click.pass_context
def template_list(ctx: click.Context) -> None:
    """List available templates."""
    config = ctx.obj["config"]
    from src.templates.engine import TemplateEngine

    engine = TemplateEngine(config.templates.user_templates_dir)
    templates = engine.list_templates()

    if not templates:
        click.echo("No templates found.")
        return

    click.echo(f"\n{len(templates)} template(s):\n")
    for t in templates:
        click.echo(f"  {t.name}")
        if t.description:
            click.echo(f"    {t.description}")
        click.echo(f"    Fields: {', '.join(f.name for f in t.fields)}")
        click.echo()


@template.command("push")
@click.argument("name")
@click.option("--folder", default=None, help="reMarkable folder (defaults to config)")
@click.pass_context
def template_push(ctx: click.Context, name: str, folder: str | None) -> None:
    """Render a template and push it as a fillable PDF to the tablet."""
    config = ctx.obj["config"]
    _setup_logging(config)

    from datetime import datetime

    from src.templates.engine import TemplateEngine

    engine = TemplateEngine(config.templates.user_templates_dir)
    if engine.get(name) is None:
        click.echo(f"Template '{name}' not found. Available:")
        for t in engine.list_templates():
            click.echo(f"  - {t.name}")
        return

    today = datetime.now(UTC).date().isoformat()
    pdf_bytes = engine.render_pdf(name, extra_values={"date": today})

    target_folder = folder or config.templates.target_folder
    asyncio.run(_push_template(config, name, pdf_bytes, target_folder))


async def _push_template(
    config: AppConfig, name: str, pdf_bytes: bytes, target_folder: str,
) -> None:
    from src.remarkable.cloud import RemarkableCloud
    from src.response.uploader import ResponseUploader

    auth = _get_auth(config)
    async with RemarkableCloud(auth) as cloud:
        uploader = ResponseUploader(cloud, response_folder=target_folder)
        title = f"Template — {name.title()}"
        doc_id = await uploader.upload_pdf(pdf_bytes, title)

    state = SyncState(resolve_path(config.sync.state_db))
    state.record_template_push(doc_id, name)
    state.close()

    click.echo(f"Pushed template '{name}' to folder '{target_folder}' (doc {doc_id[:8]})")


@cli.group()
def plugins() -> None:
    """Manage reMark plugins."""


@plugins.command("list")
@click.pass_context
def plugins_list(ctx: click.Context) -> None:
    """List discovered plugins."""
    config = ctx.obj["config"]
    from src.plugins.registry import PluginRegistry

    registry = PluginRegistry(config.plugins)
    registry.discover()
    entries = registry.list_plugins()

    if not entries:
        click.echo("No plugins found.")
        click.echo(f"(plugin_dir: {config.plugins.plugin_dir})")
        return

    click.echo(f"\nLoaded {len(entries)} plugin(s):\n")
    for p in entries:
        hooks_str = ", ".join(p["hooks"]) or "(no hooks)"
        click.echo(f"  {p['name']} v{p['version']}")
        if p["description"]:
            click.echo(f"    {p['description']}")
        click.echo(f"    Hooks: {hooks_str}")
        if p["author"]:
            click.echo(f"    Author: {p['author']}")
        click.echo()


@plugins.command("disable")
@click.argument("name")
@click.pass_context
def plugins_disable(ctx: click.Context, name: str) -> None:
    """Disable a plugin by name."""
    config = ctx.obj["config"]
    state = SyncState(resolve_path(config.sync.state_db))
    state.register_plugin(name)
    state.set_plugin_enabled(name, False)
    state.close()
    click.echo(f"Disabled plugin '{name}'. "
               f"Also add it to config.plugins.disabled to persist across DB resets.")


@plugins.command("enable")
@click.argument("name")
@click.pass_context
def plugins_enable(ctx: click.Context, name: str) -> None:
    """Enable a previously disabled plugin."""
    config = ctx.obj["config"]
    state = SyncState(resolve_path(config.sync.state_db))
    state.register_plugin(name)
    state.set_plugin_enabled(name, True)
    state.close()
    click.echo(f"Enabled plugin '{name}'.")


@plugins.command("info")
@click.argument("name")
@click.pass_context
def plugins_info(ctx: click.Context, name: str) -> None:
    """Show details for a single plugin."""
    config = ctx.obj["config"]
    from src.plugins.registry import PluginRegistry

    registry = PluginRegistry(config.plugins)
    registry.discover()
    plugin = registry.get(name)
    if plugin is None:
        click.echo(f"Plugin '{name}' not found.")
        return

    meta = plugin.metadata
    click.echo(f"\nPlugin: {meta.name}")
    click.echo(f"Version: {meta.version}")
    if meta.description:
        click.echo(f"Description: {meta.description}")
    if meta.author:
        click.echo(f"Author: {meta.author}")
    from src.plugins.hooks import (
        ActionExtractorHook,
        NoteProcessorHook,
        OCRBackendHook,
        SyncHook,
    )
    hooks = []
    for cls in (ActionExtractorHook, OCRBackendHook, NoteProcessorHook, SyncHook):
        if isinstance(plugin, cls):
            hooks.append(cls.__name__)
    click.echo(f"Hooks: {', '.join(hooks) or '(none)'}")


# Also expose `SyncState` here for plugin subcommands
from src.sync.state import SyncState  # noqa: E402


@cli.command()
@click.option("--teams/--no-teams", default=True, help="Post the digest to Teams")
@click.option("--period", default="weekly", type=click.Choice(["daily", "weekly"]))
@click.pass_context
def digest(ctx: click.Context, teams: bool, period: str) -> None:
    """Build and optionally post a Teams digest of recent activity."""
    config = ctx.obj["config"]
    _setup_logging(config)

    asyncio.run(_digest(config, period, teams))


async def _digest(config: AppConfig, period: str, teams: bool) -> None:
    from src.integrations.microsoft.teams import build_digest, post_digest
    from src.obsidian.vault import ObsidianVault
    from src.sync.state import SyncState

    state = SyncState(resolve_path(config.sync.state_db))
    vault = ObsidianVault(
        Path(config.obsidian.vault_path).expanduser(),
        config.obsidian.folder_map,
    )

    digest_data = build_digest(state, vault, period=period)
    state.close()

    click.echo(f"\nreMark — {period.title()} digest ({digest_data.date_range})")
    click.echo(f"  Notes synced: {digest_data.notes_count}")
    click.echo(f"  Open actions: {len(digest_data.action_items)}")
    click.echo(f"  API cost:     ${digest_data.cost_usd:.2f}")
    if digest_data.top_tags:
        click.echo(f"  Top tags:     {', '.join(digest_data.top_tags)}")
    click.echo()

    if teams and config.microsoft.teams.enabled:
        posted = await post_digest(config.microsoft.teams, digest_data)
        if posted:
            click.echo("Digest posted to Teams.")
        else:
            click.echo("Teams post failed (webhook disabled or errored).")
    elif teams and not config.microsoft.teams.enabled:
        click.echo("Teams integration is disabled in config.yaml (microsoft.teams.enabled).")


@cli.command(name="serve-web")
@click.option("--host", default=None, help="Override bind host")
@click.option("--port", default=None, type=int, help="Override bind port")
@click.pass_context
def serve_web(ctx: click.Context, host: str | None, port: int | None) -> None:
    """Start the web dashboard + PWA on the given host/port."""
    config = ctx.obj["config"]
    _setup_logging(config)

    bind_host = host or config.web.host
    bind_port = port or config.web.port

    import uvicorn

    from src.web.app import create_app

    app = create_app(config)
    click.echo(f"Starting reMark web at http://{bind_host}:{bind_port}/  (Ctrl+C to stop)")
    uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")


@cli.command(name="vapid-keys")
def vapid_keys() -> None:
    """Generate a new VAPID keypair for Web Push Notifications."""
    from src.web.push import generate_vapid_keys

    pub, priv = generate_vapid_keys()
    click.echo("\nAdd these to config.yaml under `web:`")
    click.echo(f"  vapid_public_key: {pub}")
    click.echo(f"  vapid_private_key: {priv}")
    click.echo("  vapid_subject: \"mailto:you@example.com\"")
    click.echo("\nKeep the private key secret.\n")


@cli.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Run health checks on the reMark installation."""
    config = ctx.obj["config"]
    _run_doctor(config)


def _run_doctor(config: AppConfig) -> None:
    import os
    import shutil

    from src.remarkable.auth import AuthManager

    checks: list[tuple[str, bool, str]] = []

    # Config loaded
    checks.append(("Config loaded", True, f"v{_version()}"))

    # Vault path
    vault_path = Path(config.obsidian.vault_path).expanduser()
    checks.append((
        "Vault directory",
        vault_path.exists(),
        str(vault_path) if vault_path.exists() else f"missing: {vault_path}",
    ))

    # Vault git repo
    if config.obsidian.git.enabled:
        from src.obsidian.git_sync import GitSync
        try:
            gs = GitSync(str(vault_path))
            is_repo = gs.is_git_repo()
            checks.append((
                "Vault is git repo",
                is_repo,
                "ok" if is_repo else "run 'git init' in vault",
            ))
        except Exception as e:
            checks.append(("Vault is git repo", False, str(e)))

    # Device token
    token_path = resolve_path(config.remarkable.device_token_path)
    if token_path.exists():
        token = token_path.read_text().strip()
        if token:
            try:
                auth = AuthManager(token_path)
                user_token_unused = auth.device_token  # noqa: F841
                checks.append(("reMarkable device token", True, str(token_path)))
            except Exception as e:
                checks.append(("reMarkable device token", False, str(e)))
        else:
            checks.append(("reMarkable device token", False, "empty file"))
    else:
        checks.append((
            "reMarkable device token",
            False,
            "run 'remark-bridge setup'",
        ))

    # Anthropic API key
    env_var = config.processing.api_key_env
    api_key = os.environ.get(env_var, "")
    checks.append((
        f"Anthropic API key ({env_var})",
        bool(api_key),
        "set" if api_key else "not set",
    ))

    # State DB
    state_db = resolve_path(config.sync.state_db)
    state_db_exists = state_db.exists() or state_db.parent.exists()
    checks.append((
        "State database location",
        state_db_exists,
        str(state_db) if state_db_exists else "parent dir missing",
    ))

    # Cairo
    cairo_ok = False
    try:
        import cairocffi  # noqa: F401
        cairo_ok = True
    except (ImportError, OSError):
        pass
    checks.append((
        "libcairo2 (for SVG→PNG)",
        cairo_ok,
        "loaded" if cairo_ok else "install libcairo2-dev",
    ))

    # Search backend (if enabled)
    if config.search.enabled:
        try:
            from src.search.backends import build_backend
            backend = build_backend(
                config.search.backend,
                model=config.search.model,
                api_key_env=config.search.api_key_env,
            )
            checks.append((
                f"Search backend '{backend.name}'",
                True,
                f"dim={backend.dimension}",
            ))
        except Exception as e:
            checks.append((f"Search backend '{config.search.backend}'", False, str(e)))

    # Microsoft (if enabled)
    if config.microsoft.enabled:
        if not config.microsoft.client_id:
            checks.append(("Microsoft client_id", False, "not set in config.yaml"))
        else:
            cache_path = Path(config.microsoft.token_cache_path).expanduser()
            if cache_path.exists():
                checks.append((
                    "Microsoft token cache",
                    True,
                    str(cache_path),
                ))
            else:
                checks.append((
                    "Microsoft token cache",
                    False,
                    "run 'remark-bridge setup-microsoft'",
                ))

    # Disk space
    vault_parent = vault_path.parent if vault_path.exists() else vault_path.parent.parent
    if vault_parent.exists():
        free_bytes = shutil.disk_usage(vault_parent).free
        free_gb = free_bytes / (1024 ** 3)
        checks.append((
            "Free disk space",
            free_gb > 1.0,
            f"{free_gb:.1f} GB free",
        ))

    # Print report
    click.echo("\n=== reMark Doctor ===\n")
    longest = max(len(name) for name, _, _ in checks)
    fail_count = 0
    for name, ok, detail in checks:
        marker = "✓" if ok else "✗"
        color = "green" if ok else "red"
        name_padded = name.ljust(longest)
        click.secho(f"  {marker} ", fg=color, nl=False)
        click.echo(f"{name_padded}  {detail}")
        if not ok:
            fail_count += 1

    click.echo()
    if fail_count == 0:
        click.secho(f"All {len(checks)} checks passed.", fg="green")
    else:
        click.secho(
            f"{fail_count}/{len(checks)} checks failed.",
            fg="yellow",
        )


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("remark-bridge")
    except Exception:
        return "dev"


@cli.command(name="setup-microsoft")
@click.pass_context
def setup_microsoft(ctx: click.Context) -> None:
    """Authenticate with Microsoft Graph for Outlook integration."""
    config = ctx.obj["config"]
    _setup_logging(config)

    if not config.microsoft.client_id:
        click.echo(
            "microsoft.client_id is not set in config.yaml.\n"
            "Register an app at https://entra.microsoft.com and add the client ID."
        )
        return

    from src.integrations.microsoft.auth import MicrosoftAuth, MicrosoftAuthError

    auth = MicrosoftAuth(
        client_id=config.microsoft.client_id,
        tenant=config.microsoft.tenant,
        token_cache_path=config.microsoft.token_cache_path,
    )

    try:
        flow = auth.start_device_flow()
    except MicrosoftAuthError as e:
        click.echo(f"Failed to start device flow: {e}")
        return

    click.echo("\nTo authenticate Microsoft:")
    click.echo(f"  1. Go to: {flow['verification_uri']}")
    click.echo(f"  2. Enter code: {flow['user_code']}")
    click.echo("\nWaiting for authorization...\n")

    try:
        auth.complete_device_flow(flow)
        click.echo("Microsoft authentication successful.")
        click.echo(f"Token cached at: {config.microsoft.token_cache_path}")
    except MicrosoftAuthError as e:
        click.echo(f"Authorization failed: {e}")


@cli.command()
@click.pass_context
def reindex(ctx: click.Context) -> None:
    """Rebuild the semantic search index from the vault."""
    config = ctx.obj["config"]
    _setup_logging(config)

    if not config.search.enabled:
        click.echo("Search is disabled. Enable it in config.yaml under `search.enabled: true`.")
        return

    click.echo(f"Reindexing vault with backend '{config.search.backend}'...")
    asyncio.run(_reindex(config))


async def _reindex(config: AppConfig) -> None:
    from src.search.backends import build_backend
    from src.search.index import VectorIndex
    from src.search.indexer import Indexer
    from src.sync.engine import SyncEngine

    backend = build_backend(
        config.search.backend,
        model=config.search.model,
        api_key_env=config.search.api_key_env,
    )
    index = VectorIndex(
        db_path=resolve_path(config.sync.state_db),
        dimension=backend.dimension,
    )
    engine = SyncEngine(config)

    indexer = Indexer(
        backend=backend,
        index=index,
        vault=engine.vault,
        chunk_size=config.search.chunk_size,
        chunk_overlap=config.search.chunk_overlap,
    )

    report = await indexer.reindex_vault()
    click.echo(
        f"Indexed {report['notes']} notes → {report['chunks']} chunks "
        f"(backend: {report['backend']}, dim: {report['dimension']})"
    )


async def _push_file(config: AppConfig, file_path: Path, folder: str) -> None:
    from src.remarkable.cloud import RemarkableCloud

    auth = _get_auth(config)

    async with RemarkableCloud(auth) as cloud:
        doc_id = await cloud.upload_document(file_path, parent_folder=folder)

    click.echo(f"Uploaded {file_path.name} → {doc_id[:8]}")


@cli.command()
@click.pass_context
def serve(ctx: click.Context) -> None:
    """Start the MCP server for Claude Desktop / Claude Code."""
    config = ctx.obj["config"]
    _setup_logging(config)

    click.echo("Starting MCP server on stdio...")

    from src.mcp.server import run_server
    asyncio.run(run_server())


@cli.command()
@click.pass_context
def migrate(ctx: click.Context) -> None:
    """One-time import of all existing notebooks."""
    config = ctx.obj["config"]
    _setup_logging(config)

    click.echo("Starting migration of all existing notebooks...")
    asyncio.run(_migrate_all(config))


async def _migrate_all(config: AppConfig) -> None:
    from src.remarkable.cloud import RemarkableCloud
    from src.remarkable.documents import DocumentManager
    from src.sync.engine import SyncEngine

    auth = _get_auth(config)
    engine = SyncEngine(config)
    ocr_pipeline = _get_ocr_pipeline(config, llm_client=engine._get_llm_client())
    download_dir = resolve_path(config.sync.state_db).parent / "downloads"

    async with RemarkableCloud(auth) as cloud:
        doc_manager = DocumentManager(cloud, download_dir)
        report = await engine.sync_once(cloud, doc_manager, ocr_pipeline)

    click.echo(
        f"\nMigration complete: {report.success_count} imported, "
        f"{report.errors} errors ({report.duration_ms}ms)"
    )


@cli.command("retag")
@click.option(
    "--dry-run", is_flag=True,
    help="Show what would change without writing",
)
@click.option(
    "--limit", type=int, default=None,
    help="Only process the first N notes (useful for testing)",
)
@click.pass_context
def retag(ctx: click.Context, dry_run: bool, limit: int | None) -> None:
    """Re-tag existing reMarkable-synced notes using the configured tagger.

    Walks the vault for every ``source: remarkable`` note and re-runs
    the tagger. Useful after enabling ``processing.hierarchical_tags``
    to backfill older notes with the new taxonomy.
    """
    config: AppConfig = ctx.obj["config"]
    _setup_logging(config)
    asyncio.run(_retag(config, dry_run, limit))


async def _retag(config: AppConfig, dry_run: bool, limit: int | None) -> None:
    import os

    import anthropic

    from src.obsidian.vault import ObsidianVault
    from src.processing.tagger import NoteTagger

    vault = ObsidianVault(
        Path(config.obsidian.vault_path).expanduser(),
        config.obsidian.folder_map,
    )
    client = anthropic.AsyncAnthropic(
        api_key=os.environ.get(config.processing.api_key_env, ""),
    )
    tagger = NoteTagger(
        client,
        config.processing.model,
        hierarchical=config.processing.hierarchical_tags,
    )

    notes = vault.list_notes_by_source("remarkable")
    if limit is not None:
        notes = notes[:limit]
    click.echo(f"Re-tagging {len(notes)} note(s)...")

    updated = 0
    for note_path in notes:
        result = vault.read_note(note_path)
        if result is None:
            continue
        fm, content = result
        new_tags = await tagger.tag(content, fm.get("title", note_path.stem))
        old_tags = fm.get("tags", []) or []

        if new_tags == old_tags:
            continue

        click.echo(
            f"  {note_path.relative_to(vault.path)}: "
            f"{len(old_tags)} → {len(new_tags)} tags"
        )
        if dry_run:
            continue

        fm["tags"] = new_tags
        vault.write_note(note_path, fm, content)
        updated += 1

    click.echo(f"Updated {updated} note(s).")


@cli.group()
def report() -> None:
    """Manage scheduled reports."""


@report.command("list")
@click.pass_context
def report_list(ctx: click.Context) -> None:
    """Show every configured report."""
    config: AppConfig = ctx.obj["config"]
    from src.sync.state import SyncState

    state = SyncState(resolve_path(config.sync.state_db))
    try:
        rows = state.list_reports()
    finally:
        state.close()
    if not rows:
        click.echo("No reports configured.")
        return
    for r in rows:
        status = "enabled" if r["enabled"] else "disabled"
        last = r.get("last_status") or "never"
        click.echo(
            f"  [{r['id']:>3}] {r['name']:<24} schedule={r['schedule']:<24} "
            f"[{status}]  last={last}  next={r.get('next_run_at') or '—'}"
        )


@report.command("run")
@click.option("--id", "report_id", type=int, required=True, help="Report id (from `report list`)")
@click.pass_context
def report_run(ctx: click.Context, report_id: int) -> None:
    """Fire a single report immediately, bypassing the schedule."""
    config: AppConfig = ctx.obj["config"]
    _setup_logging(config)
    asyncio.run(_report_run(config, report_id))


async def _report_run(config: AppConfig, report_id: int) -> None:
    import os

    from src.llm.factory import build_llm_client
    from src.reports.runner import run_report
    from src.sync.state import SyncState

    state = SyncState(resolve_path(config.sync.state_db))
    llm = build_llm_client(config.llm, anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"))
    try:
        report = state.get_report(report_id)
        if report is None:
            click.echo(f"No report with id={report_id}", err=True)
            sys.exit(1)
        result = await run_report(report, state, config, llm=llm)
    finally:
        state.close()

    click.echo(f"Report '{result.name}' delivered:")
    for ch in result.channels_ok:
        click.echo(f"  ✓ {ch}")
    for ch, err in result.channels_failed:
        click.echo(f"  ✗ {ch}: {err}")


@cli.group()
def audit() -> None:
    """Inspect and prune the structured audit log."""


@audit.command("list")
@click.option("--limit", default=50, type=int)
@click.option("--offset", default=0, type=int)
@click.option("--action", default=None, help="Filter by action (e.g. login, http, user_create)")
@click.option("--user-id", default=None, type=int)
@click.pass_context
def audit_list(
    ctx: click.Context,
    limit: int,
    offset: int,
    action: str | None,
    user_id: int | None,
) -> None:
    """Show recent audit entries."""
    config: AppConfig = ctx.obj["config"]
    from src.sync.state import SyncState

    state = SyncState(resolve_path(config.sync.state_db))
    try:
        rows = state.list_audit(
            limit=limit, offset=offset, user_id=user_id, action=action,
        )
    finally:
        state.close()
    if not rows:
        click.echo("Empty.")
        return
    for r in rows:
        click.echo(
            f"  [{r['id']:>6}] {r['ts']}  {(r['username'] or '-'):<16} "
            f"{r['action']:<14} {(r['method'] or '-'):<6} "
            f"{(r['status'] or ''):<4} {(r['resource'] or '')[:60]}"
        )


@audit.command("prune")
@click.option(
    "--days", default=90, type=int,
    help="Retention window — entries older than this many days are deleted",
)
@click.confirmation_option(prompt="Delete old audit entries?")
@click.pass_context
def audit_prune(ctx: click.Context, days: int) -> None:
    """Delete audit entries older than the retention window."""
    config: AppConfig = ctx.obj["config"]
    from src.sync.state import SyncState

    state = SyncState(resolve_path(config.sync.state_db))
    try:
        n = state.audit_prune(days)
    finally:
        state.close()
    click.echo(f"Pruned {n} entries older than {days} days.")


@cli.group("bridge-token")
def bridge_token() -> None:
    """Manage bearer tokens for the bridge HTTP API."""


@bridge_token.command("issue")
@click.option("--label", required=True, help="Human-readable label (e.g. 'obsidian-macbook')")
@click.pass_context
def bridge_token_issue(ctx: click.Context, label: str) -> None:
    """Generate a new bearer token. Shown once — copy it now."""
    config: AppConfig = ctx.obj["config"]
    from src.sync.state import SyncState

    state = SyncState(resolve_path(config.sync.state_db))
    try:
        token = state.issue_bridge_token(label)
    finally:
        state.close()

    click.echo("")
    click.echo(f"  Token for '{label}':")
    click.echo(f"    {token}")
    click.echo("")
    click.echo("  Copy this now — it won't be shown again.")


@bridge_token.command("list")
@click.pass_context
def bridge_token_list(ctx: click.Context) -> None:
    """Show issued tokens (not the secret — only labels and status)."""
    config: AppConfig = ctx.obj["config"]
    from src.sync.state import SyncState

    state = SyncState(resolve_path(config.sync.state_db))
    try:
        rows = state.list_bridge_tokens()
    finally:
        state.close()

    if not rows:
        click.echo("No tokens issued.")
        return
    for row in rows:
        status = "revoked" if row["revoked"] else "active"
        last = row.get("last_used_at") or "never"
        click.echo(
            f"  [{row['id']:>3}] {row['label']:<32} "
            f"[{status}]  last_used={last}"
        )


@bridge_token.command("revoke")
@click.option("--id", "token_id", type=int, required=True)
@click.pass_context
def bridge_token_revoke(ctx: click.Context, token_id: int) -> None:
    """Revoke a token. Clients using it start getting 401s immediately."""
    config: AppConfig = ctx.obj["config"]
    from src.sync.state import SyncState

    state = SyncState(resolve_path(config.sync.state_db))
    try:
        state.revoke_bridge_token(token_id)
    finally:
        state.close()
    click.echo(f"Token {token_id} revoked.")


@cli.group()
def queue() -> None:
    """Inspect and manage the offline/retry queue."""


@queue.command("list")
@click.option("--status", default=None, help="Filter by status (pending/done/failed)")
@click.pass_context
def queue_list(ctx: click.Context, status: str | None) -> None:
    """Show queue entries."""
    config: AppConfig = ctx.obj["config"]
    from src.sync.state import SyncState

    state = SyncState(resolve_path(config.sync.state_db))
    try:
        rows = state.list_queue(status=status)
    finally:
        state.close()

    if not rows:
        click.echo("Queue is empty.")
        return

    for row in rows:
        click.echo(
            f"  [{row['id']:>5}] {row['status']:<8} {row['op_type']:<20} "
            f"attempts={row['attempts']}/{row['max_attempts']} "
            f"doc={row.get('doc_id') or '-'} "
            f"err={(row.get('last_error') or '')[:60]}"
        )


@queue.command("retry")
@click.option("--id", "queue_id", type=int, required=True)
@click.pass_context
def queue_retry(ctx: click.Context, queue_id: int) -> None:
    """Reset a failed entry to pending."""
    config: AppConfig = ctx.obj["config"]
    from src.sync.state import SyncState

    state = SyncState(resolve_path(config.sync.state_db))
    try:
        state.retry_queue_entry(queue_id)
    finally:
        state.close()
    click.echo(f"Queue entry {queue_id} reset to pending.")


@queue.command("clear")
@click.option(
    "--status", default=None,
    help="Only delete entries with this status (omit to clear all)",
)
@click.confirmation_option(prompt="Really delete queue entries?")
@click.pass_context
def queue_clear(ctx: click.Context, status: str | None) -> None:
    """Delete queue entries."""
    config: AppConfig = ctx.obj["config"]
    from src.sync.state import SyncState

    state = SyncState(resolve_path(config.sync.state_db))
    try:
        n = state.clear_queue(status=status)
    finally:
        state.close()
    click.echo(f"Deleted {n} entries.")


@cli.group()
def device() -> None:
    """Manage registered reMarkable tablets (multi-device setups)."""


@device.command("add")
@click.option("--id", "device_id", required=True, help="Stable slug, e.g. 'pro' or 'rm2'")
@click.option("--label", required=True, help="Human-readable name for the tablet")
@click.option(
    "--subfolder", default="",
    help="Subfolder under the vault to write this device's notes into",
)
@click.option(
    "--code", default=None,
    help="One-time pairing code from my.remarkable.com — optional, run without "
         "to register later with `remark-bridge auth --device <id>`",
)
@click.pass_context
def device_add(
    ctx: click.Context,
    device_id: str,
    label: str,
    subfolder: str,
    code: str | None,
) -> None:
    """Register a new reMarkable tablet."""
    config: AppConfig = ctx.obj["config"]
    _setup_logging(config)
    from src.remarkable.auth import device_token_path_for
    from src.sync.state import SyncState

    base_dir = Path(config.remarkable.device_token_path).expanduser().parent
    token_path = device_token_path_for(device_id, base_dir)

    state = SyncState(resolve_path(config.sync.state_db))
    try:
        state.register_device(device_id, label, str(token_path), subfolder)
    finally:
        state.close()

    click.echo(f"Registered device '{label}' (id={device_id}).")
    click.echo(f"  Token path:      {token_path}")
    click.echo(f"  Vault subfolder: {subfolder or '(none)'}")

    if code:
        auth = _get_auth(config, device_id)
        asyncio.run(auth.register_device(code))
        click.echo(f"  Paired — token stored at {token_path}")
    else:
        click.echo("  To pair now, run:")
        click.echo(f"    remark-bridge auth --device {device_id}")
    click.echo(
        "  Add a matching entry under remarkable.devices in config.yaml "
        "so `sync` iterates this tablet."
    )


@device.command("list")
@click.pass_context
def device_list(ctx: click.Context) -> None:
    """List registered tablets."""
    config: AppConfig = ctx.obj["config"]
    from src.sync.state import SyncState

    state = SyncState(resolve_path(config.sync.state_db))
    try:
        rows = state.list_devices(active_only=False)
    finally:
        state.close()

    if not rows:
        click.echo("No devices registered.")
        return
    for row in rows:
        active = "active" if row["active"] else "inactive"
        last = row.get("last_sync_at") or "never"
        click.echo(
            f"  {row['id']:<12} {row['label']:<24} "
            f"subfolder={row['vault_subfolder'] or '-':<12} "
            f"[{active}]  last_sync={last}"
        )


@device.command("remove")
@click.option("--id", "device_id", required=True)
@click.pass_context
def device_remove(ctx: click.Context, device_id: str) -> None:
    """Deactivate a registered tablet (history is preserved)."""
    config: AppConfig = ctx.obj["config"]
    from src.sync.state import SyncState

    state = SyncState(resolve_path(config.sync.state_db))
    try:
        state.deactivate_device(device_id)
    finally:
        state.close()
    click.echo(f"Device '{device_id}' deactivated.")


if __name__ == "__main__":
    cli()
