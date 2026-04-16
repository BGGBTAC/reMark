"""Report runner — builds context, calls the configured LLM, dispatches to channels."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from src.config import AppConfig
from src.llm.client import LLMClient, LLMMessage
from src.sync.state import SyncState

logger = logging.getLogger(__name__)


@dataclass
class ReportResult:
    report_id: int
    name: str
    content: str
    channels_ok: list[str]
    channels_failed: list[tuple[str, str]]   # (channel, error)

    @property
    def ok(self) -> bool:
        return not self.channels_failed


_SYSTEM_PROMPT = """\
You are generating a scheduled summary report from a knowledge base.
The user will supply:

1. The purpose of the report (the "prompt" field).
2. A snapshot of recent activity + notes from a vault.

Rules:
- Write concise, skimmable Markdown. Short sentences.
- Use headings, bullet lists, and tables where appropriate.
- Cite source notes with their title (no made-up data).
- If the context is sparse, say so — don't invent content.
- Keep the total output under 800 words unless the prompt asks otherwise.
"""


async def run_report(
    report: dict,
    state: SyncState,
    config: AppConfig,
    user_id: int | None = None,
    llm: LLMClient | None = None,
) -> ReportResult:
    """Build context, call the LLM, dispatch to every configured channel.

    ``report`` is the row as returned by ``SyncState.list_reports`` /
    ``get_report``. ``user_id`` scopes the context to a specific user's
    vault — leave ``None`` for install-wide reports.

    ``llm`` is the LLMClient to use. When None the function falls back to a
    plain stats dump (useful in offline / demo mode without an API key).
    """
    name = report["name"]
    channels = json.loads(report["channels"])
    content = await _generate_summary(llm, report, state, config, user_id)

    channels_ok: list[str] = []
    channels_failed: list[tuple[str, str]] = []

    for channel in channels:
        try:
            if channel == "vault":
                _deliver_vault(config, name, content)
            elif channel == "teams":
                await _deliver_teams(config, name, content)
            elif channel == "notion":
                await _deliver_notion(config, name, content)
            else:
                raise ValueError(f"unknown channel: {channel}")
            channels_ok.append(channel)
        except Exception as exc:  # noqa: BLE001
            logger.warning("report %s → %s failed: %s", name, channel, exc)
            channels_failed.append((channel, str(exc)))

    return ReportResult(
        report_id=int(report["id"]),
        name=name,
        content=content,
        channels_ok=channels_ok,
        channels_failed=channels_failed,
    )


async def _generate_summary(
    llm: LLMClient | None,
    report: dict,
    state: SyncState,
    config: AppConfig,
    user_id: int | None,
) -> str:
    ctx_notes = state.recent_synced(limit=50, user_id=user_id)
    stats = state.get_sync_stats()

    # Compact JSON-y context — keeps token use predictable.
    context_lines = [
        "## Recent notes",
        *(
            f"- [{n['last_synced_at']}] {n['doc_name']} "
            f"(folder={n['parent_folder']}, pages={n['page_count']}, "
            f"actions={n['action_count']})"
            for n in ctx_notes
        ),
        "",
        "## Sync stats",
        f"- total: {stats.total_docs}",
        f"- synced: {stats.synced}",
        f"- errors: {stats.errors}",
        f"- last sync: {stats.last_sync or 'never'}",
    ]
    context = "\n".join(context_lines)

    user_content = (
        f"Report purpose:\n{report['prompt']}\n\n"
        f"Context (last 50 notes + current sync stats):\n\n{context}"
    )

    if llm is None:
        # No LLM available — fall back to a plain stats dump so the channel
        # delivery path is still exercised in demo / offline mode.
        return (
            f"# {report['name']}\n\n"
            f"_No LLM configured; raw context dump:_\n\n"
            f"{context}\n"
        )

    response = await llm.complete(
        system=_SYSTEM_PROMPT,
        messages=[LLMMessage(role="user", content=user_content)],
        model=config.processing.model,
        max_tokens=2048,
    )
    return response.text.strip()


# ---------------------------------------------------------------------------
# Output channels
# ---------------------------------------------------------------------------


def _deliver_vault(config: AppConfig, name: str, content: str) -> None:
    """Write the report as a dated Markdown note under Reports/."""
    from pathlib import Path

    vault = Path(config.obsidian.vault_path).expanduser()
    reports_dir = vault / "Reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    stamp = now.strftime("%Y-%m-%d")
    slug = name.replace(" ", "-").lower()
    path = reports_dir / f"{stamp}-{slug}.md"
    fm = (
        "---\n"
        f"title: {name} — {stamp}\n"
        "source: report\n"
        f"generated_at: {now.isoformat()}\n"
        "---\n\n"
    )
    path.write_text(fm + content, encoding="utf-8")


async def _deliver_teams(config: AppConfig, name: str, content: str) -> None:
    """Post an Adaptive Card to the configured Teams webhook."""
    webhook = getattr(config.microsoft.teams, "webhook_url", "")
    if not webhook:
        raise RuntimeError("microsoft.teams.webhook_url not set")
    import httpx

    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "type": "AdaptiveCard",
                "version": "1.5",
                "body": [
                    {"type": "TextBlock", "size": "Large",
                     "weight": "Bolder", "text": name},
                    {"type": "TextBlock", "wrap": True,
                     "text": content[:12000]},
                ],
            },
        }],
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(webhook, json=card)
    if resp.status_code >= 400:
        raise RuntimeError(f"Teams webhook HTTP {resp.status_code}: {resp.text[:200]}")


async def _deliver_notion(config: AppConfig, name: str, content: str) -> None:
    """Create a child page under notion.vault_mirror_page_id."""
    if not getattr(config, "notion", None) or not config.notion.enabled:
        raise RuntimeError("notion integration disabled")
    from src.integrations.notion import NotionService

    service = NotionService(config.notion)
    if not service.enabled:
        raise RuntimeError("notion token missing")
    result = await service.write_note(
        title=f"{name} — {datetime.now(UTC).strftime('%Y-%m-%d')}",
        content=content,
        tags=["report"],
    )
    if result is None:
        raise RuntimeError("notion write returned None")


class ReportRunner:
    """Thin OO wrapper so the scheduler can hold a single instance."""

    def __init__(self, config: AppConfig, state: SyncState, llm: LLMClient | None = None):
        self._config = config
        self._state = state
        self._llm = llm

    async def run(self, report: dict, user_id: int | None = None) -> ReportResult:
        return await run_report(report, self._state, self._config, user_id, llm=self._llm)
