"""Microsoft integration service — fassade for the sync engine.

Coordinates To Do task creation, calendar events, and reverse-syncing
completion status back to the Obsidian vault.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.config import MicrosoftConfig
from src.integrations.microsoft.auth import MicrosoftAuth
from src.integrations.microsoft.calendar import CalendarClient
from src.integrations.microsoft.graph import GraphClient
from src.integrations.microsoft.todo import TodoClient
from src.processing.actions import ActionItem

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """Outcome of a Microsoft sync for a single document."""

    tasks_created: list[str] = field(default_factory=list)  # Graph task IDs
    events_created: list[str] = field(default_factory=list)  # Graph event IDs
    errors: list[str] = field(default_factory=list)


class MicrosoftService:
    """High-level Microsoft integration for the sync engine."""

    def __init__(self, config: MicrosoftConfig):
        self._config = config
        self._auth: MicrosoftAuth | None = None

    def _get_auth(self) -> MicrosoftAuth:
        if self._auth is None:
            self._auth = MicrosoftAuth(
                client_id=self._config.client_id,
                tenant=self._config.tenant,
                token_cache_path=self._config.token_cache_path,
            )
        return self._auth

    @property
    def enabled(self) -> bool:
        return (
            self._config.enabled
            and bool(self._config.client_id)
            and (self._config.todo_enabled or self._config.calendar_enabled)
        )

    async def sync_actions(
        self,
        actions: list[ActionItem],
        source_note: str,
    ) -> SyncResult:
        """Push action items to To Do and deadlines to Calendar.

        Returns the IDs of created tasks/events so the caller can persist
        them for reverse-sync later.
        """
        result = SyncResult()
        if not self.enabled or not actions:
            return result

        try:
            auth = self._get_auth()
            if not auth.has_cached_token():
                logger.warning("Microsoft integration enabled but no token cached; skipping")
                return result

            async with GraphClient(auth) as graph:
                todo_client = None
                calendar_client = None

                if self._config.todo_enabled:
                    todo_client = TodoClient(
                        graph,
                        list_name=self._config.todo_list_name,
                        auto_create=self._config.todo_create_list,
                    )

                if self._config.calendar_enabled:
                    calendar_client = CalendarClient(
                        graph,
                        calendar_id=self._config.calendar_id,
                    )

                for action in actions:
                    if action.type != "task":
                        continue  # skip questions + followups for now

                    if todo_client:
                        try:
                            task_id = await todo_client.create_task(action, source_note)
                            result.tasks_created.append(task_id)
                        except Exception as e:
                            logger.warning(
                                "Failed to create task for '%s': %s",
                                action.task[:40], e,
                            )
                            result.errors.append(str(e))

                    if calendar_client and action.deadline:
                        try:
                            event_id = await calendar_client.create_deadline_event(
                                action, source_note,
                            )
                            if event_id:
                                result.events_created.append(event_id)
                        except Exception as e:
                            logger.warning(
                                "Failed to create event for '%s': %s",
                                action.task[:40], e,
                            )
                            result.errors.append(str(e))

        except Exception as e:
            logger.error("Microsoft sync failed: %s", e)
            result.errors.append(str(e))

        return result

    async def check_completed_tasks(self, task_ids: list[str]) -> list[str]:
        """Return which of the given Graph task IDs are completed."""
        if not self.enabled or not self._config.todo_enabled or not task_ids:
            return []

        try:
            auth = self._get_auth()
            if not auth.has_cached_token():
                return []

            async with GraphClient(auth) as graph:
                todo_client = TodoClient(
                    graph,
                    list_name=self._config.todo_list_name,
                    auto_create=False,
                )
                return await todo_client.list_completed_since(task_ids)
        except Exception as e:
            logger.warning("Failed to check completed tasks: %s", e)
            return []
