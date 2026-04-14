"""FastAPI app for the reMark web dashboard + PWA.

Built server-side with Jinja2 + HTMX + Alpine.js + Tailwind (CDN).
No build step required. Designed to run via `remark-bridge serve-web`.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.config import AppConfig, load_config
from src.obsidian.vault import ObsidianVault
from src.sync.state import SyncState

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"


def _version() -> str:
    """Return the installed package version, falling back if unreleased."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("remark-bridge")
        except PackageNotFoundError:
            return "0.0.0+dev"
    except Exception:
        return "unknown"

_security = HTTPBasic(auto_error=False)


def _resolve_config() -> AppConfig:
    """Load config for a request. Override via env var REMARK_CONFIG."""
    import os
    path = os.environ.get("REMARK_CONFIG", "config.yaml")
    return load_config(path)


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Factory returning a configured FastAPI app."""
    if config is None:
        config = _resolve_config()

    app = FastAPI(title=config.web.app_name, openapi_url=None)

    # Mount static files for CSS / JS / manifest / service worker
    app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static",
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["app_name"] = config.web.app_name

    def _auth_check(
        credentials: HTTPBasicCredentials | None = Depends(_security),
    ) -> None:
        """Optional HTTP Basic auth when credentials are configured."""
        if not config.web.username and not config.web.password:
            return
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Auth required",
                headers={"WWW-Authenticate": "Basic"},
            )
        ok_user = secrets.compare_digest(
            credentials.username.encode(), config.web.username.encode(),
        )
        ok_pass = secrets.compare_digest(
            credentials.password.encode(), config.web.password.encode(),
        )
        if not (ok_user and ok_pass):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
                headers={"WWW-Authenticate": "Basic"},
            )

    # -- State accessors --

    def get_vault() -> ObsidianVault:
        return ObsidianVault(
            Path(config.obsidian.vault_path).expanduser(),
            config.obsidian.folder_map,
        )

    def get_state() -> SyncState:
        from src.config import resolve_path
        return SyncState(resolve_path(config.sync.state_db))

    # -- Routes --

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, _=Depends(_auth_check)):
        state = get_state()
        try:
            stats = state.get_sync_stats()
            usage = state.get_api_usage_summary(days=30)
            recent_log = state.get_recent_log(limit=10)
        finally:
            state.close()

        vault = get_vault()
        recent_notes = []
        for md in sorted(
            vault.path.rglob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:10]:
            result = vault.read_note(md)
            if result is None:
                continue
            fm, _ = result
            if fm.get("source") == "remarkable":
                recent_notes.append({
                    "title": fm.get("title", md.stem),
                    "path": str(md.relative_to(vault.path)),
                    "summary": fm.get("summary", "")[:120],
                    "modified": datetime.fromtimestamp(
                        md.stat().st_mtime, tz=UTC,
                    ).strftime("%Y-%m-%d %H:%M"),
                })

        return templates.TemplateResponse(
            request, "dashboard.html",
            {
                "stats": stats, "usage": usage,
                "recent_notes": recent_notes, "recent_log": recent_log,
            },
        )

    @app.get("/notes", response_class=HTMLResponse)
    async def list_notes_view(
        request: Request,
        folder: str | None = None,
        q: str | None = None,
        _=Depends(_auth_check),
    ):
        vault = get_vault()
        notes = []
        for md in vault.path.rglob("*.md"):
            result = vault.read_note(md)
            if result is None:
                continue
            fm, content = result
            if fm.get("source") != "remarkable":
                continue
            rel = str(md.relative_to(vault.path))
            if folder and not rel.startswith(folder):
                continue
            if q and q.lower() not in (fm.get("title", "") + content).lower():
                continue
            notes.append({
                "title": fm.get("title", md.stem),
                "path": rel,
                "summary": fm.get("summary", "")[:160],
                "tags": fm.get("tags", []) or [],
                "action_items": fm.get("action_items", 0),
            })

        notes.sort(key=lambda n: n["title"])

        return templates.TemplateResponse(
            request, "notes.html",
            {"notes": notes, "filter_folder": folder, "filter_query": q},
        )

    @app.get("/notes/{note_path:path}", response_class=HTMLResponse)
    async def view_note(
        request: Request, note_path: str, _=Depends(_auth_check),
    ):
        vault = get_vault()
        full_path = vault.path / note_path
        result = vault.read_note(full_path)
        if result is None:
            raise HTTPException(status_code=404, detail="Note not found")
        fm, content = result

        return templates.TemplateResponse(
            request, "note_view.html",
            {"path": note_path, "frontmatter": fm, "content": content},
        )

    @app.get("/actions", response_class=HTMLResponse)
    async def actions_view(request: Request, _=Depends(_auth_check)):
        vault = get_vault()
        actions_dir = vault.path / "Actions"
        items = []
        if actions_dir.exists():
            for action_file in actions_dir.glob("*-actions.md"):
                content = action_file.read_text(encoding="utf-8")
                source = action_file.stem.replace("-actions", "")
                for line_no, line in enumerate(content.split("\n"), 1):
                    stripped = line.strip()
                    if stripped.startswith("- [ ]") or stripped.startswith("- [?]"):
                        items.append({
                            "source": source,
                            "text": stripped,
                            "file": action_file.name,
                            "line": line_no,
                            "is_question": stripped.startswith("- [?]"),
                        })

        return templates.TemplateResponse(
            request, "actions.html", {"items": items},
        )

    @app.get("/ask", response_class=HTMLResponse)
    async def ask_form(request: Request, _=Depends(_auth_check)):
        return templates.TemplateResponse(
            request, "ask.html",
            {
                "search_enabled": config.search.enabled,
                "answer": None, "hits": [], "query": "",
            },
        )

    @app.post("/ask", response_class=HTMLResponse)
    async def ask_submit(
        request: Request,
        query: str = Form(...),
        _=Depends(_auth_check),
    ):
        if not config.search.enabled:
            return templates.TemplateResponse(
                request, "ask.html",
                {
                    "search_enabled": False, "answer": None, "hits": [],
                    "query": query, "error": "Search is disabled in config.",
                },
            )

        try:
            from src.search.backends import build_backend
            from src.search.index import VectorIndex
            from src.search.query import SearchQuery
            from src.sync.engine import SyncEngine

            backend = build_backend(
                config.search.backend,
                model=config.search.model,
                api_key_env=config.search.api_key_env,
            )
            from src.config import resolve_path
            index = VectorIndex(
                db_path=resolve_path(config.sync.state_db),
                dimension=backend.dimension,
            )

            client = None
            if config.search.synthesize_answer:
                engine = SyncEngine(config)
                client = engine._get_anthropic()

            searcher = SearchQuery(
                backend=backend, index=index,
                anthropic_client=client,
                synthesis_model=config.search.synthesis_model,
            )
            result = await searcher.ask(
                query,
                top_k=config.search.top_k,
                min_score=config.search.min_score,
                synthesize=config.search.synthesize_answer,
            )

            hits = [{
                "title": Path(h.vault_path).stem,
                "path": h.vault_path,
                "score": f"{h.score:.2f}",
                "heading": h.heading_context,
                "preview": h.content[:400],
            } for h in result.hits]

            return templates.TemplateResponse(
                request, "ask.html",
                {
                    "search_enabled": True, "answer": result.answer,
                    "hits": hits, "query": query,
                },
            )
        except Exception as e:
            logger.warning("ask failed: %s", e)
            return templates.TemplateResponse(
                request, "ask.html",
                {
                    "search_enabled": True, "answer": None, "hits": [],
                    "query": query, "error": str(e),
                },
            )

    @app.get("/quick-entry", response_class=HTMLResponse)
    async def quick_entry_form(request: Request, _=Depends(_auth_check)):
        return templates.TemplateResponse(request, "quick_entry.html", {})

    @app.post("/quick-entry")
    async def quick_entry_submit(
        title: str = Form(""),
        body: str = Form(...),
        _=Depends(_auth_check),
    ):
        vault = get_vault()
        inbox = vault.path / "Inbox"
        inbox.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        safe_title = (title.strip() or "quick-note").replace("/", "-")[:80]
        file_path = inbox / f"{ts}-{safe_title}.md"

        fm = {
            "title": title.strip() or f"Quick note {ts}",
            "source": "web",
            "created_at": datetime.now(UTC).isoformat(),
        }
        vault.write_note(file_path, fm, body)

        return RedirectResponse(url="/notes", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_view(request: Request, _=Depends(_auth_check)):
        redacted = {
            "remarkable": {"sync_folders": config.remarkable.sync_folders,
                           "ignore_folders": config.remarkable.ignore_folders},
            "ocr": {"primary": config.ocr.primary, "fallback": config.ocr.fallback},
            "processing": {"model": config.processing.model,
                           "extract_actions": config.processing.extract_actions,
                           "extract_tags": config.processing.extract_tags},
            "sync": {"mode": config.sync.mode, "schedule": config.sync.schedule},
            "search": {"enabled": config.search.enabled,
                       "backend": config.search.backend},
            "microsoft": {"enabled": config.microsoft.enabled,
                          "todo_enabled": config.microsoft.todo_enabled,
                          "calendar_enabled": config.microsoft.calendar_enabled,
                          "onenote_enabled": config.microsoft.onenote.enabled,
                          "teams_enabled": config.microsoft.teams.enabled},
            "reverse_sync": {"enabled": config.reverse_sync.enabled},
            "plugins": {"enabled": config.plugins.enabled},
            "web": {"host": config.web.host, "port": config.web.port},
        }
        return templates.TemplateResponse(
            request, "settings.html", {"settings": redacted},
        )

    # -- PWA: manifest + service worker + push subscribe --

    @app.get("/manifest.webmanifest")
    async def manifest():
        return JSONResponse({
            "name": config.web.app_name,
            "short_name": "reMark",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#ffffff",
            "theme_color": "#1f2937",
            "icons": [
                {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
        })

    @app.get("/service-worker.js")
    async def service_worker():
        sw_path = STATIC_DIR / "service-worker.js"
        return HTMLResponse(
            sw_path.read_text(encoding="utf-8"),
            media_type="application/javascript",
        )

    @app.get("/vapid-public-key")
    async def vapid_public_key():
        return JSONResponse({"key": config.web.vapid_public_key})

    @app.post("/webpush/subscribe")
    async def webpush_subscribe(
        request: Request, _=Depends(_auth_check),
    ):
        data = await request.json()
        endpoint = data.get("endpoint", "")
        keys = data.get("keys", {})
        p256dh = keys.get("p256dh", "")
        auth_key = keys.get("auth", "")
        ua = request.headers.get("user-agent", "")[:200]

        if not endpoint or not p256dh or not auth_key:
            raise HTTPException(status_code=400, detail="Missing subscription fields")

        state = get_state()
        try:
            state.add_webpush_subscription(endpoint, p256dh, auth_key, ua)
        finally:
            state.close()
        return {"ok": True}

    @app.get("/healthz")
    async def health():
        """Liveness + readiness probe.

        Returns 200 with ``status="ok"`` when the state DB is reachable
        and vault path exists. 503 with ``status="degraded"`` otherwise.
        Mirrors the checks a systemd watchdog or Docker HEALTHCHECK needs.
        """
        checks: dict[str, str] = {}
        ok = True

        try:
            state = get_state()
            try:
                state.conn.execute("SELECT 1").fetchone()
                checks["state_db"] = "ok"
            finally:
                state.close()
        except Exception as exc:
            ok = False
            checks["state_db"] = f"error: {exc.__class__.__name__}"

        try:
            vault_path = Path(config.obsidian.vault_path).expanduser()
            checks["vault"] = "ok" if vault_path.exists() else "missing"
            if not vault_path.exists():
                ok = False
        except Exception as exc:
            ok = False
            checks["vault"] = f"error: {exc.__class__.__name__}"

        payload = {
            "status": "ok" if ok else "degraded",
            "version": _version(),
            "checks": checks,
        }
        return JSONResponse(
            payload,
            status_code=200 if ok else 503,
        )

    return app


__all__ = ["create_app"]
