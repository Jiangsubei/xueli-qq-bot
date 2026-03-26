"""
AI client facade that keeps the public API stable while delegating request
building, response parsing, error mapping, and HTTP session management.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import aiohttp

from src.core.config import AppConfig, config
from src.services.ai import (
    AIAPIError,
    AIHTTPSessionManager,
    AIRequestBuilder,
    AIResponse,
    AIResponseParser,
    map_client_error,
    map_http_error,
    map_json_error,
    map_timeout_error,
)

logger = logging.getLogger(__name__)


class AIClient:
    """OpenAI-compatible client facade."""

    def __init__(
        self,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        extra_params: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        response_path: Optional[str] = None,
        log_label: str = "ai",
        app_config: Optional[AppConfig] = None,
    ):
        self.app_config = app_config or config.app
        ai_service = self.app_config.ai_service
        bot_behavior = self.app_config.bot_behavior

        self.api_base = str(api_base or ai_service.api_base).rstrip("/")
        self.api_key = str(api_key or ai_service.api_key)
        self.model = str(model or ai_service.model)
        self.timeout = int(timeout if timeout is not None else bot_behavior.response_timeout)
        self.extra_params = dict(extra_params) if extra_params is not None else dict(ai_service.extra_params)
        self.extra_headers = dict(extra_headers) if extra_headers is not None else dict(ai_service.extra_headers)
        self.response_path = str(response_path or ai_service.response_path)
        self.log_label = str(log_label or "ai")
        self.chat_completions_url = f"{self.api_base}/chat/completions"

        self._request_builder = AIRequestBuilder(self.model, self.extra_params)
        self._response_parser = AIResponseParser(
            response_path=self.response_path,
            default_model=self.model,
            log_label=self.log_label,
        )
        self._session_manager = AIHTTPSessionManager(
            api_key=self.api_key,
            extra_headers=self.extra_headers,
            timeout=self.timeout,
        )
        self._semaphore = asyncio.Semaphore(5)

        logger.debug(
            "[%s] AI client prepared: model=%s url=%s timeout=%s",
            self.log_label,
            self.model,
            self.chat_completions_url,
            self.timeout,
        )

    @property
    def session(self):
        return self._session_manager.session

    async def __aenter__(self):
        await self._init_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _init_session(self):
        await self._session_manager.ensure_session()

    async def close(self):
        await self._session_manager.close()

    def _build_request_body(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        return self._request_builder.build(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            **kwargs,
        )

    def build_text_message(self, role: str, content: str) -> Dict[str, Any]:
        return {"role": role, "content": content}

    def build_multimodal_message(
        self,
        role: str,
        text: str,
        images: List[str],
        image_format: str = "base64",
    ) -> Dict[str, Any]:
        del image_format
        content: List[Dict[str, Any]] = []

        if text:
            content.append({"type": "text", "text": text})

        for image_data in images:
            image_url = image_data if image_data.startswith("data:") else f"data:image/jpeg;base64,{image_data}"
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image_url},
                }
            )

        return {"role": role, "content": content}

    def convert_to_multimodal_format(
        self,
        messages: List[Dict[str, Any]],
        images_map: Dict[int, List[str]],
    ) -> List[Dict[str, Any]]:
        result = []
        for index, message in enumerate(messages):
            if index in images_map and images_map[index]:
                result.append(
                    self.build_multimodal_message(
                        role=message.get("role", "user"),
                        text=message.get("content", "") if isinstance(message.get("content"), str) else "",
                        images=images_map[index],
                    )
                )
            else:
                result.append(message)
        return result

    def _extract_content(self, data: Dict[str, Any]) -> str:
        return self._response_parser.extract_content(data)

    def _parse_response(self, data: Dict[str, Any]) -> AIResponse:
        return self._response_parser.parse(data)

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        **kwargs,
    ) -> AIResponse:
        await self._init_session()
        body = self._build_request_body(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            **kwargs,
        )

        async with self._semaphore:
            try:
                logger.debug(
                    "[%s] AI request: model=%s messages=%s stream=%s url=%s",
                    self.log_label,
                    body.get("model"),
                    len(messages),
                    stream,
                    self.chat_completions_url,
                )
                status, response_text = await self._session_manager.post_text(
                    self.chat_completions_url,
                    body,
                )

                if status != 200:
                    logger.error(
                        "[%s] AI request failed: HTTP %s %s",
                        self.log_label,
                        status,
                        response_text[:200],
                    )
                    raise map_http_error(status, response_text)

                try:
                    data = json.loads(response_text)
                except json.JSONDecodeError as exc:
                    logger.error(
                        "[%s] invalid AI JSON response: %s %s",
                        self.log_label,
                        exc,
                        response_text[:200],
                    )
                    raise map_json_error(exc)

                return self._parse_response(data)
            except aiohttp.ClientError as exc:
                logger.error("[%s] AI client error: %s", self.log_label, exc)
                raise map_client_error(exc)
            except asyncio.TimeoutError:
                logger.error("[%s] AI request timeout: %s seconds", self.log_label, self.timeout)
                raise map_timeout_error()


__all__ = ["AIClient", "AIResponse", "AIAPIError"]
