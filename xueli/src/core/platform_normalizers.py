"""Platform-agnostic inbound event normalization helpers.

OneBot-specific normalization (normalize_onebot_message_event,
attach_normalized_onebot_event) lives in adapters/napcat/normalizer.py.
This module re-exports them for backward compatibility only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from src.adapters.base import ProtocolAdapter

from src.core.models import MessageEvent, MessageSegment
from src.core.platform_models import InboundEvent

_INBOUND_EVENT_ATTR = "_inbound_event"

# ---------------------------------------------------------------------------
# Backward-compatible re-exports (new code should import from
# adapters.napcat.normalizer instead).
# ---------------------------------------------------------------------------
from src.adapters.napcat.normalizer import (  # noqa: E402  -- re-export
    attach_normalized_onebot_event,
    normalize_onebot_message_event,
)


def _extract_reply_to_message_id(segments: List[MessageSegment]) -> str:
    for segment in segments:
        if str(segment.type or "") != "reply":
            continue
        return str((segment.data or {}).get("id") or "")
    return ""


# ---------------------------------------------------------------------------
# Platform-agnostic public helpers
# ---------------------------------------------------------------------------


def get_attached_inbound_event(event: MessageEvent) -> Optional[InboundEvent]:
    """Return the InboundEvent previously attached to a MessageEvent, if any."""
    inbound_event = getattr(event, _INBOUND_EVENT_ATTR, None)
    if isinstance(inbound_event, InboundEvent):
        return inbound_event
    return None


def get_inbound_mentioned_user_ids(event: MessageEvent) -> Tuple[str, ...]:
    """Return mentioned user IDs from the attached InboundEvent, falling back
    to QQ-specific at-qq extraction."""
    inbound_event = get_attached_inbound_event(event)
    if inbound_event is not None:
        return tuple(
            str(user_id)
            for user_id in inbound_event.mentioned_user_ids
            if str(user_id or "")
        )
    return tuple(str(user_id) for user_id in event.get_at_qqs())


def get_inbound_reply_to_message_id(event: MessageEvent) -> str:
    """Return the reply-to message ID from the attached InboundEvent."""
    inbound_event = get_attached_inbound_event(event)
    if inbound_event is not None:
        return str(inbound_event.reply_to_message_id or "")
    return _extract_reply_to_message_id(list(event.message or []))


def event_mentions_account(event: MessageEvent, account_id: Any = "") -> bool:
    """Check whether the event mentions the bot's own account."""
    inbound_event = get_attached_inbound_event(event)
    resolved_account_id = str(account_id or "").strip()
    if not resolved_account_id and inbound_event is not None:
        resolved_account_id = str(inbound_event.session.account_id or "").strip()
    if not resolved_account_id:
        resolved_account_id = str(event.self_id or "").strip()
    if not resolved_account_id:
        return False
    mentioned_user_ids = {
        str(user_id or "").strip()
        for user_id in get_inbound_mentioned_user_ids(event)
    }
    return resolved_account_id in mentioned_user_ids


def get_or_normalize_onebot_inbound_event(
    event: MessageEvent,
    *,
    platform: str = "qq",
    adapter: str = "napcat",
    protocol_adapter: "ProtocolAdapter | None" = None,
) -> InboundEvent:
    """Return the attached InboundEvent, or normalize via the OneBot path."""
    inbound_event = get_attached_inbound_event(event)
    if inbound_event is not None:
        return inbound_event
    return attach_normalized_onebot_event(
        event,
        platform=platform,
        adapter=adapter,
        protocol_adapter=protocol_adapter,
    )


def build_generic_inbound_event(
    event: MessageEvent,
    *,
    platform: str = "unknown",
    adapter: str = "unknown",
) -> InboundEvent:
    """Build a minimal platform-agnostic InboundEvent without OneBot-specific parsing.

    Used as the fallback path when no platform adapter is available.
    """
    text = str(getattr(event, "raw_message", "") or "")
    return InboundEvent(
        platform=platform,
        adapter=adapter,
        event_type="message",
        message_kind="text" if text else "unknown",
        session=None,
        sender=None,
        text=text,
        message_id=str(getattr(event, "message_id", "") or ""),
        reply_to_message_id="",
        segments=(),
        attachments=(),
        mentioned_user_ids=(),
        metadata={},
        raw_event=dict(getattr(event, "raw_data", None) or {}),
    )
