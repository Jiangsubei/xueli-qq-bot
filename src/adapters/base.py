from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from src.core.models import MessageEvent
from src.core.platform_models import InboundEvent
from src.core.platform_models import OutgoingAction


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
