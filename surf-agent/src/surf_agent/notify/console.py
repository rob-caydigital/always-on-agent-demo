from __future__ import annotations

from .base import NotifyResult


class DryRunNotifier:
    """--dry-run: decide everything, wake nobody, record nothing as woken."""

    def wake(self, message: str) -> NotifyResult:
        return NotifyResult(mode="dry_run", woke=False, detail=f"dry run: would wake with: {message!r}")
