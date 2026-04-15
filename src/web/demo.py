"""Demo-mode seeder.

Populates a transient state DB and vault with realistic-looking content
so the web UI can render without ever talking to reMarkable Cloud. The
only consumer today is the CI screenshot workflow, but the module is
kept free of CI-specific assumptions so it's useful for local demos too.

Activate with ``REMARK_DEMO_MODE=1`` before importing the FastAPI app;
``create_app`` will seed once per fresh DB.
"""

from __future__ import annotations

import os
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.config import AppConfig
from src.sync.state import SyncState


def is_enabled() -> bool:
    return os.environ.get("REMARK_DEMO_MODE", "").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Fixture content — deliberately dull so the screenshots look like a
# real knowledge base, not a marketing mock.
# ---------------------------------------------------------------------------

_DEVICES = [
    ("pro",     "Paper Pro",       "rm-pro"),
    ("rm2",     "reMarkable 2",    "rm-2"),
    ("work",    "Office tablet",   "work"),
]

_NOTES = [
    ("meeting-2026-03-12.md",        "Q2 kickoff standup",
     "Meetings",    ["project/remark-bridge", "meeting/standup"], 4),
    ("architecture-review.md",        "Architecture review — v0.7",
     "Meetings",    ["project/remark-bridge", "meeting/review"], 2),
    ("reading-deep-work.md",          "Deep Work — ch. 3 notes",
     "Reading",     ["reading/books", "topic/focus"],            0),
    ("idea-math-ocr.md",              "Idea — math OCR plugin",
     "Ideas",       ["project/remark-bridge", "topic/ocr"],      3),
    ("journal-2026-03-15.md",         "Journal — Mar 15",
     "Journal",     ["journal/daily"],                            0),
    ("onboarding-new-teammate.md",    "Onboarding plan — Lena",
     "Projects",    ["project/team", "meeting/1-on-1"],          5),
    ("vps-migration.md",              "VPS migration checklist",
     "Projects",    ["project/ops", "topic/infra"],              7),
    ("retro-q1.md",                   "Q1 retrospective",
     "Meetings",    ["meeting/retro"],                            2),
    ("interview-candidate-a.md",      "Interview — Candidate A",
     "Projects",    ["project/hiring", "meeting/1-on-1"],        1),
    ("template-push-draft.md",        "Draft — weekly-review template",
     "Templates",   ["topic/templates"],                          0),
    ("reading-pragmatic.md",          "Pragmatic Programmer — notes",
     "Reading",     ["reading/books", "topic/craft"],             0),
    ("sync-edge-cases.md",            "Edge cases worth regression tests",
     "Projects",    ["project/remark-bridge", "topic/testing"],  6),
    ("personal-goals-2026.md",        "2026 personal goals",
     "Journal",     ["journal/goals"],                            3),
    ("api-design-principles.md",     "API design principles — my shortlist",
     "Reference",   ["reference/api"],                            0),
    ("meeting-customer-acme.md",      "ACME customer sync",
     "Meetings",    ["project/customers", "meeting/external"],   4),
]


def seed(config: AppConfig) -> None:
    """Populate the state DB + vault for demo_mode.

    Idempotent — skips all writes once `demo_seeded` is present in
    ``plugin_state`` (we piggyback on an existing table rather than
    adding a new one just for the flag).
    """
    state_db = Path(config.sync.state_db).expanduser()
    state_db.parent.mkdir(parents=True, exist_ok=True)

    state = SyncState(state_db)
    try:
        already = state.conn.execute(
            "SELECT 1 FROM plugin_state WHERE name = ?",
            ("demo_seeded",),
        ).fetchone()
        if already:
            return

        vault = Path(config.obsidian.vault_path).expanduser()
        vault.mkdir(parents=True, exist_ok=True)

        _seed_devices(state)
        _seed_notes(state, vault)
        _seed_queue(state)
        _seed_api_usage(state)
        _seed_bridge_token(state)

        state.conn.execute(
            """INSERT INTO plugin_state
                 (name, enabled, config, installed_at, last_used_at)
               VALUES (?, 1, '', ?, ?)""",
            ("demo_seeded", datetime.now(UTC).isoformat(),
             datetime.now(UTC).isoformat()),
        )
        state.conn.commit()
    finally:
        state.close()


def _seed_devices(state: SyncState) -> None:
    for idx, (slug, label, subfolder) in enumerate(_DEVICES):
        state.register_device(slug, label, f"/tmp/demo/tok-{slug}", subfolder)
        # Touch a few in the past so last_sync_at looks lived-in
        when = datetime.now(UTC) - timedelta(minutes=15 + idx * 30)
        state.conn.execute(
            "UPDATE devices SET last_sync_at = ? WHERE id = ?",
            (when.isoformat(), slug),
        )
    state.conn.commit()


