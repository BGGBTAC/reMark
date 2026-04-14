"""Tests for Microsoft/Outlook integration."""

from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import MicrosoftConfig
from src.integrations.microsoft.auth import MicrosoftAuth, MicrosoftAuthError
from src.integrations.microsoft.calendar import CalendarClient
from src.integrations.microsoft.service import MicrosoftService
from src.integrations.microsoft.todo import (
    TodoClient,
    TodoTask,
    _parse_deadline,
    _priority_to_importance,
)
from src.processing.actions import ActionItem

# =====================
# MicrosoftAuth
# =====================

class TestMicrosoftAuth:
    def test_missing_client_id_raises(self, tmp_path):
        with pytest.raises(MicrosoftAuthError, match="client_id"):
            MicrosoftAuth(client_id="", token_cache_path=tmp_path / "cache.bin")

    def test_init_with_client_id(self, tmp_path):
        auth = MicrosoftAuth(
            client_id="fake-client-id",
            tenant="common",
            token_cache_path=tmp_path / "cache.bin",
        )
        assert auth._client_id == "fake-client-id"
        assert "common" in auth._authority

    def test_has_cached_token_empty(self, tmp_path):
        auth = MicrosoftAuth(
            client_id="fake",
            token_cache_path=tmp_path / "nonexistent.bin",
        )
        assert not auth.has_cached_token()

    @pytest.mark.asyncio
    async def test_get_access_token_no_cache_raises(self, tmp_path):
        auth = MicrosoftAuth(
            client_id="fake",
            token_cache_path=tmp_path / "empty.bin",
        )
        with pytest.raises(MicrosoftAuthError, match="setup-microsoft"):
            await auth.get_access_token()


# =====================
# TodoClient helpers
# =====================

class TestPriorityMapping:
    def test_high_maps_to_high(self):
        assert _priority_to_importance("high") == "high"

    def test_medium_maps_to_normal(self):
        assert _priority_to_importance("medium") == "normal"

    def test_low_maps_to_low(self):
        assert _priority_to_importance("low") == "low"

    def test_unknown_defaults_normal(self):
        assert _priority_to_importance("nonsense") == "normal"


class TestParseDeadline:
    def test_iso_date(self):
        dt = _parse_deadline("2026-04-15")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 4
        assert dt.day == 15

    def test_iso_datetime(self):
        dt = _parse_deadline("2026-04-15T10:30:00")
        assert dt is not None
        assert dt.hour == 10
        assert dt.minute == 30

    def test_invalid_format(self):
        assert _parse_deadline("Friday") is None

    def test_empty(self):
        assert _parse_deadline("") is None


# =====================
# TodoClient
# =====================

