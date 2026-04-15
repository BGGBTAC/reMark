"""FastAPI app for the reMark web dashboard + PWA.

Built server-side with Jinja2 + HTMX + Alpine.js + Tailwind (CDN).
No build step required. Designed to run via `remark-bridge serve-web`.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from src.config import AppConfig, load_config
from src.obsidian.vault import ObsidianVault
from src.sync.state import SyncState
from src.web import auth as web_auth

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"


def _strip_mask(values: dict) -> dict:
    """Drop keys equal to the MASK sentinel — used before model validation."""
    from src.web.config_writer import MASK

    out = {}
    for key, value in values.items():
        if isinstance(value, dict):
            out[key] = _strip_mask(value)
        elif value == MASK:
            continue
        else:
            out[key] = value
    return out


def _flatten(values: dict, prefix: str = "") -> dict:
    """Flatten nested dicts into dotted keys for update_section()."""
    out = {}
    for key, value in values.items():
        dotted = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, dotted))
        else:
            out[dotted] = value
    return out


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

    # REMARK_DEMO_MODE=1 populates a scratch state DB + vault with
    # deterministic demo data so the docs screenshot workflow can
    # render the UI without talking to reMarkable Cloud. Seeding is
    # idempotent — first hit writes the fixtures, subsequent startups
    # are no-ops.
    from src.web import demo

    if demo.is_enabled():
        try:
            demo.seed(config)
        except Exception as exc:  # noqa: BLE001 — demo must never crash the app
            logger.warning("demo seed failed: %s", exc)

    # FastAPI lifespan — brings the report scheduler up alongside the
    # app so a single serve-web process covers both. Skipped in demo
    # mode (screenshot workflow) and when reports are turned off.
    import asyncio as _asyncio
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):  # noqa: ANN001
        scheduler_task = None
        reports_cfg = getattr(config, "reports", None)
        reports_enabled = (
            reports_cfg is None or getattr(reports_cfg, "enabled", True)
        )
        if reports_enabled and not demo.is_enabled():
            try:
                from src.reports.scheduler import ReportScheduler

                tick = 60
                if reports_cfg is not None:
                    tick = getattr(reports_cfg, "tick_seconds", 60)
                scheduler = ReportScheduler(
                    config, get_state(), tick_seconds=tick,
                )
                _app.state.report_scheduler = scheduler
                scheduler_task = _asyncio.create_task(scheduler.run())
            except Exception as exc:  # noqa: BLE001
                logger.warning("report scheduler failed to start: %s", exc)
        try:
            yield
        finally:
            if scheduler_task is not None:
                _app.state.report_scheduler.stop()
                scheduler_task.cancel()
                try:
                    await scheduler_task
                except (_asyncio.CancelledError, Exception):
                    pass

    app = FastAPI(
        title=config.web.app_name, openapi_url=None, lifespan=lifespan,
    )

    # Session cookies — user_id + username only. The secret_key comes
    # from config; fall back to a random per-process value so an
    # unconfigured fresh install still boots (sessions reset on
    # restart, which is the right behaviour there).
    secret_key = (config.web.session_secret or "").strip()
    if not secret_key:
        secret_key = secrets.token_urlsafe(32)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        session_cookie="remark_session",
        same_site="lax",
        https_only=bool(config.web.session_https_only),
        max_age=60 * 60 * 24 * 14,   # 2 weeks
    )

    # Audit middleware — logs every state-mutating request (POST /
    # PUT / PATCH / DELETE). GETs are noisy and stay out of the audit
    # log; they still land in access logs through uvicorn.
    @app.middleware("http")
    async def audit_middleware(request: Request, call_next):
        response = await call_next(request)
        try:
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                user = None
                with contextlib.suppress(Exception):
                    user = web_auth.current_user(request)
                state = getattr(app.state, "sync_state", None)
                if state is not None:
                    state.audit(
                        action="http",
                        user_id=(int(user["id"]) if user else None),
                        username=(user["username"] if user else None),
                        resource=str(request.url.path),
                        method=request.method,
                        status=response.status_code,
                        ip=(request.client.host if request.client else None),
                        user_agent=request.headers.get("user-agent", ""),
                    )
        except Exception as exc:  # noqa: BLE001
            # Audit logging must never take down a request.
            logger.warning("audit middleware failed: %s", exc)
        return response

    # Mount static files for CSS / JS / manifest / service worker
    app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static",
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["app_name"] = config.web.app_name

    def _auth_check(request: Request) -> dict:
        """Session-first auth, with the legacy HTTP Basic path as a
        fallback for automation scripts that already rely on it.

        Returns the authenticated user dict so downstream handlers
        can scope their data (per-user vaults, audit metadata, ...).
        """
        user = web_auth.current_user(request)
        if user is not None:
            return user

        # Legacy Basic auth fallback — same semantics as pre-0.7.
        if config.web.username and config.web.password:
            header = request.headers.get("authorization", "")
            if header.lower().startswith("basic "):
                from base64 import b64decode
                try:
                    raw = b64decode(header.split(" ", 1)[1]).decode()
                    u, _, p = raw.partition(":")
                except Exception:
                    u, p = "", ""
                ok_user = secrets.compare_digest(
                    u.encode(), config.web.username.encode(),
                )
                ok_pass = secrets.compare_digest(
                    p.encode(), config.web.password.encode(),
                )
                if ok_user and ok_pass:
                    return {"id": None, "username": config.web.username, "role": "admin"}

        # No session + no Basic match → redirect browsers to /login,
        # return 401 to JSON/API clients.
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/login"},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Auth required",
        )

    # -- State accessors --

    def get_vault() -> ObsidianVault:
        return ObsidianVault(
            Path(config.obsidian.vault_path).expanduser(),
            config.obsidian.folder_map,
        )

    def get_state() -> SyncState:
        """Return a process-wide SyncState singleton.

        Constructing a ``SyncState`` runs ``_ensure_schema`` which
        acquires the WAL write lock. On hot routes (``/api/*``, every
        dashboard refresh) that would stall the async event loop and
        race the sync daemon for the lock. Cache one connection per
        process; callers must NOT ``close()`` it.
        """
        from src.config import resolve_path

        cached = getattr(app.state, "sync_state", None)
        if cached is None:
            cached = SyncState(resolve_path(config.sync.state_db))
            cached._shared = True  # marks close() as a no-op
            app.state.sync_state = cached
            # v0.7+ bootstrap — seeds an admin user on a fresh DB.
            # Safe to call on every restart (no-op when users exist).
            try:
                web_auth.bootstrap_admin(cached)
            except Exception as exc:  # noqa: BLE001
                logger.warning("admin bootstrap failed: %s", exc)
        return cached

    def _scope_user_id(request: Request) -> int | None:
        """Return the user_id the current viewer should see rows for.

        ``None`` means "no filter" — admins see everything across the
        install, consistent with how the CLI works today. Regular
        users only ever see rows tagged with their own id.
        """
        user = web_auth.current_user(request)
        if user is None:
            return None
        if user.get("role") == "admin":
            return None
        return int(user["id"])

    # -- Routes --

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, error: str | None = None):
        return templates.TemplateResponse(
            request, "login.html", {"error": error},
        )

    @app.post("/login")
    async def login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ):
        state = get_state()
        user = web_auth.authenticate(state, username, password)
        if user is None:
            state.audit(
                action="login_failed", username=username,
                ip=(request.client.host if request.client else None),
                user_agent=request.headers.get("user-agent", ""),
            )
            return templates.TemplateResponse(
                request, "login.html",
                {"error": "Invalid username or password."},
                status_code=401,
            )
        request.session["user_id"] = int(user["id"])
        request.session["username"] = user["username"]
        state.audit(
            action="login", user_id=int(user["id"]),
            username=user["username"],
            ip=(request.client.host if request.client else None),
            user_agent=request.headers.get("user-agent", ""),
        )
        return RedirectResponse(url="/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        state = get_state()
        user_id = request.session.get("user_id")
        username = request.session.get("username")
        request.session.clear()
        if user_id:
            state.audit(
                action="logout", user_id=int(user_id), username=username,
            )
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/reports", response_class=HTMLResponse)
    async def reports_view(
        request: Request, _=Depends(_auth_check),
        saved: str | None = None, error: str | None = None,
    ):
        web_auth.require_admin(request)
        state = get_state()
        return templates.TemplateResponse(
            request, "reports.html",
            {
                "reports": state.list_reports(),
                "saved": saved, "error": error,
            },
        )

    @app.post("/reports/create")
    async def reports_create(
        request: Request,
        name: str = Form(...),
        schedule: str = Form(...),
        prompt: str = Form(...),
        channels: list[str] = Form(default=[]),
        enabled: str = Form("on"),
    ):
        admin = web_auth.require_admin(request)
        state = get_state()
        if state.get_report_by_name(name):
            return RedirectResponse("/reports?error=exists", status_code=303)
        if not channels:
            return RedirectResponse("/reports?error=no-channels", status_code=303)
        from src.reports.scheduler import next_run

        try:
            first_run = next_run(schedule, datetime.now(UTC))
        except ValueError as exc:
            return RedirectResponse(
                f"/reports?error={str(exc)[:80]}", status_code=303,
            )
        report_id = state.create_report(
            name=name,
            schedule=schedule,
            prompt=prompt,
            channels=channels,
            enabled=(enabled == "on"),
            created_by=int(admin["id"]),
        )
        state.update_report(report_id, next_run_at=first_run.isoformat())
        return RedirectResponse("/reports?saved=1", status_code=303)

    @app.post("/reports/{report_id}/toggle")
    async def reports_toggle(request: Request, report_id: int):
        web_auth.require_admin(request)
        state = get_state()
        report = state.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404)
        state.update_report(report_id, enabled=not bool(report["enabled"]))
        return RedirectResponse("/reports", status_code=303)

    @app.post("/reports/{report_id}/delete")
    async def reports_delete(request: Request, report_id: int):
        web_auth.require_admin(request)
        state = get_state()
        state.delete_report(report_id)
        return RedirectResponse("/reports", status_code=303)

    @app.post("/reports/{report_id}/run")
    async def reports_run_now(request: Request, report_id: int):
        """Ad-hoc trigger — runs the report immediately, bypassing the schedule."""
        web_auth.require_admin(request)
        state = get_state()
        report = state.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404)
        from src.reports.runner import run_report

        try:
            result = await run_report(report, state, config)
            state.update_report(
                report_id,
                last_run_at=datetime.now(UTC).isoformat(),
                last_status=("ok" if result.ok else "partial"),
                last_error=(
                    "; ".join(f"{c}: {e}" for c, e in result.channels_failed)
                    if result.channels_failed else None
                ),
            )
        except Exception as exc:  # noqa: BLE001
            state.update_report(
                report_id,
                last_run_at=datetime.now(UTC).isoformat(),
                last_status="error",
                last_error=str(exc)[:500],
            )
            return RedirectResponse(
                f"/reports?error={str(exc)[:80]}", status_code=303,
            )
        return RedirectResponse("/reports?saved=1", status_code=303)

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_view(
        request: Request,
        _=Depends(_auth_check),
        action: str | None = None,
        user_id: int | None = None,
        page: int = 1,
    ):
        web_auth.require_admin(request)
        state = get_state()
        page = max(page, 1)
        page_size = 100
        offset = (page - 1) * page_size
        rows = state.list_audit(
            limit=page_size, offset=offset,
            action=action or None, user_id=user_id,
        )
        return templates.TemplateResponse(
            request, "audit.html",
            {
                "rows": rows,
                "page": page,
                "has_more": len(rows) == page_size,
                "filter_action": action or "",
                "filter_user_id": user_id or "",
            },
        )

    @app.get("/audit.csv")
    async def audit_csv(
        request: Request,
        _=Depends(_auth_check),
        action: str | None = None,
        user_id: int | None = None,
        limit: int = 5000,
    ):
        """Export the audit log as CSV (admin only)."""
        import csv
        import io

        web_auth.require_admin(request)
        state = get_state()
        rows = state.list_audit(
            limit=limit, offset=0,
            action=action or None, user_id=user_id,
        )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "id", "ts", "user_id", "username", "action", "resource",
            "method", "status", "ip", "user_agent", "details",
        ])
        for r in rows:
            writer.writerow([r.get(k, "") for k in (
                "id", "ts", "user_id", "username", "action", "resource",
                "method", "status", "ip", "user_agent", "details",
            )])
        from fastapi.responses import Response
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="audit.csv"',
            },
        )

    @app.get("/users", response_class=HTMLResponse)
    async def users_view(request: Request, _=Depends(_auth_check)):
        admin = web_auth.require_admin(request)
        state = get_state()
        return templates.TemplateResponse(
            request, "users.html",
            {"users": state.list_users(), "current_user": admin},
        )

    @app.post("/users/create")
    async def users_create(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        role: str = Form("user"),
        vault_path: str = Form(""),
    ):
        admin = web_auth.require_admin(request)
        state = get_state()
        if state.get_user(username):
            return RedirectResponse(url="/users?error=exists", status_code=303)
        if role not in ("admin", "user"):
            role = "user"
        user_id = state.create_user(
            username=username,
            password_hash=web_auth.hash_password(password),
            role=role,
            vault_path=(vault_path.strip() or None),
        )
        state.audit(
            action="user_create", user_id=admin["id"], username=admin["username"],
            resource=f"user:{user_id}", details=f"created '{username}' ({role})",
        )
        return RedirectResponse(url="/users", status_code=303)

    @app.post("/users/{user_id}/toggle")
    async def users_toggle(request: Request, user_id: int):
        admin = web_auth.require_admin(request)
        state = get_state()
        target = state.get_user_by_id(user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="User not found")
        if target["id"] == admin["id"]:
            raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
        state.set_user_active(user_id, not bool(target["active"]))
        state.audit(
            action="user_toggle", user_id=admin["id"], username=admin["username"],
            resource=f"user:{user_id}",
            details=f"active={not bool(target['active'])}",
        )
        return RedirectResponse(url="/users", status_code=303)

    @app.post("/users/{user_id}/password")
    async def users_password(
        request: Request,
        user_id: int,
        password: str = Form(...),
    ):
        admin = web_auth.require_admin(request)
        state = get_state()
        if not password or len(password) < 8:
            raise HTTPException(status_code=400, detail="Password too short (min 8 chars)")
        state.set_user_password(user_id, web_auth.hash_password(password))
        state.audit(
            action="user_password", user_id=admin["id"], username=admin["username"],
            resource=f"user:{user_id}",
        )
        return RedirectResponse(url="/users", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, _=Depends(_auth_check)):
        state = get_state()
        try:
            stats = state.get_sync_stats()
            usage = state.get_api_usage_summary(days=30)
            recent_log = state.get_recent_log(limit=10)
            queue_summary = state.queue_summary()
            recent_rows = state.recent_synced(
                limit=10, user_id=_scope_user_id(request),
            )
        finally:
            state.close()

        # Pull "Recent notes" from the state DB instead of walking the
        # whole vault with rglob on every request. The vault path
        # lookup remains so we can link to the rendered note.
        vault = get_vault()
        vault_root = vault.path.resolve()
        recent_notes = []
        for row in recent_rows:
            vault_path = Path(row["vault_path"]) if row["vault_path"] else None
            rel = ""
            if vault_path is not None:
                try:
                    rel = str(vault_path.resolve().relative_to(vault_root))
                except (ValueError, OSError):
                    rel = vault_path.name
            recent_notes.append({
                "title": row["doc_name"] or Path(row["vault_path"] or "").stem,
                "path": rel,
                "summary": (row.get("parent_folder") or "")[:120],
                "modified": row["last_synced_at"] or "",
            })

        return templates.TemplateResponse(
            request, "dashboard.html",
            {
                "stats": stats, "usage": usage,
                "recent_notes": recent_notes, "recent_log": recent_log,
                "queue_summary": queue_summary,
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
        state = get_state()
        try:
            rows = state.list_synced(
                folder=folder, query=q, limit=300,
                user_id=_scope_user_id(request),
            )
        finally:
            state.close()

        vault_root = vault.path.resolve()
        notes = []
        for row in rows:
            vault_path = row.get("vault_path") or ""
            try:
                rel = str(Path(vault_path).resolve().relative_to(vault_root))
            except (ValueError, OSError):
                rel = Path(vault_path).name
            notes.append({
                "title": row["doc_name"] or Path(vault_path).stem,
                "path": rel,
                "summary": (row.get("parent_folder") or "")[:160],
                "tags": [],
                "action_items": row.get("action_count") or 0,
            })

        return templates.TemplateResponse(
            request, "notes.html",
            {"notes": notes, "filter_folder": folder, "filter_query": q},
        )

    @app.get("/notes/{note_path:path}", response_class=HTMLResponse)
    async def view_note(
        request: Request, note_path: str, _=Depends(_auth_check),
    ):
        vault = get_vault()
        vault_root = vault.path.resolve()
        # ``vault.path / "../../.../x"`` is a valid Path object — the /
        # operator doesn't normalize. Resolve both sides and require
        # the result to live under the vault root, otherwise the
        # endpoint would leak any file the process can read.
        try:
            full_path = (vault.path / note_path).resolve()
        except (OSError, RuntimeError):
            raise HTTPException(status_code=404, detail="Note not found") from None
        if full_path != vault_root and vault_root not in full_path.parents:
            raise HTTPException(status_code=404, detail="Note not found")

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
                mode=config.search.mode,
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
            # Log the real error server-side but never render it into
            # the HTML — exception messages can carry API keys
            # (httpx status errors echo the URL; Anthropic errors echo
            # a token prefix) and users' browsers cache pages.
            logger.warning("ask failed: %s", e)
            return templates.TemplateResponse(
                request, "ask.html",
                {
                    "search_enabled": True, "answer": None, "hits": [],
                    "query": query,
                    "error": "Search failed — check server logs for details.",
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

    # -- Bridge API (bearer-token auth, for external clients like the
    # Obsidian companion plugin) ---------------------------------------

    def _bridge_auth(request: Request) -> str:
        """Resolve a Bearer token to a label. 401 on anything invalid."""
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise HTTPException(
                status_code=401, detail="Bearer token required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = header.split(" ", 1)[1].strip()
        state = get_state()
        try:
            label = state.verify_bridge_token(token)
        finally:
            state.close()
        if label is None:
            raise HTTPException(
                status_code=401, detail="Invalid or revoked token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return label

    @app.get("/api/status")
    async def api_status(request: Request):
        _label = _bridge_auth(request)
        state = get_state()
        try:
            stats = state.get_sync_stats()
            queue = state.queue_summary()
        finally:
            state.close()
        return {
            "version": _version(),
            "client": _label,
            "sync": {
                "total_docs": stats.total_docs,
                "synced": stats.synced,
                "errors": stats.errors,
                "pending": stats.pending,
                "last_sync": stats.last_sync,
            },
            "queue": queue,
        }

    @app.post("/api/push")
    async def api_push(request: Request):
        """Enqueue an Obsidian note for reverse-sync to the tablet.

        Payload: ``{"vault_path": "relative/path.md"}``. The vault path
        must resolve inside the configured vault directory — absolute
        paths or paths that escape via ``..`` are rejected.
        """
        _label = _bridge_auth(request)
        payload = await request.json()
        rel = str(payload.get("vault_path", "")).strip()
        if not rel:
            raise HTTPException(status_code=400, detail="vault_path required")

        vault_root = Path(config.obsidian.vault_path).expanduser().resolve()
        target = (vault_root / rel).resolve()
        if vault_root not in target.parents and target != vault_root:
            raise HTTPException(
                status_code=400, detail="vault_path escapes the vault",
            )
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="Note not found")

        state = get_state()
        try:
            ok = state.enqueue_reverse_push(str(target))
        finally:
            state.close()
        return {"queued": ok, "vault_path": rel}

    @app.get("/templates", response_class=HTMLResponse)
    async def templates_index(request: Request, _=Depends(_auth_check)):
        from src.config import resolve_path
        from src.templates.engine import TemplateEngine

        engine = TemplateEngine(resolve_path(config.templates.user_templates_dir))
        templates_list = sorted(
            engine.list_templates(), key=lambda t: t.name,
        )
        # Also expose the raw list of YAML files in the user dir so the
        # editor can reach files whose YAML failed to parse.
        user_dir = Path(config.templates.user_templates_dir).expanduser()
        user_files = (
            sorted(p.name for p in user_dir.glob("*.yaml"))
            if user_dir.exists() else []
        )
        return templates.TemplateResponse(
            request, "templates_index.html",
            {"templates": templates_list, "user_files": user_files},
        )

    @app.get("/templates/{name}", response_class=HTMLResponse)
    async def templates_edit(
        request: Request, name: str, _=Depends(_auth_check),
    ):
        import re as _re

        if not _re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise HTTPException(status_code=400, detail="Invalid template name")

        user_dir = Path(config.templates.user_templates_dir).expanduser()
        user_path = user_dir / f"{name}.yaml"
        builtin_path = Path(__file__).parent.parent / "templates" / "builtin" / f"{name}.yaml"

        if user_path.exists():
            source = user_path.read_text(encoding="utf-8")
            origin = "user"
        elif builtin_path.exists():
            source = builtin_path.read_text(encoding="utf-8")
            origin = "builtin"
        else:
            source = f"name: {name}\ndescription: \"\"\nfields: []\n"
            origin = "new"

        return templates.TemplateResponse(
            request, "templates_edit.html",
            {
                "name": name,
                "source": source,
                "origin": origin,
                "saved": request.query_params.get("saved") == "1",
                "error": None,
            },
        )

    @app.post("/templates/{name}")
    async def templates_save(
        request: Request, name: str, _=Depends(_auth_check),
    ):
        import re as _re

        import yaml as _yaml

        if not _re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise HTTPException(status_code=400, detail="Invalid template name")

        form_data = await request.form()
        source = str(form_data.get("source", ""))

        # Validate before writing — refuse to save a template the engine
        # would later drop on disk.
        try:
            data = _yaml.safe_load(source)
            if not isinstance(data, dict) or "name" not in data:
                raise ValueError("Top-level must be a mapping with a 'name' key.")
            # Parse via engine's own loader to catch field-level issues.
            from src.templates.engine import _parse_template  # type: ignore[attr-defined]
            _parse_template(data)
        except Exception as exc:
            return templates.TemplateResponse(
                request, "templates_edit.html",
                {
                    "name": name,
                    "source": source,
                    "origin": "user",
                    "saved": False,
                    "error": str(exc),
                },
                status_code=400,
            )

        user_dir = Path(config.templates.user_templates_dir).expanduser()
        user_dir.mkdir(parents=True, exist_ok=True)
        user_path = user_dir / f"{name}.yaml"
        user_path.write_text(source, encoding="utf-8")

        state = get_state()
        try:
            state._log("templates", None, f"saved {name}")
        finally:
            state.close()

        return RedirectResponse(
            url=f"/templates/{name}?saved=1", status_code=303,
        )

    @app.post("/templates/{name}/preview")
    async def templates_preview(
        request: Request, name: str, _=Depends(_auth_check),
    ):
        """Render a template preview as PDF bytes for live preview."""
        import tempfile

        import yaml as _yaml

        form_data = await request.form()
        source = str(form_data.get("source", ""))

        try:
            data = _yaml.safe_load(source)
            if not isinstance(data, dict):
                raise ValueError("Not a YAML mapping")
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        # Write to a temp dir, point a new engine at it, render PDF.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / f"{name}.yaml"
            tmp_path.write_text(source, encoding="utf-8")
            try:
                from src.templates.engine import TemplateEngine
                engine = TemplateEngine(tmp)
                pdf = engine.render_pdf(data["name"], {})
            except Exception as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)

        from fastapi.responses import Response
        return Response(content=pdf, media_type="application/pdf")

    @app.get("/queue", response_class=HTMLResponse)
    async def queue_view(
        request: Request,
        status: str | None = None,
        _=Depends(_auth_check),
    ):
        state = get_state()
        try:
            rows = state.list_queue(status=status)
            summary = state.queue_summary()
        finally:
            state.close()
        return templates.TemplateResponse(
            request, "queue.html",
            {"rows": rows, "summary": summary, "filter_status": status},
        )

    @app.post("/queue/{queue_id}/retry")
    async def queue_retry(queue_id: int, _=Depends(_auth_check)):
        state = get_state()
        try:
            state.retry_queue_entry(queue_id)
        finally:
            state.close()
        return RedirectResponse(url="/queue", status_code=303)

    @app.post("/queue/clear")
    async def queue_clear(
        request: Request, _=Depends(_auth_check),
    ):
        form = await request.form()
        status = form.get("status") or None
        state = get_state()
        try:
            state.clear_queue(status=status)
        finally:
            state.close()
        return RedirectResponse(url="/queue", status_code=303)

    @app.get("/devices", response_class=HTMLResponse)
    async def devices_view(request: Request, _=Depends(_auth_check)):
        state = get_state()
        try:
            rows = state.list_devices(
                active_only=False, user_id=_scope_user_id(request),
            )
        finally:
            state.close()
        return templates.TemplateResponse(
            request, "devices.html", {"devices": rows},
        )

    # Sections users can edit from the UI. Ordered deliberately to
    # put the commonly-tweaked ones first.
    editable_sections = [
        ("remarkable", "reMarkable", "remarkable"),
        ("sync", "Sync", "sync"),
        ("processing", "Processing (AI)", "processing"),
        ("ocr", "OCR", "ocr"),
        ("obsidian", "Obsidian vault", "obsidian"),
        ("search", "Search", "search"),
        ("microsoft", "Microsoft Graph", "microsoft"),
        ("notion", "Notion", "notion"),
        ("reverse_sync", "Reverse sync", "reverse_sync"),
        ("response", "Responses", "response"),
        ("templates", "Templates", "templates"),
        ("reports", "Reports", "reports"),
        ("plugins", "Plugins", "plugins"),
        ("web", "Web UI", "web"),
        ("logging", "Logging", "logging"),
    ]
    # Changes to these sections require a daemon restart to take effect.
    restart_sections = {"sync", "web", "logging", "remarkable"}

    def _collect_secret_keys(model_cls, prefix: str = "") -> set[str]:
        from pydantic import BaseModel

        from src.web.config_writer import is_secret_field
        keys: set[str] = set()
        for name, info in model_cls.model_fields.items():
            dotted = f"{prefix}{name}"
            anno = info.annotation
            if isinstance(anno, type) and issubclass(anno, BaseModel):
                keys |= _collect_secret_keys(anno, prefix=f"{dotted}.")
            elif is_secret_field(name):
                keys.add(dotted)
        return keys

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_index(request: Request, _=Depends(_auth_check)):
        return templates.TemplateResponse(
            request, "settings.html",
            {"sections": editable_sections},
        )

    @app.get("/settings/{section}", response_class=HTMLResponse)
    async def settings_section(
        request: Request, section: str, _=Depends(_auth_check),
    ):
        from src.config import AppConfig as _AppCfg
        from src.web.settings_forms import build_form

        if section not in {s[0] for s in editable_sections}:
            raise HTTPException(status_code=404, detail="Unknown section")

        model_field = _AppCfg.model_fields[section]
        submodel = model_field.annotation
        current = getattr(config, section)

        form = build_form(submodel, current, title=section.capitalize())
        return templates.TemplateResponse(
            request, "settings_section.html",
            {
                "section": section,
                "form": form,
                "restart_required": section in restart_sections,
                "saved": request.query_params.get("saved") == "1",
                "error": None,
            },
        )

    @app.post("/settings/{section}")
    async def settings_section_save(
        request: Request, section: str, _=Depends(_auth_check),
    ):
        import os

        from src.config import AppConfig as _AppCfg
        from src.web.config_writer import update_section
        from src.web.settings_forms import build_form, parse_form

        if section not in {s[0] for s in editable_sections}:
            raise HTTPException(status_code=404, detail="Unknown section")

        form_raw = await request.form()
        raw = {k: v for k, v in form_raw.items()}
        submodel = _AppCfg.model_fields[section].annotation
        updates = parse_form(submodel, raw)

        # Validate by instantiating the submodel. Failing fields show
        # up in a re-rendered form rather than a 500.
        try:
            submodel(**_strip_mask(updates))
        except Exception as exc:
            current = getattr(config, section)
            return templates.TemplateResponse(
                request, "settings_section.html",
                {
                    "section": section,
                    "form": build_form(submodel, current),
                    "restart_required": section in restart_sections,
                    "saved": False,
                    "error": str(exc),
                },
                status_code=400,
            )

        # Apply to YAML on disk (keeping comments) and leak the change
        # into the running config so the user sees it immediately.
        config_path = os.environ.get("REMARK_CONFIG", "config.yaml")
        secret_keys = _collect_secret_keys(submodel)
        update_section(config_path, section, _flatten(updates), secret_keys)

        # Mutate the in-memory config for keys that don't require
        # process restart. Sync/web/logging changes show the banner.
        if section not in restart_sections:
            setattr(config, section, submodel(**_strip_mask(updates)))

        # Audit trail.
        state = get_state()
        try:
            state._log("settings", None, f"updated {section}")
        finally:
            state.close()

        return RedirectResponse(
            url=f"/settings/{section}?saved=1", status_code=303,
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
