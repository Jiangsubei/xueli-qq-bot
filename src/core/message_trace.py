from __future__ import annotations

from uuid import uuid4

from src.core.models import MessageEvent, MessageType


def build_trace_id(message_id: int | str) -> str:
    return f"msg-{str(message_id or '0').strip() or '0'}-{uuid4().hex[:8]}"


def get_execution_key(event: MessageEvent) -> str:
    if event.message_type == MessageType.PRIVATE.value:
        return f"private:{event.user_id}"
    return f"group:{event.group_id}"


def format_trace_log(
    *,
    trace_id: str,
    session_key: str = "",
    message_id: int | str = "",
) -> str:
    return f"trace={trace_id} session={session_key} message_id={message_id}"
