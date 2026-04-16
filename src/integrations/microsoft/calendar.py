"""Outlook Calendar integration via Graph API.

Creates calendar events from action items that carry deadlines, and
from meeting notes detected in synced documents.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.integrations.microsoft.graph import GraphClient, GraphError
from src.integrations.microsoft.todo import _parse_deadline
from src.processing.actions import ActionItem

logger = logging.getLogger(__name__)


@dataclass
class CalendarEvent:
    """A summary of a created or fetched calendar event."""

    id: str
    subject: str
    start: str  # ISO datetime
    end: str
    body_preview: str = ""


class CalendarClient:
    """High-level client for Outlook Calendar."""

    def __init__(self, graph: GraphClient, calendar_id: str = ""):
        self._graph = graph
        self._calendar_id = calendar_id

    def _path(self, suffix: str = "") -> str:
        """Build the base path for the configured calendar."""
        base = f"/me/calendars/{self._calendar_id}" if self._calendar_id else "/me/calendar"
        return base + suffix

    async def create_deadline_event(
        self,
        action: ActionItem,
        source_note: str | None = None,
        duration_minutes: int = 30,
    ) -> str | None:
        """Create a calendar event for an action item with a deadline.

        Returns the event ID, or None if the deadline couldn't be parsed.
        """
        if not action.deadline:
            return None

        dt = _parse_deadline(action.deadline)
        if dt is None:
            logger.debug("Could not parse deadline '%s', skipping event", action.deadline)
            return None

        end = dt + timedelta(minutes=duration_minutes)

        body_parts = [action.task]
        if source_note:
            body_parts.append(f"\nSource: {source_note}")
        if action.source_context and action.source_context != action.task:
            body_parts.append(f"\nContext: {action.source_context}")

        event_body = {
            "subject": action.task,
            "start": {
                "dateTime": dt.isoformat(),
                "timeZone": "UTC",
            },
            "end": {
                "dateTime": end.isoformat(),
                "timeZone": "UTC",
            },
            "body": {
                "contentType": "text",
                "content": "\n".join(body_parts),
            },
            "reminderMinutesBeforeStart": 30,
            "isReminderOn": True,
        }

        try:
            result = await self._graph.post(self._path("/events"), body=event_body)
            event_id = result["id"]
            logger.info(
                "Created calendar event '%s' on %s (%s)",
                action.task[:50],
                dt.date().isoformat(),
                event_id[:8],
            )
            return event_id
        except GraphError as e:
            logger.warning("Failed to create calendar event: %s", e)
            return None

    async def create_meeting_event(
        self,
        subject: str,
        start: datetime,
        duration_minutes: int = 60,
        notes: str = "",
        attendees: list[str] | None = None,
    ) -> str | None:
        """Create a calendar event for a meeting referenced in notes."""
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        end = start + timedelta(minutes=duration_minutes)

        event_body: dict = {
            "subject": subject,
            "start": {
                "dateTime": start.isoformat(),
                "timeZone": "UTC",
            },
            "end": {
                "dateTime": end.isoformat(),
                "timeZone": "UTC",
            },
        }

        if notes:
            event_body["body"] = {
                "contentType": "text",
                "content": notes,
            }

        if attendees:
            event_body["attendees"] = [
                {"emailAddress": {"address": addr}, "type": "required"} for addr in attendees
            ]

        try:
            result = await self._graph.post(self._path("/events"), body=event_body)
            return result["id"]
        except GraphError as e:
            logger.warning("Failed to create meeting event: %s", e)
            return None

    async def delete_event(self, event_id: str) -> bool:
        """Delete a calendar event."""
        try:
            await self._graph.delete(self._path(f"/events/{event_id}"))
            return True
        except GraphError as e:
            logger.warning("Failed to delete event %s: %s", event_id[:8], e)
            return False
