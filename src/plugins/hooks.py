"""Plugin hook base classes.

Plugins subclass one or more of these to extend reMark. The registry
discovers implementations and invokes them at the appropriate pipeline
stages. All hooks are optional — a plugin can implement just one kind.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class PluginMetadata:
    """Identity data every plugin must provide."""

    name: str
    version: str = "0.0.1"
    description: str = ""
    author: str = ""


class Plugin(ABC):
    """Base class every reMark plugin must inherit from."""

    @property
    @abstractmethod
    def metadata(self) -> PluginMetadata: ...

    def configure(self, settings: dict[str, Any]) -> None:  # noqa: B027
        """Receive plugin-specific config from config.plugins.settings[name].

        Default: no-op. Plugins override to consume their own settings.
        """


class ActionExtractorHook(Plugin):
    """Extend action item detection with custom patterns or heuristics."""

    @abstractmethod
    async def extract(self, text: str, context: dict) -> list[dict]:
        """Return a list of action dicts with at least a 'task' key."""


class OCRBackendHook(Plugin):
    """Provide an additional OCR engine usable from the pipeline."""

    @abstractmethod
    async def recognize_page(self, page_image: bytes) -> dict:
        """Return dict with keys: text, confidence (0-1), engine (name)."""


class NoteProcessorHook(Plugin):
    """Post-process a structured note before it's written to the vault."""

    @abstractmethod
    async def process(self, content: str, frontmatter: dict) -> tuple[str, dict]:
        """Return possibly-modified (content, frontmatter)."""


class SyncHook(Plugin):
    """Fire pre- and post-sync events (e.g. notifications, telemetry)."""

    async def before_sync(self, context: dict) -> None:
        """Called at the start of each sync cycle."""

    async def after_sync(self, context: dict, report: dict) -> None:
        """Called at the end of each sync cycle with the SyncReport data."""

    async def after_document(self, doc_id: str, vault_path: str, result: dict) -> None:
        """Called after each individual document is processed."""
