"""Tests for the v0.7 audit log."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.sync.state import SyncState


@pytest.fixture
def state(tmp_path):
    s = SyncState(tmp_path / "state.db")
    yield s
    s.close()


class TestAuditInsertAndList:
    def test_insert_minimal(self, state):
        state.audit(action="login", user_id=1, username="admin")
        rows = state.list_audit()
        assert len(rows) == 1
        assert rows[0]["action"] == "login"
        assert rows[0]["user_id"] == 1
        assert rows[0]["username"] == "admin"

    def test_list_filter_by_action(self, state):
        state.audit(action="login", username="a")
        state.audit(action="http", username="a")
        state.audit(action="login", username="b")
        logins = state.list_audit(action="login")
        assert {r["username"] for r in logins} == {"a", "b"}

    def test_list_filter_by_user_id(self, state):
        state.audit(action="login", user_id=1)
        state.audit(action="login", user_id=2)
        assert len(state.list_audit(user_id=1)) == 1

    def test_list_respects_limit_and_offset(self, state):
        for i in range(5):
            state.audit(action="http", resource=f"/p/{i}")
        # Rows are returned most-recent-first (ORDER BY id DESC).
        all_rows = state.list_audit(limit=5)
        first_two = state.list_audit(limit=2, offset=0)
        next_two = state.list_audit(limit=2, offset=2)
        assert [r["id"] for r in first_two] == [r["id"] for r in all_rows[:2]]
        assert [r["id"] for r in next_two] == [r["id"] for r in all_rows[2:4]]

    def test_user_agent_is_truncated(self, state):
        state.audit(action="http", user_agent="x" * 500)
        row = state.list_audit()[0]
        assert len(row["user_agent"]) == 255


class TestAuditPrune:
    def test_prune_deletes_old_rows(self, state):
        old_iso = (datetime.now(UTC) - timedelta(days=120)).isoformat()
        # Insert a row, then back-date it
        state.audit(action="http")
        state.conn.execute("UPDATE audit_log SET ts = ?", (old_iso,))
        state.conn.commit()

        deleted = state.audit_prune(retention_days=90)
        assert deleted == 1
        assert state.list_audit() == []

    def test_prune_keeps_recent(self, state):
        state.audit(action="http")
        assert state.audit_prune(retention_days=90) == 0
        assert len(state.list_audit()) == 1
