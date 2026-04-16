"""Bridge API — per-note sync status.

Auth: Bearer token (same token as /api/push). Path traversal is blocked
by resolving the vault-relative path and asserting the resolved target
stays under the configured vault root.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

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
