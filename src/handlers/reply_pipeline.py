from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Tuple

from src.core.config import AppConfig
from src.core.models import Conversation, MessageEvent, MessageHandlingPlan, MessageType
from src.core.runtime_metrics import RuntimeMetrics
from src.services.ai_client import AIAPIError, AIResponse

logger = logging.getLogger(__name__)


@dataclass
class PreparedReplyRequest:
    original_user_message: str
    model_user_message: str
    history_user_message: str
    base64_images: List[str]
    conversation: Conversation
    related_history_messages: List[Dict[str, Any]]
    messages: List[Dict[str, Any]]
    fallback_response: Optional[str] = None


@dataclass
class ReplyResult:
    text: str
    source: str = "ai"


class ReplyPipelineHost(Protocol):
    ai_client: Any
    memory_manager: Any
    app_config: AppConfig
    runtime_metrics: Optional[RuntimeMetrics]

    async def download_images(self, event: MessageEvent) -> List[str]: ...
    async def analyze_event_images(self, event: MessageEvent, user_text: str, base64_images: Optional[List[str]] = None) -> Dict[str, Any]: ...
    def vision_enabled(self) -> bool: ...
    def _get_conversation_key(self, event: MessageEvent) -> str: ...
    def _get_conversation(self, key: str) -> Conversation: ...
    def _build_assistant_identity_prompt(self) -> str: ...
    def _format_group_window_context(self, reply_context: Optional[Dict[str, Any]]) -> str: ...
    def _build_system_prompt(self) -> str: ...
    def _format_system_prompt_log_with_history(self, event: MessageEvent, messages: List[Dict[str, Any]], related_history_messages: Optional[List[Dict[str, Any]]] = None) -> str: ...


