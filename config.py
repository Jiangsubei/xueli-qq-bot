"""配置模块 - 从 config.json 加载"""
import json


class Config:
    """配置类 - 支持嵌套和扁平化访问"""

    # 配置映射表：扁平化名称 -> (节名, 键名)
    _MAPPING = {
        # NapCat
        'NAPCAT_WS_URL': ('napcat', 'ws_url'),
        'NAPCAT_HTTP_URL': ('napcat', 'http_url'),
        # AI 服务
        'OPENAI_API_BASE': ('ai_service', 'api_base'),
        'OPENAI_API_KEY': ('ai_service', 'api_key'),
        'OPENAI_MODEL': ('ai_service', 'model'),
        'OPENAI_EXTRA_PARAMS': ('ai_service', 'extra_params'),
        'OPENAI_EXTRA_HEADERS': ('ai_service', 'extra_headers'),
        'OPENAI_RESPONSE_PATH': ('ai_service', 'response_path'),
        # 机器人行为
        'BOT_NAME': ('bot_behavior', 'name'),
        'MAX_CONTEXT_LENGTH': ('bot_behavior', 'max_context_length'),
        'MAX_MESSAGE_LENGTH': ('bot_behavior', 'max_message_length'),
        'RESPONSE_TIMEOUT': ('bot_behavior', 'response_timeout'),
        'RATE_LIMIT_INTERVAL': ('bot_behavior', 'rate_limit_interval'),
        # 系统提示词
        'PERSONALITY': ('personality', 'content'),
        'DIALOGUE_STYLE': ('dialogue_style', 'content'),
        'BEHAVIOR': ('behavior', 'content'),
    }

    def __init__(self, path: str = "config.json"):
        """从 JSON 文件加载配置"""
        self._data = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except Exception as e:
            print(f"⚠️ 加载 {path} 失败: {e}，使用空配置")

    def __getattr__(self, name: str):
        """支持扁平化访问（如 config.OPENAI_API_BASE）"""
        # 先检查是否是已知配置项
        if name in self._MAPPING:
            section, key = self._MAPPING[name]
            if section in self._data and key in self._data[section]:
                value = self._data[section][key]
                # 类型转换
                if name in ['MAX_CONTEXT_LENGTH', 'MAX_MESSAGE_LENGTH', 'RESPONSE_TIMEOUT']:
                    return int(value)
                if name == 'RATE_LIMIT_INTERVAL':
                    return float(value)
                if name in ['OPENAI_EXTRA_PARAMS', 'OPENAI_EXTRA_HEADERS']:
                    if isinstance(value, dict):
                        import json
                        return json.dumps(value, ensure_ascii=False)
                return value
            return None

        # 否则尝试直接访问节（如 config.napcat）
        if name in self._data:
            return self._data[name]

        raise AttributeError(f"'Config' object has no attribute '{name}'")


# 全局配置实例
config = Config()