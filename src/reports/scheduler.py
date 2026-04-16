"""Cron-ish scheduler for reports.

Deliberately minimal — we don't need full cron semantics for reports:

* ``every <N>m|h|d`` — fire every N minutes / hours / days
* ``daily HH:MM``   — fire once per day at the given UTC time
* ``weekly <DAY> HH:MM`` — fire once per week; DAY is ``mon..sun``

That's enough to cover "daily standup summary", "weekly retro",
"every hour refresh". Richer cron lives in the system cron table or
systemd timers already; we only want an inline scheduler so
``remark-bridge serve-web`` can run reports unattended without
spawning a second long-running process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import UTC, datetime, timedelta

from src.config import AppConfig
from src.llm.factory import build_llm_client
from src.reports.runner import ReportRunner
from src.sync.state import SyncState

logger = logging.getLogger(__name__)

_EVERY_RE = re.compile(r"^every\s+(\d+)\s*(m|min|h|hour|d|day)s?$", re.I)
_DAILY_RE = re.compile(r"^daily\s+(\d{1,2}):(\d{2})$", re.I)
_WEEKLY_RE = re.compile(
    r"^weekly\s+(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+(\d{1,2}):(\d{2})$",
    re.I,
)
_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def next_run(schedule: str, reference: datetime) -> datetime:
    """Compute the next firing time strictly after ``reference``.

    Raises ``ValueError`` on malformed schedules so the UI can surface
    a clean error at save time.
    """
    s = schedule.strip().lower()
    ref = reference.astimezone(UTC)

    m = _EVERY_RE.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        if unit.startswith("m"):
            delta = timedelta(minutes=n)
        elif unit.startswith("h"):
            delta = timedelta(hours=n)
        else:
            delta = timedelta(days=n)
        return ref + delta

    m = _DAILY_RE.match(s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        candidate = ref.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= ref:
            candidate += timedelta(days=1)
        return candidate

    m = _WEEKLY_RE.match(s)
    if m:
        day = _WEEKDAYS.index(m.group(1).lower()[:3])
        hh, mm = int(m.group(2)), int(m.group(3))
        candidate = ref.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta_days = (day - candidate.weekday()) % 7
        candidate += timedelta(days=delta_days)
        if candidate <= ref:
            candidate += timedelta(days=7)
        return candidate

    raise ValueError(
        f"Unknown schedule '{schedule}'. "
        "Expected 'every 30m', 'daily 09:00', 'weekly mon 09:00'."
    )


class ReportScheduler:
    """Fires due reports on a configurable tick interval.

    Run it alongside the web app with::

        asyncio.create_task(scheduler.run())

    or from a standalone CLI entry point.
    """

    def __init__(
        self,
        config: AppConfig,
        state: SyncState,
        tick_seconds: int = 60,
    ):
        self._config = config
        self._state = state
        _llm = build_llm_client(
            config.llm,
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        )
        self._runner = ReportRunner(config, state, llm=_llm)
        self._tick = tick_seconds
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        logger.info("ReportScheduler started (tick=%ds)", self._tick)
        while not self._stopped.is_set():
            try:
                await self._fire_due()
            except Exception as exc:  # noqa: BLE001
                logger.warning("ReportScheduler tick failed: %s", exc)
            import contextlib
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=self._tick)

    async def _fire_due(self) -> None:
        reports = self._state.due_reports()
        if not reports:
            return
        now = datetime.now(UTC)
        for report in reports:
            name = report["name"]
            logger.info("Firing report '%s'", name)
            try:
                result = await self._runner.run(report)
                self._state.update_report(
                    int(report["id"]),
                    last_run_at=now.isoformat(),
                    next_run_at=next_run(report["schedule"], now).isoformat(),
                    last_status=("ok" if result.ok else "partial"),
                    last_error=(
                        "; ".join(f"{c}: {e}" for c, e in result.channels_failed)
                        if result.channels_failed else None
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Report '%s' crashed: %s", name, exc, exc_info=True)
                self._state.update_report(
                    int(report["id"]),
                    last_run_at=now.isoformat(),
                    next_run_at=next_run(report["schedule"], now).isoformat(),
                    last_status="error",
                    last_error=str(exc)[:500],
                )
