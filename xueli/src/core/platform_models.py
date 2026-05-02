from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional, Tuple

SessionScope = Literal["private", "shared", "channel", "api", "system", "unknown"]
EventType = Literal["message", "notice", "request", "meta_event", "command", "unknown"]
MessageKind = Literal["text", "image", "mixed", "unknown"]


@dataclass(frozen=True)
class AttachmentRef:
    kind: str = "unknown"
    attachment_id: str = ""
    url: str = ""
    name: str = ""
    mime_type: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SenderRef:
    user_id: str = ""
    display_name: str = ""
    platform_user_id: str = ""
    is_bot: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionRef:
    platform: str
    scope: SessionScope
    conversation_id: str
    user_id: str = ""
    account_id: str = ""
    channel_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        if self.conversation_id:
            return self.conversation_id
        parts = [self.platform or "unknown", self.scope or "unknown"]
        if self.channel_id:
            parts.append(f"channel:{self.channel_id}")
        if self.user_id:
            parts.append(f"user:{self.user_id}")
        if self.account_id:
            parts.append(f"account:{self.account_id}")
        return ":".join(parts)

    @property
    def qualified_key(self) -> str:
        key = self.key
        if not self.platform:
            return key
        platform_prefix = f"{self.platform}:"
        return key if key.startswith(platform_prefix) else f"{self.platform}:{key}"


@dataclass(frozen=True)
class PlatformCapabilities:
    supports_text: bool = True
    supports_images: bool = False
    supports_face: bool = False
    supports_mface: bool = False
    supports_quote_reply: bool = False
    supports_groups: bool = False
    supports_message_edit: bool = False
    supports_files: bool = False
    supports_proactive_push: bool = False


@dataclass(frozen=True)
class InboundEvent:
    platform: str
    adapter: str
    event_type: EventType
    message_kind: MessageKind
    session: SessionRef
    sender: SenderRef
    text: str = ""
    message_id: str = ""
    reply_to_message_id: str = ""
    segments: Tuple[Dict[str, Any], ...] = ()
    attachments: Tuple[AttachmentRef, ...] = ()
    mentioned_user_ids: Tuple[str, ...] = ()
    capabilities: PlatformCapabilities = field(default_factory=PlatformCapabilities)
    metadata: Dict[str, Any] = field(default_factory=dict)
    raw_event: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_text(self) -> bool:
        return bool(str(self.text or "").strip())

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)

    @property
    def is_private(self) -> bool:
        return self.session.scope == "private"

    @property
    def is_group(self) -> bool:
        return self.session.scope == "shared"


@dataclass(frozen=True)
class OutgoingAction:
    session: Optional[SessionRef] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    action_type: str = field(init=False, default="action")


@dataclass(frozen=True)
class ReplyAction(OutgoingAction):
    text: str = ""
    segments: Tuple[Dict[str, Any], ...] = ()
    quote_message_id: str = ""
    action_type: str = field(init=False, default="reply")


@dataclass(frozen=True)
class FaceAction(OutgoingAction):
    face_id: str = ""
    action_type: str = field(init=False, default="face")


@dataclass(frozen=True)
class MfaceAction(OutgoingAction):
    emoji_id: str = ""
    emoji_package_id: str = ""
    key: str = ""
    summary: str = ""
    action_type: str = field(init=False, default="mface")


@dataclass(frozen=True)
class NoopAction(OutgoingAction):
    reason: str = ""
    action_type: str = field(init=False, default="no_op")
