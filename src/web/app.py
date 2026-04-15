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
        return cached

    # -- Routes --

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, _=Depends(_auth_check)):
        state = get_state()
        try:
            stats = state.get_sync_stats()
            usage = state.get_api_usage_summary(days=30)
            recent_log = state.get_recent_log(limit=10)
            queue_summary = state.queue_summary()
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
            rows = state.list_devices(active_only=False)
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
