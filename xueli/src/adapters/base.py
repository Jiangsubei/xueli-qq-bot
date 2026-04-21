from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from src.core.models import MessageEvent
from src.core.platform_models import InboundEvent
from src.core.platform_models import OutgoingAction

# Standard AT mention pattern used by OneBot-compatible CQ codes.
_ONEBOT_AT_PATTERN = re.compile(r"\[CQ:at,qq=\d+\]")


class ProtocolAdapter(ABC):
    """Interface for platform-specific message normalization operations.

    Implement this to move CQ-code parsing, AT stripping, repeat-echo detection,
    and other protocol-specific logic out of the platform-agnostic message handler.
    """

    @abstractmethod
    def strip_mentions(self, text: str) -> str:
        """Remove @mention tokens from message text for a clean user-facing string."""
        ...

    @abstractmethod
    def extract_mentions(self, event: MessageEvent) -> List[str]:
        """Return the list of user IDs mentioned in the message."""
        ...

    def check_repeat_echo(self, event: MessageEvent, text: str) -> Optional[str]:
        """Check if this message triggers repeat-echo. Returns the echoed text or None."""
        del event, text
        return None


class PlatformAdapter(ABC):
    platform: str = "unknown"
    adapter_name: str = "unknown"

    @abstractmethod
    async def run(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def send(self, data: Dict[str, Any]) -> bool: ...

    @abstractmethod
    async def send_action(self, action: OutgoingAction) -> bool: ...

    @abstractmethod
    def is_ready(self) -> bool: ...

    def attach_inbound_event(self, event: MessageEvent) -> Optional[InboundEvent]:
        del event
        return None

    def normalize_inbound_payload(self, payload: Dict[str, Any]) -> Optional[InboundEvent]:
        del payload
        return None

    def as_protocol_adapter(self) -> Optional[ProtocolAdapter]:
        """Return a ProtocolAdapter view of this adapter, or None if not supported."""
        return None
