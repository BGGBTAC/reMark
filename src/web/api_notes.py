"""Bridge API — per-note sync status and preview rendering.

Auth: Bearer token (same token as /api/push). Path traversal is blocked
by resolving the vault-relative path and asserting the resolved target
stays under the configured vault root.
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

logger = logging.getLogger(__name__)

# In-process cache: rm_hash → (inserted_at, png_bytes). Keyed on the sha256
# of the raw .rm bytes so a changed note automatically invalidates the entry.
_PREVIEW_CACHE: dict[str, tuple[float, bytes]] = {}
_PREVIEW_TTL_SECONDS = 24 * 60 * 60

router = APIRouter(prefix="/api/notes", tags=["bridge"])


def _bridge_auth(request: Request) -> str:
    """Resolve a Bearer token to a label. 401 on anything invalid.

    Mirrors the same logic as the inline ``_bridge_auth`` closure in
    ``app.py`` but reads the shared SyncState directly from the request's
    app state so it works from an external router module.
    """
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = header.split(" ", 1)[1].strip()
    state = request.app.state.sync_state
    label = state.verify_bridge_token(token)
    if label is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or revoked token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return label


def _resolve_vault_path(request: Request, vault_path: str) -> Path:
    """Resolve a vault-relative path and reject anything that escapes the root."""
    vault_root = Path(request.app.state.config.obsidian.vault_path).expanduser().resolve()
    target = (vault_root / vault_path).resolve()
    if vault_root not in target.parents and vault_root != target:
        raise HTTPException(status_code=400, detail="path traversal detected")
    return target


def _render_first_page(rm_bytes: bytes) -> bytes:
    """Render page 0 of an .rm document to PNG bytes.

    Writes the bytes to a temporary file so the existing file-based renderer
    can parse them without a separate code path.
    """
    import tempfile

    from src.remarkable.formats import _render_ocr_svg, _svg_to_png  # type: ignore[attr-defined]
    from src.remarkable.formats import parse_rm_file

    with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as tmp:
        tmp.write(rm_bytes)
        tmp_path = Path(tmp.name)

    try:
        page = parse_rm_file(tmp_path)
    except Exception as exc:
        logger.warning("failed to parse .rm bytes for preview: %s", exc)
        raise HTTPException(status_code=500, detail="failed to render preview") from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    if not page.lines:
        raise HTTPException(status_code=404, detail="page has no renderable content")

    # Lower DPI is fine for preview thumbnails — faster, smaller response.
    dpi = 150
    svg = _render_ocr_svg(page.lines, dpi, high_contrast=False)
    return _svg_to_png(svg, dpi)


@router.get("/{vault_path:path}/status")
async def get_note_status(
    request: Request,
    vault_path: str,
):
    """Return sync metadata for a vault-relative note path.

    Responds with the last sync timestamp, device, and error state as
    tracked in the sync_state table. 404 if the note has never been synced.
    Auth: Bearer token.
    """
    _bridge_auth(request)
    _resolve_vault_path(request, vault_path)
    state = request.app.state.sync_state
    row = state.get_sync_state_by_vault_path(vault_path)
    if row is None:
        raise HTTPException(status_code=404, detail="note not tracked")
    return {
        "vault_path": vault_path,
        "synced_at": row.get("synced_at"),
        "device_id": row.get("device_id"),
        "pending_push": bool(row.get("pending_push", False)),
        "last_error": row.get("last_error"),
    }


@router.get("/{vault_path:path}/preview")
async def get_note_preview(
    request: Request,
    vault_path: str,
):
    """Return a PNG preview of the first .rm page for a synced note.

    The PNG is generated from the cached .rm bytes (written by the sync
    engine to ``~/.remark-bridge/cache/<doc_id>/last.rm``) and cached
    in-process for 24 h keyed on the sha256 of the raw .rm bytes. Returns
    404 when no .rm file has been cached yet for the note (available after
    the next full sync cycle).
    Auth: Bearer token.
    """
    _bridge_auth(request)
    _resolve_vault_path(request, vault_path)
    state = request.app.state.sync_state

    rm_bytes = state.load_last_rm_bytes(vault_path)
    if rm_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="no .rm file cached for this note — try again after the next sync",
        )

    rm_hash = hashlib.sha256(rm_bytes).hexdigest()
    now = time.time()
    cached = _PREVIEW_CACHE.get(rm_hash)
    if cached is not None and (now - cached[0]) < _PREVIEW_TTL_SECONDS:
        return Response(content=cached[1], media_type="image/png")

    png = _render_first_page(rm_bytes)
    _PREVIEW_CACHE[rm_hash] = (now, png)
    return Response(content=png, media_type="image/png")
