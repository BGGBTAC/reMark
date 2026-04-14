"""Microsoft To Do integration via Graph API.

Creates tasks from reMark-extracted action items, tracks their
status, and syncs completion state back to the Obsidian vault.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from src.integrations.microsoft.graph import GraphClient, GraphError
from src.processing.actions import ActionItem

logger = logging.getLogger(__name__)


@dataclass
class TodoTask:
    """A task on a Microsoft To Do list."""

    id: str
    title: str
    status: str  # "notStarted" | "inProgress" | "completed" | "deferred"
    list_id: str
    due_date: str | None = None

    @property
    def is_completed(self) -> bool:
        return self.status == "completed"


class TodoClient:
    """High-level client for Microsoft To Do."""

    def __init__(self, graph: GraphClient, list_name: str, auto_create: bool = True):
        self._graph = graph
        self._list_name = list_name
        self._auto_create = auto_create
        self._list_id: str | None = None

    async def get_or_create_list(self) -> str:
        """Ensure the target task list exists. Returns its ID."""
        if self._list_id:
            return self._list_id

        data = await self._graph.get("/me/todo/lists")
        for lst in data.get("value", []):
            if lst.get("displayName") == self._list_name:
                self._list_id = lst["id"]
                return self._list_id

        if not self._auto_create:
            raise GraphError(
                f"Task list '{self._list_name}' not found and auto_create is disabled"
            )

        created = await self._graph.post(
            "/me/todo/lists",
            body={"displayName": self._list_name},
        )
        self._list_id = created["id"]
        logger.info("Created To Do list '%s' (%s)", self._list_name, self._list_id[:8])
        return self._list_id

    async def create_task(
        self,
        action: ActionItem,
        source_note: str | None = None,
    ) -> str:
        """Create a task in Microsoft To Do from an ActionItem.

        Returns the new task's Graph ID.
        """
        list_id = await self.get_or_create_list()

        body: dict = {
            "title": action.task,
            "importance": _priority_to_importance(action.priority),
        }

        if action.deadline:
            parsed = _parse_deadline(action.deadline)
            if parsed:
                body["dueDateTime"] = {
                    "dateTime": parsed.isoformat(),
                    "timeZone": "UTC",
                }

        if source_note or action.source_context:
            note_parts = []
            if source_note:
                note_parts.append(f"Source: {source_note}")
            if action.source_context and action.source_context != action.task:
                note_parts.append(f"Context: {action.source_context}")
            if action.assignee:
                note_parts.append(f"Assignee: @{action.assignee}")
            body["body"] = {
                "content": "\n".join(note_parts),
                "contentType": "text",
            }

        result = await self._graph.post(f"/me/todo/lists/{list_id}/tasks", body=body)
        task_id = result["id"]
        logger.info("Created To Do task '%s' (%s)", action.task[:50], task_id[:8])
        return task_id

    async def get_task(self, task_id: str) -> TodoTask | None:
        """Fetch a single task by ID."""
        list_id = await self.get_or_create_list()
        try:
            data = await self._graph.get(f"/me/todo/lists/{list_id}/tasks/{task_id}")
        except GraphError:
            return None

        return TodoTask(
            id=data["id"],
            title=data.get("title", ""),
            status=data.get("status", "notStarted"),
            list_id=list_id,
            due_date=(data.get("dueDateTime") or {}).get("dateTime"),
        )

    async def list_completed_since(self, task_ids: list[str]) -> list[str]:
        """Return IDs of tasks from the given list that are marked completed."""
        if not task_ids:
            return []

        list_id = await self.get_or_create_list()
        completed: list[str] = []

        # Graph doesn't support filtering by ID list, so query each or batch
        # For a small list, individual GETs are fine
        for tid in task_ids:
            try:
                data = await self._graph.get(f"/me/todo/lists/{list_id}/tasks/{tid}")
                if data.get("status") == "completed":
                    completed.append(tid)
            except GraphError:
                # Task may have been deleted
                continue

        return completed

    async def delete_task(self, task_id: str) -> bool:
        """Delete a task. Returns True on success."""
        list_id = await self.get_or_create_list()
        try:
            await self._graph.delete(f"/me/todo/lists/{list_id}/tasks/{task_id}")
            return True
        except GraphError as e:
            logger.warning("Failed to delete task %s: %s", task_id[:8], e)
            return False


# -- Helpers --

_PRIORITY_MAP = {
    "high": "high",
    "medium": "normal",
    "low": "low",
}


def _priority_to_importance(priority: str) -> str:
    return _PRIORITY_MAP.get(priority, "normal")


def _parse_deadline(deadline: str) -> datetime | None:
    """Attempt to parse various deadline formats into a UTC datetime."""
    deadline = deadline.strip()

    # ISO format (2026-04-15 or 2026-04-15T10:00:00)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(deadline, fmt)
            return dt.replace(tzinfo=UTC)
        except ValueError:
            continue

    # Relative dates could be added later (e.g. "Friday", "next week")
    return None
