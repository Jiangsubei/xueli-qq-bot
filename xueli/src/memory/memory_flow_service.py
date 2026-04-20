from __future__ import annotations

import logging
from typing import Callable
from typing import TYPE_CHECKING, Any

from src.core.config import CharacterGrowthConfig, MemoryDisputeConfig
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
        if not self.memory_manager or not str(prepared.original_user_message or "").strip():
            return
        try:
            dialogue_key = host._get_conversation_key(event)
            self.memory_manager.register_dialogue_turn(
                user_id=str(event.user_id),
                user_message=prepared.original_user_message,
                assistant_message=reply_text,
                dialogue_key=dialogue_key,
                message_type=event.message_type,
                group_id=str(event.group_id or ""),
                message_id=str(event.message_id or ""),
            )
            scheduler = getattr(self.memory_manager, "schedule_memory_extraction", None)
            if callable(scheduler):
                task = scheduler(
                    str(event.user_id),
                    dialogue_key=dialogue_key,
                    message_type=event.message_type,
                    group_id=str(event.group_id or ""),
                )
                self._schedule_post_extraction_processing(
                    host=host,
                    event=event,
                    task=task,
                )
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
                record = self.evidence_store.add_record(
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
                    self.evidence_store.build_signal(
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
        self.character_card_service.refresh_snapshot(user_id)