class TestTodoClient:
    @pytest.mark.asyncio
    async def test_get_or_create_list_existing(self):
        graph = MagicMock()
        graph.get = AsyncMock(return_value={
            "value": [
                {"id": "list-abc", "displayName": "reMark"},
                {"id": "list-xyz", "displayName": "Other"},
            ],
        })

        client = TodoClient(graph, list_name="reMark")
        list_id = await client.get_or_create_list()

        assert list_id == "list-abc"

    @pytest.mark.asyncio
    async def test_get_or_create_list_creates(self):
        graph = MagicMock()
        graph.get = AsyncMock(return_value={"value": []})
        graph.post = AsyncMock(return_value={"id": "new-list"})

        client = TodoClient(graph, list_name="reMark", auto_create=True)
        list_id = await client.get_or_create_list()

        assert list_id == "new-list"
        graph.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_or_create_list_no_auto_create(self):
        from src.integrations.microsoft.graph import GraphError

        graph = MagicMock()
        graph.get = AsyncMock(return_value={"value": []})

        client = TodoClient(graph, list_name="Missing", auto_create=False)
        with pytest.raises(GraphError, match="not found"):
            await client.get_or_create_list()

    @pytest.mark.asyncio
    async def test_create_task_basic(self):
        graph = MagicMock()
        graph.get = AsyncMock(return_value={"value": [{"id": "list-1", "displayName": "reMark"}]})
        graph.post = AsyncMock(return_value={"id": "task-abc"})

        client = TodoClient(graph, list_name="reMark")
        action = ActionItem(task="Write tests", priority="medium")

        task_id = await client.create_task(action)

        assert task_id == "task-abc"
        call_args = graph.post.call_args
        body = call_args.kwargs["body"]
        assert body["title"] == "Write tests"
        assert body["importance"] == "normal"

    @pytest.mark.asyncio
    async def test_create_task_with_deadline(self):
        graph = MagicMock()
        graph.get = AsyncMock(return_value={"value": [{"id": "list-1", "displayName": "reMark"}]})
        graph.post = AsyncMock(return_value={"id": "task-deadline"})

        client = TodoClient(graph, list_name="reMark")
        action = ActionItem(
            task="Ship feature",
            priority="high",
            deadline="2026-05-01",
        )

        await client.create_task(action, source_note="Sprint Planning")

        body = graph.post.call_args.kwargs["body"]
        assert body["importance"] == "high"
        assert "dueDateTime" in body
        assert "2026-05-01" in body["dueDateTime"]["dateTime"]
        assert "Sprint Planning" in body["body"]["content"]

    @pytest.mark.asyncio
    async def test_get_task_success(self):
        graph = MagicMock()
        graph.get = AsyncMock(side_effect=[
            {"value": [{"id": "list-1", "displayName": "reMark"}]},
            {"id": "task-1", "title": "Do X", "status": "completed"},
        ])

        client = TodoClient(graph, list_name="reMark")
        task = await client.get_task("task-1")

        assert task is not None
        assert task.is_completed
        assert task.title == "Do X"

    @pytest.mark.asyncio
    async def test_list_completed_since(self):
        graph = MagicMock()
        graph.get = AsyncMock(side_effect=[
            {"value": [{"id": "list-1", "displayName": "reMark"}]},
            {"id": "t1", "title": "A", "status": "completed"},
            {"id": "t2", "title": "B", "status": "notStarted"},
            {"id": "t3", "title": "C", "status": "completed"},
        ])

        client = TodoClient(graph, list_name="reMark")
        completed = await client.list_completed_since(["t1", "t2", "t3"])

        assert "t1" in completed
        assert "t3" in completed
        assert "t2" not in completed


class TestTodoTask:
    def test_is_completed(self):
        t = TodoTask(id="x", title="y", status="completed", list_id="l")
        assert t.is_completed

    def test_not_completed(self):
        t = TodoTask(id="x", title="y", status="notStarted", list_id="l")
        assert not t.is_completed


# =====================
# CalendarClient
# =====================

class TestCalendarClient:
    @pytest.mark.asyncio
    async def test_create_deadline_event(self):
        graph = MagicMock()
        graph.post = AsyncMock(return_value={"id": "event-abc"})

        client = CalendarClient(graph)
        action = ActionItem(
            task="Submit report",
            deadline="2026-04-20",
        )

        event_id = await client.create_deadline_event(action, source_note="Weekly")

        assert event_id == "event-abc"
        body = graph.post.call_args.kwargs["body"]
        assert body["subject"] == "Submit report"
        assert "2026-04-20" in body["start"]["dateTime"]
        assert "Weekly" in body["body"]["content"]

    @pytest.mark.asyncio
    async def test_create_deadline_event_no_deadline(self):
        graph = MagicMock()
        client = CalendarClient(graph)
        action = ActionItem(task="No deadline")

        event_id = await client.create_deadline_event(action)
        assert event_id is None

    @pytest.mark.asyncio
    async def test_create_deadline_event_unparseable(self):
        graph = MagicMock()
        client = CalendarClient(graph)
        action = ActionItem(task="X", deadline="Friday")

        event_id = await client.create_deadline_event(action)
        assert event_id is None

    @pytest.mark.asyncio
    async def test_create_meeting_event(self):
        from datetime import datetime

        graph = MagicMock()
        graph.post = AsyncMock(return_value={"id": "mtg-1"})

        client = CalendarClient(graph)
        event_id = await client.create_meeting_event(
            subject="Team Sync",
            start=datetime(2026, 4, 15, 10, 0, tzinfo=UTC),
            duration_minutes=60,
            notes="Agenda items",
            attendees=["alice@example.com"],
        )

        assert event_id == "mtg-1"
        body = graph.post.call_args.kwargs["body"]
        assert body["subject"] == "Team Sync"
        assert len(body["attendees"]) == 1


