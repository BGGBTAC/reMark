"""Scheduled reports — periodic Claude-powered summaries.

A report is a named job with a cron schedule, a prompt, and a list of
output channels (``teams``, ``notion``, ``vault``). At each firing the
runner gathers recent vault content + sync stats, asks Claude to
generate a structured summary against the prompt, and pushes the
result to every configured channel.

Persistence lives in :class:`src.sync.state.SyncState`'s ``reports``
table; runtime orchestration in :class:`ReportRunner` here.
"""

from src.reports.runner import ReportRunner, run_report
from src.reports.scheduler import ReportScheduler

__all__ = ["ReportRunner", "ReportScheduler", "run_report"]
