from __future__ import annotations

import aiohttp

from .types import AIAPIError


def map_http_error(status: int, response_text: str) -> AIAPIError:
    preview = (response_text or "")[:500]
    return AIAPIError(f"API 请求失败: {status}, {preview}")


def map_json_error(exc: Exception) -> AIAPIError:
    return AIAPIError(f"无法解析 API 响应: {exc}")


def map_client_error(exc: aiohttp.ClientError) -> AIAPIError:
    return AIAPIError(f"HTTP 请求失败: {exc}")


def map_timeout_error() -> AIAPIError:
    return AIAPIError("请求超时，请稍后重试")