class ReplyPipeline:
    def __init__(self, host: ReplyPipelineHost):
        self.host = host

    async def prepare_request(self, *, event: MessageEvent, user_message: str, plan: Optional[MessageHandlingPlan] = None) -> PreparedReplyRequest:
        original_user_message = user_message
        base64_images = await self._download_images_if_needed(event)
        conversation = self.host._get_conversation(self.host._get_conversation_key(event))
        memory_context, related_history_messages, is_first_turn = await self.load_memory_context(
            event=event,
            user_message=original_user_message,
            conversation=conversation,
        )
        vision_analysis = await self._resolve_vision_analysis(
            event=event,
            user_message=original_user_message,
            base64_images=base64_images,
            plan=plan,
        )
        system_prompt = self.build_response_system_prompt(
            event=event,
            memory_context=memory_context,
            is_first_turn=is_first_turn,
            plan=plan,
        )
        model_user_message = self._build_model_user_message(
            original_user_message=original_user_message,
            vision_analysis=vision_analysis,
        )
        history_user_message = self._build_history_user_message(
            original_user_message=original_user_message,
            vision_analysis=vision_analysis,
            has_image=event.has_image(),
        )
        fallback_response = self._build_fallback_response(
            event=event,
            original_user_message=original_user_message,
            vision_analysis=vision_analysis,
        )
        messages: List[Dict[str, Any]] = []
        if fallback_response is None:
            messages = self.build_response_messages(
                system_prompt=system_prompt,
                user_message=model_user_message,
                base64_images=base64_images,
                related_history_messages=related_history_messages,
            )
        return PreparedReplyRequest(
            original_user_message=original_user_message,
            model_user_message=model_user_message,
            history_user_message=history_user_message,
            base64_images=base64_images,
            conversation=conversation,
            related_history_messages=related_history_messages,
            messages=messages,
            fallback_response=fallback_response,
        )

    async def execute(self, *, event: MessageEvent, user_message: str, plan: Optional[MessageHandlingPlan] = None) -> ReplyResult:
        prepared = await self.prepare_request(event=event, user_message=user_message, plan=plan)
        self._log_prompt_if_enabled(event, prepared)
        try:
            source = "fallback" if prepared.fallback_response is not None else "ai"
            response = AIResponse(content=prepared.fallback_response) if prepared.fallback_response is not None else await self._request_model_reply(event, prepared)
            self._persist_reply_result(event, prepared, response)
            logger.info(
                "[reply] reply generated: user=%s group=%s length=%s",
                event.user_id,
                event.group_id,
                len(response.content),
            )
            return ReplyResult(text=response.content, source=source)
        except AIAPIError as exc:
            logger.error("[reply] AI request failed: %s", exc)
            return ReplyResult(text=f"AI 服务暂时不可用，请稍后再试。\n错误信息: {exc}", source="fallback")
        except Exception as exc:
            logger.error("[reply] unexpected reply failure: %s", exc, exc_info=True)
            return ReplyResult(text="处理消息时出错，请稍后再试。", source="fallback")

    async def _download_images_if_needed(self, event: MessageEvent) -> List[str]:
        if not event.has_image() or not self._vision_enabled():
            return []
        logger.info("[reply] processing images")
        return await self.host.download_images(event)

    def _log_prompt_if_enabled(self, event: MessageEvent, prepared: PreparedReplyRequest) -> None:
        if not self.host.app_config.bot_behavior.log_full_prompt:
            return
        if not prepared.messages:
            logger.info(
                "[SYSTEM PROMPT]\nuser=%s\nconversation=%s\n[vision fallback reply without model request]",
                event.user_id,
                self.host._get_conversation_key(event),
            )
            return
        logger.info(
            self.host._format_system_prompt_log_with_history(
                event,
                prepared.messages,
                related_history_messages=prepared.related_history_messages,
            )
        )

    async def _request_model_reply(self, event: MessageEvent, prepared: PreparedReplyRequest) -> AIResponse:
        logger.info(
            "[reply] sending request: user=%s group=%s images=%s history=%s multimodal=%s",
            event.user_id,
            event.group_id,
            len(prepared.base64_images),
            len(prepared.related_history_messages),
            self._should_use_multimodal_reply(prepared.base64_images),
        )
        return await self.chat_with_tools(messages=prepared.messages, user_id=str(event.user_id), temperature=0.7)

    def _persist_reply_result(self, event: MessageEvent, prepared: PreparedReplyRequest, response: AIResponse) -> None:
        prepared.conversation.add_message("user", prepared.history_user_message)
        prepared.conversation.add_message("assistant", response.content)
        if not self.host.memory_manager or not prepared.original_user_message.strip():
            return
        try:
            dialogue_key = self.host._get_conversation_key(event)
            self.host.memory_manager.register_dialogue_turn(
                user_id=str(event.user_id),
                user_message=prepared.original_user_message,
                assistant_message=response.content,
                dialogue_key=dialogue_key,
                message_type=event.message_type,
                group_id=str(event.group_id or ""),
                message_id=str(event.message_id or ""),
            )
            self._schedule_memory_extraction(
                str(event.user_id),
                dialogue_key=dialogue_key,
                message_type=event.message_type,
                group_id=str(event.group_id or ""),
            )
        except Exception as exc:
            logger.warning("[reply] failed to register memory side effects: %s", exc, exc_info=True)

    def _schedule_memory_extraction(
        self,
        user_id: str,
        *,
        dialogue_key: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
    ) -> None:
        scheduler = getattr(self.host.memory_manager, "schedule_memory_extraction", None)
        if callable(scheduler):
            scheduler(
                user_id,
                dialogue_key=dialogue_key,
                message_type=message_type,
                group_id=group_id,
            )

    def build_memory_tools(self) -> List[Dict[str, Any]]:
        if not self.host.memory_manager:
            return []
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_memories",
                    "description": "Search the long-term memory store for relevant memories.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
                        },
                        "required": ["query"],
                    },
                },
            }
        ]

    def augment_system_prompt_for_tools(self, system_prompt: str, tools: List[Dict[str, Any]]) -> str:
        if not tools:
            return system_prompt
        return (
            f"{system_prompt}\n\n"
            "在回答前，如果你明确需要回忆长期记忆，可以调用 search_memories 工具。"
            "只有在确实能帮助回答时才调用，不要滥用。"
        )
    async def execute_tool_call(self, tool_call: Dict[str, Any], user_id: str) -> Dict[str, Any]:
        function_payload = tool_call.get("function") or {}
        name = str(function_payload.get("name", "")).strip()
        arguments_text = str(function_payload.get("arguments", "") or "{}").strip()
        try:
            arguments = json.loads(arguments_text) if arguments_text else {}
        except json.JSONDecodeError:
            arguments = {}

        if name != "search_memories" or not self.host.memory_manager:
            return {"tool_call_id": tool_call.get("id", ""), "role": "tool", "name": name or "unknown", "content": "工具不可用"}

        query = str(arguments.get("query", "")).strip()
        top_k = int(arguments.get("top_k", 5) or 5)
        if not query:
            content = "未提供查询内容"
        else:
            try:
                payload = await self.host.memory_manager.search_memories_with_context(
                    user_id=user_id,
                    query=query,
                    top_k=max(1, min(top_k, 10)),
                    include_conversations=True,
                    read_scope=self._get_memory_read_scope(),
                )
                memory_lines = []
                for index, item in enumerate(payload.get("memories", [])[:top_k], start=1):
                    content_text = getattr(item, "content", None)
                    if content_text is None and isinstance(item, dict):
                        content_text = item.get("content", "")
                    owner = getattr(item, "owner_user_id", None)
                    if owner is None and isinstance(item, dict):
                        owner = item.get("owner_user_id", "")
                    normalized = self._format_legacy_important_memories([{"content": content_text or "", "owner_user_id": owner or ""}])
                    if normalized:
                        memory_lines.append(f"{index}. {normalized.splitlines()[-1]}")
                content = "\n".join(memory_lines) if memory_lines else "没有找到相关记忆"
            except Exception as exc:
                logger.warning("[reply] tool call failed: %s", exc)
                content = f"检索失败: {exc}"

        return {
            "tool_call_id": tool_call.get("id", ""),
            "role": "tool",
            "name": name,
            "content": content,
        }

    async def chat_with_tools(self, messages: List[Dict[str, Any]], user_id: str, temperature: float = 0.7) -> AIResponse:
        tools = self.build_memory_tools()
        request_messages = list(messages)
        if tools:
            request_messages[0] = {
                "role": "system",
                "content": self.augment_system_prompt_for_tools(str(request_messages[0].get("content", "")), tools),
            }
        response = await self.host.ai_client.chat_completion(
            messages=request_messages,
            temperature=temperature,
            tools=tools or None,
        )
        tool_calls = list(getattr(response, "tool_calls", None) or [])
        if not tool_calls:
            return response

        follow_up_messages = list(request_messages)
        follow_up_messages.append(
            {
                "role": "assistant",
                "content": response.content,
                "tool_calls": tool_calls,
            }
        )
        for tool_call in tool_calls:
            follow_up_messages.append(await self.execute_tool_call(tool_call, user_id))
        return await self.host.ai_client.chat_completion(messages=follow_up_messages, temperature=temperature)

    async def load_memory_context(
        self,
        *,
        event: MessageEvent,
        user_message: str,
        conversation: Conversation,
    ) -> Tuple[str, List[Dict[str, Any]], bool]:
        if not self.host.memory_manager:
            return "", [], len(conversation.messages) == 0

        is_first_turn = len(conversation.messages) == 0
        access_context = self._build_memory_access_context(event)
        if is_first_turn:
            try:
                important = await self.host.memory_manager.get_important_memories(
                    user_id=str(event.user_id),
                    limit=5,
                    read_scope=self._get_memory_read_scope(),
                    access_context=access_context,
                )
            except Exception as exc:
                logger.warning("[reply] failed to load important memories: %s", exc)
                important = []
            formatted = self._format_legacy_important_memories(important)
            return formatted, [], True

        try:
            payload = await self.host.memory_manager.search_memories_with_context(
                user_id=str(event.user_id),
                query=user_message,
                top_k=5,
                include_conversations=True,
                read_scope=self._get_memory_read_scope(),
                access_context=access_context,
            )
        except Exception as exc:
            logger.warning("[reply] failed to search memory context: %s", exc)
            return await self._load_legacy_memory_context(event=event, user_message=user_message, conversation=conversation)

        memories = payload.get("memories", []) if isinstance(payload, dict) else []
        history_messages = payload.get("history_messages", []) if isinstance(payload, dict) else []
        memory_context = self._format_legacy_important_memories(memories)
        return memory_context, list(history_messages or []), False

    async def _load_legacy_memory_context(
        self,
        *,
        event: MessageEvent,
        user_message: str,
        conversation: Conversation,
    ) -> Tuple[str, List[Dict[str, Any]], bool]:
        del user_message
        if not self.host.memory_manager:
            return "", [], len(conversation.messages) == 0
        try:
            important = await self.host.memory_manager.get_important_memories(
                user_id=str(event.user_id),
                limit=5,
                read_scope=self._get_memory_read_scope(),
                access_context=self._build_memory_access_context(event),
            )
        except Exception as exc:
            logger.warning("[reply] legacy memory loading failed: %s", exc)
            important = []
        return self._format_legacy_important_memories(important), [], len(conversation.messages) == 0

    def _format_legacy_important_memories(self, memories: List[Any]) -> str:
        if not memories:
            return ""
        lines = ["=== memory ==="]
        formatter = getattr(self.host, "_format_memory_prompt_entry", None)
        for index, item in enumerate(memories, start=1):
            content = getattr(item, "content", None)
            owner = getattr(item, "owner_user_id", "")
            if content is None and isinstance(item, dict):
                content = item.get("content", "")
                owner = item.get("owner_user_id", "")
            text = str(content or "").strip()
            if not text:
                continue
            if callable(formatter):
                text = formatter(text, str(owner or ""))
            lines.append(f"{index}. {text}")
        return "\n".join(lines) + ("\n" if len(lines) > 1 else "")

    def _get_memory_read_scope(self) -> str:
        getter = getattr(self.host, "_get_memory_read_scope", None)
        if callable(getter):
            return str(getter())
        return getattr(self.host.app_config.memory, "read_scope", "user")

    def _build_memory_access_context(self, event: MessageEvent):
        builder = getattr(self.host.memory_manager, "build_access_context", None)
        if not callable(builder):
            return None
        return builder(
            user_id=str(event.user_id),
            message_type=event.message_type or MessageType.PRIVATE.value,
            group_id=str(event.group_id or ""),
            read_scope=self._get_memory_read_scope(),
        )

    def _build_session_context_prompt(self, event: Optional[MessageEvent]) -> str:
        if event is None:
            return ""
        session_type = str(event.message_type or "").strip().lower() or MessageType.PRIVATE.value
        lines = [
            "【会话】",
            f"会话: {session_type}",
            f"用户ID: {event.user_id}",
        ]
        if session_type == MessageType.GROUP.value and event.group_id is not None:
            lines.append(f"群ID: {event.group_id}")
        return "\n".join(lines)

    def build_response_system_prompt(
        self,
        *,
        event: Optional[MessageEvent] = None,
        memory_context: str,
        is_first_turn: bool,
        plan: Optional[MessageHandlingPlan] = None,
    ) -> str:
        del is_first_turn
        parts = [
            self.host._build_assistant_identity_prompt(),
            self.host._build_system_prompt(),
            self._build_session_context_prompt(event),
        ]
        group_context = self.host._format_group_window_context(getattr(plan, "reply_context", None)) if plan else ""
        if group_context:
            parts.append(group_context)
        if memory_context:
            parts.append(memory_context.strip())
        return "\n\n".join(part for part in parts if str(part or "").strip())

    def build_response_messages(
        self,
        system_prompt: str,
        user_message: str,
        base64_images: List[str],
        related_history_messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        del base64_images
        messages = [self.host.ai_client.build_text_message("system", system_prompt)]
        messages.extend(list(related_history_messages or []))
        messages.append(self.host.ai_client.build_text_message("user", user_message))
        return messages

    async def _resolve_vision_analysis(
        self,
        *,
        event: MessageEvent,
        user_message: str,
        base64_images: List[str],
        plan: Optional[MessageHandlingPlan],
    ) -> Dict[str, Any]:
        reused = self._extract_reusable_vision_analysis(event=event, plan=plan)
        if reused:
            if self.host.runtime_metrics:
                self.host.runtime_metrics.record_vision_request(reused_from_plan=True)
            return reused
        if not event.has_image() or not self._vision_enabled():
            return {}
        return await self.host.analyze_event_images(event, user_message, base64_images=base64_images)

    def _extract_reusable_vision_analysis(self, *, event: MessageEvent, plan: Optional[MessageHandlingPlan]) -> Dict[str, Any]:
        if not plan or not getattr(plan, "reply_context", None):
            return {}
        window_messages = list((plan.reply_context or {}).get("window_messages") or [])
        for item in reversed(window_messages):
            if int(item.get("message_id", 0) or 0) != int(event.message_id or 0):
                continue
            if not bool(item.get("has_image", False)):
                continue
            analysis = {
                "per_image_descriptions": list(item.get("per_image_descriptions") or []),
                "merged_description": str(item.get("merged_description", "") or ""),
                "vision_success_count": int(item.get("vision_success_count", 0) or 0),
                "vision_failure_count": int(item.get("vision_failure_count", 0) or 0),
                "vision_source": str(item.get("vision_source", "") or ""),
                "vision_error": str(item.get("vision_error", "") or ""),
                "vision_available": bool(item.get("vision_available", False)),
            }
            if self._has_usable_vision_analysis(analysis):
                return analysis
        return {}
    def _build_model_user_message(self, *, original_user_message: str, vision_analysis: Dict[str, Any]) -> str:
        return self._compose_vision_augmented_text(
            original_user_message=original_user_message,
            vision_analysis=vision_analysis,
            include_original_label=True,
        )

    def _build_history_user_message(self, *, original_user_message: str, vision_analysis: Dict[str, Any], has_image: bool) -> str:
        if not has_image:
            return original_user_message
        return self._compose_vision_augmented_text(
            original_user_message=original_user_message,
            vision_analysis=vision_analysis,
            include_original_label=False,
        )

    def _build_history_image_failure_note(self, original_user_message: str) -> str:
        if original_user_message.strip():
            return f"{original_user_message}\n[图片未成功识别]"
        return "[图片未成功识别]"

    def _compose_vision_augmented_text(
        self,
        *,
        original_user_message: str,
        vision_analysis: Dict[str, Any],
        include_original_label: bool,
    ) -> str:
        original_text = str(original_user_message or "").strip()
        if not self._has_usable_vision_analysis(vision_analysis):
            if vision_analysis and not original_text:
                return self._build_history_image_failure_note(original_text)
            return original_text

        lines: List[str] = []
        if original_text:
            lines.append(f"用户原文: {original_text}" if include_original_label else original_text)
        merged = str((vision_analysis or {}).get("merged_description", "") or "").strip()
        details = [str(item).strip() for item in (vision_analysis or {}).get("per_image_descriptions", []) if str(item).strip()]
        if merged:
            lines.append("图片摘要: " + merged)
        elif details:
            lines.append("图片摘要: " + "；".join(details))
        if not lines and merged:
            lines.append("图片摘要: " + merged)
        elif not lines and details:
            lines.append("图片摘要: " + "；".join(details))
        return "\n".join(lines).strip()

    def _build_fallback_response(self, *, event: MessageEvent, original_user_message: str, vision_analysis: Dict[str, Any]) -> Optional[str]:
        if not event.has_image():
            return None
        if original_user_message.strip():
            return None
        if self._has_usable_vision_analysis(vision_analysis):
            return None
        return "抱歉我现在看不清图片呢。"

    def _has_usable_vision_analysis(self, vision_analysis: Dict[str, Any]) -> bool:
        if not vision_analysis:
            return False
        if bool(vision_analysis.get("vision_available", False)):
            return True
        if str(vision_analysis.get("merged_description", "") or "").strip():
            return True
        return any(str(item).strip() for item in vision_analysis.get("per_image_descriptions", []) or [])

    def _vision_enabled(self) -> bool:
        checker = getattr(self.host, "vision_enabled", None)
        return bool(checker()) if callable(checker) else False

    def _should_use_multimodal_reply(self, base64_images: List[str]) -> bool:
        del base64_images
        return False
