from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from src.core.message_trace import format_trace_log
from src.core.log_text import preview_json_for_log
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
            fallback_text = str(prepared.fallback_response or "").strip()
            return AIResponse(content=fallback_text, segments=[fallback_text] if fallback_text else [])
        response = await self._request_model_reply(event, prepared)
        return self._normalize_visible_reply(response=response, event=event, prepared=prepared)

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
        return await self.pipeline.chat_with_tools(
            messages=messages,
            user_id=user_id,
            temperature=temperature,
            event=event,
            trace_id=trace_id,
        )

    def _normalize_visible_reply(self, *, response: AIResponse, event: Any, prepared: Any) -> AIResponse:
        raw_text = str(getattr(response, "content", "") or "").strip()
        segments = self._parse_segmented_reply(raw_text)
        if not segments:
            segments = [raw_text] if raw_text else []
        visible_text = "\n".join(segments).strip()
        trace_id = prepared.message_context.trace_id if prepared.message_context else ""
        if raw_text.startswith("[") and segments:
            logger.info(
                "结构化回复解析：%s segments=%s raw=%s",
                format_trace_log(trace_id=trace_id, session_key=self.host._get_conversation_key(event), message_id=getattr(event, "message_id", "")),
                len(segments),
                preview_json_for_log(raw_text),
            )
        return AIResponse(
            content=visible_text,
            segments=segments,
            usage=getattr(response, "usage", None),
            model=str(getattr(response, "model", "") or ""),
            finish_reason=str(getattr(response, "finish_reason", "") or ""),
            tool_calls=getattr(response, "tool_calls", None),
            raw_response=getattr(response, "raw_response", None),
        )

    def _parse_segmented_reply(self, raw_text: str) -> List[str]:
        text = str(raw_text or "").strip()
        if not text or not text.startswith("["):
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        result: List[str] = []
        for item in payload:
            if not isinstance(item, str):
                return []
            normalized = str(item or "").strip()
            if normalized:
                result.append(normalized)
        return result
