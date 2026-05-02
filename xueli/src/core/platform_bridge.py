from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.core.models import MessageEvent
from src.core.platform_models import AttachmentRef, InboundEvent


def build_message_event_from_inbound(
    inbound_event: InboundEvent,
    *,
    self_id: Any = "",
    raw_data: Optional[Dict[str, Any]] = None,
) -> MessageEvent:
    payload = dict(raw_data or {})
    payload.setdefault("post_type", "message")
    payload.setdefault("message_type", _message_type_from_scope(inbound_event.session.scope))
    payload.setdefault("message_id", inbound_event.message_id or "")
    payload.setdefault("self_id", self_id or inbound_event.session.account_id or "")
    payload.setdefault("user_id", inbound_event.sender.user_id or inbound_event.session.user_id or "")
    payload.setdefault("group_id", inbound_event.session.channel_id or None)
    payload.setdefault("raw_message", inbound_event.text)
    payload.setdefault("message", _message_segments_from_inbound(inbound_event))
    payload.setdefault("sender", _sender_payload(inbound_event))

    event = MessageEvent.from_dict(payload)
    setattr(event, "_inbound_event", inbound_event)
    return event


def _message_type_from_scope(scope: str) -> str:
    if str(scope or "").strip().lower() in {"shared", "channel"}:
        return "group"
    return "private"


def _message_segments_from_inbound(inbound_event: InboundEvent) -> List[Dict[str, Any]]:
    if inbound_event.segments:
        return [dict(item or {}) for item in inbound_event.segments]

    segments: List[Dict[str, Any]] = []
    if inbound_event.text:
        segments.append({"type": "text", "data": {"text": inbound_event.text}})
    for attachment in inbound_event.attachments:
        image_segment = _image_segment_from_attachment(attachment)
        if image_segment is not None:
            segments.append(image_segment)
    return segments


def _image_segment_from_attachment(attachment: AttachmentRef) -> Optional[Dict[str, Any]]:
    if str(attachment.kind or "") != "image":
        return None
    file_value = str(attachment.attachment_id or attachment.url or attachment.name or "")
    segment = {"type": "image", "data": {"file": file_value}}
    if attachment.url:
        segment["data"]["url"] = str(attachment.url)
    if attachment.name:
        segment["data"]["file_name"] = str(attachment.name)
    return segment


def _sender_payload(inbound_event: InboundEvent) -> Dict[str, Any]:
    display_name = str(inbound_event.sender.display_name or "")
    if inbound_event.session.scope == "shared":
        return {"card": display_name, "nickname": display_name}
    return {"nickname": display_name, "card": display_name}
