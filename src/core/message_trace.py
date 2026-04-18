from __future__ import annotations

from uuid import uuid4

from src.core.models import MessageEvent, MessageType
from src.core.platform_models import SessionRef
from src.core.platform_normalizers import get_or_normalize_onebot_inbound_event


def build_trace_id(message_id: int | str) -> str:
    return f"msg-{str(message_id or '0').strip() or '0'}-{uuid4().hex[:8]}"


def get_execution_key(event: MessageEvent) -> str:
    inbound_event = get_or_normalize_onebot_inbound_event(event)
    if inbound_event is not None:
        return get_execution_key_for_session(inbound_event.session)
    if event.message_type == MessageType.PRIVATE.value:
        return f"private:{event.user_id}"
    return f"group:{event.group_id}"


def get_execution_key_for_session(session: SessionRef) -> str:
    if session.scope in {"group", "channel"}:
        channel_id = str(session.channel_id or "").strip()
        if channel_id:
            if session.platform:
                return f"{session.platform}:{session.scope}:{channel_id}"
            return f"{session.scope}:{channel_id}"
    return session.qualified_key


def format_trace_log(
    *,
    trace_id: str,
    session_key: str = "",
    message_id: int | str = "",
) -> str:
    return f"trace={trace_id} session={session_key} message_id={message_id}"
