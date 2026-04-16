"""Regression tests for the v0.6.5 security audit findings.

Each test locks in a specific fix so a future refactor can't silently
un-do it:

- H1: /notes/{path} must not leak files outside the vault
- H2: bridge-token verification is constant-time and uses
  ``secrets.compare_digest``
- M2: when: expression sandbox enforces size + recursion caps
- M4: multi-device sync clones the config per device
- M7: device-token file never exists with world-readable permissions
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from fastapi.testclient import TestClient

from src.config import AppConfig
from src.sync.state import SyncState


@pytest.fixture
def app_env(tmp_path):
    """A minimal but realistic AppConfig + state DB for the web app."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# inside vault\n", encoding="utf-8")
    # A "sensitive" file that lives outside the vault — the traversal
    # test checks we can't read this.
    outside = tmp_path / "SECRET.txt"
    outside.write_text("device-jwt-here", encoding="utf-8")

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cfg = {
        "sync": {"state_db": str(state_dir / "state.db")},
        "obsidian": {"vault_path": str(vault)},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))

    from src.web.app import create_app

    app = create_app(AppConfig(**cfg))
    return TestClient(app), vault, outside


# ---------------------------------------------------------------------------
# H1 — path traversal
# ---------------------------------------------------------------------------


class TestViewNotePathTraversal:
    def test_dot_dot_returns_404(self, app_env):
        client, _, _ = app_env
        resp = client.get("/notes/../../SECRET.txt")
        # Starlette may 404 before reaching the handler on some
        # traversal patterns — that's fine, we just need to never
        # serve the file.
        assert resp.status_code == 404
        assert "device-jwt-here" not in resp.text

    def test_dot_dot_with_encoded_slashes(self, app_env):
        client, _, _ = app_env
        resp = client.get("/notes/..%2F..%2FSECRET.txt")
        assert resp.status_code == 404
        assert "device-jwt-here" not in resp.text

    def test_absolute_outside_vault_rejected(self, app_env):
        client, _, outside = app_env
        # Use the absolute path as the note_path segment. The vault /
        # operator makes this a path inside the vault (the leading
        # slash is re-attached) but resolve() yields the real absolute
        # path — must be detected as outside.
        resp = client.get(f"/notes/{outside}")
        assert resp.status_code == 404

    def test_legit_path_still_works(self, app_env):
        client, _, _ = app_env
        resp = client.get("/notes/note.md")
        assert resp.status_code == 200
        assert "inside vault" in resp.text


# ---------------------------------------------------------------------------
# H2 — constant-time token verification
# ---------------------------------------------------------------------------


class TestBridgeTokenVerification:
    def test_uses_compare_digest(self, tmp_path, monkeypatch):
        state = SyncState(tmp_path / "s.db")
        try:
            token = state.issue_bridge_token("x")

            calls: list[tuple[str, str]] = []

            import secrets as _secrets

            real = _secrets.compare_digest

            def tracking(a, b):
                calls.append((a, b))
                return real(a, b)

            monkeypatch.setattr(_secrets, "compare_digest", tracking)

            assert state.verify_bridge_token(token) == "x"
            # Must have gone through compare_digest at least once
            assert calls, (
                "verify_bridge_token fell back to a direct string compare — "
                "timing channel re-opened"
            )
        finally:
            state.close()

    def test_wrong_token_never_matches(self, tmp_path):
        state = SyncState(tmp_path / "s.db")
        try:
            state.issue_bridge_token("real")
            assert state.verify_bridge_token("bogus-" * 8) is None
        finally:
            state.close()

    def test_empty_token_rejected(self, tmp_path):
        state = SyncState(tmp_path / "s.db")
        try:
            state.issue_bridge_token("real")
            assert state.verify_bridge_token("") is None
            assert state.verify_bridge_token("   ") is None
        finally:
            state.close()


# ---------------------------------------------------------------------------
# M2 — when: sandbox DoS caps
# ---------------------------------------------------------------------------


