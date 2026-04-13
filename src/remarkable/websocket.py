"""WebSocket connection to reMarkable Cloud notifications.

Provides low-level WebSocket connectivity and message parsing
for real-time document change notifications.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import websockets

from src.remarkable.auth import AuthManager
from src.remarkable.cloud import RemarkableCloud

logger = logging.getLogger(__name__)


@dataclass
class NotificationEvent:
    """A parsed notification from the reMarkable WebSocket."""

    event_type: str  # "DocAdded", "DocDeleted", "DocChanged"
    doc_id: str
    source_device_id: str = ""
    parent: str = ""
    name: str = ""
    doc_type: str = ""
    version: int = 0
    bookmarked: bool = False
    raw: dict | None = None


class RemarkableWebSocket:
    """WebSocket client for reMarkable Cloud notifications."""

    def __init__(self, auth: AuthManager, cloud: RemarkableCloud):
        self._auth = auth
        self._cloud = cloud
        self._ws = None
        self._device_id: str | None = None

    async def connect(self) -> None:
        """Connect to the notification WebSocket."""
        host = await self._cloud.get_notifications_host()
        token = await self._auth.get_user_token()

        url = f"{host}/notifications/ws/json/1"
        headers = {"Authorization": f"Bearer {token}"}

        self._ws = await websockets.connect(url, additional_headers=headers)
        logger.info("WebSocket connected to %s", host)

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        if self._ws:
            await self._ws.close()
            self._ws = None
            logger.info("WebSocket disconnected")

    async def receive(self) -> NotificationEvent | None:
        """Wait for and return the next notification event.

        Returns None if the message couldn't be parsed.
        """
        if not self._ws:
            raise ConnectionError("WebSocket not connected")

        raw = await self._ws.recv()
        return _parse_notification(raw)

    async def ping(self) -> None:
        """Send a keepalive ping."""
        if self._ws:
            await self._ws.ping()

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._ws.open

    def set_device_id(self, device_id: str) -> None:
        """Set our device ID for filtering self-triggered events."""
        self._device_id = device_id

    def is_self_event(self, event: NotificationEvent) -> bool:
        """Check if an event was triggered by our own uploads."""
        if not self._device_id:
            return False
        return event.source_device_id == self._device_id


def _parse_notification(raw: str | bytes) -> NotificationEvent | None:
    """Parse a raw WebSocket message into a NotificationEvent."""
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")

        data = json.loads(raw)

        # The notification format has a message wrapper
        message = data if "event" in data else data.get("message", data)

        event_type = message.get("event", message.get("type", "unknown"))
        attributes = message.get("attributes", message)

        return NotificationEvent(
            event_type=event_type,
            doc_id=attributes.get("id", attributes.get("ID", "")),
            source_device_id=attributes.get("sourceDeviceID", ""),
            parent=attributes.get("parent", attributes.get("Parent", "")),
            name=attributes.get("VissibleName", attributes.get("visibleName", "")),
            doc_type=attributes.get("Type", attributes.get("type", "")),
            version=int(attributes.get("Version", attributes.get("version", 0))),
            bookmarked=attributes.get("Bookmarked", False),
            raw=data,
        )

    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning("Failed to parse notification: %s", e)
        return None
