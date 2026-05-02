from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any, Dict, List, Optional

from src.core.config import AppConfig
from src.core.message_trace import get_execution_key
from src.core.model_invocation_router import ModelInvocationRouter, ModelInvocationType
from src.core.models import MessageEvent, MessageType
from src.core.prompt_templates import PromptTemplateLoader
from src.core.platform_models import FaceAction, MfaceAction, OutgoingAction, SessionRef
from src.core.runtime_metrics import RuntimeMetrics

from .models import (
    DEFAULT_EMOTION_LABELS,
    DEFAULT_REPLY_TONES,
    EmojiRecord,
    EmojiReplyDecision,
    EmojiReplySelection,
)
from .repository import EmojiRepository

logger = logging.getLogger(__name__)


class EmojiReplyService:
    def __init__(
        self,
        *,
        repository: EmojiRepository,
        ai_client: Any,
        runtime_metrics: Optional[RuntimeMetrics],
        app_config: AppConfig,
        model_invocation_router: Optional[ModelInvocationRouter] = None,
    ) -> None:
        self.repository = repository
        self.ai_client = ai_client
        self.runtime_metrics = runtime_metrics
        self.app_config = app_config
        self.model_invocation_router = model_invocation_router
        emoji_config = app_config.emoji
        self.enabled = bool(emoji_config.enabled and getattr(emoji_config, "reply_enabled", True))
        self.cooldown_seconds = float(getattr(emoji_config, "reply_cooldown_seconds", 180.0))
        self.emotion_labels = list(getattr(emoji_config, "emotion_labels", DEFAULT_EMOTION_LABELS))
        self.reply_tones = list(DEFAULT_REPLY_TONES)
        self._rng = random.Random()
        self._group_last_sent_at: Dict[str, float] = {}
        self.template_loader = PromptTemplateLoader()

    async def plan_follow_up(
        self,
        *,
        event: MessageEvent,
        user_message: str,
        assistant_reply: str,
        reply_context: Optional[Dict[str, Any]] = None,
        trace_id: str = "",
    ) -> EmojiReplySelection:
        if not self.enabled:
            return self._skip("feature_disabled")
        if event.message_type != MessageType.GROUP.value:
            return self._skip("unsupported_message_type")
        if not assistant_reply.strip():
            return self._skip("empty_reply")
        group_id = event.raw_data.get("group_id", "")
        if self._group_cooldown_active(group_id):
            return self._skip("group_cooldown")

        decision = await self._decide_reply_intent(
            user_message=user_message,
            assistant_reply=assistant_reply,
            reply_context=reply_context,
            trace_id=trace_id,
            session_key=get_execution_key(event) if group_id else "",
            message_id=getattr(event, "message_id", 0),
        )
        if self.runtime_metrics:
            self.runtime_metrics.record_emoji_reply_decision(1)
        if not decision.should_send:
            return self._skip("model_declined", decision=decision)

        candidates = await self.repository.find_reply_candidates(
            target_intent=decision.target_intent,
            target_tone=decision.target_tone,
            target_emotion=decision.target_emotion,
        )
        cooled_candidates = [item for item in candidates if not self._emoji_cooldown_active(item)]
        if not cooled_candidates:
            if self.runtime_metrics:
                self.runtime_metrics.record_emoji_reply_no_candidate(1)
            return self._skip("no_candidate", decision=decision)

        selected = self._weighted_pick(cooled_candidates)
        if not selected:
            if self.runtime_metrics:
                self.runtime_metrics.record_emoji_reply_no_candidate(1)
            return self._skip("selection_failed", decision=decision)
        return EmojiReplySelection(decision=decision, emoji=selected)

    async def mark_follow_up_sent(self, *, event: MessageEvent, selection: EmojiReplySelection) -> Optional[EmojiRecord]:
        if not selection.emoji:
            return None
        group_id = event.raw_data.get("group_id", "")
        if group_id:
            self._group_last_sent_at[str(group_id)] = time.monotonic()
        record = await self.repository.mark_auto_reply_sent(selection.emoji.emoji_id)
        if self.runtime_metrics:
            self.runtime_metrics.record_emoji_reply_sent(1)
        return record

    def build_follow_up_action(
        self,
        *,
        selection: EmojiReplySelection,
        session: SessionRef,
    ) -> Optional[OutgoingAction]:
        if not selection.emoji:
            return None
        emoji = selection.emoji
        if emoji.sticker_kind == "face" and emoji.native_id:
            return FaceAction(session=session, face_id=emoji.native_id)
        if emoji.sticker_kind == "mface" and emoji.native_id:
            return MfaceAction(
                session=session,
                emoji_id=emoji.native_id,
                emoji_package_id=emoji.emoji_package_id,
                key=emoji.native_key,
                summary=emoji.native_summary,
            )
        return None

    async def _decide_reply_intent(
        self,
        *,
        user_message: str,
        assistant_reply: str,
        reply_context: Optional[Dict[str, Any]],
        trace_id: str = "",
        session_key: str = "",
        message_id: Any = "",
    ) -> EmojiReplyDecision:
        if not self.ai_client:
            return EmojiReplyDecision(should_send=False, reason="ai_client_unavailable")

        window_messages = list((reply_context or {}).get("window_messages") or [])[-4:]
        payload = {
            "user_message": user_message,
            "assistant_reply": assistant_reply,
            "recent_context": [
                {
                    "user_id": str(item.get("user_id", "")),
                    "text": str(item.get("text", "")),
                    "is_latest": bool(item.get("is_latest", False)),
                }
                for item in window_messages
            ],
        }

        messages = [
            self.ai_client.build_text_message("system", self._build_system_prompt()),
            self.ai_client.build_text_message("user", json.dumps(payload, ensure_ascii=False)),
        ]
        try:
            async def run_chat():
                return await self.ai_client.chat_completion(messages=messages, temperature=0.1)

            if self.model_invocation_router is not None:
                response = await self.model_invocation_router.submit(
                    purpose=ModelInvocationType.EMOJI_REPLY_DECISION,
                    trace_id=trace_id,
                    session_key=session_key,
                    message_id=message_id,
                    label="表情跟进决策",
                    runner=run_chat,
                )
            else:
                response = await run_chat()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[表情服务] 表情包回复意图判断失败")
            return EmojiReplyDecision(should_send=False, reason=f"decision_error:{exc}")

        data = self._extract_json_object(getattr(response, "content", ""))
        should_send = bool(data.get("should_send", False))
        target_tone = str(data.get("tone", "")).strip()
        target_emotion = str(data.get("emotion", "")).strip()
        target_intent = str(data.get("intent", "")).strip()
        reason = str(data.get("reason", "")).strip()

        if target_tone not in self.reply_tones:
            target_tone = ""
        if not target_emotion or not str(target_emotion).strip():
            target_emotion = ""
        if not target_intent and target_tone and target_emotion:
            target_intent = f"{target_tone}-{target_emotion}"
        if target_intent and target_tone and target_emotion:
            expected_intent = f"{target_tone}-{target_emotion}"
            if target_intent != expected_intent:
                target_intent = expected_intent

        return EmojiReplyDecision(
            should_send=bool(should_send and (target_tone or target_emotion or target_intent)),
            target_tone=target_tone,
            target_emotion=target_emotion,
            target_intent=target_intent,
            reason=reason,
        )

    def _weighted_pick(self, candidates: List[EmojiRecord]) -> Optional[EmojiRecord]:
        if not candidates:
            return None
        weights = [max(0.01, float(item.manual_weight) / (1.0 + float(item.auto_reply_count))) for item in candidates]
        return self._rng.choices(candidates, weights=weights, k=1)[0]

    def _group_cooldown_active(self, group_id: Optional[int]) -> bool:
        if group_id is None:
            return False
        last_sent = self._group_last_sent_at.get(str(group_id), 0.0)
        return (time.monotonic() - last_sent) < self.cooldown_seconds

    def _emoji_cooldown_active(self, emoji: EmojiRecord) -> bool:
        if not emoji.last_auto_reply_at:
            return False
        try:
            last_ts = self._parse_utc_timestamp(emoji.last_auto_reply_at)
        except ValueError:
            return False
        return (time.time() - last_ts) < self.cooldown_seconds

    def _parse_utc_timestamp(self, value: str) -> float:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("empty timestamp")
        return __import__("datetime").datetime.fromisoformat(normalized).timestamp()

    def _build_system_prompt(self) -> str:
        return self.template_loader.render(
            "emoji_reply.prompt",
            emotion_labels=" / ".join(self.emotion_labels),
            reply_tones=" / ".join(self.reply_tones),
        )

    def _extract_json_object(self, content: str) -> Dict[str, Any]:
        text = str(content or "").strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

        fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
        if fenced_match:
            return json.loads(fenced_match.group(1))

        json_match = re.search(r"\{.*\}", text, re.S)
        if json_match:
            return json.loads(json_match.group(0))
        return {}

    def _skip(
        self,
        reason: str,
        *,
        decision: Optional[EmojiReplyDecision] = None,
    ) -> EmojiReplySelection:
        if self.runtime_metrics and reason != "no_candidate":
            self.runtime_metrics.record_emoji_reply_skipped(1)
        return EmojiReplySelection(decision=decision or EmojiReplyDecision(), skip_reason=reason)
