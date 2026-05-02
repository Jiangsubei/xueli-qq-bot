from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.core.config import MemoryDisputeConfig
from src.core.models import FactEvidenceRecord
from src.memory.memory_dispute_resolver import MemoryDisputeResolver
from src.memory.storage.fact_evidence_store import FactEvidenceStore

if TYPE_CHECKING:
    from src.handlers.reply_pipeline import PreparedReplyRequest

logger = logging.getLogger(__name__)


class MemoryFlowService:
    """Coordinate reply-side memory effects without leaking them into prompt compilation."""

    def __init__(
        self,
        memory_manager: Any = None,
        *,
        dispute_config: MemoryDisputeConfig | None = None,
        evidence_store: FactEvidenceStore | None = None,
        character_card_service: Any = None,
    ) -> None:
        self.memory_manager = memory_manager
        self.dispute_config = dispute_config or MemoryDisputeConfig()
        self.evidence_store = evidence_store
        self.character_card_service = character_card_service
        self.dispute_resolver = MemoryDisputeResolver(self.dispute_config)

    def on_reply_generated(
        self,
        *,
        host: Any,
        event: Any,
        prepared: "PreparedReplyRequest",
        reply_text: str,
    ) -> None:
        """回复生成后的副作用处理（记忆写入调度）。

        负责：
        1. 有效性检查（无文本且无图片则跳过）
        2. 注册本轮对话（user ↔ assistant）到对话历史
        3. 提取图片描述并一并注册
        4. 调度后续记忆提取任务（异步）
        5. 记录角色成长数据
        所有异常均捕获并记录日志，不向上传播以避免污染主流程。
        """
        has_text = bool(str(prepared.original_user_message or "").strip())
        has_image = bool(prepared.base64_images)
        if not self.memory_manager or (not has_text and not has_image):
            return
        try:
            dialogue_key = host._get_conversation_key(event)

            # 从 vision_analysis 提取图片描述（用于记忆存档）
            image_description = ""
            if prepared.message_context and prepared.message_context.vision_analysis:
                va = prepared.message_context.vision_analysis
                image_description = str(va.get("merged_description") or "").strip()
                if not image_description:
                    parts = [str(p).strip() for p in (va.get("per_image_descriptions") or []) if str(p).strip()]
                    if parts:
                        image_description = "；".join(parts)

            # 注册本轮对话到记忆系统
            raw_data = getattr(event, "raw_data", None)
            if raw_data is not None:
                group_id_value = str(raw_data.get("group_id", "") or "")
            else:
                group_id_value = str(getattr(event, "group_id", "") or "")
            self.memory_manager.register_dialogue_turn(
                user_id=str(event.user_id),
                user_message=prepared.original_user_message,
                assistant_message=reply_text,
                dialogue_key=dialogue_key,
                message_type=event.message_type,
                group_id=group_id_value,
                message_id=str(event.message_id or ""),
                image_description=image_description,
            )

            # 调度异步记忆提取（基于阈值触发或定时）
            scheduler = getattr(self.memory_manager, "schedule_memory_extraction", None)
            if callable(scheduler):
                task = scheduler(
                    str(event.user_id),
                    dialogue_key=dialogue_key,
                    message_type=event.message_type,
                    group_id=group_id_value,
                )
                self._schedule_post_extraction_processing(
                    host=host,
                    event=event,
                    task=task,
                )

            # 角色人设成长记录
            self._record_character_growth(host=host, event=event, prepared=prepared, reply_text=reply_text)
        except Exception as exc:
            logger.warning("记录记忆副作用失败：%s", exc, exc_info=True)

    def _schedule_post_extraction_processing(self, *, host: Any, event: Any, task: Any) -> None:
        if task is None:
            return

        def _callback(_task: Any) -> None:
            try:
                loop = getattr(_task, "get_loop", lambda: None)()
                if loop is None:
                    return
                loop.create_task(self._process_memory_disputes(host=host, event=event))
            except Exception as exc:
                logger.warning("调度记忆争议处理失败：%s", exc, exc_info=True)

        add_done = getattr(task, "add_done_callback", None)
        if callable(add_done):
            add_done(_callback)

    async def _process_memory_disputes(self, *, host: Any, event: Any) -> None:
        if not self.memory_manager or self.evidence_store is None or not self.dispute_config.enabled:
            return
        try:
            user_id = str(event.user_id)
            ordinary_memories = await self.memory_manager.get_user_memories(user_id)
            important_loader = getattr(self.memory_manager, "get_important_memories", None)
            important_memories = await important_loader(user_id, min_priority=1, limit=50) if callable(important_loader) else []
            all_memories = [("ordinary", item) for item in ordinary_memories] + [("important", item) for item in important_memories]
            existing_records = self.evidence_store.list_records(user_id)
            existing_memory_ids = {str(item.source_memory_id or "") for item in existing_records}
            for memory_type, item in all_memories:
                metadata = dict(getattr(item, "metadata", {}) or {})
                memory_id = str(getattr(item, "id", "") or "")
                if not memory_id or memory_id in existing_memory_ids:
                    continue
                decision = self.dispute_resolver.resolve_from_memory_metadata(metadata)
                if decision.level == "ignore":
                    continue
                record = await self.evidence_store.add_record_async(
                    FactEvidenceRecord(
                        record_id="",
                        user_id=user_id,
                        source_memory_id=memory_id,
                        source_memory_type=memory_type,
                        decision_level=decision.level,
                        confidence=decision.confidence,
                        action=decision.action,
                        conflict_type=decision.conflict_type,
                        summary=decision.summary,
                        reason=decision.reason,
                        targets=list(decision.targets or []),
                        evidence=list(decision.evidence or []),
                        metadata={"content": str(getattr(item, "content", "") or "")},
                    )
                )
                if decision.level == "high_confidence":
                    await self.evidence_store.build_signal_async(
                        user_id=user_id,
                        record=record,
                        ttl_hours=self.dispute_config.signal_ttl_hours,
                    )
        except Exception as exc:
            logger.warning("后台记忆争议处理失败：%s", exc, exc_info=True)

    def _record_character_growth(self, *, host: Any, event: Any, prepared: "PreparedReplyRequest", reply_text: str) -> None:
        del reply_text
        if self.character_card_service is None:
            return
        user_id = str(event.user_id)
        self.character_card_service.record_explicit_feedback(user_id, prepared.original_user_message)
        message_context = getattr(prepared, "message_context", None)
        chat_mode = str(getattr(event, "message_type", "private") or "private").strip().lower()
        if chat_mode == "group":
            self.character_card_service.record_interaction_signal(user_id, "group_light_presence")
        else:
            self.character_card_service.record_interaction_signal(user_id, "private_continue")
        final_style = getattr(message_context, "final_style_guide", None)
        if final_style and "接住" in str(getattr(final_style, "warmth_guidance", "") or ""):
            self.character_card_service.record_interaction_signal(user_id, "comfort_acceptance")
        estimated_tone = self._estimate_user_emotion(prepared.original_user_message)
        if estimated_tone:
            self.character_card_service.record_emotion(user_id, estimated_tone)
        self.character_card_service.refresh_snapshot(user_id)
        self._update_relationship(host=host, event=event, prepared=prepared, user_id=user_id, estimated_tone=estimated_tone)

    def _update_relationship(self, *, host: Any, event: Any, prepared: "PreparedReplyRequest", user_id: str, estimated_tone: str) -> None:
        del host, event, prepared
        if self.character_card_service is None:
            return
        if not self.character_card_service.config.relationship_tracking_enabled:
            return
        is_friction = estimated_tone in {"生气", "无语", "委屈"}
        if estimated_tone in {"开心", "喜欢", "惊讶"}:
            delta = self.character_card_service.config.intimacy_gain_per_high_quality
        elif is_friction:
            delta = -self.character_card_service.config.intimacy_loss_per_friction
        else:
            delta = self.character_card_service.config.intimacy_gain_per_high_quality * 0.5
        self.character_card_service.update_intimacy(user_id=user_id, delta=delta, is_friction=is_friction)

    @staticmethod
    def _estimate_user_emotion(text: str) -> str:
        """Lightweight emotion estimation via keyword matching (no LLM call)."""
        normalized = str(text or "").strip()
        if not normalized:
            return ""
        patterns = {
            "伤心": ["好难过", "难受", "哭了", "想哭", "崩溃", "绝望", "好苦", "好累", "心累"],
            "生气": ["气死", "好气", "无语死了", "真无语", "烦死了", "受够了", "滚", "傻逼"],
            "开心": ["哈哈", "太好了", "真棒", "开心", "高兴", "嘻嘻", "嘿嘿", "好耶", "！"],
            "惊讶": ["不会吧", "真的假的", "居然", "竟然", "我靠", "震惊"],
            "困惑": ["什么意思", "没懂", "不懂", "搞不懂", "怎么样", "什么情况"],
            "平静": ["嗯", "好", "行", "没问题", "知道了"],
        }
        for tone, keywords in patterns.items():
            if any(kw in normalized for kw in keywords):
                return tone
        return ""
