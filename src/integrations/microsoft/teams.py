"""Microsoft Teams integration — Daily/Weekly digest + meeting correlation.

Uses Incoming Webhook for posting (simplest auth model); future versions
could switch to a Teams bot via Graph. Meeting correlation uses the
Graph calendar API to match Outlook events to vault notes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from src.config import TeamsConfig
from src.http_pool import SharedHttpPool
from src.integrations.microsoft.graph import GraphClient
from src.obsidian.vault import ObsidianVault
from src.sync.state import SyncState

logger = logging.getLogger(__name__)


@dataclass
class DigestData:
    """Data assembled for a digest post."""
    period: str                    # "daily" | "weekly"
    notes_count: int
    action_items: list[dict]
    top_tags: list[str]
    cost_usd: float
    date_range: str


def build_digest(
    state: SyncState,
    vault: ObsidianVault,
    period: str = "weekly",
) -> DigestData:
    """Collect data for a digest post from the state DB and vault."""
    now = datetime.now(UTC)
    days = 1 if period == "daily" else 7
    start = now - timedelta(days=days)
    cutoff = start.isoformat()

    # Notes synced in the window
    rows = state.conn.execute(
        "SELECT doc_name, action_count FROM sync_state "
        "WHERE last_synced_at >= ? AND status = 'synced'",
        (cutoff,),
    ).fetchall()
    notes_count = len(rows)

    # Aggregate tags from vault
    tag_counts: dict[str, int] = {}
    for note_path in vault.list_notes_by_source("remarkable"):
        result = vault.read_note(note_path)
        if result is None:
            continue
        fm, _ = result
        synced = fm.get("last_synced", "")
        if synced and synced >= cutoff:
            for tag in fm.get("tags") or []:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

    top_tags = sorted(tag_counts.items(), key=lambda kv: -kv[1])[:5]

    # Collect open action items
    actions_dir = vault.path / "Actions"
    action_items: list[dict] = []
    if actions_dir.exists():
        for action_file in actions_dir.glob("*-actions.md"):
            source = action_file.stem.replace("-actions", "")
            for line in action_file.read_text(encoding="utf-8").split("\n"):
                stripped = line.strip()
                if stripped.startswith("- [ ]"):
                    action_items.append({
                        "source": source,
                        "text": stripped[6:].strip(),
                    })

    # API cost in window
    usage = state.get_api_usage_summary(days=days)

    date_range = f"{start.date().isoformat()} → {now.date().isoformat()}"

    return DigestData(
        period=period,
        notes_count=notes_count,
        action_items=action_items[:10],
        top_tags=[tag for tag, _ in top_tags],
        cost_usd=usage["total_cost_usd"],
        date_range=date_range,
    )


def render_adaptive_card(digest: DigestData, title_prefix: str = "reMark") -> dict:
    """Render a Teams Adaptive Card payload for the given digest."""
    facts = [
        {"title": "Period", "value": f"{digest.period.title()} ({digest.date_range})"},
        {"title": "Synced notes", "value": str(digest.notes_count)},
        {"title": "Open actions", "value": str(len(digest.action_items))},
        {"title": "API cost", "value": f"${digest.cost_usd:.2f}"},
    ]
    if digest.top_tags:
        facts.append({"title": "Top tags", "value": ", ".join(digest.top_tags)})

    action_blocks = []
    for item in digest.action_items[:5]:
        action_blocks.append({
            "type": "TextBlock",
            "text": f"• {item['text']}  _({item['source']})_",
            "wrap": True,
            "spacing": "None",
        })

    body = [
        {
            "type": "TextBlock",
            "text": f"{title_prefix} — {digest.period.title()} digest",
            "weight": "Bolder",
            "size": "Large",
        },
        {
            "type": "FactSet",
            "facts": facts,
        },
    ]
    if action_blocks:
        body.append({
            "type": "TextBlock",
            "text": "Open action items",
            "weight": "Bolder",
            "spacing": "Medium",
        })
        body.extend(action_blocks)

    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": body,
            },
        }],
    }
    return card


async def send_card(
    webhook_url: str,
    card: dict,
    *,
    http_pool: SharedHttpPool | None = None,
) -> bool:
    """POST an Adaptive Card payload to a Teams webhook URL.

    When the caller provides ``http_pool`` the request reuses an existing
    keep-alive connection instead of paying the TLS handshake on every
    dispatch. The fallback (no pool) keeps backward compatibility for
    callers that aren't pool-aware yet.
    """
    try:
        if http_pool is not None:
            client = await http_pool.client()
            resp = await client.post(webhook_url, json=card)
        else:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    webhook_url,
                    content=json.dumps(card),
                    headers={"Content-Type": "application/json"},
                )
        resp.raise_for_status()
        return resp.status_code < 300
    except httpx.HTTPStatusError as e:
        logger.warning(
            "Teams webhook returned %d: %s",
            e.response.status_code, e.response.text[:200],
        )
        return False
    except httpx.TransportError as e:
        logger.warning("Teams webhook failed: %s", e)
        return False


async def post_digest(
    config: TeamsConfig,
    digest: DigestData,
    *,
    http_pool: SharedHttpPool | None = None,
) -> bool:
    """Post a digest card to the configured Teams webhook."""
    if not config.enabled or not config.webhook_url:
        return False

    card = render_adaptive_card(digest)

    try:
        if http_pool is not None:
            client = await http_pool.client()
            resp = await client.post(
                config.webhook_url,
                content=json.dumps(card),
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code >= 400:
                logger.warning(
                    "Teams webhook returned %d: %s",
                    resp.status_code, resp.text[:200],
                )
                return False
            return True
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                resp = await client.post(
                    config.webhook_url,
                    content=json.dumps(card),
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code >= 400:
                    logger.warning(
                        "Teams webhook returned %d: %s",
                        resp.status_code, resp.text[:200],
                    )
                    return False
                return True
            except httpx.TransportError as e:
                logger.warning("Teams webhook failed: %s", e)
                return False
    except httpx.TransportError as e:
        logger.warning("Teams webhook failed: %s", e)
        return False


@dataclass
class MeetingMatch:
    """A correlation between an Outlook meeting and a vault note."""
    subject: str
    start: str
    note_path: str
    note_title: str


async def correlate_meetings(
    graph: GraphClient,
    vault: ObsidianVault,
    days_ahead: int = 7,
    days_behind: int = 7,
) -> list[MeetingMatch]:
    """Fetch upcoming/past Outlook meetings and match them to vault notes.

    A match is found if a note's title substring (case-insensitive) appears
    in the event subject or vice versa.
    """
    start = (datetime.now(UTC) - timedelta(days=days_behind)).isoformat()
    end = (datetime.now(UTC) + timedelta(days=days_ahead)).isoformat()

    params = {
        "startDateTime": start,
        "endDateTime": end,
    }
    try:
        data = await graph.get("/me/calendarView", params=params)
    except Exception as e:
        logger.warning("Could not fetch meetings: %s", e)
        return []

    events = data.get("value", [])
    note_index: dict[str, Path] = {}
    for note_path in vault.list_notes_by_source("remarkable"):
        result = vault.read_note(note_path)
        if result is None:
            continue
        fm, _ = result
        title = fm.get("title", note_path.stem).lower()
        if title:
            note_index[title] = note_path

    matches: list[MeetingMatch] = []
    for event in events:
        subject = event.get("subject", "")
        start_dt = (event.get("start") or {}).get("dateTime", "")
        if not subject:
            continue
        lowered = subject.lower()
        for title, note_path in note_index.items():
            if title in lowered or lowered in title:
                matches.append(MeetingMatch(
                    subject=subject,
                    start=start_dt,
                    note_path=str(note_path),
                    note_title=title,
                ))
                break

    return matches
