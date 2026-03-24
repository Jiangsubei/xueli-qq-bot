"""
消息处理器模块
处理私聊和群聊消息，调用 AI 生成回复
"""
import asyncio
import logging
import time
from typing import Dict, Optional, List, Any
import re

from config import config
from models import MessageEvent, MessageType, Conversation
from ai_client import AIClient, AIAPIError

logger = logging.getLogger(__name__)


class MessageHandler:
    """消息处理器"""

    def __init__(self, ai_client: Optional[AIClient] = None):
        # 如果没有传入 ai_client，使用配置创建
        self.ai_client = ai_client or self._create_ai_client()
        self.conversations: Dict[str, Conversation] = {}
        self.last_send_time: Dict[str, float] = {}
        self.rate_limit_lock = asyncio.Lock()

        # 编译@机器人的正则表达式
        self.at_pattern = re.compile(rf'\[CQ:at,qq={config.BOT_NAME}\]|\[CQ:at,qq=\d+\]')

    def _create_ai_client(self) -> AIClient:
        """根据配置创建 AI 客户端"""
        logger.info(f"创建 AI 客户端: {config.OPENAI_API_BASE}, 模型: {config.OPENAI_MODEL}")
        return AIClient()

    def _get_conversation_key(self, event: MessageEvent) -> str:
        """获取对话的 key"""
        if event.message_type == MessageType.PRIVATE.value:
            return f"private:{event.user_id}"
        else:
            return f"group:{event.group_id}:{event.user_id}"

    def _get_conversation(self, key: str) -> Conversation:
        """获取或创建对话"""
        if key not in self.conversations:
            self.conversations[key] = Conversation()
        return self.conversations[key]

    def _clean_expired_conversations(self):
        """清理过期的对话"""
        expired_keys = [
            key for key, conv in self.conversations.items()
            if conv.is_expired()
        ]
        for key in expired_keys:
            del self.conversations[key]
            logger.debug(f"清理过期对话: {key}")

    def should_process(self, event: MessageEvent) -> bool:
        """
        判断是否应该处理这条消息

        - 私聊消息：直接处理
        - 群聊消息：需要 @ 机器人
        """
        # 忽略自己发送的消息
        if event.user_id == event.self_id:
            return False

        if event.message_type == MessageType.PRIVATE.value:
            # 私聊消息直接处理
            return True
        elif event.message_type == MessageType.GROUP.value:
            # 群聊消息需要 @ 机器人
            return event.is_at(event.self_id)

        return False

    def extract_user_message(self, event: MessageEvent) -> str:
        """
        提取用户发送的纯文本消息
        去除 @ 机器人部分
        """
        text = event.extract_text()

        # 如果是群聊，移除 @ 机器人的部分
        if event.message_type == MessageType.GROUP.value:
            # 移除 [CQ:at,qq=xxx] 格式的 @
            text = re.sub(r'\[CQ:at,qq=\d+\]', '', text)
            # 移除纯文本 @ 机器人名
            text = text.replace(f"@{config.BOT_NAME}", "")

        # 清理多余空格
        text = text.strip()

        return text

    def check_command(self, text: str, event: MessageEvent) -> Optional[str]:
        """
        检查是否为特殊命令

        返回:
            - command_result: 如果是命令，返回命令执行结果
            - None: 不是命令
        """
        text_lower = text.lower().strip()

        # /reset 清空历史
        if text_lower in ["/reset", "/清除", "/清空"]:
            key = self._get_conversation_key(event)
            if key in self.conversations:
                del self.conversations[key]
            return "✅ 对话历史已清空"

        # /help 显示帮助
        if text_lower in ["/help", "/帮助", "帮助"]:
            return self._get_help_text()

        # /status 查看状态
        if text_lower in ["/status", "/状态"]:
            return self._get_status_text()

        return None

    def _get_help_text(self) -> str:
        """获取帮助文本"""
        return f"""🤖 {config.BOT_NAME} 使用帮助

💬 基本使用：
• 私聊：直接发送消息即可
• 群聊：@{config.BOT_NAME} + 消息

📝 可用命令：
• /reset 或 /清除 - 清空当前对话历史
• /help 或 /帮助 - 显示此帮助信息
• /status 或 /状态 - 查看机器人状态

⚠️ 注意事项：
• 对话历史默认保留最近 {config.MAX_CONTEXT_LENGTH} 轮
• 单条消息最大长度 {config.MAX_MESSAGE_LENGTH} 字符
• 请文明用语，遵守群规
"""

    def _get_status_text(self) -> str:
        """获取状态文本"""
        active_conversations = len(self.conversations)
        return f"""📊 {config.BOT_NAME} 状态

🟢 运行状态：正常
💬 活跃对话数：{active_conversations}
🤖 AI 服务：{config.OPENAI_API_BASE}
📝 模型：{config.OPENAI_MODEL}
⏱️ 响应超时：{config.RESPONSE_TIMEOUT}秒
📏 消息长度限制：{config.MAX_MESSAGE_LENGTH}字符
"""

    async def check_rate_limit(self, target_id: str) -> bool:
        """
        检查发送频率限制

        Returns:
            True: 可以发送
            False: 需要等待
        """
        async with self.rate_limit_lock:
            now = time.time()
            last_time = self.last_send_time.get(target_id, 0)

            if now - last_time < config.RATE_LIMIT_INTERVAL:
                wait_time = config.RATE_LIMIT_INTERVAL - (now - last_time)
                logger.debug(f"频率限制，需要等待 {wait_time:.2f} 秒")
                await asyncio.sleep(wait_time)

            self.last_send_time[target_id] = time.time()
            return True

    async def get_ai_response(self, event: MessageEvent) -> str:
        """
        调用 AI API 获取回复
        """
        user_message = self.extract_user_message(event)

        # 检查命令
        command_result = self.check_command(user_message, event)
        if command_result is not None:
            return command_result

        # 获取对话历史
        key = self._get_conversation_key(event)
        conversation = self._get_conversation(key)

        # 构建消息列表
        messages = [
            {"role": "system", "content": config.SYSTEM_PROMPT},
            *conversation.get_messages(config.MAX_CONTEXT_LENGTH),
            {"role": "user", "content": user_message}
        ]

        try:
            # 调用 AI API
            logger.info(f"调用 AI API，用户: {event.user_id}")
            response = await self.ai_client.chat_completion(
                messages=messages,
                temperature=0.7
            )

            # 更新对话历史
            conversation.add_message("user", user_message)
            conversation.add_message("assistant", response.content)

            logger.info(f"AI 响应成功，长度: {len(response.content)}")
            return response.content

        except AIAPIError as e:
            logger.error(f"AI API 错误: {e}")
            return f"❌ AI 服务暂时不可用，请稍后重试\n错误信息: {str(e)}"
        except Exception as e:
            logger.error(f"调用 AI 时发生错误: {e}", exc_info=True)
            return f"❌ 处理消息时出错，请稍后重试"

    def split_long_message(self, message: str) -> List[str]:
        """
        将长消息分割成多个短消息

        QQ 单条消息约 4500 字符限制，这里设置更安全的值
        """
        max_length = config.MAX_MESSAGE_LENGTH

        if len(message) <= max_length:
            return [message]

        parts = []
        current_part = ""

        # 按行分割，尽量保持段落完整
        lines = message.split('\n')

        for line in lines:
            # 如果单行就超过限制，需要进一步分割
            if len(line) > max_length:
                # 先保存当前累积的内容
                if current_part:
                    parts.append(current_part)
                    current_part = ""

                # 按字符分割长行
                for i in range(0, len(line), max_length):
                    parts.append(line[i:i + max_length])
                continue

            # 检查添加这行后是否超过限制
            if len(current_part) + len(line) + 1 > max_length:
                parts.append(current_part)
                current_part = line
            else:
                if current_part:
                    current_part += '\n' + line
                else:
                    current_part = line

        # 添加最后一部分
        if current_part:
            parts.append(current_part)

        return parts


class AIAPIError(Exception):
    """AI API 错误"""
    pass