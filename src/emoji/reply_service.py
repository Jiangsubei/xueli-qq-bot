from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any, Dict, List, Optional

from src.core.config import AppConfig
from src.core.models import MessageEvent, MessageType
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
    ) -> None:
        self.repository = repository
        self.ai_client = ai_client
        self.runtime_metrics = runtime_metrics
        self.app_config = app_config
        emoji_config = app_config.emoji
        self.enabled = bool(emoji_config.enabled and getattr(emoji_config, "reply_enabled", True))
        self.cooldown_seconds = float(getattr(emoji_config, "reply_cooldown_seconds", 180.0))
        self.emotion_labels = list(getattr(emoji_config, "emotion_labels", DEFAULT_EMOTION_LABELS))
        self.reply_tones = list(DEFAULT_REPLY_TONES)
        self._rng = random.Random()
        self._group_last_sent_at: Dict[str, float] = {}

    async def plan_follow_up(
        self,
        *,
        event: MessageEvent,
        user_message: str,
        assistant_reply: str,
        reply_context: Optional[Dict[str, Any]] = None,
    ) -> EmojiReplySelection:
        if not self.enabled:
            return self._skip("feature_disabled")
        if event.message_type != MessageType.GROUP.value:
            return self._skip("unsupported_message_type")
        if not assistant_reply.strip():
            return self._skip("empty_reply")
        if self._group_cooldown_active(event.group_id):
            return self._skip("group_cooldown")

        decision = await self._decide_reply_intent(
            user_message=user_message,
            assistant_reply=assistant_reply,
            reply_context=reply_context,
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
        if event.group_id is not None:
            self._group_last_sent_at[str(event.group_id)] = time.monotonic()
        record = await self.repository.mark_auto_reply_sent(selection.emoji.emoji_id)
        if self.runtime_metrics:
            self.runtime_metrics.record_emoji_reply_sent(1)
        return record

    async def get_image_path(self, selection: EmojiReplySelection) -> Optional[str]:
        if not selection.emoji:
            return None
        return await self.repository.get_image_path(selection.emoji.emoji_id)

    async def _decide_reply_intent(
        self,
        *,
        user_message: str,
        assistant_reply: str,
        reply_context: Optional[Dict[str, Any]],
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
            response = await self.ai_client.chat_completion(messages=messages, temperature=0.1)
        except Exception as exc:
            logger.warning("表情包回复意图判断失败：%s", exc)
            return EmojiReplyDecision(should_send=False, reason=f"decision_error:{exc}")

        data = self._extract_json_object(getattr(response, "content", ""))
        should_send = bool(data.get("should_send", False))
        target_tone = str(data.get("tone", "")).strip()
        target_emotion = str(data.get("emotion", "")).strip()
        target_intent = str(data.get("intent", "")).strip()
        reason = str(data.get("reason", "")).strip()

        if target_tone not in self.reply_tones:
            target_tone = ""
        if target_emotion not in self.emotion_labels:
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
        labels = " / ".join(self.emotion_labels)
        tones = " / ".join(self.reply_tones)
        return (
            "你是群聊表情包回复决策助手。请同时结合用户语气、最近群聊上下文和助手最终回复，判断是否适合补发一张表情包。\n"
            "只输出 JSON 对象，不要输出 markdown 或解释。\n"
            "JSON 结构必须是："
            '{"should_send":true,"tone":"安慰","emotion":"委屈","intent":"安慰-委屈","reason":"一句简短理由"}\n'
            f"tone 只能从这些标签里选：{tones}\n"
            f"emotion 只能从这些标签里选：{labels}\n"
            "如果不适合发表情包，should_send 设为 false，tone/emotion/intent 置空。\n"
            "严肃问答、命令结果、冗长说明、风险提示场景默认不要发表情包。"
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
