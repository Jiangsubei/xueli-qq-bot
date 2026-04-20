from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from src.core.message_trace import format_trace_log
from src.core.model_invocation_router import ModelInvocationType
from src.services.ai_client import AIResponse

logger = logging.getLogger(__name__)


class ReplyGenerationService:
    """Isolated model request layer for visible reply generation."""

    def __init__(self, host: Any, pipeline: Any) -> None:
        self.host = host
        self.pipeline = pipeline

    async def generate_reply(
        self,
        *,
        event: Any,
        prepared: Any,
    ) -> AIResponse:
        if prepared.fallback_response is not None:
            return AIResponse(content=prepared.fallback_response)
        return await self._request_model_reply(event, prepared)

    async def _request_model_reply(self, event: Any, prepared: Any) -> AIResponse:
        trace_id = prepared.message_context.trace_id if prepared.message_context else ""
        logger.debug(
            "开始请求 AI：%s 用户=%s，群=%s，图片数=%s，历史数=%s，多模态=%s",
            format_trace_log(trace_id=trace_id, session_key=self.host._get_conversation_key(event), message_id=event.message_id),
            event.user_id,
            event.group_id,
            len(prepared.base64_images),
            len(prepared.related_history_messages),
            self.pipeline._should_use_multimodal_reply(prepared.base64_images),
        )
        return await self._chat_with_tools(
            messages=prepared.messages,
            user_id=str(event.user_id),
            temperature=0.7,
            event=event,
            trace_id=trace_id,
        )

    async def _chat_with_tools(
        self,
        *,
        messages: List[Dict[str, Any]],
        user_id: str,
        temperature: float,
        event: Optional[Any],
        trace_id: str,
    ) -> AIResponse:
        tools = self.pipeline.build_memory_tools()
        request_messages = list(messages)
        if tools and request_messages:
            request_messages[0] = {
                "role": "system",
                "content": self.pipeline.augment_system_prompt_for_tools(str(request_messages[0].get("content", "")), tools),
            }
        self.pipeline._log_actual_prompt_messages(event=event, messages=request_messages, title="[FULL PROMPT]", trace_id=trace_id)
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
        follow_up_messages.append({"role": "assistant", "content": response.content, "tool_calls": tool_calls})
        for tool_call in tool_calls:
            follow_up_messages.append(await self.pipeline.execute_tool_call(tool_call, user_id))
        self.pipeline._log_actual_prompt_messages(event=event, messages=follow_up_messages, title="[FULL PROMPT][ROUND 2]", trace_id=trace_id)
        return await self._invoke_reply_model(
            messages=follow_up_messages,
            temperature=temperature,
            tools=None,
            event=event,
            trace_id=trace_id,
            label="主回复生成-工具续轮",
        )

    async def _invoke_reply_model(
        self,
        *,
        messages: List[Dict[str, Any]],
        temperature: float,
        tools: Optional[List[Dict[str, Any]]],
        event: Optional[Any],
        trace_id: str,
        label: str,
    ) -> AIResponse:
        async def run_chat():
            return await self.host.ai_client.chat_completion(messages=messages, temperature=temperature, tools=tools)

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