def _seed_notes(state: SyncState, vault: Path) -> None:
    rng = random.Random(0xC0FFEE)  # deterministic output
    for i, (fname, title, folder, tags, actions) in enumerate(_NOTES):
        device = _DEVICES[i % len(_DEVICES)][0]
        subfolder = _DEVICES[i % len(_DEVICES)][2]
        folder_path = vault / subfolder / folder
        folder_path.mkdir(parents=True, exist_ok=True)
        note_path = folder_path / fname
        tags_yaml = "\n".join(f"  - {t}" for t in tags)
        body = _demo_body(title, tags, actions, rng)
        note_path.write_text(
            f"---\ntitle: {title}\nsource: remarkable\ntags:\n{tags_yaml}\n"
            f"action_items: {actions}\nsummary: {title} — demo note\n---\n\n{body}\n",
            encoding="utf-8",
        )
        synced_at = (datetime.now(UTC) - timedelta(hours=i * 3)).isoformat()
        state.mark_synced(
            doc_id=f"demo-doc-{i:02d}",
            doc_name=title,
            parent_folder=folder,
            cloud_hash=f"hash-{i:02d}",
            vault_path=str(note_path),
            ocr_engine="remarkable_builtin",
            page_count=rng.randint(1, 6),
            action_count=actions,
            device_id=device,
        )
        # Back-date the row so ORDER BY last_synced_at produces variety
        state.conn.execute(
            "UPDATE sync_state SET last_synced_at = ? WHERE doc_id = ?",
            (synced_at, f"demo-doc-{i:02d}"),
        )
    state.conn.commit()


def _seed_queue(state: SyncState) -> None:
    state.enqueue("process_document", doc_id="demo-doc-14", payload="hash-14")
    state.enqueue("process_document", doc_id="demo-doc-05", payload="hash-05")
    qid = state.enqueue("process_document", doc_id="demo-doc-03", payload="hash-03")
    # Push it past the failure cap so the dashboard widget shows ⚠
    state.conn.execute(
        """UPDATE sync_queue
           SET status = 'failed', attempts = 5, max_attempts = 5,
               last_error = 'Cloud API returned HTTP 503'
           WHERE id = ?""",
        (qid,),
    )
    state.conn.commit()


def _seed_api_usage(state: SyncState) -> None:
    providers = [
        ("anthropic", "claude-sonnet-4-20250514", "structure", 18500, 1200, 0.21),
        ("anthropic", "claude-sonnet-4-20250514", "tagger",     4800,  350, 0.06),
        ("anthropic", "claude-sonnet-4-20250514", "summary",    6200,  800, 0.08),
        ("voyage",    "voyage-3.5",               "embed",     12400,    0, 0.02),
    ]
    for provider, model, op, it, ot, cost in providers:
        state.log_api_usage(
            provider=provider, model=model, operation=op,
            input_tokens=it, output_tokens=ot, cost_usd=cost,
        )


def _seed_bridge_token(state: SyncState) -> None:
    state.issue_bridge_token("obsidian-demo")


def _demo_body(title: str, tags: list[str], actions: int, rng: random.Random) -> str:
    lines = [f"# {title}", "", f"_Demo note seeded for the docs screenshots._", ""]
    for _ in range(rng.randint(2, 4)):
        lines.extend([rng.choice(_PARAGRAPHS), ""])
    if actions:
        lines.append("## Action items")
        lines.append("")
        for i in range(actions):
            lines.append(f"- [ ] {rng.choice(_ACTIONS)}")
        lines.append("")
    return "\n".join(lines)


_PARAGRAPHS = [
    "Captured on the reMarkable during the morning walk. The OCR passed "
    "cleanly on the first try — no VLM fallback needed.",
    "We settled on the split-systemd layout for the VPS rollout. Keeping "
    "the web unit always-on and letting the timer drive sync simplifies "
    "log correlation.",
    "Two themes keep showing up when I reread the last month of notes: "
    "response time is the feature, and documentation drift is a "
    "correctness bug.",
    "The main tradeoff with hierarchical tags is migrating older notes. "
    "The retag CLI handles the backfill but it is not instant, so stage "
    "it overnight.",
]

_ACTIONS = [
    "Draft a proposal for the v0.7 scope",
    "Sketch the schema for the planned reports dashboard",
    "Ask the ops team about the staging-vs-prod secrets split",
    "Prune the stale Obsidian plugins before the next sync",
    "Block an hour Friday for the retrospective write-up",
    "Share the onboarding doc with the team",
    "Close out the offline-queue bug report",
]


__all__ = ["is_enabled", "seed"]
