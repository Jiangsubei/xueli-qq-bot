from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from src.services.ai_client import AIAPIError


@dataclass
class MessagePipelineError(Exception):
    category: str
    message: str

    def __str__(self) -> str:
        return self.message


class ConfigurationError(MessagePipelineError):
    def __init__(self, message: str):
        super().__init__("configuration_error", message)


class ModelRequestError(MessagePipelineError):
    def __init__(self, message: str):
        super().__init__("model_request_error", message)


class ModelParseError(MessagePipelineError):
    def __init__(self, message: str):
        super().__init__("model_parse_error", message)


class ImageProcessingError(MessagePipelineError):
    def __init__(self, message: str):
        super().__init__("image_processing_error", message)


class MemoryOperationError(MessagePipelineError):
    def __init__(self, message: str):
        super().__init__("memory_error", message)


class SendError(MessagePipelineError):
    def __init__(self, message: str):
        super().__init__("send_error", message)


class PipelineExecutionError(MessagePipelineError):
    def __init__(self, message: str):
        super().__init__("pipeline_error", message)


def classify_pipeline_error(exc: Exception) -> str:
    if isinstance(exc, MessagePipelineError):
        return exc.category
    if isinstance(exc, asyncio.TimeoutError):
        return "model_request_error"
    if isinstance(exc, AIAPIError):
        return "model_request_error"
    if isinstance(exc, json.JSONDecodeError):
        return "model_parse_error"
    return "pipeline_error"


def wrap_model_request_error(exc: Exception, default_message: str = "模型请求失败") -> ModelRequestError:
    return ModelRequestError(str(exc) or default_message)


def wrap_model_parse_error(exc: Exception, default_message: str = "模型响应解析失败") -> ModelParseError:
    return ModelParseError(str(exc) or default_message)


def wrap_memory_error(exc: Exception, default_message: str = "记忆处理失败") -> MemoryOperationError:
    return MemoryOperationError(str(exc) or default_message)


def wrap_image_error(exc: Exception, default_message: str = "图片处理失败") -> ImageProcessingError:
    return ImageProcessingError(str(exc) or default_message)


def wrap_pipeline_error(exc: Exception | Any, default_message: str = "消息流水线处理失败") -> PipelineExecutionError:
    return PipelineExecutionError(str(exc) or default_message)
