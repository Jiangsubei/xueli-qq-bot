"""
配置文件 - 从 JSON 文件加载配置，支持热重载

使用方法:
1. 修改 config.json 文件
2. 在运行时调用 config.reload() 重新加载配置
   或通过配置 API 动态修改
"""
import os
import json
import time
from dataclasses import dataclass, field
from typing import Dict, Any, Optional
from threading import Lock


@dataclass
class BotConfig:
    """机器人配置 - 从 config.json 加载，支持热重载"""

    # ============================================
    # NapCat WebSocket 配置
    # ============================================
    NAPCAT_WS_URL: str = "ws://0.0.0.0:8095"
    NAPCAT_HTTP_URL: str = "http://127.0.0.1:6700"

    # ============================================
    # OpenAI 兼容服务配置（通用）
    # ============================================
    OPENAI_API_BASE: str = "https://api.openai.com/v1"
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-3.5-turbo"
    OPENAI_EXTRA_PARAMS: str = "{}"
    OPENAI_EXTRA_HEADERS: str = "{}"
    OPENAI_RESPONSE_PATH: str = "choices.0.message.content"

    # ============================================
    # 机器人行为配置
    # ============================================
    BOT_NAME: str = "AI助手"
    MAX_CONTEXT_LENGTH: int = 10
    MAX_MESSAGE_LENGTH: int = 4000
    RESPONSE_TIMEOUT: int = 60
    RATE_LIMIT_INTERVAL: float = 1.0

    # ============================================
    # 系统提示词
    # ============================================
    SYSTEM_PROMPT: str = """你是一个友好、有帮助的AI助手。你可以回答用户的各种问题，提供有用的建议和信息。
请保持回答简洁、准确，并尽可能提供有价值的内容。
如果用户询问你的身份，请告诉他们你是由AI驱动的QQ机器人助手。"""

    def __post_init__(self):
        """初始化配置"""
        self._config_file = os.path.join(os.path.dirname(__file__), "config.json")
        self._last_modified = 0
        self._lock = Lock()
        self._loaded = False

        # 加载配置
        self.load()

    def load(self) -> bool:
        """
        从 JSON 文件加载配置

        Returns:
            bool: 是否成功加载
        """
        with self._lock:
            if not os.path.exists(self._config_file):
                print(f"⚠️ 配置文件 {self._config_file} 不存在，使用默认配置")
                self._create_default_config()
                return False

            try:
                with open(self._config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                self._load_from_dict(data)
                self._last_modified = os.path.getmtime(self._config_file)
                self._loaded = True

                if not hasattr(self, '_silent_load'):
                    print(f"✅ 已从 {self._config_file} 加载配置")

                return True

            except Exception as e:
                print(f"⚠️ 加载配置文件失败: {e}，使用默认配置")
                return False

    def reload(self) -> bool:
        """
        重新加载配置（热重载）

        Returns:
            bool: 是否成功重新加载
        """
        self._silent_load = True  # 静默加载标记
        result = self.load()
        if result:
            print(f"✅ 配置已重新加载")
        delattr(self, '_silent_load')
        return result

    def auto_reload(self) -> bool:
        """
        自动检查并重新加载配置（如果文件有修改）

        Returns:
            bool: 是否重新加载了配置
        """
        if not os.path.exists(self._config_file):
            return False

        try:
            current_mtime = os.path.getmtime(self._config_file)
            if current_mtime > self._last_modified:
                print(f"📝 检测到配置文件修改，正在重新加载...")
                return self.reload()
        except Exception as e:
            print(f"⚠️ 检查配置文件时出错: {e}")

        return False

    def save(self) -> bool:
        """
        将当前配置保存到 JSON 文件

        Returns:
            bool: 是否成功保存
        """
        data = self._to_dict()

        try:
            with open(self._config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self._last_modified = os.path.getmtime(self._config_file)
            print(f"✅ 配置已保存到 {self._config_file}")
            return True
        except Exception as e:
            print(f"❌ 保存配置文件失败: {e}")
            return False

    def update(self, section: str, key: str, value: Any) -> bool:
        """
        更新单个配置项

        Args:
            section: 配置节名（如 'napcat', 'ai_service' 等）
            key: 配置项名
            value: 新值

        Returns:
            bool: 是否成功更新
        """
        attr_name = self._get_attr_name(section, key)
        if hasattr(self, attr_name):
            # 类型转换
            old_value = getattr(self, attr_name)
            if isinstance(old_value, int):
                value = int(value)
            elif isinstance(old_value, float):
                value = float(value)
            elif isinstance(old_value, str) and isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)

            setattr(self, attr_name, value)
            print(f"✅ 配置已更新: {section}.{key} = {value}")
            return True
        else:
            print(f"⚠️ 未知配置项: {section}.{key}")
            return False

    def _load_from_dict(self, data: dict):
        """从字典加载配置"""
        # NapCat 配置
        if "napcat" in data:
            napcat = data["napcat"]
            if "ws_url" in napcat:
                self.NAPCAT_WS_URL = napcat["ws_url"]
            if "http_url" in napcat:
                self.NAPCAT_HTTP_URL = napcat["http_url"]

        # AI 服务配置
        if "ai_service" in data:
            ai = data["ai_service"]
            if "api_base" in ai:
                self.OPENAI_API_BASE = ai["api_base"]
            if "api_key" in ai:
                self.OPENAI_API_KEY = ai["api_key"]
            if "model" in ai:
                self.OPENAI_MODEL = ai["model"]
            if "extra_params" in ai:
                self.OPENAI_EXTRA_PARAMS = json.dumps(ai["extra_params"], ensure_ascii=False)
            if "extra_headers" in ai:
                self.OPENAI_EXTRA_HEADERS = json.dumps(ai["extra_headers"], ensure_ascii=False)
            if "response_path" in ai:
                self.OPENAI_RESPONSE_PATH = ai["response_path"]

        # 机器人行为配置
        if "bot_behavior" in data:
            behavior = data["bot_behavior"]
            if "name" in behavior:
                self.BOT_NAME = behavior["name"]
            if "max_context_length" in behavior:
                self.MAX_CONTEXT_LENGTH = int(behavior["max_context_length"])
            if "max_message_length" in behavior:
                self.MAX_MESSAGE_LENGTH = int(behavior["max_message_length"])
            if "response_timeout" in behavior:
                self.RESPONSE_TIMEOUT = int(behavior["response_timeout"])
            if "rate_limit_interval" in behavior:
                self.RATE_LIMIT_INTERVAL = float(behavior["rate_limit_interval"])

        # 系统提示词
        if "system_prompt" in data:
            prompt = data["system_prompt"]
            if "content" in prompt:
                self.SYSTEM_PROMPT = prompt["content"]

    def _to_dict(self) -> dict:
        """将当前配置转换为字典"""
        return {
            "napcat": {
                "ws_url": self.NAPCAT_WS_URL,
                "http_url": self.NAPCAT_HTTP_URL
            },
            "ai_service": {
                "api_base": self.OPENAI_API_BASE,
                "api_key": self.OPENAI_API_KEY,
                "model": self.OPENAI_MODEL,
                "extra_params": json.loads(self.OPENAI_EXTRA_PARAMS) if self.OPENAI_EXTRA_PARAMS else {},
                "extra_headers": json.loads(self.OPENAI_EXTRA_HEADERS) if self.OPENAI_EXTRA_HEADERS else {},
                "response_path": self.OPENAI_RESPONSE_PATH
            },
            "bot_behavior": {
                "name": self.BOT_NAME,
                "max_context_length": self.MAX_CONTEXT_LENGTH,
                "max_message_length": self.MAX_MESSAGE_LENGTH,
                "response_timeout": self.RESPONSE_TIMEOUT,
                "rate_limit_interval": self.RATE_LIMIT_INTERVAL
            },
            "system_prompt": {
                "content": self.SYSTEM_PROMPT
            }
        }

    def _get_attr_name(self, section: str, key: str) -> str:
        """获取配置项对应的属性名"""
        mapping = {
            ("napcat", "ws_url"): "NAPCAT_WS_URL",
            ("napcat", "http_url"): "NAPCAT_HTTP_URL",
            ("ai_service", "api_base"): "OPENAI_API_BASE",
            ("ai_service", "api_key"): "OPENAI_API_KEY",
            ("ai_service", "model"): "OPENAI_MODEL",
            ("ai_service", "extra_params"): "OPENAI_EXTRA_PARAMS",
            ("ai_service", "extra_headers"): "OPENAI_EXTRA_HEADERS",
            ("ai_service", "response_path"): "OPENAI_RESPONSE_PATH",
            ("bot_behavior", "name"): "BOT_NAME",
            ("bot_behavior", "max_context_length"): "MAX_CONTEXT_LENGTH",
            ("bot_behavior", "max_message_length"): "MAX_MESSAGE_LENGTH",
            ("bot_behavior", "response_timeout"): "RESPONSE_TIMEOUT",
            ("bot_behavior", "rate_limit_interval"): "RATE_LIMIT_INTERVAL",
            ("system_prompt", "content"): "SYSTEM_PROMPT",
        }
        return mapping.get((section, key), "")

    def get_extra_params(self) -> Dict[str, Any]:
        """解析并返回额外请求参数"""
        try:
            return json.loads(self.OPENAI_EXTRA_PARAMS) if self.OPENAI_EXTRA_PARAMS else {}
        except json.JSONDecodeError:
            return {}

    def get_extra_headers(self) -> Dict[str, str]:
        """解析并返回额外请求头"""
        try:
            return json.loads(self.OPENAI_EXTRA_HEADERS) if self.OPENAI_EXTRA_HEADERS else {}
        except json.JSONDecodeError:
            return {}


# 全局配置实例
config = BotConfig()