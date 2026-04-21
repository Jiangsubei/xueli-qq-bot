from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from src.adapters.base import PlatformAdapter, ProtocolAdapter
from src.core.platform_models import (
    AttachmentRef,
    ImageAction,
    InboundEvent,
    OutgoingAction,
    PlatformCapabilities,
    ReplyAction,
    SenderRef,
    SessionRef,
)
from src.core.models import MessageEvent

PayloadEmitter = Callable[[Dict[str, Any]], Awaitable[bool] | bool | None]
LifecycleHook = Callable[[], Awaitable[None] | None]


class _NoOpProtocolAdapter(ProtocolAdapter):
    """No-op protocol adapter for API platform — text is already clean."""

    def strip_mentions(self, text: str) -> str:
        return text

    def extract_mentions(self, event: MessageEvent) -> List[str]:
        return []


class ApiAdapter(PlatformAdapter):
    platform = "api"
    adapter_name = "openapi"

    def __init__(
        self,
        *,
        emit: Optional[PayloadEmitter] = None,
        on_connect: Optional[LifecycleHook] = None,
        on_disconnect: Optional[LifecycleHook] = None,
    ) -> None:
        self._emit = emit
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._ready = True
        self.sent_payloads: list[Dict[str, Any]] = []
        self._protocol = _NoOpProtocolAdapter()

    async def run(self) -> None:
        self._ready = True
        await self._run_hook(self._on_connect)

    async def disconnect(self) -> None:
        self._ready = False
        await self._run_hook(self._on_disconnect)

    async def send(self, data: Dict[str, Any]) -> bool:
        payload = dict(data or {})
        self.sent_payloads.append(payload)
        if self._emit is None:
            return True
        result = self._emit(payload)
        if inspect.isawaitable(result):
            result = await result
        return True if result is None else bool(result)

    async def send_action(self, action: OutgoingAction) -> bool:
        return await self.send(self._action_to_payload(action))

    def is_ready(self) -> bool:
        return self._ready

    def as_protocol_adapter(self) -> ProtocolAdapter:
        return self._protocol

    async def _run_hook(self, hook: Optional[LifecycleHook]) -> None:
        if hook is None:
            return
        result = hook()
        if inspect.isawaitable(result):
            await result

    def normalize_inbound_payload(self, payload: Dict[str, Any]) -> Optional[InboundEvent]:
        raw_payload = dict(payload or {})
        event_type = str(raw_payload.get("event_type") or "message").strip() or "message"
        if event_type != "message":
            return None

        session_payload = dict(raw_payload.get("session") or {})
        sender_payload = dict(raw_payload.get("sender") or {})
        attachment_payloads = list(raw_payload.get("attachments") or [])
        text = str(raw_payload.get("text") or "")
        segments = tuple(dict(item or {}) for item in raw_payload.get("segments") or [])
        attachments = tuple(self._attachment_from_payload(item) for item in attachment_payloads)
        session = self._session_from_payload(session_payload, raw_payload)
        sender = self._sender_from_payload(sender_payload, session=session)
        capabilities = self._capabilities_from_payload(dict(raw_payload.get("capabilities") or {}))
        mentioned_user_ids = tuple(str(item) for item in raw_payload.get("mentioned_user_ids") or [] if str(item or ""))

        return InboundEvent(
            platform=self.platform,
            adapter=self.adapter_name,
            event_type="message",
            message_kind=self._resolve_message_kind(text=text, attachments=attachments),
            session=session,
            sender=sender,
            text=text,
            message_id=str(raw_payload.get("message_id") or ""),
            reply_to_message_id=str(raw_payload.get("reply_to_message_id") or ""),
            segments=segments,
            attachments=attachments,
            mentioned_user_ids=mentioned_user_ids,
            capabilities=capabilities,
            metadata=dict(raw_payload.get("metadata") or {}),
            raw_event=raw_payload,
        )

    def _action_to_payload(self, action: OutgoingAction) -> Dict[str, Any]:
        if isinstance(action, ReplyAction):
            return {
                "action": "reply",
                "session": self._session_to_payload(action.session),
                "message": {
                    "text": action.text,
                    "segments": [dict(item or {}) for item in action.segments],
                    "quote_message_id": action.quote_message_id,
                },
                "metadata": dict(action.metadata or {}),
            }
        if isinstance(action, ImageAction):
            return {
                "action": "image",
                "session": self._session_to_payload(action.session),
                "image": {
                    "url": action.image_url,
                    "path": action.image_path,
                    "caption": action.caption,
                },
                "metadata": dict(action.metadata or {}),
            }
        raise TypeError(f"unsupported action type: {action.__class__.__name__}")

    def _session_from_payload(self, session_payload: Dict[str, Any], raw_payload: Dict[str, Any]) -> SessionRef:
        scope = str(session_payload.get("scope") or "private").strip() or "private"
        user_id = str(session_payload.get("user_id") or raw_payload.get("user_id") or "")
        account_id = str(session_payload.get("account_id") or raw_payload.get("account_id") or "")
        channel_id = str(session_payload.get("channel_id") or raw_payload.get("channel_id") or "")
        conversation_id = str(session_payload.get("conversation_id") or "").strip()
        if not conversation_id:
            identity = channel_id or user_id or str(raw_payload.get("message_id") or "unknown")
            conversation_id = f"api:{scope}:{identity}"
        return SessionRef(
            platform=self.platform,
            scope=scope,
            conversation_id=conversation_id,
            user_id=user_id,
            account_id=account_id,
            channel_id=channel_id,
            metadata=dict(session_payload.get("metadata") or {}),
        )

    @staticmethod
    def _sender_from_payload(sender_payload: Dict[str, Any], *, session: SessionRef) -> SenderRef:
        return SenderRef(
            user_id=str(sender_payload.get("user_id") or session.user_id or ""),
            display_name=str(sender_payload.get("display_name") or sender_payload.get("name") or ""),
            platform_user_id=str(sender_payload.get("platform_user_id") or ""),
            is_bot=bool(sender_payload.get("is_bot", False)),
            metadata=dict(sender_payload.get("metadata") or {}),
        )

    @staticmethod
    def _attachment_from_payload(payload: Dict[str, Any]) -> AttachmentRef:
        item = dict(payload or {})
        return AttachmentRef(
            kind=str(item.get("kind") or "unknown"),
            attachment_id=str(item.get("attachment_id") or item.get("id") or ""),
            url=str(item.get("url") or ""),
            name=str(item.get("name") or ""),
            mime_type=str(item.get("mime_type") or ""),
            metadata=dict(item.get("metadata") or {}),
        )

    @staticmethod
    def _capabilities_from_payload(payload: Dict[str, Any]) -> PlatformCapabilities:
        return PlatformCapabilities(
            supports_text=bool(payload.get("supports_text", True)),
            supports_images=bool(payload.get("supports_images", False)),
            supports_quote_reply=bool(payload.get("supports_quote_reply", False)),
            supports_groups=bool(payload.get("supports_groups", False)),
            supports_message_edit=bool(payload.get("supports_message_edit", False)),
            supports_files=bool(payload.get("supports_files", False)),
            supports_proactive_push=bool(payload.get("supports_proactive_push", False)),
        )

    @staticmethod
    def _resolve_message_kind(*, text: str, attachments: Iterable[AttachmentRef]) -> str:
        has_text = bool(str(text or "").strip())
        attachment_list = list(attachments)
        has_image = any(str(item.kind or "") == "image" for item in attachment_list)
        if has_text and has_image:
            return "mixed"
        if has_image:
            return "image"
        if has_text:
            return "text"
        return "unknown"

    @staticmethod
    def _session_to_payload(session: Optional[SessionRef]) -> Dict[str, Any]:
        if session is None:
            return {}
        return {
            "platform": session.platform,
            "scope": session.scope,
            "conversation_id": session.conversation_id,
            "user_id": session.user_id,
            "account_id": session.account_id,
            "channel_id": session.channel_id,
            "metadata": dict(session.metadata or {}),
        }
