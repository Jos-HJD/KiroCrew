"""The channel notice for a removed inbound session-resume binding.

``SessionMap`` audits every removal itself but cannot reach the conversation —
that needs a transport, which the gateway owns. These pin the other half: the
listener ``DashboardState`` registers, which reason it stays quiet for, and the
delivery it performs through the governed cross-surface ladder.

All against fakes — no real transport, session manager, or network.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.messaging.link import ChannelLink

LINK = ChannelLink(channel_type="discord", channel_id="chan-1")
KEY = "discord:kirocrew:direct:42"


class _Transport:
    def __init__(self, fail: bool = False, proactive: bool = True) -> None:
        self.sent: list[tuple[str, str, str | None]] = []
        self.fail = fail
        self.capabilities = SimpleNamespace(
            supports_proactive_send=proactive, max_message_chars=2000
        )

    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        if self.fail:
            raise RuntimeError("discord down")
        self.sent.append((conversation_id, content, thread_id))
        return "mid-1"


@pytest.fixture()
def state(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


@contextmanager
def _permit_governance():
    """Permit the ladder's governance gate, which it imports at call time."""
    with patch(
        "kiro_crew.platform.governance_profiles.vet_and_audit",
        return_value=SimpleNamespace(permitted=True),
    ):
        yield


def _listener(state: DashboardState):
    """Install the listener and return the closure handed to the session map."""
    state.wire_session_unbind_listener()
    state.sessions.set_unbind_listener.assert_called_once()
    return state.sessions.set_unbind_listener.call_args[0][0]


class TestWiring:
    def test_wire_installs_the_listener(self, state: DashboardState) -> None:
        assert callable(_listener(state))

    @pytest.mark.asyncio
    async def test_a_removal_schedules_the_notice(self, state: DashboardState) -> None:
        listener = _listener(state)
        with patch.object(state, "_notify_inbound_unbind", AsyncMock()) as notify:
            listener(KEY, LINK, "dashboard_unlink")
            await asyncio.sleep(0)

        notify.assert_awaited_once_with(KEY, LINK, "dashboard_unlink")

    @pytest.mark.asyncio
    async def test_user_unlink_is_not_announced(self, state: DashboardState) -> None:
        """The in-channel command already replied there, so a notice would echo it."""
        listener = _listener(state)
        with patch.object(state, "_notify_inbound_unbind", AsyncMock()) as notify:
            listener(KEY, LINK, "user_unlink")
            await asyncio.sleep(0)

        notify.assert_not_awaited()

    def test_no_running_loop_is_a_silent_skip(self, state: DashboardState) -> None:
        """A synchronous caller (a worker thread, the CLI) has nothing to send on."""
        listener = _listener(state)
        with patch.object(state, "_notify_inbound_unbind", AsyncMock()) as notify:
            listener(KEY, LINK, "entry_deleted")

        notify.assert_not_awaited()


class TestDelivery:
    @pytest.mark.asyncio
    async def test_notice_reaches_the_bound_conversation(self, state: DashboardState) -> None:
        transport = _Transport()
        state.channel_transports["discord"] = transport

        with _permit_governance():
            await state._notify_inbound_unbind(KEY, LINK, "dashboard_unlink")

        assert len(transport.sent) == 1
        conversation_id, text, _thread = transport.sent[0]
        assert conversation_id == "chan-1"
        assert "detached" in text
        assert "dashboard_unlink" in text
        assert "!sessions" in text

    @pytest.mark.asyncio
    async def test_notice_names_the_session_by_key_without_a_tab(
        self, state: DashboardState
    ) -> None:
        """No slot displays the session, so the key is what identifies it."""
        transport = _Transport()
        state.channel_transports["discord"] = transport

        with _permit_governance():
            await state._notify_inbound_unbind(KEY, LINK, "session_destroyed")

        assert KEY in transport.sent[0][1]

    @pytest.mark.asyncio
    async def test_unregistered_transport_is_a_noop(self, state: DashboardState) -> None:
        with _permit_governance():
            await state._notify_inbound_unbind(KEY, LINK, "entry_deleted")

    @pytest.mark.asyncio
    async def test_governance_denial_sends_nothing(self, state: DashboardState) -> None:
        transport = _Transport()
        state.channel_transports["discord"] = transport

        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=SimpleNamespace(permitted=False),
        ):
            await state._notify_inbound_unbind(KEY, LINK, "entry_deleted")

        assert transport.sent == []

    @pytest.mark.asyncio
    async def test_a_channel_that_cannot_be_pushed_to_is_skipped(
        self, state: DashboardState
    ) -> None:
        state.channel_transports["discord"] = _Transport(proactive=False)

        with _permit_governance():
            await state._notify_inbound_unbind(KEY, LINK, "entry_deleted")

    @pytest.mark.asyncio
    async def test_delivery_failure_is_swallowed(self, state: DashboardState) -> None:
        """The binding is already gone and audited; a failed notice adds nothing."""
        state.channel_transports["discord"] = _Transport(fail=True)

        with _permit_governance():
            await state._notify_inbound_unbind(KEY, LINK, "entry_deleted")
