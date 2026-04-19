from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Tuple

from src.core.config import AppConfig
from src.core.message_trace import format_trace_log
from src.core.model_invocation_router import ModelInvocationType
from src.core.pipeline_errors import classify_pipeline_error
from src.core.models import Conversation, MessageEvent, MessageHandlingPlan, MessageType, PromptPlan, TemporalContext
from src.core.runtime_metrics import RuntimeMetrics
from src.handlers.message_context import MessageContext
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
    message_context: Optional[MessageContext] = None


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
    async def analyze_event_images(
        self,
        event: MessageEvent,
        user_text: str,
        base64_images: Optional[List[str]] = None,
        trace_id: str = "",
    ) -> Dict[str, Any]: ...
    def vision_enabled(self) -> bool: ...
    def _get_conversation_key(self, event: MessageEvent) -> str: ...
    def _get_conversation(self, key: str) -> Conversation: ...
    def _build_assistant_identity_prompt(self) -> str: ...
    def _format_group_window_context(self, reply_context: Optional[Dict[str, Any]]) -> str: ...
    def _build_system_prompt(self) -> str: ...
    def _format_system_prompt_log_with_history(
        self,
        event: MessageEvent,
        messages: List[Dict[str, Any]],
        related_history_messages: Optional[List[Dict[str, Any]]] = None,
        title: str = "[FULL PROMPT]",
        trace_id: str = "",
    ) -> str: ...
    async def build_message_context(
        self,
        event: MessageEvent,
        *,
        plan: Optional[MessageHandlingPlan] = None,
        trace_id: str = "",
        include_memory: bool = True,
    ) -> MessageContext: ...
    model_invocation_router: Any


