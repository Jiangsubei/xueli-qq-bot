"""AI service internals for the OpenAI-compatible client facade."""

from .error_mapper import (
    map_client_error,
    map_http_error,
    map_json_error,
    map_timeout_error,
)
from .request_builder import AIRequestBuilder
from .response_parser import AIResponseParser
from .session_manager import AIHTTPSessionManager
from .types import AIAPIError, AIResponse

__all__ = [
    "AIAPIError",
    "AIHTTPSessionManager",
    "AIRequestBuilder",
    "AIResponse",
    "AIResponseParser",
    "map_client_error",
    "map_http_error",
    "map_json_error",
    "map_timeout_error",
]
