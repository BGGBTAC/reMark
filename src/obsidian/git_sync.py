"""Git sync for the Obsidian vault.

Handles committing and pushing vault changes after sync cycles.
Uses GitPython for programmatic git operations.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from git import InvalidGitRepositoryError, Repo
from git.exc import GitCommandError

logger = logging.getLogger(__name__)


class GitSync:
    """Manages git operations on the Obsidian vault."""

    def __init__(
        self,
        vault_path: Path | str,
        remote: str = "origin",
        branch: str = "main",
        commit_template: str = "sync: {count} notes from reMarkable ({date})",
    ):
        self._vault_path = Path(vault_path).expanduser().resolve()
        self._remote = remote
        self._branch = branch
        self._commit_template = commit_template
        self._repo: Repo | None = None

    @property
    def repo(self) -> Repo:
        if self._repo is None:
            try:
                self._repo = Repo(self._vault_path)
            except InvalidGitRepositoryError:
                raise GitSyncError(
                    f"Not a git repository: {self._vault_path}. "
                    "Initialize with `git init` or set git.enabled: false in config."
                )
        return self._repo

    def is_git_repo(self) -> bool:
        """Check if the vault is a git repository."""
        try:
            _ = self.repo
            return True
        except GitSyncError:
            return False

    def has_changes(self) -> bool:
        """Check if there are uncommitted changes."""
        return self.repo.is_dirty(untracked_files=True)

    def commit(self, note_count: int, message: str | None = None) -> str | None:
        """Stage all changes and commit.

        Returns the commit hash, or None if nothing to commit.
        """
        if not self.has_changes():
            logger.debug("No changes to commit")
            return None

        # Stage everything
        self.repo.git.add(A=True)

        if not message:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            message = self._commit_template.format(count=note_count, date=date_str)

        self.repo.index.commit(message)
        commit_hash = self.repo.head.commit.hexsha[:8]
        logger.info("Committed: %s (%s)", message, commit_hash)
        return commit_hash

    def push(self) -> bool:
        """Push to the configured remote.

        Returns True on success, False on failure.
        """
        try:
            remote = self.repo.remote(self._remote)
            remote.push(self._branch)
            logger.info("Pushed to %s/%s", self._remote, self._branch)
            return True
        except GitCommandError as e:
            logger.warning("Push failed: %s", e)
            return False
        except ValueError:
            logger.warning("Remote '%s' not found", self._remote)
            return False

    def commit_and_push(self, note_count: int) -> str | None:
        """Stage, commit, and push in one operation.

        Returns commit hash or None.
        """
        commit_hash = self.commit(note_count)
        if commit_hash:
            self.push()
        return commit_hash

    def status(self) -> dict:
        """Get current git status."""
        repo = self.repo
        return {
            "branch": repo.active_branch.name,
            "dirty": repo.is_dirty(untracked_files=True),
            "untracked": len(repo.untracked_files),
            "ahead": _commits_ahead(repo, self._remote, self._branch),
            "last_commit": repo.head.commit.hexsha[:8] if repo.head.is_valid() else None,
            "last_commit_msg": repo.head.commit.message.strip() if repo.head.is_valid() else None,
        }


class GitSyncError(Exception):
    """Raised when git operations fail."""


def _commits_ahead(repo: Repo, remote: str, branch: str) -> int:
    """Count commits ahead of the remote tracking branch."""
    try:
        tracking = f"{remote}/{branch}"
        return sum(1 for _ in repo.iter_commits(f"{tracking}..HEAD"))
    except (GitCommandError, ValueError):
        return 0
