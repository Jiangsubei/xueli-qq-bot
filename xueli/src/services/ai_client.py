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


class _RetryableHTTPError(Exception):
    """Internal signal: HTTP 429 / 5xx that should trigger a retry."""

    def __init__(self, status: int, response_text: str) -> None:
        super().__init__(f"Retryable HTTP {status}")
        self.status = status
        self.response_text = response_text


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
            "[%s] AI 客户端已准备：模型=%s，地址=%s，超时=%s 秒",
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

    async def _do_chat_completion(
        self,
        body: Dict[str, Any],
    ) -> AIResponse:
        async with self._semaphore:
            logger.debug(
                "[%s] 发起 AI 请求：模型=%s，消息数=%s，流式=%s，地址=%s",
                self.log_label,
                body.get("model"),
                len(body.get("messages", [])),
                body.get("stream", False),
                self.chat_completions_url,
            )
            status, response_text = await self._session_manager.post_text(
                self.chat_completions_url,
                body,
            )

            if status == 429 or status >= 500:
                logger.warning("[%s] HTTP %s — 需要重试", self.log_label, status)
                raise _RetryableHTTPError(status, response_text)

            if status != 200:
                logger.error("[%s] 请求失败：HTTP %s", self.log_label, status)
                raise map_http_error(status, response_text)

            try:
                data = json.loads(response_text)
            except json.JSONDecodeError:
                logger.warning("[%s] 响应 JSON 无效，需要重试", self.log_label)
                raise  # re-raise raw so chat_completion retry loop can catch it

            return self._parse_response(data)

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        max_retries: int = 3,
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

        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                return await self._do_chat_completion(body)
            except _RetryableHTTPError as exc:
                last_exc = exc
                if attempt >= max_retries:
                    logger.error("[%s] 重试 %s 次后仍失败", self.log_label, max_retries)
                    raise map_http_error(exc.status, exc.response_text)
                delay = 2.0 ** attempt
                logger.info("[%s] 第 %s/%s 次重试，等待 %.1fs", self.log_label, attempt + 1, max_retries, delay)
                await asyncio.sleep(delay)
            except aiohttp.ClientError as exc:
                last_exc = exc
                if attempt >= max_retries:
                    logger.error("[%s] 重试 %s 次后仍失败", self.log_label, max_retries)
                    raise map_client_error(exc)
                delay = 2.0 ** attempt
                logger.info("[%s] 第 %s/%s 次重试（网络错误），等待 %.1fs", self.log_label, attempt + 1, max_retries, delay)
                await asyncio.sleep(delay)
            except asyncio.TimeoutError:
                last_exc = asyncio.TimeoutError()
                if attempt >= max_retries:
                    logger.error("[%s] 请求超时（%s 次后仍失败）", self.log_label, max_retries)
                    raise map_timeout_error()
                delay = 2.0 ** attempt
                logger.info("[%s] 第 %s/%s 次重试（超时），等待 %.1fs", self.log_label, attempt + 1, max_retries, delay)
                await asyncio.sleep(delay)
            except json.JSONDecodeError as exc:
                last_exc = exc
                if attempt >= max_retries:
                    logger.error("[%s] JSON 解析失败（%s 次后仍失败）", self.log_label, max_retries)
                    raise map_json_error(exc)
                delay = 2.0 ** attempt
                logger.info("[%s] 第 %s/%s 次重试（JSON 解析错误），等待 %.1fs", self.log_label, attempt + 1, max_retries, delay)
                await asyncio.sleep(delay)
