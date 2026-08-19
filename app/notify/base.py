"""Notification provider abstraction.

Adding a channel means subclassing :class:`NotificationProvider` and
registering it. The digest builder and the notification rules are entirely
provider-agnostic.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class NotificationMessage:
    """A rendered notification, in both plain and rich form.

    Providers pick whichever representation they support; ``text`` is always
    populated so a new provider needs nothing extra to work.
    """

    text: str
    subject: str | None = None
    #: Optional Markdown/HTML variant for providers that render formatting.
    rich_text: str | None = None
    #: Optional standalone HTML document, for providers that render a full
    #: page rather than a chat message (email). ``text`` remains the fallback.
    html: str | None = None
    #: (label, url) pairs for providers that support link buttons.
    links: list[tuple[str, str]] = field(default_factory=list)
    job_ids: list[int] = field(default_factory=list)


@dataclass
class SendResult:
    ok: bool
    provider: str
    error: str | None = None
    detail: str | None = None


class NotificationProvider(abc.ABC):
    """Base class for all notification channels."""

    name: str = "unnamed"
    display_name: str = "Unnamed provider"
    #: Config keys required before this provider can send.
    required_config: tuple[str, ...] = ()

    def is_configured(self) -> bool:
        return True

    @abc.abstractmethod
    async def send(self, message: NotificationMessage) -> SendResult:
        """Deliver the message. Must not raise."""