# =====================
# MicrosoftService
# =====================

class TestMicrosoftService:
    def test_disabled_when_no_client_id(self):
        config = MicrosoftConfig(enabled=True, client_id="")
        service = MicrosoftService(config)
        assert not service.enabled

    def test_disabled_when_no_features(self):
        config = MicrosoftConfig(
            enabled=True,
            client_id="x",
            todo_enabled=False,
            calendar_enabled=False,
        )
        service = MicrosoftService(config)
        assert not service.enabled

    def test_enabled_with_todo(self):
        config = MicrosoftConfig(
            enabled=True,
            client_id="x",
            todo_enabled=True,
            calendar_enabled=False,
        )
        service = MicrosoftService(config)
        assert service.enabled

    @pytest.mark.asyncio
    async def test_sync_actions_skips_when_disabled(self):
        config = MicrosoftConfig(enabled=False)
        service = MicrosoftService(config)

        result = await service.sync_actions(
            [ActionItem(task="X")], source_note="N",
        )
        assert result.tasks_created == []
        assert result.events_created == []

    @pytest.mark.asyncio
    async def test_sync_actions_skips_when_no_token(self, tmp_path):
        config = MicrosoftConfig(
            enabled=True,
            client_id="fake",
            token_cache_path=str(tmp_path / "no-cache.bin"),
        )
        service = MicrosoftService(config)

        result = await service.sync_actions(
            [ActionItem(task="X")], source_note="N",
        )
        # No cached token -> skipped, no errors
        assert result.tasks_created == []

    @pytest.mark.asyncio
    async def test_sync_actions_ignores_questions(self, tmp_path):
        config = MicrosoftConfig(
            enabled=True,
            client_id="fake",
            token_cache_path=str(tmp_path / "cache.bin"),
            todo_enabled=True,
        )
        service = MicrosoftService(config)

        # Mock auth as having a cached token
        mock_auth = MagicMock()
        mock_auth.has_cached_token.return_value = True
        service._auth = mock_auth

        mock_graph_ctx = AsyncMock()
        mock_graph_ctx.__aenter__ = AsyncMock(return_value=mock_graph_ctx)
        mock_graph_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.integrations.microsoft.service.GraphClient",
            return_value=mock_graph_ctx,
        ), patch(
            "src.integrations.microsoft.service.TodoClient",
        ) as mock_todo_cls:
            mock_todo = AsyncMock()
            mock_todo_cls.return_value = mock_todo

            # Only pass questions → no tasks should be created
            result = await service.sync_actions(
                [ActionItem(task="Why X?", type="question")],
                source_note="N",
            )

            mock_todo.create_task.assert_not_called()
            assert result.tasks_created == []


# =====================
# State external_links
# =====================

class TestExternalLinks:
    def test_record_and_retrieve(self, tmp_path):
        from src.sync.state import SyncState

        state = SyncState(tmp_path / "s.db")
        state.record_external_link("doc-1", "microsoft_todo", "task", "task-abc")

        links = state.get_external_links(doc_id="doc-1")
        assert len(links) == 1
        assert links[0]["external_id"] == "task-abc"
        assert links[0]["status"] == "active"
        state.close()

    def test_mark_completed(self, tmp_path):
        from src.sync.state import SyncState

        state = SyncState(tmp_path / "s.db")
        state.record_external_link("doc-1", "microsoft_todo", "task", "task-abc")
        state.mark_external_link_completed("microsoft_todo", "task-abc")

        active = state.get_external_links(status="active")
        assert len(active) == 0

        completed = state.get_external_links(status="completed")
        assert len(completed) == 1
        state.close()

    def test_filters(self, tmp_path):
        from src.sync.state import SyncState

        state = SyncState(tmp_path / "s.db")
        state.record_external_link("doc-1", "microsoft_todo", "task", "t1")
        state.record_external_link("doc-1", "microsoft_calendar", "event", "e1")
        state.record_external_link("doc-2", "microsoft_todo", "task", "t2")

        only_tasks = state.get_external_links(doc_id="doc-1", kind="task")
        assert len(only_tasks) == 1
        assert only_tasks[0]["external_id"] == "t1"
        state.close()
