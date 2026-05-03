"""OneBot/NapCat protocol-specific message normalization.

Moved from core/platform_normalizers.py to keep core free of platform details.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from src.adapters.base import ProtocolAdapter

from src.core.models import MessageEvent, MessageSegment, MessageType
from src.core.platform_models import (
    AttachmentRef,
    InboundEvent,
    PlatformCapabilities,
    SenderRef,
    SessionRef,
)

_INBOUND_EVENT_ATTR = "_inbound_event"


def _segment_to_payload(segment: MessageSegment) -> Dict[str, Any]:
    return {"type": str(segment.type or ""), "data": dict(segment.data or {})}


def _segment_to_attachment(segment: MessageSegment) -> Optional[AttachmentRef]:
    if not segment.is_image():
        return None
    return AttachmentRef(
        kind="image",
        attachment_id=str(segment.get_image_file_id() or ""),
        url=str(segment.get_image_url() or ""),
        name=str(segment.get_image_filename() or ""),
        metadata=dict(segment.data or {}),
    )


def _extract_reply_to_message_id(segments: List[MessageSegment]) -> str:
    for segment in segments:
        if str(segment.type or "") != "reply":
            continue
        return str((segment.data or {}).get("id") or "")
    return ""


def _resolve_message_kind(segments: List[MessageSegment]) -> str:
    has_text = any(
        str(segment.type or "") == "text"
        and str((segment.data or {}).get("text") or "").strip()
        for segment in segments
    )
    has_image = any(segment.is_image() for segment in segments)
    if has_text and has_image:
        return "mixed"
    if has_image:
        return "image"
    if has_text:
        return "text"
    return "unknown"


def _build_session(event: MessageEvent, platform: str) -> SessionRef:
    user_id = str(event.user_id or "")
    account_id = str(event.self_id or "")
    if event.message_type == MessageType.PRIVATE.value:
        return SessionRef(
            platform=platform,
            scope="private",
            conversation_id=f"private:{user_id}",
            user_id=user_id,
            account_id=account_id,
        )
    group_id = str(event.raw_data.get("group_id", "") or "")
    return SessionRef(
        platform=platform,
        scope="shared",
        conversation_id=f"group:{group_id}",
        user_id=user_id,
        account_id=account_id,
        channel_id=group_id,
    )


def _build_sender(event: MessageEvent) -> SenderRef:
    user_id = str(event.user_id or "")
    display_name = (
        event.get_sender_display_name()
        or event.get_sender_nickname()
        or user_id
    )
    return SenderRef(
        user_id=user_id,
        display_name=display_name,
        platform_user_id=user_id,
        is_bot=bool(
            event.user_id
            and event.self_id
            and int(event.user_id) == int(event.self_id)
        ),
        metadata=event.get_sender_info(),
    )


def normalize_onebot_message_event(
    event: MessageEvent,
    *,
    platform: str = "qq",
    adapter: str = "napcat",
    protocol_adapter: "ProtocolAdapter | None" = None,
) -> InboundEvent:
    """Normalize a OneBot/NapCat MessageEvent into a platform-agnostic InboundEvent."""
    segments = list(event.message or [])
    normalized_segments: Tuple[Dict[str, Any], ...] = tuple(
        _segment_to_payload(segment) for segment in segments
    )
    attachments: Tuple[AttachmentRef, ...] = tuple(
        attachment
        for attachment in (
            _segment_to_attachment(segment) for segment in segments
        )
        if attachment is not None
    )
    mentioned_user_ids = tuple(str(user_id) for user_id in event.get_at_qqs())

    raw_text = event.extract_text()
    if protocol_adapter is not None:
        clean_text = protocol_adapter.strip_mentions(raw_text)
    else:
        clean_text = raw_text

    return InboundEvent(
        platform=platform,
        adapter=adapter,
        event_type="message",
        message_kind=_resolve_message_kind(segments),
        session=_build_session(event, platform=platform),
        sender=_build_sender(event),
        text=clean_text,
        message_id=str(event.message_id or ""),
        reply_to_message_id=_extract_reply_to_message_id(segments),
        segments=normalized_segments,
        attachments=attachments,
        mentioned_user_ids=mentioned_user_ids,
        capabilities=PlatformCapabilities(
            supports_text=True,
            supports_images=True,
            supports_face=True,
            supports_mface=True,
            supports_quote_reply=True,
            supports_groups=True,
            supports_proactive_push=True,
        ),
        metadata={
            "message_type": event.message_type,
            "sub_type": event.sub_type,
            "raw_message": event.raw_message,
        },
        raw_event=dict(event.raw_data or {}),
    )


def attach_normalized_onebot_event(
    event: MessageEvent,
    *,
    platform: str = "qq",
    adapter: str = "napcat",
    protocol_adapter: "ProtocolAdapter | None" = None,
) -> InboundEvent:
    """Normalize and attach an InboundEvent to a MessageEvent."""
    inbound_event = normalize_onebot_message_event(
        event,
        platform=platform,
        adapter=adapter,
        protocol_adapter=protocol_adapter,
    )
    setattr(event, _INBOUND_EVENT_ATTR, inbound_event)
    return inbound_event
