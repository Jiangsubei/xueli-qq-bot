"""
AI API 客户端模块 - 通用 OpenAI 兼容实现
支持任意遵循 OpenAI API 规范的服务
"""
import asyncio
import json
import logging
from typing import List, Dict, Any, Optional, AsyncGenerator
import aiohttp
from dataclasses import dataclass

from config import config

logger = logging.getLogger(__name__)


@dataclass
class AIResponse:
    """AI 响应"""
    content: str
    usage: Optional[Dict[str, int]] = None
    model: str = ""
    finish_reason: str = ""
    raw_response: Optional[Dict[str, Any]] = None  # 保存完整响应用于调试


class AIClient:
    """
    通用 OpenAI 兼容 API 客户端

    支持任意遵循 OpenAI API 规范的服务:
    - OpenAI (api.openai.com)
    - DeepSeek (api.deepseek.com)
    - OpenRouter (openrouter.ai)
    - Azure OpenAI
    - 本地 Ollama
    - 其他兼容服务
    """

    def __init__(
        self,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 60,
        extra_params: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        response_path: Optional[str] = None
    ):
        """
        初始化 AI 客户端

        Args:
            api_base: API 基础 URL，如 https://api.openai.com/v1
            api_key: API 密钥
            model: 模型名称
            timeout: 请求超时时间（秒）
            extra_params: 额外请求参数
            extra_headers: 额外请求头
            response_path: 响应内容提取路径
        """
        # 优先使用传入参数，否则使用配置
        self.api_base = (api_base or config.OPENAI_API_BASE).rstrip('/')
        self.api_key = api_key or config.OPENAI_API_KEY
        self.model = model or config.OPENAI_MODEL
        self.timeout = timeout

        # 额外参数和请求头
        self.extra_params = extra_params or config.get_extra_params()
        self.extra_headers = extra_headers or config.get_extra_headers()
        self.response_path = response_path or config.OPENAI_RESPONSE_PATH

        # 构建完整 API URL
        self.chat_completions_url = f"{self.api_base}/chat/completions"

        # HTTP 会话
        self.session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(5)  # 限制并发请求数

        logger.debug(f"AIClient 初始化: url={self.chat_completions_url}, model={self.model}")

    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self._init_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        await self.close()

    async def _init_session(self):
        """初始化 HTTP 会话"""
        if not self.session:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            # 合并额外请求头
            headers.update(self.extra_headers)

            self.session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )

    async def close(self):
        """关闭 HTTP 会话"""
        if self.session:
            await self.session.close()
            self.session = None

    def _build_request_body(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        **kwargs
    ) -> Dict[str, Any]:
        """
        构建请求体

        Args:
            messages: 消息列表
            temperature: 采样温度
            max_tokens: 最大生成 token 数
            stream: 是否流式输出
            **kwargs: 其他参数

        Returns:
            请求体字典
        """
        body = {
            "model": self.model,
            "messages": messages,
            "stream": stream
        }

        # 添加可选参数
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        # 合并额外参数（来自配置或构造时传入）
        body.update(self.extra_params)

        # 合并调用时传入的参数
        body.update(kwargs)

        return body

    def _extract_content(self, data: Dict[str, Any]) -> str:
        """
        从响应数据中提取内容

        支持可配置的响应路径，如:
        - choices.0.message.content (标准 OpenAI)
        - output.choices.0.message.content (某些变体)
        - text.0 (某些简化服务)

        Args:
            data: 响应数据字典

        Returns:
            提取的内容字符串
        """
        try:
            # 按路径分割
            path_parts = self.response_path.split('.')

            current = data
            for part in path_parts:
                # 处理数组索引，如 "0"
                if part.isdigit():
                    current = current[int(part)]
                else:
                    current = current[part]

            return str(current) if current else ""

        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"无法从路径 '{self.response_path}' 提取内容: {e}")
            logger.debug(f"响应数据: {data}")
            # 返回备用内容
            return data.get("content", "") or data.get("text", "") or ""

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        **kwargs
    ) -> AIResponse:
        """
        发起聊天补全请求

        这是通用的 OpenAI 兼容 API 调用方法，支持:
        - OpenAI (api.openai.com)
        - DeepSeek (api.deepseek.com)
        - OpenRouter (openrouter.ai)
        - Azure OpenAI
        - 本地 Ollama
        - 其他兼容服务

        Args:
            messages: 消息列表，格式为 [
                {"role": "system", "content": "..."},
                {"role": "user", "content": "..."}
            ]
            temperature: 采样温度 (0-2)
            max_tokens: 最大生成 token 数
            stream: 是否使用流式输出
            **kwargs: 其他参数，会合并到请求体中

        Returns:
            AIResponse 对象，包含:
            - content: AI 生成的回复内容
            - usage: token 使用情况 (如果 API 返回)
            - model: 实际使用的模型名称
            - finish_reason: 生成结束原因
            - raw_response: 完整的 API 响应 (用于调试)

        Raises:
            AIAPIError: 当 API 请求失败时
            asyncio.TimeoutError: 当请求超时时
        """
        await self._init_session()

        # 构建请求体
        body = self._build_request_body(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            **kwargs
        )

        async with self._semaphore:
            try:
                logger.debug(f"发送请求到 {self.chat_completions_url}")
                logger.debug(f"请求体: {body}")

                async with self.session.post(
                    self.chat_completions_url,
                    json=body
                ) as response:
                    response_text = await response.text()

                    if response.status != 200:
                        logger.error(f"API 请求失败: {response.status}, {response_text}")
                        raise AIAPIError(
                            f"API 请求失败: {response.status}, {response_text[:500]}"
                        )

                    # 解析响应
                    try:
                        data = json.loads(response_text)
                    except json.JSONDecodeError as e:
                        logger.error(f"JSON 解析失败: {e}, 响应: {response_text[:500]}")
                        raise AIAPIError(f"无法解析 API 响应: {e}")

                    return self._parse_response(data)

            except aiohttp.ClientError as e:
                logger.error(f"HTTP 请求失败: {e}")
                raise AIAPIError(f"HTTP 请求失败: {e}")
            except asyncio.TimeoutError:
                logger.error(f"请求超时 (>{self.timeout}秒)")
                raise AIAPIError(f"请求超时，请稍后重试")

    def _parse_response(self, data: Dict[str, Any]) -> AIResponse:
        """
        解析 API 响应

        Args:
            data: API 返回的 JSON 数据

        Returns:
            AIResponse 对象
        """
        try:
            # 提取内容
            content = self._extract_content(data)

            # 尝试提取标准字段
            choice = data.get("choices", [{}])[0] if data.get("choices") else {}
            if not choice:
                # 某些服务可能格式不同
                choice = data

            finish_reason = choice.get("finish_reason", "")
            usage = data.get("usage")
            model = data.get("model", self.model)

            return AIResponse(
                content=content,
                usage=usage,
                model=model,
                finish_reason=finish_reason,
                raw_response=data  # 保存完整响应用于调试
            )

        except Exception as e:
            logger.error(f"解析响应失败: {e}, 数据: {str(data)[:500]}")
            # 尝试返回原始内容
            return AIResponse(
                content=str(data)[:1000],
                model=self.model,
                raw_response=data
            )


class AIAPIError(Exception):
    """AI API 错误"""
    pass