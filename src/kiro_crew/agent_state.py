"""Sidecar store for KiroCrew's per-agent bookkeeping.

kiro-cli validates ``~/.kiro/agents/*.json`` with serde ``deny_unknown_fields``
and rejects the *entire* spec on any unknown key, then silently falls back to
the default agent (``--agent <name>`` resolves to default with only a stderr
"no agent with name X found" line). KiroCrew therefore keeps its private
per-agent bookkeeping OUT of the kiro spec and in this sidecar, so every spec
stays schema-valid for kiro-cli.

Two values are tracked, both kept in this sidecar rather than the kiro spec:

- ``model_managed`` (bool): whether an agent's ``model`` should track the
  shipped ``defaults.json`` (so a default bump propagates) or is an explicit
  user pick frozen against future bumps.
- ``cc_model`` (str): a per-agent model for the ``claude_code`` provider (that
  backend can't pick a per-agent model from ``--agent`` the way kiro-cli does).

A third, per-managed-server, exists for one narrow reason:

- ``parked_mcp_fields`` (dict): the USER-OWNED fields of a managed MCP server
  whose ``spec_gate`` is currently closed. Withholding such a server means
  removing its ``mcpServers`` entry, and the entry can carry customizations a
  refresh deliberately PRESERVES rather than rewrites (``autoApprove``, extra
  ``env`` keys). Deleting them would make an off/on cycle silently reset the
  user's configuration, so they are parked here and restored when the gate
  reopens. They cannot be parked in the spec itself: ``deny_unknown_fields``
  rejects the whole file on any key kiro-cli does not know.

State file (``~/.kiro/crew/agent_model_state.json``, honoring ``KIROCREW_HOME``)::

    {
      "kirocrew":           {"model_managed": true},
      "kirocrew-heartbeat": {"cc_model": "claude-sonnet-4.6"}
    }

This is a near-leaf module: it imports only the stdlib plus the leaf
``config.paths`` and ``atomic_write`` helpers, so it never participates in the
``agent`` <-> ``config.loader`` import cycle.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

_STATE_FILENAME = "agent_model_state.json"
_MODEL_MANAGED = "model_managed"
_CC_MODEL = "cc_model"
_PARKED_MCP = "parked_mcp_fields"

# Guards in-process read-modify-write races (e.g. dashboard PATCH vs gateway
# refresh). Cross-process atomicity is provided by ``atomic_write``.
_lock = threading.RLock()


def _state_path() -> Path:
    """Return the sidecar path (resolved fresh so KIROCREW_HOME is honored)."""
    return config_dir() / _STATE_FILENAME


def _read() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    atomic_write(_state_path(), json.dumps(data, indent=2, sort_keys=True) + "\n")


def _entry(data: dict, name: str) -> dict:
    entry = data.get(name)
    return entry if isinstance(entry, dict) else {}


def get_model_managed(name: str) -> bool | None:
    """Return the agent's managed flag, or ``None`` when unset (grandfathered)."""
    with _lock:
        value = _entry(_read(), name).get(_MODEL_MANAGED)
    return bool(value) if isinstance(value, bool) else None


def set_model_managed(name: str, value: bool) -> None:
    with _lock:
        data = _read()
        entry = data.get(name)
        if not isinstance(entry, dict):
            entry = {}
        entry[_MODEL_MANAGED] = bool(value)
        data[name] = entry
        _write(data)


def get_cc_model(name: str) -> str | None:
    """Return the agent's claude_code-provider model, or ``None`` when unset."""
    with _lock:
        value = _entry(_read(), name).get(_CC_MODEL)
    return value if isinstance(value, str) and value else None


def set_cc_model(name: str, value: str | None) -> None:
    """Set (or clear, when ``value`` is falsy) the agent's claude_code model."""
    with _lock:
        data = _read()
        entry = data.get(name)
        if not isinstance(entry, dict):
            entry = {}
        if value:
            entry[_CC_MODEL] = str(value)
        else:
            entry.pop(_CC_MODEL, None)
        if entry:
            data[name] = entry
        else:
            data.pop(name, None)
        _write(data)


def prune(name: str) -> None:
    """Drop an agent's entry entirely (call when the agent is deleted)."""
    with _lock:
        data = _read()
        if name in data:
            data.pop(name, None)
            _write(data)


def get_parked_mcp_fields(name: str, server: str) -> dict:
    """Return the parked user-owned fields for *server*, or ``{}``.

    ``{}`` covers "nothing parked" and "the sidecar is unreadable" alike: the
    restore is additive, so an unavailable park costs the user their
    customization on this one cycle and never corrupts the emitted spec.
    """
    with _lock:
        parked = _entry(_read(), name).get(_PARKED_MCP)
    if not isinstance(parked, dict):
        return {}
    fields = parked.get(server)
    return dict(fields) if isinstance(fields, dict) else {}


def set_parked_mcp_fields(name: str, server: str, fields: dict | None) -> None:
    """Park (or clear, when *fields* is empty) a server's user-owned fields.

    Clearing prunes the empty containers so the sidecar does not accumulate a
    row per server that was merely gated once.
    """
    with _lock:
        data = _read()
        entry = data.get(name)
        if not isinstance(entry, dict):
            entry = {}
        parked = entry.get(_PARKED_MCP)
        if not isinstance(parked, dict):
            parked = {}
        if fields:
            parked[server] = dict(fields)
        else:
            parked.pop(server, None)
        if parked:
            entry[_PARKED_MCP] = parked
        else:
            entry.pop(_PARKED_MCP, None)
        if entry:
            data[name] = entry
        else:
            data.pop(name, None)
        _write(data)
