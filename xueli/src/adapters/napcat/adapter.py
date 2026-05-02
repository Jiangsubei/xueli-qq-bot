from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from src.adapters.base import PlatformAdapter, ProtocolAdapter, _ONEBOT_AT_PATTERN
from src.adapters.napcat.connection import NapCatConnection
from src.adapters.napcat.normalizer import attach_normalized_onebot_event
from src.core.models import MessageEvent, MessageSegment
from src.core.platform_models import FaceAction, InboundEvent, MfaceAction, OutgoingAction, ReplyAction, SessionRef


class NapCatProtocolAdapter(ProtocolAdapter):
    """Protocol adapter for QQ (OneBot-compatible) protocol."""

    # CQ code AT pattern: [CQ:at,qq=123456]
    _at_pattern = re.compile(r"\[CQ:at,qq=\d+\]")

    def strip_mentions(self, text: str) -> str:
        """Remove CQ-code AT mentions from message text."""
        return self._at_pattern.sub("", text)

    def extract_mentions(self, event: MessageEvent) -> List[str]:
        """Extract mentioned user IDs from CQ at segments."""
        return [str(qq) for qq in event.get_at_qqs() if str(qq or "").strip()]


class NapCatAdapter(PlatformAdapter):
    platform = "qq"
    adapter_name = "napcat"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        on_message: Callable[[Dict[str, Any]], Awaitable[None]],
        on_connect: Callable[[], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]],
        connection: Optional[NapCatConnection] = None,
    ) -> None:
        self._connection = connection or NapCatConnection(
            host=host,
            port=port,
            on_message=on_message,
            on_connect=on_connect,
            on_disconnect=on_disconnect,
        )
        self._protocol = NapCatProtocolAdapter()

    async def run(self) -> None:
        await self._connection.run()

    async def disconnect(self) -> None:
        await self._connection.disconnect()

    async def send(self, data: Dict[str, Any]) -> bool:
        return await self._connection.send(data)

    async def send_action(self, action: OutgoingAction) -> bool:
        payload = self._action_to_payload(action)
        return await self.send(payload)

    def is_ready(self) -> bool:
        return bool(getattr(self._connection, "_connected", False))

    def attach_inbound_event(self, event: MessageEvent) -> Optional[InboundEvent]:
        return attach_normalized_onebot_event(
            event,
            platform=self.platform,
            adapter=self.adapter_name,
            protocol_adapter=self._protocol,
        )

    def as_protocol_adapter(self) -> Optional[ProtocolAdapter]:
        return self._protocol

    def build_mention_payload(self, user_id: str) -> Dict[str, Any]:
        return MessageSegment.at(user_id).to_dict()

    def _action_to_payload(self, action: OutgoingAction) -> Dict[str, Any]:
        if isinstance(action, ReplyAction):
            return self._build_reply_payload(action)
        if isinstance(action, FaceAction):
            return self._build_face_payload(action)
        if isinstance(action, MfaceAction):
            return self._build_mface_payload(action)
        raise TypeError(f"unsupported action type: {action.__class__.__name__}")

    def _build_reply_payload(self, action: ReplyAction) -> Dict[str, Any]:
        session = self._require_session(action.session)
        message = self._build_reply_message(action, session=session)
        if session.scope == "private":
            return {
                "action": "send_private_msg",
                "params": {"user_id": int(session.user_id), "message": message},
            }
        if session.scope in {"group", "shared"}:
            return {
                "action": "send_group_msg",
                "params": {"group_id": int(session.channel_id), "message": message},
            }
        raise ValueError(f"unsupported session scope for reply: {session.scope}")

    def _build_face_payload(self, action: FaceAction) -> Dict[str, Any]:
        session = self._require_session(action.session)
        if not str(action.face_id or "").strip():
            raise ValueError("face action requires face_id")
        message = [MessageSegment.face(action.face_id).to_dict()]
        if session.scope == "private":
            return {
                "action": "send_private_msg",
                "params": {"user_id": int(session.user_id), "message": message},
            }
        if session.scope in {"group", "shared"}:
            return {
                "action": "send_group_msg",
                "params": {"group_id": int(session.channel_id), "message": message},
            }
        raise ValueError(f"unsupported session scope for face action: {session.scope}")

    def _build_mface_payload(self, action: MfaceAction) -> Dict[str, Any]:
        session = self._require_session(action.session)
        if not str(action.emoji_id or "").strip():
            raise ValueError("mface action requires emoji_id")
        segment = MessageSegment.mface(
            emoji_id=action.emoji_id,
            emoji_package_id=action.emoji_package_id,
            key=action.key,
            summary=action.summary,
        ).to_dict()
        if session.scope == "private":
            return {
                "action": "send_private_msg",
                "params": {"user_id": int(session.user_id), "message": [segment]},
            }
        if session.scope not in {"group", "shared"}:
            raise ValueError(f"unsupported session scope for mface action: {session.scope}")
        return {
            "action": "send_group_msg",
            "params": {
                "group_id": int(session.channel_id),
                "message": [segment],
            },
        }

    def _build_reply_message(self, action: ReplyAction, *, session: SessionRef) -> Any:
        if action.segments:
            return self._segments_to_message(action.segments)
        if action.quote_message_id and session.scope == "private":
            segments = [
                MessageSegment.reply(action.quote_message_id).to_dict(),
                MessageSegment.text(action.text).to_dict(),
            ]
            return segments
        return action.text

    @staticmethod
    def _segments_to_message(segments: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
        return [dict(segment or {}) for segment in segments]

    @staticmethod
    def _require_session(session: Optional[SessionRef]) -> SessionRef:
        if session is None:
            raise ValueError("outgoing action requires session")
        return session
