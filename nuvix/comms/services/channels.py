"""Delivery backends.

Each channel is a small class behind one method so that swapping the console
backend for SES, Twilio or FCM is a settings change. The sweep, the dedup and
the rate limiting all sit *above* this layer and are identical for every
channel -- which is the point: the hard part of financial communications is
deciding what to send, not transporting it.
"""

from __future__ import annotations

import logging
from typing import Protocol

from nuvix.comms.models import Channel, Message

logger = logging.getLogger(__name__)


class DeliveryError(Exception):
    """The provider rejected or failed to accept the message."""


class ChannelBackend(Protocol):
    name: str

    def send(self, message: Message) -> dict: ...


class ConsoleBackend:
    """Structured-log backend. The default outside production.

    Deliberately the default: it makes the whole pipeline exercisable in tests
    and in local development without credentials, a network call or the risk
    of actually messaging a real person from a dev box.
    """

    name = "console"

    def send(self, message: Message) -> dict:
        logger.info(
            "message_delivered",
            extra={
                "channel": message.channel,
                "user_id": str(message.user_id),
                "subject": message.subject,
                "dedup_key": message.dedup_key,
            },
        )
        return {"provider": "console", "accepted": True}


class NullBackend:
    """Accepts and discards. Used to disable a channel without deleting rules."""

    name = "null"

    def send(self, message: Message) -> dict:
        return {"provider": "null", "accepted": True}


_BACKENDS: dict[str, ChannelBackend] = {
    Channel.PUSH: ConsoleBackend(),
    Channel.EMAIL: ConsoleBackend(),
    Channel.SMS: ConsoleBackend(),
    Channel.IN_APP: ConsoleBackend(),
}


def backend_for(channel: str) -> ChannelBackend:
    return _BACKENDS.get(channel, ConsoleBackend())


def register_backend(channel: str, backend: ChannelBackend) -> None:
    """Swap a backend at runtime -- used by tests and by deployment wiring."""

    _BACKENDS[channel] = backend