class ReplyPipeline:
    def __init__(self, host: ReplyPipelineHost):
        self.host = host

    async def prepare_request(
        self,
        *,
        event: MessageEvent,
        user_message: str,
        plan: Optional[MessageHandlingPlan] = None,
        context: Optional[MessageContext] = None,
    ) -> PreparedReplyRequest:
        message_context = context
        if message_context is None:
            message_context = await self.host.build_message_context(
                event,
                plan=plan,
                include_memory=True,
            )
        original_user_message = str(message_context.user_message or user_message or "")
        base64_images = list(message_context.base64_images or [])
        if not base64_images and not message_context.vision_analysis:
            base64_images = await self._download_images_if_needed(event)
        conversation = message_context.conversation or self.host._get_conversation(self.host._get_conversation_key(event))
        recent_history_text = str(message_context.recent_history_text or "").strip() or self._build_recent_history_text(
            event=event,
            conversation=conversation,
            plan=plan,
        )
        person_fact_context = str(message_context.person_fact_context or "")
        persistent_memory_context = str(message_context.persistent_memory_context or "")
        session_restore_context = str(message_context.session_restore_context or "")
        precise_recall_context = str(message_context.precise_recall_context or "")
        dynamic_memory_context = str(message_context.dynamic_memory_context or "")
        related_history_messages = list(message_context.related_history_messages or [])
        is_first_turn = bool(message_context.is_first_turn)
        vision_analysis = dict(message_context.vision_analysis or {})
        if vision_analysis and self.host.runtime_metrics:
            self.host.runtime_metrics.record_vision_request(reused_from_plan=True)
        if not vision_analysis:
            vision_analysis = await self._resolve_vision_analysis(
                event=event,
                user_message=original_user_message,
                base64_images=base64_images,
                plan=plan,
                trace_id=message_context.trace_id if message_context else "",
            )
        model_user_message = self._build_model_user_message(
            original_user_message=original_user_message,
            vision_analysis=vision_analysis,
        )
        prompt_plan = getattr(message_context, "prompt_plan", None) or getattr(plan, "prompt_plan", None)
        system_prompt = self.build_response_system_prompt(
            event=event,
            temporal_context=getattr(message_context, "temporal_context", None),
            prompt_plan=prompt_plan,
            person_fact_context=person_fact_context,
            persistent_memory_context=persistent_memory_context,
            session_restore_context=session_restore_context,
            precise_recall_context=precise_recall_context,
            dynamic_memory_context=dynamic_memory_context,
            is_first_turn=is_first_turn,
            plan=plan,
            recent_history_text=recent_history_text,
            current_message=model_user_message,
        )
        history_user_message = self._build_history_user_message(
            original_user_message=original_user_message,
            vision_analysis=vision_analysis,
            has_image=self._event_has_image(event),
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
            message_context=message_context,
        )

    async def execute(
        self,
        *,
        event: MessageEvent,
        user_message: str,
        plan: Optional[MessageHandlingPlan] = None,
        context: Optional[MessageContext] = None,
    ) -> ReplyResult:
        prepared = await self.prepare_request(event=event, user_message=user_message, plan=plan, context=context)
        self._log_prompt_if_enabled(event, prepared)
        trace_id = prepared.message_context.trace_id if prepared.message_context else ""
        trace_log = format_trace_log(trace_id=trace_id, session_key=self.host._get_conversation_key(event), message_id=event.message_id)
        try:
            source = "fallback" if prepared.fallback_response is not None else "ai"
            response = AIResponse(content=prepared.fallback_response) if prepared.fallback_response is not None else await self._request_model_reply(event, prepared)
            self._persist_reply_result(event, prepared, response)
            logger.debug(
                "回复生成完成：%s 用户=%s，群=%s，长度=%s",
                trace_log,
                event.user_id,
                event.group_id,
                len(response.content),
            )
            return ReplyResult(text=response.content, source=source)
        except asyncio.TimeoutError:
            logger.error("回复生成失败：%s category=model_request_error 错误=模型响应超时", trace_log)
            return ReplyResult(text="AI 服务响应超时，请稍后再试。", source="fallback")
        except AIAPIError as exc:
            logger.error("回复生成失败：%s category=model_request_error 错误=%s", trace_log, exc)
            return ReplyResult(text=f"AI 服务暂时不可用，请稍后再试。\n错误信息: {exc}", source="fallback")
        except Exception as exc:
            logger.error("回复流程异常：%s category=%s 错误=%s", trace_log, classify_pipeline_error(exc), exc, exc_info=True)
            return ReplyResult(text="处理消息时出错，请稍后再试。", source="fallback")

    async def _download_images_if_needed(self, event: MessageEvent) -> List[str]:
        if not self._event_has_image(event) or not self._vision_enabled():
            return []
        logger.debug("开始处理图片输入")
        return await self.host.download_images(event)

    def _log_prompt_if_enabled(self, event: MessageEvent, prepared: PreparedReplyRequest) -> None:
        if not self.host.app_config.bot_behavior.log_full_prompt:
            return
        if not prepared.messages:
            trace_id = prepared.message_context.trace_id if prepared.message_context else ""
            logger.info(
                    "[系统提示词]\ntrace=%s\n用户=%s\n会话=%s\n[视觉兜底回复：未请求模型]",
                trace_id,
                event.user_id,
                self.host._get_conversation_key(event),
            )
        return

    async def _request_model_reply(self, event: MessageEvent, prepared: PreparedReplyRequest) -> AIResponse:
        trace_id = prepared.message_context.trace_id if prepared.message_context else ""
        logger.debug(
            "开始请求 AI：%s 用户=%s，群=%s，图片数=%s，历史数=%s，多模态=%s",
            format_trace_log(trace_id=trace_id, session_key=self.host._get_conversation_key(event), message_id=event.message_id),
            event.user_id,
            event.group_id,
            len(prepared.base64_images),
            len(prepared.related_history_messages),
            self._should_use_multimodal_reply(prepared.base64_images),
        )
        return await self.chat_with_tools(
            messages=prepared.messages,
            user_id=str(event.user_id),
            temperature=0.7,
            event=event,
            trace_id=trace_id,
        )

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
                trace_id=prepared.message_context.trace_id if prepared.message_context else "",
            )
        except Exception as exc:
            logger.warning("记录记忆副作用失败：%s", exc, exc_info=True)

    def _schedule_memory_extraction(
        self,
        user_id: str,
        *,
        dialogue_key: Optional[str] = None,
        message_type: str = "private",
        group_id: Optional[str] = None,
        trace_id: str = "",
    ) -> None:
        scheduler = getattr(self.host.memory_manager, "schedule_memory_extraction", None)
        if callable(scheduler):
            if trace_id:
                logger.info("已调度记忆提取：%s user=%s", format_trace_log(trace_id=trace_id, session_key=str(dialogue_key or ""), message_id=""), user_id)
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
            "如果你需要记住重要的东西，或者被明确要求记住什么事情，可以调用 search_memories 工具。"
            "只有在确实需要用时才能调用，不要滥用。"
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
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("记忆工具调用失败：%s", exc)
                content = f"检索失败: {exc}"

        return {
            "tool_call_id": tool_call.get("id", ""),
            "role": "tool",
            "name": name,
            "content": content,
        }

    async def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        user_id: str,
        temperature: float = 0.7,
        event: Optional[MessageEvent] = None,
        trace_id: str = "",
    ) -> AIResponse:
        tools = self.build_memory_tools()
        request_messages = list(messages)
        if tools:
            request_messages[0] = {
                "role": "system",
                "content": self.augment_system_prompt_for_tools(str(request_messages[0].get("content", "")), tools),
            }
        self._log_actual_prompt_messages(event=event, messages=request_messages, title="[FULL PROMPT]", trace_id=trace_id)
        response = await self._invoke_reply_model(
            messages=request_messages,
            temperature=temperature,
            tools=tools or None,
            event=event,
            trace_id=trace_id,
            label="主回复生成",
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
        self._log_actual_prompt_messages(event=event, messages=follow_up_messages, title="[FULL PROMPT][ROUND 2]", trace_id=trace_id)
        return await self._invoke_reply_model(
            messages=follow_up_messages,
            temperature=temperature,
            event=event,
            trace_id=trace_id,
            label="主回复生成-工具续轮",
        )

    async def _invoke_reply_model(
        self,
        *,
        messages: List[Dict[str, Any]],
        temperature: float,
        event: Optional[MessageEvent],
        trace_id: str,
        label: str,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> AIResponse:
        async def run_chat():
            return await self.host.ai_client.chat_completion(
                messages=messages,
                temperature=temperature,
                tools=tools,
            )

        router = getattr(self.host, "model_invocation_router", None)
        if router is None:
            return await run_chat()
        return await router.submit(
            purpose=ModelInvocationType.REPLY_GENERATION,
            trace_id=trace_id,
            session_key=self.host._get_conversation_key(event) if event else "",
            message_id=getattr(event, "message_id", ""),
            label=label,
            runner=run_chat,
        )

    def _log_actual_prompt_messages(
        self,
        *,
        event: Optional[MessageEvent],
        messages: List[Dict[str, Any]],
        title: str,
        trace_id: str = "",
    ) -> None:
        if not event or not self.host.app_config.bot_behavior.log_full_prompt:
            return
        formatter = getattr(self.host, "_format_system_prompt_log_with_history", None)
        if callable(formatter):
            logger.info(formatter(event, messages, title=title, trace_id=trace_id))

    async def load_memory_context(
        self,
        *,
        event: MessageEvent,
        user_message: str,
        conversation: Conversation,
        prompt_plan: Optional[PromptPlan] = None,
    ) -> Tuple[str, str, str, str, str, List[Dict[str, Any]], bool]:
        if not self.host.memory_manager:
            return "", "", "", "", "", [], len(conversation.messages) == 0

        is_first_turn = len(conversation.messages) == 0
        access_context = self._build_memory_access_context(event)
        person_fact_context = ""
        if self._layer_enabled(prompt_plan, "enable_person_facts", default=True):
            person_fact_context = await self._load_person_fact_context(
                user_id=str(event.user_id),
                access_context=access_context,
            )
        persistent_memory_context = await self._load_persistent_memory_context(
            user_id=str(event.user_id),
            access_context=access_context,
        )
        include_sections = {
            "session_restore": self._layer_enabled(prompt_plan, "enable_session_restore", default=True),
            "precise_recall": self._layer_enabled(prompt_plan, "enable_precise_recall", default=True),
            "dynamic": self._layer_enabled(prompt_plan, "enable_dynamic_memory", default=True),
        }
        section_intensity = {
            "session_restore": str(getattr(prompt_plan, "restore_intensity", "normal") or "normal"),
            "precise_recall": str(getattr(prompt_plan, "recall_intensity", "normal") or "normal"),
            "dynamic": str(getattr(prompt_plan, "dynamic_intensity", "normal") or "normal"),
        }
        for key, enabled in include_sections.items():
            if not enabled:
                section_intensity[key] = "off"

        try:
            payload = await self.host.memory_manager.search_memories_with_context(
                user_id=str(event.user_id),
                query=user_message,
                top_k=self._memory_top_k(prompt_plan),
                include_conversations=self._layer_enabled(prompt_plan, "enable_recent_context", default=True),
                include_sections=include_sections,
                section_intensity=section_intensity,
                read_scope=self._get_memory_read_scope(),
                access_context=access_context,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("检索记忆上下文失败：%s", exc)
            return person_fact_context, persistent_memory_context, "", "", "", [], is_first_turn

        memories = payload.get("memories", []) if isinstance(payload, dict) else []
        session_restore_entries = payload.get("session_restore", []) if isinstance(payload, dict) else []
        precise_recall_entries = payload.get("precise_recall", []) if isinstance(payload, dict) else []
        history_messages = payload.get("history_messages", []) if isinstance(payload, dict) else []
        if not include_sections["session_restore"]:
            session_restore_entries = []
        if not include_sections["precise_recall"]:
            precise_recall_entries = []
        if not include_sections["dynamic"]:
            memories = []
        if not self._layer_enabled(prompt_plan, "enable_recent_context", default=True):
            history_messages = []
        session_restore_context = self._format_memory_context(self._collect_memory_lines(session_restore_entries))
        precise_recall_context = self._format_memory_context(self._collect_memory_lines(precise_recall_entries))
        dynamic_memory_context = self._format_memory_context(
            self._dedupe_memory_lines(
                self._collect_memory_lines(memories),
                existing_lines=self._collect_memory_lines_from_text(persistent_memory_context),
            )
        )
        return (
            person_fact_context,
            persistent_memory_context,
            session_restore_context,
            precise_recall_context,
            dynamic_memory_context,
            list(history_messages or []),
            is_first_turn,
        )

    async def _load_person_fact_context(
        self,
        *,
        user_id: str,
        access_context: Any,
    ) -> str:
        if not self.host.memory_manager:
            return ""
        try:
            return await self.host.memory_manager.format_person_facts_for_prompt(
                user_id=user_id,
                access_context=access_context,
                limit=6,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("加载人物事实失败：%s", exc)
            return ""

    async def _load_persistent_memory_context(
        self,
        *,
        user_id: str,
        access_context: Any,
    ) -> str:
        if not self.host.memory_manager:
            return ""
        try:
            important = await self.host.memory_manager.get_important_memories(
                user_id=user_id,
                limit=5,
                read_scope=self._get_memory_read_scope(),
                access_context=access_context,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("加载持续关键信息失败：%s", exc)
            important = []
        return self._format_memory_context(self._collect_memory_lines(important))

    def _format_legacy_important_memories(self, memories: List[Any]) -> str:
        return self._format_memory_context(self._collect_memory_lines(memories))

    def _collect_memory_lines(self, memories: List[Any]) -> List[str]:
        lines: List[str] = []
        formatter = getattr(self.host, "_format_memory_prompt_entry", None)
        for item in memories:
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
            lines.append(text)
        return lines

    def _format_memory_context(self, memory_lines: List[str]) -> str:
        if not memory_lines:
            return ""
        return "\n".join(f"{index}. {text}" for index, text in enumerate(memory_lines, start=1))

    def _collect_memory_lines_from_text(self, memory_context: str) -> List[str]:
        lines: List[str] = []
        for raw_line in str(memory_context or "").splitlines():
            line = str(raw_line or "").strip()
            if not line:
                continue
            lines.append(re.sub(r"^\d+\.\s*", "", line))
        return lines

    def _dedupe_memory_lines(self, memory_lines: List[str], *, existing_lines: List[str]) -> List[str]:
        seen = {self._normalize_memory_line(line) for line in existing_lines if self._normalize_memory_line(line)}
        deduped: List[str] = []
        for line in memory_lines:
            normalized = self._normalize_memory_line(line)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(line)
        return deduped

    def _normalize_memory_line(self, line: str) -> str:
        return re.sub(r"\s+", " ", str(line or "").strip()).lower()

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

    def _assistant_display_name(self) -> str:
        getter = getattr(self.host, "_get_assistant_name", None)
        if callable(getter):
            name = str(getter() or "").strip()
            if name:
                return name
        fallback = getattr(getattr(self.host, "app_config", None), "assistant_profile", None)
        return str(getattr(fallback, "name", "") or "").strip() or "助手"

    def _format_identity_label(self, user_id: Any, display_name: str = "") -> str:
        identifier = str(user_id or "").strip() or "unknown"
        name = str(display_name or "").strip()
        if name and name != identifier:
            return f"{identifier}（{name}）"
        return identifier

    def _current_user_label(self, event: Optional[MessageEvent]) -> str:
        if event is None:
            return "unknown"
        return self._format_identity_label(event.user_id, self._sender_display_name(event))

    def _sender_display_name(self, event: MessageEvent) -> str:
        getter = getattr(self.host, "_get_sender_display_name", None)
        if callable(getter):
            return str(getter(event) or "")
        return event.get_sender_display_name()

    def _event_has_image(self, event: MessageEvent) -> bool:
        checker = getattr(self.host, "_has_image_input", None)
        if callable(checker):
            return bool(checker(event))
        return event.has_image()

    def _assistant_self_label(self, event: Optional[MessageEvent], plan: Optional[MessageHandlingPlan] = None) -> str:
        if event is not None:
            return self._format_identity_label(event.self_id, self._assistant_display_name())
        if plan and getattr(plan, "reply_context", None):
            self_id = (plan.reply_context or {}).get("assistant_self_id")
            if self_id:
                return self._format_identity_label(self_id, self._assistant_display_name())
        return self._assistant_display_name()

    def _build_session_context_prompt(self, event: Optional[MessageEvent]) -> str:
        if event is None:
            return ""
        session_type = str(event.message_type or "").strip().lower() or MessageType.PRIVATE.value
        session_label = "群聊" if session_type == MessageType.GROUP.value else "私聊"
        return f"请注意你现在正在和{session_label}里的“用户ID：{self._current_user_label(event)}”说话。"

    def _build_reply_context_prompt(
        self,
        *,
        event: Optional[MessageEvent],
        plan: Optional[MessageHandlingPlan],
    ) -> str:
        if not plan or event is None:
            return ""
        session_type = str(event.message_type or "").strip().lower() or MessageType.PRIVATE.value
        if session_type != MessageType.GROUP.value:
            return ""
        return f"你回复的理由是：{plan.reason}"

    def _build_history_context_prompt(
        self,
        event: Optional[MessageEvent],
        recent_history_text: str,
        plan: Optional[MessageHandlingPlan],
    ) -> str:
        assistant_label = self._assistant_self_label(event, plan)
        session_type = str(getattr(event, "message_type", "") or "").strip().lower()
        if session_type == MessageType.GROUP.value:
            if recent_history_text.strip():
                return (
                    f"在当前这条群消息之前，群里刚刚聊了这些内容。"
                    f"注意你的id和昵称是：{assistant_label}。\n"
                    f"{recent_history_text.strip()}"
                )
            return ""
        if recent_history_text.strip():
            return (
                f"在这条消息之前，你们刚刚聊了这些内容。"
                f"注意你的id和昵称是：{assistant_label}。\n"
                f"{recent_history_text.strip()}"
            )
        return ""

    def _layer_enabled(self, prompt_plan: Optional[PromptPlan], attr: str, default: bool = True) -> bool:
        if not prompt_plan or not getattr(prompt_plan, "policy", None):
            return default
        return bool(getattr(prompt_plan.policy, attr, default))

    def _context_budget(self, prompt_plan: Optional[PromptPlan]) -> str:
        if not prompt_plan:
            return "normal"
        value = str(getattr(prompt_plan, "context_budget", "normal") or "normal").strip().lower()
        return value if value in {"low", "normal", "high"} else "normal"

    def _memory_top_k(self, prompt_plan: Optional[PromptPlan]) -> int:
        return {
            "low": 3,
            "normal": 5,
            "high": 7,
        }.get(self._context_budget(prompt_plan), 5)

    def _history_limit(self, prompt_plan: Optional[PromptPlan]) -> int:
        base = max(0, int(self.host.app_config.bot_behavior.max_context_length or 0))
        if base <= 0:
            return 0
        budget = self._context_budget(prompt_plan)
        if budget == "low":
            return min(base, 3)
        if budget == "high":
            return max(base, min(base + 2, 12))
        return base

    def _build_continuity_guidance_prompt(self, prompt_plan: Optional[PromptPlan]) -> str:
        mode = str(getattr(prompt_plan, "continuity_mode", "direct_continue") or "direct_continue").strip().lower() if prompt_plan else "direct_continue"
        guidance = {
            "direct_continue": "这次回复更适合直接顺着当前语境接下去。",
            "resume_recent_topic": "这次回复更适合承接最近刚聊过的话题，但不要把旧内容展开得太重。",
            "resume_old_topic": "这次回复更适合带一点重新接上旧话题的承接感，可以自然回忆之前聊到的重点。",
            "memory_query": "这次回复更像是在围绕记忆或旧信息作答，优先保证事实和上下文对应正确。",
            "casual_chat": "这次回复更像是轻松闲聊，优先自然、轻盈，不要过度分析。",
            "clarification": "这次回复更像是在澄清或修正理解，优先准确，不要急着延展话题。",
        }.get(mode, "")
        return f"连续性策略：{guidance}" if guidance else ""

    def _build_engagement_mode_prompt(self, prompt_plan: Optional[PromptPlan]) -> str:
        mode = str(getattr(prompt_plan, "engagement_mode", "neutral") or "neutral").strip().lower() if prompt_plan else "neutral"
        guidance = {
            "neutral": "",
            "gentle_care": "陪伴方式：优先做轻柔关怀，先接住状态和情绪，再决定要不要补建议，不要突然上价值。",
            "topic_continue": "陪伴方式：优先顺着刚刚的话题自然往下接，延续上下文，不要生硬跳题。",
            "light_presence": "陪伴方式：轻轻接一句保持存在感即可，不要把接话写成抢话。",
        }.get(mode, "")
        return guidance

    def _build_reply_style_prompt(self, prompt_plan: Optional[PromptPlan]) -> str:
        style = str(getattr(prompt_plan, "reply_style", "normal") or "normal").strip().lower() if prompt_plan else "normal"
        guidance = {
            "concise": "回复风格：偏简洁，能一句说清就不要展开太多。",
            "normal": "回复风格：自然均衡，既要有回应感，也不要写得太满。",
            "deep": "回复风格：可以适度深入，补足理解、情绪承接和自然延伸，但不要写成长篇说教。",
        }.get(style, "")
        return guidance

    def _build_context_budget_prompt(self, prompt_plan: Optional[PromptPlan]) -> str:
        budget = self._context_budget(prompt_plan)
        guidance = {
            "low": "上下文预算：低，优先围绕当前消息和最必要信息回复。",
            "normal": "上下文预算：正常，可以参考最近上下文和必要记忆。",
            "high": "上下文预算：高，可以更充分结合近期上下文、会话恢复和相关记忆来组织回复。",
        }.get(budget, "")
        return guidance

    def _build_prompt_plan_notes_prompt(self, prompt_plan: Optional[PromptPlan]) -> str:
        notes = str(getattr(prompt_plan, "notes", "") or "").strip() if prompt_plan else ""
        return f"补充提示：{notes}" if notes else ""

    def _build_temporal_context_prompt(self, temporal_context: Optional[TemporalContext], prompt_plan: Optional[PromptPlan]) -> str:
        if not self._layer_enabled(prompt_plan, "enable_temporal_context", default=True):
            return ""
        summary = str(getattr(temporal_context, "summary_text", "") or "").strip()
        if not summary:
            return ""
        mode = str(getattr(prompt_plan, "temporal_mode", "light") or "light").strip().lower() if prompt_plan else "light"
        if mode == "off":
            return ""
        if mode == "explicit":
            recent_bucket = str(getattr(temporal_context, "recent_gap_bucket", "unknown") or "unknown")
            continuity_hint = str(getattr(temporal_context, "continuity_hint", "unknown") or "unknown")
            return (
                "这是当前消息和已有上下文之间的时间连续性信息：\n"
                f"- {summary}\n"
                f"- 最近时间跨度分层：{recent_bucket}\n"
                f"- 连续性判断倾向：{continuity_hint}"
            )
        return "这是当前消息和已有上下文之间的时间连续性信息：\n- " + summary

    def _build_current_message_prompt(self, event: Optional[MessageEvent], current_message: str) -> str:
        message = str(current_message or "").strip()
        if event is None:
            return f"当前消息：{message}" if message else "当前消息："
        sender_label = self._current_user_label(event)
        return f"当前消息来自用户 {sender_label}：{message}" if message else f"当前消息来自用户 {sender_label}："

    def _build_persistent_memory_prompt(self, persistent_memory_context: str) -> str:
        if persistent_memory_context.strip():
            return "这些是其他需要注意的关键信息：\n" + persistent_memory_context.strip()
        return "当前没有额外需要你持续注意的关键信息。"

    def _build_person_fact_prompt(self, person_fact_context: str) -> str:
        if person_fact_context.strip():
            return "这些是关于当前用户的长期人物事实：\n" + person_fact_context.strip()
        return "当前没有独立整理出的人物事实。"

    def _build_session_restore_prompt(self, session_restore_context: str) -> str:
        if session_restore_context.strip():
            return "这是上一轮相关会话的恢复摘要：\n" + session_restore_context.strip()
        return "当前没有可用的上一轮会话恢复摘要。"

    def _build_precise_recall_prompt(self, precise_recall_context: str) -> str:
        if precise_recall_context.strip():
            return "这是和当前话题直接相关的旧对话定位：\n" + precise_recall_context.strip()
        return "当前没有定位到更早的相关旧对话。"

    def _build_dynamic_memory_prompt(self, dynamic_memory_context: str) -> str:
        if dynamic_memory_context.strip():
            return "与当前消息关联的记忆有：\n" + dynamic_memory_context.strip()
        return "当前消息没有额外关联的记忆。"

    def _build_reply_scope_prompt(self, event: Optional[MessageEvent]) -> str:
        session_type = str(getattr(event, "message_type", "") or "").strip().lower()
        if session_type == MessageType.GROUP.value:
            return (
                "注意请从当前消息开始回复。最近聊天记录、人物事实、会话恢复摘要、旧对话定位和关联记忆只用于帮助你理解上下文，"
                "不要把它们当成当前要回复的消息，也不要转而回复其他用户之前说的话，"
                "不要直接复述、照搬或引用其中的原文内容，也不要向用户暴露这些内容来自提示词中的聊天记录或记忆。"
            )
        return (
            "注意请从当前消息开始回复。最近聊天记录、人物事实、会话恢复摘要、旧对话定位和关联记忆只用于帮助你理解上下文，"
            "不要把它们当成当前要回复的消息，不要直接复述、照搬或引用其中的原文内容，"
            "也不要向用户暴露这些内容来自提示词中的聊天记录或记忆。"
        )

    def build_response_system_prompt(
        self,
        *,
        event: Optional[MessageEvent] = None,
        person_fact_context: str,
        persistent_memory_context: str,
        session_restore_context: str,
        precise_recall_context: str,
        dynamic_memory_context: str,
        is_first_turn: bool,
        plan: Optional[MessageHandlingPlan] = None,
        recent_history_text: str = "",
        current_message: str = "",
        temporal_context: Optional[TemporalContext] = None,
        prompt_plan: Optional[PromptPlan] = None,
    ) -> str:
        parts = [
            self.host._build_assistant_identity_prompt(),
            self.host._build_system_prompt(),
            self._build_session_context_prompt(event),
            self._build_reply_context_prompt(event=event, plan=plan),
            self._build_continuity_guidance_prompt(prompt_plan),
            self._build_engagement_mode_prompt(prompt_plan),
            self._build_reply_style_prompt(prompt_plan),
            self._build_context_budget_prompt(prompt_plan),
            self._build_prompt_plan_notes_prompt(prompt_plan),
            self._build_temporal_context_prompt(temporal_context, prompt_plan),
            self._build_history_context_prompt(event, recent_history_text, plan)
            if self._layer_enabled(prompt_plan, "enable_recent_context", default=True)
            else "",
            self._build_person_fact_prompt(person_fact_context)
            if self._layer_enabled(prompt_plan, "enable_person_facts", default=True)
            else "",
            self._build_session_restore_prompt(session_restore_context)
            if self._layer_enabled(prompt_plan, "enable_session_restore", default=True)
            else "",
            self._build_precise_recall_prompt(precise_recall_context)
            if self._layer_enabled(prompt_plan, "enable_precise_recall", default=True)
            else "",
            self._build_current_message_prompt(event, current_message),
            self._build_persistent_memory_prompt(persistent_memory_context),
            self._build_dynamic_memory_prompt(dynamic_memory_context)
            if self._layer_enabled(prompt_plan, "enable_dynamic_memory", default=True)
            else "",
            self._build_reply_scope_prompt(event)
            if self._layer_enabled(prompt_plan, "enable_reply_scope", default=True)
            else "",
        ]
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

    def _build_recent_history_text(
        self,
        *,
        event: MessageEvent,
        conversation: Conversation,
        plan: Optional[MessageHandlingPlan],
    ) -> str:
        if str(event.message_type or "").strip().lower() == MessageType.GROUP.value:
            history_lines = self._build_group_window_history_lines(plan=plan, current_message_id=int(event.message_id or 0))
            if history_lines:
                return "\n".join(history_lines)
        return self._build_conversation_history_text(event=event, conversation=conversation, plan=plan)

    def _build_conversation_history_text(
        self,
        *,
        event: MessageEvent,
        conversation: Conversation,
        plan: Optional[MessageHandlingPlan],
    ) -> str:
        max_length = self._history_limit(getattr(plan, "prompt_plan", None) if plan else None)
        if max_length <= 0:
            return ""
        rendered: List[str] = []
        for item in conversation.get_messages(max_length=max_length):
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            rendered.append(self._format_conversation_message_for_prompt(item=item, event=event, plan=plan))
        return "\n".join(line for line in rendered if line)

    def _format_conversation_message_for_prompt(
        self,
        *,
        item: Dict[str, Any],
        event: MessageEvent,
        plan: Optional[MessageHandlingPlan],
    ) -> str:
        role = str(item.get("role") or "user").strip().lower()
        if role == "assistant":
            speaker = f"你 {self._assistant_self_label(event, plan)}"
        elif role == "system":
            speaker = "系统"
        else:
            speaker = f"用户 {self._current_user_label(event)}"
        content = str(item.get("content") or "").strip()
        if not content:
            return ""
        return f"{speaker}: {content}"

    def _build_group_window_history_lines(
        self,
        *,
        plan: Optional[MessageHandlingPlan],
        current_message_id: int,
    ) -> List[str]:
        if not plan or not getattr(plan, "reply_context", None):
            return []
        window_messages = list((plan.reply_context or {}).get("window_messages") or [])
        max_length = self._history_limit(getattr(plan, "prompt_plan", None) if plan else None)
        if max_length <= 0:
            return []
        rendered: List[str] = []
        for item in window_messages:
            if bool(item.get("is_latest")):
                continue
            if current_message_id and int(item.get("message_id", 0) or 0) == current_message_id:
                continue
            content = self._format_window_message_for_model(item, plan=plan)
            if not content:
                continue
            rendered.append(content)
        return rendered[-max_length:]

    def _window_speaker_label(self, item: Dict[str, Any], plan: Optional[MessageHandlingPlan]) -> str:
        role = str(item.get("speaker_role") or "user").strip().lower()
        if role == "assistant":
            return f"你 {self._assistant_self_label(None, plan)}"
        return f"用户 {self._format_identity_label(item.get('user_id'), str(item.get('speaker_name') or ''))}"

    def _format_window_message_for_model(self, item: Dict[str, Any], plan: Optional[MessageHandlingPlan] = None) -> str:
        speaker = self._window_speaker_label(item, plan)
        text = self._window_display_text(item)
        if text:
            return f"{speaker}: {text}"
        merged_description = str(item.get("merged_description") or "").strip()
        if merged_description:
            return f"{speaker}: 图片摘要: {merged_description}"
        per_image_descriptions = [
            str(value).strip()
            for value in (item.get("per_image_descriptions") or [])
            if str(value).strip()
        ]
        if per_image_descriptions:
            return f"{speaker}: 图片摘要: " + "；".join(per_image_descriptions)
        if bool(item.get("has_image")):
            return f"{speaker}: [图片]"
        return ""

    def _window_display_text(self, item: Dict[str, Any]) -> str:
        text = str(item.get("display_text") or item.get("text") or item.get("raw_text") or "").strip()
        if text and text != "[空]":
            return text
        raw_image_count = int(item.get("raw_image_count", item.get("image_count", 0)) or 0)
        if bool(item.get("raw_has_image")) or raw_image_count > 0:
            return "[图片]" if raw_image_count <= 1 else f"[图片 x{raw_image_count}]"
        return ""

    async def _resolve_vision_analysis(
        self,
        *,
        event: MessageEvent,
        user_message: str,
        base64_images: List[str],
        plan: Optional[MessageHandlingPlan],
        trace_id: str = "",
    ) -> Dict[str, Any]:
        reused = self._extract_reusable_vision_analysis(event=event, plan=plan)
        if reused:
            if self.host.runtime_metrics:
                self.host.runtime_metrics.record_vision_request(reused_from_plan=True)
            return reused
        if not self._event_has_image(event) or not self._vision_enabled():
            return {}
        return await self.host.analyze_event_images(
            event,
            user_message,
            base64_images=base64_images,
            trace_id=trace_id,
        )

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
        if not self._event_has_image(event):
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