class TestWhenSandboxCaps:
    def test_over_length_expr_rejected(self):
        from src.templates.engine import ConditionError, evaluate_condition

        expr = " or ".join(["x == 1"] * 200)  # well past MAX_WHEN_EXPR_LEN
        with pytest.raises(ConditionError):
            evaluate_condition(expr, {"x": 1})

    def test_over_node_count_rejected(self):
        # Stay under the char cap but try to blow the node cap with
        # dozens of OR'd comparisons — each Compare pulls in multiple
        # AST nodes. 100 clauses exceeds MAX_WHEN_AST_NODES=200.
        from src.templates.engine import ConditionError, evaluate_condition

        expr = " or ".join(f"k{i} == {i}" for i in range(120))
        with pytest.raises(ConditionError):
            evaluate_condition(expr, {})

    def test_recursion_error_converted(self):
        """Deeply nested parens trigger RecursionError, which the
        sandbox must catch and re-raise as ConditionError."""
        from src.templates.engine import ConditionError, evaluate_condition

        expr = "(" * 400 + "1" + ")" * 400
        with pytest.raises(ConditionError):
            evaluate_condition(expr, {})


# ---------------------------------------------------------------------------
# M4 — multi-device config isolation
# ---------------------------------------------------------------------------


class TestMultiDeviceConfigIsolation:
    @pytest.mark.asyncio
    async def test_device_filters_do_not_leak_into_shared_config(
        self,
        tmp_path,
        monkeypatch,
    ):
        from src.config import DeviceConfig
        from src.main import _sync_once

        vault = tmp_path / "vault"
        vault.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        cfg = AppConfig(
            **{
                "sync": {"state_db": str(state_dir / "state.db")},
                "obsidian": {"vault_path": str(vault)},
            }
        )
        cfg.remarkable.sync_folders = ["Shared"]
        cfg.remarkable.devices = [
            DeviceConfig(
                id="pro",
                label="Pro",
                vault_subfolder="rm-pro",
                sync_folders=["Only-Pro"],
            ),
        ]

        # Stub out the heavy parts so we only exercise the loop.
        # RemarkableCloud + DocumentManager are imported lazily inside
        # _sync_once, so we patch at the modules where they originate.
        class FakeCloud:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

        monkeypatch.setattr(
            "src.remarkable.cloud.RemarkableCloud",
            lambda _auth: FakeCloud(),
        )
        monkeypatch.setattr(
            "src.remarkable.documents.DocumentManager",
            lambda *a, **kw: object(),
        )
        monkeypatch.setattr("src.main._get_auth", lambda *a, **kw: object())
        monkeypatch.setattr("src.main._get_ocr_pipeline", lambda _cfg, **kw: object())

        from src.sync.engine import SyncReport

        async def fake_sync_once(self, *args, **kwargs):
            # The device-specific engine should see the Pro-only filters.
            assert self._config.remarkable.sync_folders == ["Only-Pro"]
            return SyncReport()

        monkeypatch.setattr(
            "src.sync.engine.SyncEngine.sync_once",
            fake_sync_once,
        )

        await _sync_once(cfg)

        # After the loop, the shared config must remain untouched.
        assert cfg.remarkable.sync_folders == ["Shared"]


# ---------------------------------------------------------------------------
# M7 — atomic token file write
# ---------------------------------------------------------------------------


class TestDeviceTokenPermissions:
    @pytest.mark.asyncio
    async def test_saved_token_file_is_0600(self, tmp_path, monkeypatch):
        from src.remarkable.auth import AuthManager

        token_path = tmp_path / "device_token"
        manager = AuthManager(token_path)

        # Don't actually hit the Cloud API — mock the POST that returns
        # the device token.
        async def fake_post(*args, **kwargs):
            class R:
                status_code = 200
                text = "fake-device-token"

            return R()

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            post = AsyncMock(side_effect=fake_post)

        monkeypatch.setattr(
            "src.remarkable.auth.httpx.AsyncClient",
            lambda: FakeClient(),
        )

        await manager.register_device("one-time-code")

        mode = stat.S_IMODE(os.stat(token_path).st_mode)
        assert mode == 0o600, f"token file ended up {oct(mode)}"
        # Sibling tempfiles must not linger after the atomic swap
        leftovers = [p for p in Path(tmp_path).iterdir() if p.name.startswith(".device_token.")]
        assert leftovers == []
