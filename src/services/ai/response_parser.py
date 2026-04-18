from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from .types import AIResponse

logger = logging.getLogger(__name__)


class AIResponseParser:
    """Parse heterogeneous OpenAI-compatible responses into a stable shape."""

    def __init__(self, *, response_path: str, default_model: str, log_label: str = "ai"):
        self.response_path = response_path or "choices.0.message.content"
        self.default_model = default_model
        self.log_label = log_label

    def extract_content(self, data: Dict[str, Any]) -> str:
        value = self._resolve_path(data, self.response_path)
        if value is not None:
            return self._stringify_content(value)

        for fallback_path in (
            "choices.0.message.content",
            "output.choices.0.message.content",
            "choices.0.text",
            "text",
            "content",
            "output_text",
        ):
            fallback_value = self._resolve_path(data, fallback_path)
            if fallback_value is not None:
                return self._stringify_content(fallback_value)

        logger.error("[%s] 提取响应内容失败：路径=%s", self.log_label, self.response_path)
        logger.debug("[%s] 响应预览：%s", self.log_label, str(data)[:300])
        return ""

    def parse(self, data: Dict[str, Any]) -> AIResponse:
        try:
            content = self.extract_content(data)
            choice = self._extract_choice(data)
            message = choice.get("message", {}) if isinstance(choice, dict) else {}
            tool_calls = self._extract_tool_calls(data, choice, message)
            finish_reason = ""
            if isinstance(choice, dict):
                finish_reason = str(choice.get("finish_reason", "") or "")

            return AIResponse(
                content=content,
                usage=data.get("usage"),
                model=str(data.get("model") or self.default_model),
                finish_reason=finish_reason,
                tool_calls=tool_calls,
                raw_response=data,
            )
        except Exception as exc:
            logger.error("[%s] 解析响应失败：%s", self.log_label, exc)
            logger.debug("[%s] 响应预览：%s", self.log_label, str(data)[:200])
            return AIResponse(
                content=str(data)[:1000],
                model=self.default_model,
                raw_response=data,
            )

    def _extract_choice(self, data: Dict[str, Any]) -> Dict[str, Any]:
        for path in ("choices.0", "output.choices.0", "output.0"):
            choice = self._resolve_path(data, path)
            if isinstance(choice, dict):
                return choice
        return data

    def _extract_tool_calls(
        self,
        data: Dict[str, Any],
        choice: Dict[str, Any],
        message: Dict[str, Any],
    ) -> Optional[List[Dict[str, Any]]]:
        candidates = [
            message.get("tool_calls") if isinstance(message, dict) else None,
            choice.get("tool_calls") if isinstance(choice, dict) else None,
            self._resolve_path(data, "choices.0.message.tool_calls"),
            self._resolve_path(data, "output.choices.0.message.tool_calls"),
        ]
        for candidate in candidates:
            if isinstance(candidate, list):
                return candidate
        return None

    def _resolve_path(self, data: Any, path: str) -> Any:
        current = data
        for part in path.split("."):
            if isinstance(current, list) and part.isdigit():
                index = int(part)
                if index >= len(current):
                    return None
                current = current[index]
                continue
            if isinstance(current, dict):
                if part not in current:
                    return None
                current = current[part]
                continue
            return None
        return current

    def _stringify_content(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: List[str] = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(part for part in parts if part)
        return str(value)
