from __future__ import annotations

from typing import Any, Dict, List

from src.core.message_trace import get_execution_key
from src.core.models import CharacterCardSnapshot, ConversationContextItem
from src.handlers.conversation_engagement import build_companionship_signals
from src.handlers.conversation_timeline_formatter import ConversationTimelineFormatter
from src.handlers.message_context import MessageContext
from src.handlers.reply_style_policy import ReplyStylePolicy
from src.core.pipeline_errors import ImageProcessingError, wrap_image_error


class ConversationContextBuilder:
    """Build structured reply context shared by timing, prompt rendering, and memory."""

    def __init__(
        self,
        host: Any,
        *,
        timeline_formatter: ConversationTimelineFormatter | None = None,
        style_policy: ReplyStylePolicy | None = None,
    ) -> None:
        self.host = host
        self.timeline_formatter = timeline_formatter or ConversationTimelineFormatter()
        self.style_policy = style_policy or ReplyStylePolicy()

    async def build(
        self,
        event: Any,
        *,
        plan: Any = None,
        trace_id: str = "",
        include_memory: bool = True,
    ) -> MessageContext:
        reply_context = dict(getattr(plan, "reply_context", None) or {})
        context_event = reply_context.get("window_event") if getattr(reply_context.get("window_event"), "message_type", None) else event
        user_message = str(reply_context.get("merged_user_message") or self.host.extract_user_message(context_event)).strip()
        execution_key = get_execution_key(context_event)
        conversation_key = self.host._get_conversation_key(context_event)
        conversation = self.host._get_conversation(conversation_key)
        window_messages = list(reply_context.get("window_messages") or [])
        temporal_context = self.host._build_temporal_context(
            event=context_event,
            conversation=conversation,
            reply_context=reply_context,
        )

        person_fact_context = ""
        persistent_memory_context = ""
        session_restore_context = ""
        precise_recall_context = ""
        dynamic_memory_context = ""
        related_history_messages: List[Dict[str, Any]] = []
        is_first_turn = len(conversation.messages) == 0
        if include_memory:
            (
                person_fact_context,
                persistent_memory_context,
                session_restore_context,
                precise_recall_context,
                dynamic_memory_context,
                related_history_messages,
                is_first_turn,
            ) = await self.host._load_memory_context(
                event=context_event,
                user_message=user_message,
                conversation=conversation,
                plan=plan,
            )

        base64_images: List[str] = []
        vision_analysis = self.host.reply_pipeline._extract_reusable_vision_analysis(event=context_event, plan=plan)
        if self.host._has_image_input(context_event) and not vision_analysis and self.host.vision_enabled():
            try:
                base64_images = await self.host.download_images(context_event)
                if base64_images:
                    vision_analysis = await self.host.analyze_event_images(
                        context_event,
                        user_message,
                        base64_images=base64_images,
                        trace_id=trace_id,
                    )
            except ImageProcessingError:
                raise
            except Exception as exc:
                raise wrap_image_error(exc)

        prompt_plan = getattr(plan, "prompt_plan", None)
        planning_signals = dict(reply_context.get("planning_signals") or {})
        if not planning_signals:
            previous_role = ""
            previous_user_id = str(context_event.user_id)
            if conversation.messages:
                previous_role = str(conversation.messages[-1].get("role") or "").strip().lower()
            planning_signals = build_companionship_signals(
                user_message,
                current_user_id=context_event.user_id,
                previous_speaker_role=previous_role,
                previous_user_id=previous_user_id,
                recent_gap_bucket=str(getattr(temporal_context, "recent_gap_bucket", "unknown") or "unknown"),
                recent_history_count=len(conversation.messages),
            )
        rendered_recent_history = self.timeline_formatter.render_recent_history(
            window_messages=window_messages,
            prompt_plan=prompt_plan,
            temporal_context=temporal_context,
            chat_mode=str(getattr(context_event, "message_type", "private") or "private"),
        )
        rendered_timeline_summary = self.timeline_formatter.render_summary(temporal_context)
        context_items = self.timeline_formatter.build_items(window_messages)
        context_items.extend(self._memory_items("person_fact", person_fact_context, count_in_context=False))
        context_items.extend(self._memory_items("session_restore", session_restore_context, count_in_context=False))
        context_items.extend(self._memory_items("precise_recall", precise_recall_context, count_in_context=False))
        context_items.extend(self._memory_items("dynamic_memory", dynamic_memory_context, count_in_context=False))
        context_items.extend(self._memory_items("reference_note", persistent_memory_context, count_in_context=False))
        if str((vision_analysis or {}).get("merged_description", "") or "").strip():
            context_items.append(
                ConversationContextItem(
                    kind="vision_note",
                    text=str(vision_analysis.get("merged_description") or "").strip(),
                    count_in_context=False,
                )
            )
        if rendered_timeline_summary:
            context_items.append(
                ConversationContextItem(
                    kind="system_note",
                    text=rendered_timeline_summary,
                    count_in_context=False,
                )
            )
        soft_uncertainty_signals = []
        evidence_store = getattr(self.host, "fact_evidence_store", None)
        if evidence_store is not None:
            loader = getattr(evidence_store, "get_active_signals", None)
            if callable(loader):
                soft_uncertainty_signals = list(loader(str(context_event.user_id), limit=3) or [])
        character_card_snapshot = getattr(self.host, "get_character_card_snapshot", lambda _user_id: None)(str(context_event.user_id))
        message_context = MessageContext(
            trace_id=trace_id,
            execution_key=execution_key,
            conversation_key=conversation_key,
            user_message=user_message,
            current_sender_label=self.host._format_identity_label(context_event.user_id, self.host._get_sender_display_name(context_event)),
            is_first_turn=is_first_turn,
            current_event_time=temporal_context.current_event_time,
            previous_message_time=temporal_context.previous_message_time,
            conversation_last_time=temporal_context.conversation_last_time,
            previous_session_time=temporal_context.previous_session_time,
            temporal_context=temporal_context,
            context_items=context_items,
            window_messages=window_messages,
            recent_history_text=rendered_recent_history or self.host.reply_pipeline._build_recent_history_text(
                event=context_event,
                conversation=conversation,
                plan=plan,
            ),
            rendered_recent_history=rendered_recent_history,
            rendered_timeline_summary=rendered_timeline_summary,
            rendered_memory_sections={
                "person_facts": person_fact_context,
                "persistent_memory": persistent_memory_context,
                "session_restore": session_restore_context,
                "precise_recall": precise_recall_context,
                "dynamic_memory": dynamic_memory_context,
            },
            base64_images=base64_images,
            vision_analysis=vision_analysis,
            person_fact_context=person_fact_context,
            persistent_memory_context=persistent_memory_context,
            session_restore_context=session_restore_context,
            precise_recall_context=precise_recall_context,
            dynamic_memory_context=dynamic_memory_context,
            related_history_messages=related_history_messages,
            reply_context=reply_context,
            direct_reply_text=str(reply_context.get("direct_reply_text") or "").strip(),
            planning_signals=planning_signals,
            soft_uncertainty_signals=soft_uncertainty_signals,
            character_card_snapshot=character_card_snapshot or CharacterCardSnapshot(),
            window_reason=str(reply_context.get("window_reason") or ""),
            prompt_plan=prompt_plan,
            conversation=conversation,
        )
        message_context.final_style_guide = self.style_policy.build(
            prompt_plan=prompt_plan,
            temporal_context=temporal_context,
            chat_mode=str(getattr(context_event, "message_type", "private") or "private"),
            planner_reason=str(getattr(plan, "reason", "") or ""),
            planning_signals=message_context.planning_signals,
            soft_uncertainty_signals=message_context.soft_uncertainty_signals,
            character_card_snapshot=message_context.character_card_snapshot,
        )
        return message_context

    def _memory_items(self, kind: str, content: str, *, count_in_context: bool) -> List[ConversationContextItem]:
        items: List[ConversationContextItem] = []
        for raw_line in str(content or "").splitlines():
            text = str(raw_line or "").strip()
            if not text:
                continue
            items.append(ConversationContextItem(kind=kind, text=text, count_in_context=count_in_context))
        return items
