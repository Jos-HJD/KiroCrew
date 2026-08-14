"""Durable record of the resume binding a Discord conversation expects.

An inbound resume binding lives on the BOUND SESSION's session-map entry, so
whatever destroys that entry — an overflow recycle, a restart prune, a dashboard
mirror unlink — also destroys the only evidence the conversation was ever
attached. The inbound resolver then finds no owner, the dispatcher falls back to
the DM's own session, and the user is told nothing: the silent misrouting an
inbound binding exists to prevent.

The record is therefore keyed by CHANNEL and kept OUTSIDE the session map, since
it has to outlive the entry whose loss it exists to detect, and it is reloaded
from disk so a restart can still name what was lost.

The store is advisory: losing it costs one notice, never a turn. The binding
itself is the session map's, and nothing here decides where a message runs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

_FILENAME = "discord_resume_expectations.json"


@dataclass(frozen=True)
class ResumeExpectation:
    """The session a Discord conversation is attached to, and its display title."""

    key: str
    title: str


def _read(path: Path) -> dict[str, ResumeExpectation]:
    """Load the store, treating any unreadable or malformed file as empty."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    records: dict[str, ResumeExpectation] = {}
    for channel_id, value in raw.items():
        if not isinstance(value, dict):
            continue
        key = str(value.get("key") or "")
        if key:
            records[str(channel_id)] = ResumeExpectation(key, str(value.get("title") or ""))
    return records


class ResumeExpectations:
    """Channel id → :class:`ResumeExpectation`, persisted as one small JSON file.

    Read on every inbound Discord message, so the file is loaded once and served
    from memory; writes are whole-file and atomic and happen only when a
    conversation attaches, rebinds or detaches — once per user action, never per
    turn. Owner-only permissions because a session title is conversation text.

    Single-writer by construction: one gateway owns a Discord connection, and a
    pod resolves a different data home, which the per-call path check below
    picks up rather than answering from the previous home's file.
    """

    def __init__(self) -> None:
        self._loaded_from: Path | None = None
        self._records: dict[str, ResumeExpectation] = {}

    def _path(self) -> Path:
        return config_dir() / _FILENAME

    def _store(self) -> dict[str, ResumeExpectation]:
        path = self._path()
        if path != self._loaded_from:
            self._records = _read(path)
            self._loaded_from = path
        return self._records

    def get(self, channel_id: str) -> ResumeExpectation | None:
        return self._store().get(channel_id)

    def record(self, channel_id: str, key: str, title: str) -> None:
        """Remember the binding *channel_id* holds now, replacing any earlier one."""
        self._store()[channel_id] = ResumeExpectation(key, title)
        self._flush()

    def clear(self, channel_id: str) -> bool:
        """Forget *channel_id*'s expectation; True when there was one to forget."""
        if self._store().pop(channel_id, None) is None:
            return False
        self._flush()
        return True

    def _flush(self) -> None:
        payload = {
            channel_id: {"key": record.key, "title": record.title}
            for channel_id, record in self._records.items()
        }
        try:
            atomic_write(self._path(), json.dumps(payload, indent=2), mode=0o600)
        except OSError:
            # In-memory state stays authoritative for this process, so the notice
            # still fires until restart. Never fail a resume over a lost write.
            logger.warning("discord: could not persist resume expectations", exc_info=True)
