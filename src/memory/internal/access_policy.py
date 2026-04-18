from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, Optional


class MemoryVisibility(str, Enum):
    PRIVATE = "private"
    SHARED = "shared"


class MemoryContentCategory(str, Enum):
    PERSONAL_INFO = "personal_info"
    PERSONAL_PREFERENCE = "personal_preference"
    PERSONAL_BOUNDARY = "personal_boundary"
    PLAN = "plan"
    BACKGROUND = "background"
    ADDRESSING_PREFERENCE = "addressing_preference"
    GROUP_RULE = "group_rule"
    BOT_RULE = "bot_rule"
    PUBLIC_RULE = "public_rule"
    UNKNOWN = "unknown"


class MemoryApplicabilityScope(str, Enum):
    DEFAULT = "default"
    PRIVATE_CHAT = "private_chat"
    GROUP_MEMBER = "group_member"
    GROUP = "group"
    SHARED = "shared"


@dataclass(frozen=True)
class MemoryAccessContext:
    requester_user_id: str
    message_type: str = "private"
    group_id: str = ""
    read_scope: str = "user"


class MemoryAccessPolicy:
    SCHEMA_VERSION = 2
    _SHARED_CATEGORIES = {
        MemoryContentCategory.GROUP_RULE.value,
        MemoryContentCategory.BOT_RULE.value,
        MemoryContentCategory.PUBLIC_RULE.value,
    }
    _PRIVATE_CATEGORIES = {
        MemoryContentCategory.PERSONAL_INFO.value,
        MemoryContentCategory.PERSONAL_PREFERENCE.value,
        MemoryContentCategory.PERSONAL_BOUNDARY.value,
        MemoryContentCategory.PLAN.value,
        MemoryContentCategory.BACKGROUND.value,
        MemoryContentCategory.UNKNOWN.value,
    }
    _ADDRESSING_PATTERNS = [
        re.compile(pattern, re.I)
        for pattern in [
            r"\bcall me\b",
            r"\baddress me as\b",
            r"叫我",
            r"称呼我",
            r"喊我",
        ]
    ]

    def normalize_read_scope(self, value: Optional[str]) -> str:
        normalized = str(value or "user").strip().lower()
        return normalized if normalized in {"user", "global"} else "user"

    def build_context(
        self,
        *,
        requester_user_id: str,
        message_type: Optional[str] = None,
        group_id: Optional[str] = None,
        read_scope: Optional[str] = None,
    ) -> MemoryAccessContext:
        return MemoryAccessContext(
            requester_user_id=str(requester_user_id or ""),
            message_type=str(message_type or "private"),
            group_id=str(group_id or ""),
            read_scope=self.normalize_read_scope(read_scope),
        )

    def normalize_memory_record(
        self,
        *,
        content: str,
        owner_user_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        source: str = "",
    ) -> Dict[str, Any]:
        prepared = dict(metadata or {})
        category = self._normalize_category(
            prepared.get("content_category"),
            content=content,
            source=source,
            metadata=prepared,
        )
        visibility = self._normalize_visibility(prepared.get("visibility"), category, prepared)
        applicability = self._normalize_applicability_scope(
            prepared.get("applicability_scope"),
            category=category,
            owner_user_id=str(owner_user_id or prepared.get("owner_user_id") or ""),
            metadata=prepared,
        )

        prepared["owner_user_id"] = str(owner_user_id or prepared.get("owner_user_id") or "")
        prepared["visibility"] = visibility
        prepared["content_category"] = category
        prepared["applicability_scope"] = applicability
        prepared["schema_version"] = int(prepared.get("schema_version") or self.SCHEMA_VERSION)

        if visibility == MemoryVisibility.SHARED.value:
            prepared.setdefault("shared_reason", self._default_shared_reason(category))
        elif "shared_reason" in prepared and not prepared["shared_reason"]:
            prepared.pop("shared_reason", None)

        return prepared

    def is_accessible(
        self,
        *,
        owner_user_id: str,
        metadata: Optional[Dict[str, Any]],
        context: MemoryAccessContext,
    ) -> bool:
        prepared = self.normalize_memory_record(
            content="",
            owner_user_id=owner_user_id,
            metadata=metadata,
        )
        visibility = prepared["visibility"]
        applicability = prepared["applicability_scope"]
        requester = str(context.requester_user_id or "")
        owner = str(owner_user_id or prepared.get("owner_user_id") or "")

        if not self._scope_matches(applicability, requester_user_id=requester, message_type=context.message_type, group_id=context.group_id):
            return False

        if visibility == MemoryVisibility.PRIVATE.value:
            return owner == requester

        if owner == requester:
            return True

        return context.read_scope == "global"

    def is_shared(self, metadata: Optional[Dict[str, Any]]) -> bool:
        prepared = self.normalize_memory_record(content="", metadata=metadata)
        return prepared["visibility"] == MemoryVisibility.SHARED.value

    def is_addressing(self, metadata: Optional[Dict[str, Any]]) -> bool:
        prepared = self.normalize_memory_record(content="", metadata=metadata)
        return prepared["content_category"] == MemoryContentCategory.ADDRESSING_PREFERENCE.value

    def matches_scene(self, metadata: Optional[Dict[str, Any]], context: MemoryAccessContext) -> bool:
        prepared = self.normalize_memory_record(content="", metadata=metadata)
        return self._scope_matches(
            prepared["applicability_scope"],
            requester_user_id=context.requester_user_id,
            message_type=context.message_type,
            group_id=context.group_id,
        )

    def classify_for_prompt(self, metadata: Optional[Dict[str, Any]], owner_user_id: str, requester_user_id: str) -> str:
        prepared = self.normalize_memory_record(content="", owner_user_id=owner_user_id, metadata=metadata)
        if prepared["content_category"] == MemoryContentCategory.ADDRESSING_PREFERENCE.value:
            return "addressing"
        if prepared["visibility"] == MemoryVisibility.SHARED.value and owner_user_id != requester_user_id:
            return "shared"
        return "private"

    def _normalize_visibility(
        self,
        explicit_value: Any,
        category: str,
        metadata: Dict[str, Any],
    ) -> str:
        normalized = str(explicit_value or "").strip().lower()
        if normalized not in {MemoryVisibility.PRIVATE.value, MemoryVisibility.SHARED.value}:
            normalized = ""

        explicit_shared = bool(metadata.get("explicitly_authorized_shared") or metadata.get("shared_authorized"))
        if category == MemoryContentCategory.ADDRESSING_PREFERENCE.value:
            return MemoryVisibility.PRIVATE.value
        if category in self._SHARED_CATEGORIES:
            return MemoryVisibility.SHARED.value
        if explicit_shared:
            return MemoryVisibility.SHARED.value
        if normalized == MemoryVisibility.SHARED.value and category not in self._PRIVATE_CATEGORIES:
            return MemoryVisibility.SHARED.value
        return MemoryVisibility.PRIVATE.value

    def _normalize_category(
        self,
        explicit_value: Any,
        *,
        content: str,
        source: str,
        metadata: Dict[str, Any],
    ) -> str:
        normalized = str(explicit_value or "").strip().lower()
        valid_values = {item.value for item in MemoryContentCategory}
        if normalized in valid_values:
            return normalized

        tags = {str(tag).strip().lower() for tag in metadata.get("tags", []) if str(tag).strip()}
        marker_values = tags | {
            str(metadata.get("memory_kind", "")).strip().lower(),
            str(metadata.get("rule_kind", "")).strip().lower(),
            str(source or "").strip().lower(),
        }

        if any(pattern.search(str(content or "")) for pattern in self._ADDRESSING_PATTERNS):
            return MemoryContentCategory.ADDRESSING_PREFERENCE.value
        if {"group_rule", "grouprule"} & marker_values:
            return MemoryContentCategory.GROUP_RULE.value
        if {"bot_rule", "botrule"} & marker_values:
            return MemoryContentCategory.BOT_RULE.value
        if {"shared", "public_rule", "public"} & marker_values and (
            metadata.get("explicitly_authorized_shared") or metadata.get("shared_authorized")
        ):
            return MemoryContentCategory.PUBLIC_RULE.value
        if {"personal_info", "profile"} & marker_values:
            return MemoryContentCategory.PERSONAL_INFO.value
        if {"preference", "personal_preference"} & marker_values:
            return MemoryContentCategory.PERSONAL_PREFERENCE.value
        if {"taboo", "boundary", "personal_boundary"} & marker_values:
            return MemoryContentCategory.PERSONAL_BOUNDARY.value
        if {"plan"} & marker_values:
            return MemoryContentCategory.PLAN.value
        if {"background"} & marker_values:
            return MemoryContentCategory.BACKGROUND.value
        if metadata.get("explicitly_authorized_shared") or metadata.get("shared_authorized"):
            return MemoryContentCategory.PUBLIC_RULE.value
        return MemoryContentCategory.UNKNOWN.value

    def _normalize_applicability_scope(
        self,
        value: Any,
        *,
        category: str,
        owner_user_id: str,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        if isinstance(value, dict):
            kind = str(value.get("kind") or MemoryApplicabilityScope.DEFAULT.value).strip().lower()
            normalized = {
                "kind": kind if kind in {item.value for item in MemoryApplicabilityScope} else MemoryApplicabilityScope.DEFAULT.value,
            }
            if value.get("group_id"):
                normalized["group_id"] = str(value.get("group_id"))
            if value.get("user_id"):
                normalized["user_id"] = str(value.get("user_id"))
            return normalized

        source_message_type = str(metadata.get("source_message_type") or "").strip().lower()
        source_group_id = str(metadata.get("source_group_id") or metadata.get("group_id") or "").strip()

        if category == MemoryContentCategory.ADDRESSING_PREFERENCE.value:
            if source_message_type == "group" and source_group_id:
                return {
                    "kind": MemoryApplicabilityScope.GROUP_MEMBER.value,
                    "group_id": source_group_id,
                    "user_id": owner_user_id,
                }
            return {
                "kind": MemoryApplicabilityScope.PRIVATE_CHAT.value,
                "user_id": owner_user_id,
            }

        if category == MemoryContentCategory.GROUP_RULE.value and source_group_id:
            return {
                "kind": MemoryApplicabilityScope.GROUP.value,
                "group_id": source_group_id,
            }

        if source_message_type == "group" and source_group_id and owner_user_id:
            return {
                "kind": MemoryApplicabilityScope.GROUP_MEMBER.value,
                "group_id": source_group_id,
                "user_id": owner_user_id,
            }

        if source_message_type == "private" and owner_user_id:
            return {
                "kind": MemoryApplicabilityScope.PRIVATE_CHAT.value,
                "user_id": owner_user_id,
            }

        if category in self._SHARED_CATEGORIES:
            return {"kind": MemoryApplicabilityScope.SHARED.value}

        return {"kind": MemoryApplicabilityScope.DEFAULT.value}

    def _scope_matches(
        self,
        applicability_scope: Dict[str, Any],
        *,
        requester_user_id: str,
        message_type: str,
        group_id: str,
    ) -> bool:
        kind = str(applicability_scope.get("kind") or MemoryApplicabilityScope.DEFAULT.value).strip().lower()
        if kind == MemoryApplicabilityScope.DEFAULT.value:
            return True
        if kind == MemoryApplicabilityScope.SHARED.value:
            return True
        if kind == MemoryApplicabilityScope.PRIVATE_CHAT.value:
            return str(message_type or "private") == "private" and (
                not applicability_scope.get("user_id")
                or str(applicability_scope.get("user_id")) == str(requester_user_id)
            )
        if kind == MemoryApplicabilityScope.GROUP_MEMBER.value:
            return (
                str(message_type or "") == "group"
                and str(applicability_scope.get("group_id") or "") == str(group_id or "")
                and str(applicability_scope.get("user_id") or requester_user_id) == str(requester_user_id)
            )
        if kind == MemoryApplicabilityScope.GROUP.value:
            return str(message_type or "") == "group" and str(applicability_scope.get("group_id") or "") == str(group_id or "")
        return True

    def _default_shared_reason(self, category: str) -> str:
        if category == MemoryContentCategory.GROUP_RULE.value:
            return "group_rule"
        if category == MemoryContentCategory.BOT_RULE.value:
            return "bot_rule"
        return "explicitly_authorized_shared"

    def dedupe_entries(self, entries: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
        seen = set()
        result = []
        for entry in entries:
            content = re.sub(r"\s+", " ", str(entry.get("content", "")).strip().lower())
            if not content or content in seen:
                continue
            seen.add(content)
            result.append(entry)
        return result
