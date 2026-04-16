"""Tests for v0.7 scheduled reports."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from src.llm.client import LLMClient, LLMResponse
from src.sync.state import SyncState


class _StubLLM(LLMClient):
    """Minimal stub — records complete() calls and returns canned text."""

    provider = "stub"

    def __init__(self, text: str = ""):
        self._text = text
        self.calls: list = []

    async def complete(self, system, messages, model, max_tokens=4096):
        self.calls.append((system, messages, model, max_tokens))
        return LLMResponse(
            text=self._text,
            input_tokens=1,
            output_tokens=1,
            provider=self.provider,
            model=model,
        )

    async def complete_vision(self, system, image, prompt, model, max_tokens=2048):
        raise NotImplementedError


@pytest.fixture
def state(tmp_path):
    s = SyncState(tmp_path / "state.db")
    yield s
    s.close()


class TestReportsCRUD:
    def test_create_and_get(self, state):
        rid = state.create_report(
            name="weekly",
            schedule="weekly mon 09:00",
            prompt="Summarize the week",
            channels=["vault", "teams"],
        )
        assert rid > 0
        row = state.get_report(rid)
        assert row["name"] == "weekly"
        assert json.loads(row["channels"]) == ["vault", "teams"]
        assert row["enabled"] == 1

    def test_unique_name(self, state):
        import sqlite3

        state.create_report("a", "daily 09:00", "p", ["vault"])
        with pytest.raises(sqlite3.IntegrityError):
            state.create_report("a", "daily 10:00", "p2", ["vault"])

    def test_update_partial(self, state):
        rid = state.create_report("r", "every 1h", "p", ["vault"])
        state.update_report(rid, enabled=False, prompt="new-prompt")
        row = state.get_report(rid)
        assert row["enabled"] == 0
        assert row["prompt"] == "new-prompt"
        # Unchanged fields preserved
        assert row["schedule"] == "every 1h"

    def test_delete(self, state):
        rid = state.create_report("r", "daily 01:00", "p", ["vault"])
        state.delete_report(rid)
        assert state.get_report(rid) is None

    def test_due_filters_disabled_and_future(self, state):
        past = "2020-01-01T00:00:00+00:00"
        future = "2099-01-01T00:00:00+00:00"

        r_disabled = state.create_report("d", "every 1h", "p", ["vault"])
        state.update_report(
            r_disabled,
            enabled=False,
            next_run_at=past,
        )

        r_future = state.create_report("f", "every 1h", "p", ["vault"])
        state.update_report(r_future, next_run_at=future)

        r_due = state.create_report("due", "every 1h", "p", ["vault"])
        state.update_report(r_due, next_run_at=past)

        due_ids = {r["id"] for r in state.due_reports()}
        assert r_due in due_ids
        assert r_disabled not in due_ids
        assert r_future not in due_ids


class TestScheduleParser:
    def test_every_minutes(self):
        from src.reports.scheduler import next_run

        ref = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)
        assert next_run("every 30m", ref) == datetime(2026, 4, 15, 12, 30, tzinfo=UTC)

    def test_every_hours(self):
        from src.reports.scheduler import next_run

        ref = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)
        assert next_run("every 2h", ref) == datetime(2026, 4, 15, 14, 0, tzinfo=UTC)

    def test_daily_before_today(self):
        from src.reports.scheduler import next_run

        ref = datetime(2026, 4, 15, 8, 0, tzinfo=UTC)
        assert next_run("daily 09:00", ref) == datetime(
            2026,
            4,
            15,
            9,
            0,
            tzinfo=UTC,
        )

    def test_daily_after_today_rolls_over(self):
        from src.reports.scheduler import next_run

        ref = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
        assert next_run("daily 09:00", ref) == datetime(
            2026,
            4,
            16,
            9,
            0,
            tzinfo=UTC,
        )

    def test_weekly(self):
        from src.reports.scheduler import next_run

        # Wed 2026-04-15 09:00 UTC — weekly mon 09:00 should be next Monday
        ref = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
        nxt = next_run("weekly mon 09:00", ref)
        assert nxt.weekday() == 0  # Monday
        assert nxt.hour == 9 and nxt.minute == 0
        assert nxt > ref

    def test_invalid_raises(self):
        from src.reports.scheduler import next_run

        with pytest.raises(ValueError):
            next_run("does-not-parse", datetime.now(UTC))


class TestRunnerVaultChannel:
    """The vault channel is purely local — covers the happy path."""

    @pytest.mark.asyncio
    async def test_vault_channel_writes_file(self, state, tmp_path):
        from src.config import AppConfig
        from src.reports.runner import run_report

        vault = tmp_path / "vault"
        vault.mkdir()
        config = AppConfig()
        config.obsidian.vault_path = str(vault)

        rid = state.create_report(
            name="smoke",
            schedule="every 1h",
            prompt="Smoke test",
            channels=["vault"],
        )

        # llm=None → offline fallback path, no API call made
        result = await run_report(state.get_report(rid), state, config, llm=None)
        assert result.channels_ok == ["vault"]
        assert result.channels_failed == []
        assert any((vault / "Reports").glob("*.md"))

    @pytest.mark.asyncio
    async def test_runner_uses_llm_client(self, state, tmp_path):
        from src.config import AppConfig
        from src.reports.runner import run_report

        vault = tmp_path / "vault"
        vault.mkdir()
        config = AppConfig()
        config.obsidian.vault_path = str(vault)

        llm = _StubLLM(text="## Weekly summary\n\nAll good.")

        rid = state.create_report(
            name="weekly",
            schedule="weekly mon 09:00",
            prompt="Summarise the week",
            channels=["vault"],
        )

        result = await run_report(state.get_report(rid), state, config, llm=llm)

        assert result.channels_ok == ["vault"]
        assert result.channels_failed == []
        # The LLM was called and its output ended up in the vault file
        assert llm.calls
        md_files = list((vault / "Reports").glob("*.md"))
        assert md_files
        content = md_files[0].read_text()
        assert "Weekly summary" in content
