"""CLI entry point for reMark.

All user-facing commands go through this Click-based CLI.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click

from src.config import AppConfig, load_config, resolve_path


def _setup_logging(config: AppConfig) -> None:
    """Configure logging from config settings."""
    log_config = config.logging
    level = getattr(logging, log_config.level, logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]

    log_file = resolve_path(log_config.file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(str(log_file))
    handlers.append(file_handler)

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def _get_auth(config: AppConfig):
    from src.remarkable.auth import AuthManager
    return AuthManager(resolve_path(config.remarkable.device_token_path))


def _get_ocr_pipeline(config: AppConfig):
    from src.ocr.pipeline import OCRPipeline
    return OCRPipeline(config.ocr)


@click.group()
@click.option("--config", "-c", "config_path", default="config.yaml", help="Path to config file")
@click.pass_context
def cli(ctx: click.Context, config_path: str) -> None:
    """reMark — reMarkable ↔ Obsidian sync with intelligent processing."""
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
    from src.remarkable.cloud import RemarkableCloud
    from src.remarkable.documents import DocumentManager
    from src.sync.engine import SyncEngine

    auth = _get_auth(config)
    engine = SyncEngine(config)
    ocr_pipeline = _get_ocr_pipeline(config)
    download_dir = resolve_path(config.sync.state_db).parent / "downloads"

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


async def _sync_continuous(config: AppConfig) -> None:
    from src.sync.engine import SyncEngine
    from src.sync.scheduler import SyncScheduler

    auth = _get_auth(config)
    engine = SyncEngine(config)
    ocr_pipeline = _get_ocr_pipeline(config)
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
    ocr_pipeline = _get_ocr_pipeline(config)
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


async def _process_single(config: AppConfig, notebook_name: str) -> None:
    from src.remarkable.cloud import RemarkableCloud
    from src.remarkable.documents import DocumentManager
    from src.sync.engine import SyncEngine

    auth = _get_auth(config)
    engine = SyncEngine(config)
    ocr_pipeline = _get_ocr_pipeline(config)
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
    ocr_pipeline = _get_ocr_pipeline(config)
    download_dir = resolve_path(config.sync.state_db).parent / "downloads"

    async with RemarkableCloud(auth) as cloud:
        doc_manager = DocumentManager(cloud, download_dir)
        report = await engine.sync_once(cloud, doc_manager, ocr_pipeline)

    click.echo(
        f"\nMigration complete: {report.success_count} imported, "
        f"{report.errors} errors ({report.duration_ms}ms)"
    )


if __name__ == "__main__":
    cli()
