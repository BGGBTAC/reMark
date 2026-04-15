"""Tests for v0.7 multi-user auth + per-user isolation."""

from __future__ import annotations

import pytest

from src.sync.state import SyncState


@pytest.fixture
def state(tmp_path):
    s = SyncState(tmp_path / "state.db")
    yield s
    s.close()


class TestUsersTable:
    def test_create_user_returns_id(self, state):
        uid = state.create_user("alice", "hash", role="user")
        assert uid > 0

    def test_unique_username(self, state):
        import sqlite3

        state.create_user("alice", "h")
        with pytest.raises(sqlite3.IntegrityError):
            state.create_user("alice", "h2")

    def test_get_user(self, state):
        uid = state.create_user("bob", "h", role="admin")
        row = state.get_user("bob")
        assert row is not None
        assert row["id"] == uid
        assert row["role"] == "admin"

    def test_ensure_default_admin_is_idempotent(self, state):
        a = state.ensure_default_admin("h1")
        assert a is not None
        b = state.ensure_default_admin("h2")
        assert b is None      # table no longer empty
        users = state.list_users()
        assert len(users) == 1
        assert users[0]["username"] == "admin"

    def test_touch_login_updates_column(self, state):
        uid = state.create_user("c", "h")
        assert state.get_user("c")["last_login_at"] is None
        state.touch_user_login(uid)
        assert state.get_user("c")["last_login_at"] is not None

    def test_toggle_active(self, state):
        uid = state.create_user("d", "h")
        state.set_user_active(uid, False)
        assert state.get_user("d")["active"] == 0

    def test_password_update(self, state):
        uid = state.create_user("e", "old-hash")
        state.set_user_password(uid, "new-hash")
        assert state.get_user("e")["password_hash"] == "new-hash"


class TestBcryptHelpers:
    def test_hash_and_verify_round_trip(self):
        from src.web.auth import hash_password, verify_password

        h = hash_password("hunter2")
        assert h != "hunter2"
        assert verify_password("hunter2", h) is True
        assert verify_password("hunter3", h) is False

    def test_authenticate_success(self, state):
        from src.web.auth import authenticate, hash_password

        state.create_user("x", hash_password("pw"))
        user = authenticate(state, "x", "pw")
        assert user is not None
        assert user["username"] == "x"

    def test_authenticate_rejects_bad_password(self, state):
        from src.web.auth import authenticate, hash_password

        state.create_user("y", hash_password("pw"))
        assert authenticate(state, "y", "wrong") is None

    def test_authenticate_rejects_inactive(self, state):
        from src.web.auth import authenticate, hash_password

        uid = state.create_user("z", hash_password("pw"))
        state.set_user_active(uid, False)
        assert authenticate(state, "z", "pw") is None


class TestUserScopedQueries:
    def test_mark_synced_tags_user_id(self, state):
        state.mark_synced(
            doc_id="d1", doc_name="A", parent_folder="F",
            cloud_hash="h", vault_path="/v/a.md",
            ocr_engine="test", page_count=1, action_count=0,
            device_id="default", user_id=42,
        )
        row = state.conn.execute(
            "SELECT user_id FROM sync_state WHERE doc_id = ?", ("d1",),
        ).fetchone()
        assert row["user_id"] == 42

    def test_recent_synced_filter(self, state):
        for i, uid in enumerate([7, 7, 99], 1):
            state.mark_synced(
                doc_id=f"d{i}", doc_name=f"n{i}", parent_folder="F",
                cloud_hash=f"h{i}", vault_path=f"/v/{i}.md",
                ocr_engine="t", page_count=1, action_count=0,
                device_id="default", user_id=uid,
            )
        by_7 = state.recent_synced(user_id=7)
        by_99 = state.recent_synced(user_id=99)
        assert {r["doc_id"] for r in by_7} == {"d1", "d2"}
        assert {r["doc_id"] for r in by_99} == {"d3"}
        # No filter → see all
        assert len(state.recent_synced()) == 3

    def test_list_synced_filter(self, state):
        state.mark_synced(
            doc_id="u1", doc_name="x", parent_folder="F",
            cloud_hash="h", vault_path="/v/x.md",
            ocr_engine="t", page_count=1, action_count=0,
            user_id=5,
        )
        state.mark_synced(
            doc_id="u2", doc_name="y", parent_folder="F",
            cloud_hash="h", vault_path="/v/y.md",
            ocr_engine="t", page_count=1, action_count=0,
            user_id=9,
        )
        only_5 = state.list_synced(user_id=5)
        assert [r["doc_id"] for r in only_5] == ["u1"]
