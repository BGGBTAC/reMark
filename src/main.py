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
