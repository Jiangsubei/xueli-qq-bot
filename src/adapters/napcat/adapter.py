from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Iterable, Optional

from src.adapters.base import PlatformAdapter
from src.adapters.napcat.connection import NapCatConnection
from src.core.models import MessageEvent, MessageSegment
from src.core.platform_models import ImageAction, InboundEvent, OutgoingAction, ReplyAction, SessionRef
from src.core.platform_normalizers import attach_normalized_onebot_event


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
        )

    def _action_to_payload(self, action: OutgoingAction) -> Dict[str, Any]:
        if isinstance(action, ReplyAction):
            return self._build_reply_payload(action)
        if isinstance(action, ImageAction):
            return self._build_image_payload(action)
        raise TypeError(f"unsupported action type: {action.__class__.__name__}")

    def _build_reply_payload(self, action: ReplyAction) -> Dict[str, Any]:
        session = self._require_session(action.session)
        message = self._build_reply_message(action, session=session)
        if session.scope == "private":
            return {
                "action": "send_private_msg",
                "params": {"user_id": int(session.user_id), "message": message},
            }
        if session.scope == "group":
            return {
                "action": "send_group_msg",
                "params": {"group_id": int(session.channel_id), "message": message},
            }
        raise ValueError(f"unsupported session scope for reply: {session.scope}")

    def _build_image_payload(self, action: ImageAction) -> Dict[str, Any]:
        session = self._require_session(action.session)
        image_file = str(action.image_path or action.image_url or "")
        if not image_file:
            raise ValueError("image action requires image_path or image_url")
        segments = [MessageSegment.image(image_file)]
        if action.caption:
            segments.append(MessageSegment.text(action.caption))
        if session.scope != "group":
            raise ValueError(f"unsupported session scope for image action: {session.scope}")
        return {
            "action": "send_group_msg",
            "params": {
                "group_id": int(session.channel_id),
                "message": [segment.to_dict() for segment in segments],
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
