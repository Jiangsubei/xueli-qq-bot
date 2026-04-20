from __future__ import annotations

from typing import Any, Dict, List


class AIRequestBuilder:
    """Build request bodies while keeping merge rules explicit and testable."""

    def __init__(self, model: str, extra_params: Dict[str, Any] | None = None):
        self.model = model
        self.extra_params = dict(extra_params or {})

    def build(
        self,
        *,
        messages: List[Dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
        }
        body.update(self.extra_params)
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        body.update({key: value for key, value in kwargs.items() if value is not None})
        return body
