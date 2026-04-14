"""Example plugin: extract @-mentions as high-priority action items.

Copy this file into `~/.config/remark/plugins/` and rename it to something
more specific. It's a minimal reference showing how to implement a hook.
"""

from __future__ import annotations

import re

from src.plugins.hooks import ActionExtractorHook, PluginMetadata

MENTION_RE = re.compile(r"@(\w+)\b")


class AtMentionExtractor(ActionExtractorHook):
    """Emit an action item whenever someone is @-mentioned in a note."""

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="at-mention-extractor",
            version="0.1.0",
            description="Create action items for @-mentions.",
        )

    async def extract(self, text: str, context: dict) -> list[dict]:
        seen: set[str] = set()
        actions: list[dict] = []

        for line in text.split("\n"):
            for match in MENTION_RE.finditer(line):
                user = match.group(1)
                if user.lower() in seen:
                    continue
                seen.add(user.lower())
                actions.append({
                    "task": f"Follow up with @{user}: {line.strip()[:120]}",
                    "type": "followup",
                    "priority": "high",
                    "assignee": user,
                    "source_context": line.strip(),
                })

        return actions
