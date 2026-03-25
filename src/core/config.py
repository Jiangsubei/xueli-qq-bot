"""配置模块 - 从 config.json 加载。"""
import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)


class Config:
    """配置类，支持嵌套与扁平化访问。"""

    _MAPPING = {
        # NapCat
        "NAPCAT_WS_URL": ("napcat", "ws_url"),
        "NAPCAT_HTTP_URL": ("napcat", "http_url"),
        # AI 服务
        "OPENAI_API_BASE": ("ai_service", "api_base"),
        "OPENAI_API_KEY": ("ai_service", "api_key"),
        "OPENAI_MODEL": ("ai_service", "model"),
        "OPENAI_EXTRA_PARAMS": ("ai_service", "extra_params"),
        "OPENAI_EXTRA_HEADERS": ("ai_service", "extra_headers"),
        "OPENAI_RESPONSE_PATH": ("ai_service", "response_path"),
        # 机器人行为
        "BOT_NAME": ("bot_behavior", "name"),
        "MAX_CONTEXT_LENGTH": ("bot_behavior", "max_context_length"),
        "MAX_MESSAGE_LENGTH": ("bot_behavior", "max_message_length"),
        "RESPONSE_TIMEOUT": ("bot_behavior", "response_timeout"),
        "RATE_LIMIT_INTERVAL": ("bot_behavior", "rate_limit_interval"),
        "LOG_FULL_PROMPT": ("bot_behavior", "log_full_prompt"),
        # 系统提示词
        "PERSONALITY": ("personality", "content"),
        "DIALOGUE_STYLE": ("dialogue_style", "content"),
        "BEHAVIOR": ("behavior", "content"),
        # 记忆模块
        "MEMORY_ENABLED": ("memory", "enabled"),
        "MEMORY_STORAGE_PATH": ("memory", "storage_path"),
        "MEMORY_BM25_TOP_K": ("memory", "bm25_top_k"),
        "MEMORY_RERANK_ENABLED": ("memory", "rerank_enabled"),
        "MEMORY_RERANK_TOP_K": ("memory", "rerank_top_k"),
        "MEMORY_AUTO_EXTRACT": ("memory", "auto_extract"),
        "MEMORY_EXTRACT_EVERY_N_TURNS": ("memory", "extract_every_n_turns"),
        "MEMORY_CONVERSATION_SAVE_INTERVAL": ("memory", "conversation_save_interval"),
        "MEMORY_EXTRACTION_API_BASE": ("memory", "extraction_api_base"),
        "MEMORY_EXTRACTION_API_KEY": ("memory", "extraction_api_key"),
        "MEMORY_EXTRACTION_MODEL": ("memory", "extraction_model"),
        "MEMORY_EXTRACTION_EXTRA_PARAMS": ("memory", "extraction_extra_params"),
        "MEMORY_EXTRACTION_EXTRA_HEADERS": ("memory", "extraction_extra_headers"),
        "MEMORY_EXTRACTION_RESPONSE_PATH": ("memory", "extraction_response_path"),
        "MEMORY_ORDINARY_DECAY_ENABLED": ("memory", "ordinary_decay_enabled"),
        "MEMORY_ORDINARY_HALF_LIFE_DAYS": ("memory", "ordinary_half_life_days"),
        "MEMORY_ORDINARY_FORGET_THRESHOLD": ("memory", "ordinary_forget_threshold"),
    }

    def __init__(self, path: str = None):
        """从 JSON 文件加载配置。"""
        self._data: Dict[str, Any] = {}

        if path is None:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            possible_paths = [
                os.path.join(current_dir, "config.json"),
                os.path.join(current_dir, "..", "..", "config.json"),
                os.path.join(os.getcwd(), "config.json"),
            ]
            for candidate in possible_paths:
                normalized = os.path.normpath(candidate)
                if os.path.exists(normalized):
                    path = normalized
                    break
            else:
                path = os.path.normpath(possible_paths[1])

        try:
            with open(path, "r", encoding="utf-8") as file:
                raw_text = file.read()
                self._data = json.loads(self._strip_json_comments(raw_text))
            logger.info("配置加载成功: %s", path)
        except Exception as exc:
            logger.warning("加载配置失败 %s: %s，将使用空配置", path, exc)

    def __getattr__(self, name: str):
        """支持扁平化访问，如 config.OPENAI_API_BASE。"""
        if name in self._MAPPING:
            section, key = self._MAPPING[name]
            if section in self._data and key in self._data[section]:
                value = self._data[section][key]
                if name in ["MAX_CONTEXT_LENGTH", "MAX_MESSAGE_LENGTH", "RESPONSE_TIMEOUT"]:
                    return int(value)
                if name in [
                    "RATE_LIMIT_INTERVAL",
                    "MEMORY_ORDINARY_HALF_LIFE_DAYS",
                    "MEMORY_ORDINARY_FORGET_THRESHOLD",
                ]:
                    return float(value)
                if name in [
                    "OPENAI_EXTRA_PARAMS",
                    "OPENAI_EXTRA_HEADERS",
                    "MEMORY_EXTRACTION_EXTRA_PARAMS",
                    "MEMORY_EXTRACTION_EXTRA_HEADERS",
                ] and isinstance(value, dict):
                    return json.dumps(value, ensure_ascii=False)
                return value
            return None

        if name in self._data:
            return self._data[name]

        raise AttributeError(f"'Config' object has no attribute '{name}'")

    def _parse_json_object(self, value: Any) -> Dict[str, Any]:
        """将配置值解析为字典。"""
        try:
            if isinstance(value, dict):
                return value
            return json.loads(value) if value else {}
        except Exception:
            return {}

    def _strip_json_comments(self, text: str) -> str:
        """Strip // and /* */ comments while keeping string content intact."""
        result = []
        index = 0
        in_string = False
        escaped = False
        length = len(text)

        while index < length:
            char = text[index]
            next_char = text[index + 1] if index + 1 < length else ""

            if in_string:
                result.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                index += 1
                continue

            if char == '"':
                in_string = True
                result.append(char)
                index += 1
                continue

            if char == "/" and next_char == "/":
                index += 2
                while index < length and text[index] not in "\r\n":
                    index += 1
                continue

            if char == "/" and next_char == "*":
                index += 2
                while index + 1 < length and not (text[index] == "*" and text[index + 1] == "/"):
                    index += 1
                index += 2
                continue

            result.append(char)
            index += 1

        return "".join(result)

    def get_extra_params(self) -> Dict[str, Any]:
        """解析并返回主对话额外请求参数。"""
        return self._parse_json_object(self.OPENAI_EXTRA_PARAMS)

    def get_extra_headers(self) -> Dict[str, str]:
        """解析并返回主对话额外请求头。"""
        return self._parse_json_object(self.OPENAI_EXTRA_HEADERS)

    def get_memory_extraction_extra_params(self) -> Dict[str, Any]:
        """返回记忆提取额外参数，未配置时回退到主对话配置。"""
        value = self.MEMORY_EXTRACTION_EXTRA_PARAMS
        if value is None:
            return self.get_extra_params()
        return self._parse_json_object(value)

    def get_memory_extraction_extra_headers(self) -> Dict[str, str]:
        """返回记忆提取额外请求头，未配置时回退到主对话配置。"""
        value = self.MEMORY_EXTRACTION_EXTRA_HEADERS
        if value is None:
            return self.get_extra_headers()
        return self._parse_json_object(value)

    def get_memory_extraction_client_config(self) -> Dict[str, Any]:
        """返回记忆提取使用的 AIClient 配置，未配置时回退到主对话模型。"""
        return {
            "api_base": self.MEMORY_EXTRACTION_API_BASE or self.OPENAI_API_BASE,
            "api_key": self.MEMORY_EXTRACTION_API_KEY or self.OPENAI_API_KEY,
            "model": self.MEMORY_EXTRACTION_MODEL or self.OPENAI_MODEL,
            "extra_params": self.get_memory_extraction_extra_params(),
            "extra_headers": self.get_memory_extraction_extra_headers(),
            "response_path": self.MEMORY_EXTRACTION_RESPONSE_PATH or self.OPENAI_RESPONSE_PATH,
        }


config = Config()
