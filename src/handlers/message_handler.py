"""
消息处理器模块。

负责处理私聊和群聊消息，调用 AI 生成回复，并接入长期记忆能力。
"""
import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from src.core.config import config
from src.core.models import Conversation, MessageEvent, MessageType
from src.services.ai_client import AIAPIError, AIClient
from src.services.image_client import ImageClient

try:
    from src.memory import MemoryManager

    MEMORY_MODULE_AVAILABLE = True
except ImportError:
    MEMORY_MODULE_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("记忆模块不可用，相关功能已关闭")

logger = logging.getLogger(__name__)


class MessageHandler:
    """消息处理器。"""

    def __init__(
        self,
        ai_client: Optional[AIClient] = None,
        image_client: Optional[ImageClient] = None,
        memory_manager: Optional[Any] = None,
    ):
        self.ai_client = ai_client or self._create_ai_client()
        self.image_client = image_client or ImageClient()
        self.conversations: Dict[str, Conversation] = {}
        self.last_send_time: Dict[str, float] = {}
        self.rate_limit_lock = asyncio.Lock()
        self.memory_manager = memory_manager
        self._memory_initialized = False
        self.at_pattern = re.compile(rf"\[CQ:at,qq={config.BOT_NAME}\]|\[CQ:at,qq=\d+\]")

    def _create_ai_client(self) -> AIClient:
        """根据配置创建 AI 客户端。"""
        logger.info(f"初始化 AI 客户端: 模型={config.OPENAI_MODEL}")
        return AIClient()

    def _build_system_prompt(self) -> str:
        """构建完整系统提示词。"""
        parts = []

        if hasattr(config, "PERSONALITY") and config.PERSONALITY:
            parts.append(f"【人格】\n{config.PERSONALITY}")
        if hasattr(config, "DIALOGUE_STYLE") and config.DIALOGUE_STYLE:
            parts.append(f"【对话风格】\n{config.DIALOGUE_STYLE}")
        if hasattr(config, "BEHAVIOR") and config.BEHAVIOR:
            parts.append(f"【行为】\n{config.BEHAVIOR}")

        return "\n\n".join(parts) if parts else "你是一个友好、有帮助的 AI 助手。"

    def _build_memory_tools(self) -> List[Dict[str, Any]]:
        """Return tool definitions exposed to the chat model."""
        if not self.memory_manager:
            return []

        return [
            {
                "type": "function",
                "function": {
                    "name": "remember_important_memory",
                    "description": (
                        "当用户明确要求你记住某件事，或明确要求未来长期遵守某个偏好、禁忌、称呼、规则时，"
                        "调用此工具将该内容写入重要记忆。只要你已经判断需要长期记住，就应调用此工具，不要只在文字里答应。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "需要写入重要记忆的内容，使用简洁明确的中文陈述句。",
                            },
                            "priority": {
                                "type": "integer",
                                "description": "重要程度，1 到 5。用户明确要求记住时通常用 5。",
                                "minimum": 1,
                                "maximum": 5,
                            },
                        },
                        "required": ["content"],
                        "additionalProperties": False,
                    },
                },
            }
        ]

    def _augment_system_prompt_for_tools(self, system_prompt: str, tools: List[Dict[str, Any]]) -> str:
        """Add concise tool-usage guidance to the system prompt."""
        if not tools:
            return system_prompt

        tool_instruction = (
            "你可以使用 remember_important_memory 工具写入重要记忆。\n"
            "当出现以下情况时，应优先调用该工具，而不是只在回复里口头答应：\n"
            "1. 用户明确要求你记住某件事，例如“记住”“记得”“别忘了”。\n"
            "2. 用户明确要求你以后长期遵守某条规则、称呼、偏好、禁忌或约束。\n"
            "3. 用户明确说明某条信息未来会长期有效，希望你后续持续按此执行。\n"
            "如果只是普通聊天、临时话题、一次性感受，不要调用。\n"
            "如果已经决定调用工具，应先调用工具完成保存，再继续正常回复用户。"
        )
        return f"{tool_instruction}\n\n{system_prompt}"

    async def _execute_tool_call(self, tool_call: Dict[str, Any], user_id: str) -> Dict[str, Any]:
        """Execute a single model-requested tool call."""
        tool_call_id = tool_call.get("id", "")
        function_info = tool_call.get("function", {}) or {}
        tool_name = function_info.get("name", "")
        raw_arguments = function_info.get("arguments", "") or "{}"

        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            arguments = {}

        if tool_name != "remember_important_memory" or not self.memory_manager:
            return {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps(
                    {"ok": False, "error": f"unsupported tool: {tool_name}"},
                    ensure_ascii=False,
                ),
            }

        content = str(arguments.get("content", "")).strip()
        priority = arguments.get("priority", 5)

        try:
            priority_value = int(priority)
        except (TypeError, ValueError):
            priority_value = 5
        priority_value = max(1, min(priority_value, 5))

        if not content:
            result = {"ok": False, "error": "content is required"}
        else:
            memory = await self.memory_manager.add_important_memory(
                user_id=user_id,
                content=content,
                source="tool_call",
                priority=priority_value,
            )
            result = {
                "ok": memory is not None,
                "content": content,
                "priority": priority_value,
            }
            logger.info("模型调用重要记忆工具: 用户=%s, 内容=%s", user_id, content[:50])

        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(result, ensure_ascii=False),
        }

    async def _chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        user_id: str,
        temperature: float = 0.7,
    ) -> AIResponse:
        """Run a small tool-calling loop for the chat model."""
        tools = self._build_memory_tools()
        tools_enabled = bool(tools)
        working_messages = list(messages)
        response = None

        for _ in range(3):
            request_kwargs: Dict[str, Any] = {
                "messages": working_messages,
                "temperature": temperature,
            }
            if tools_enabled:
                request_kwargs["tools"] = tools
                request_kwargs["tool_choice"] = "auto"

            try:
                response = await self.ai_client.chat_completion(**request_kwargs)
            except AIAPIError as exc:
                if tools_enabled:
                    logger.warning("当前模型或上游可能不支持工具调用，已回退普通对话: %s", exc)
                    tools_enabled = False
                    continue
                raise

            tool_calls = response.tool_calls or []
            if not tool_calls:
                return response

            assistant_tool_message: Dict[str, Any] = {
                "role": "assistant",
                "tool_calls": tool_calls,
            }
            if response.content:
                assistant_tool_message["content"] = response.content
            else:
                assistant_tool_message["content"] = ""
            working_messages.append(assistant_tool_message)

            for tool_call in tool_calls:
                tool_message = await self._execute_tool_call(tool_call, user_id=user_id)
                working_messages.append(tool_message)

        return response or AIResponse(content="")

    def _format_prompt_message_content(self, content: Any) -> str:
        """把消息内容格式化为可读文本。"""
        if isinstance(content, str):
            return content

        if isinstance(content, list):
            lines = []
            image_count = 0
            for part in content:
                if not isinstance(part, dict):
                    lines.append(str(part))
                    continue

                part_type = part.get("type")
                if part_type == "text":
                    text = part.get("text", "")
                    if text:
                        lines.append(text)
                elif part_type == "image_url":
                    image_count += 1

            if image_count:
                lines.append(f"[图片 {image_count} 张]")

            return "\n".join(lines) if lines else str(content)

        return str(content)

    def _format_full_prompt_log(self, event: MessageEvent, messages: List[Dict[str, Any]]) -> str:
        """只输出实际发送给模型的系统提示词。"""
        system_message = next(
            (message for message in messages if message.get("role") == "system"),
            None,
        )
        system_content = ""
        if system_message:
            system_content = self._format_prompt_message_content(system_message.get("content", ""))

        lines = [
            "[SYSTEM PROMPT]",
            f"用户: {event.user_id}",
            f"会话: {self._get_conversation_key(event)}",
            "",
            system_content if system_content else "[空]",
        ]
        return "\n".join(lines).rstrip()

    def _format_system_prompt_log_with_history(
        self,
        event: MessageEvent,
        messages: List[Dict[str, Any]],
        related_history_messages: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """输出系统提示词，并附带本次请求的关联历史。"""
        system_message = next(
            (message for message in messages if message.get("role") == "system"),
            None,
        )
        system_content = ""
        if system_message:
            system_content = self._format_prompt_message_content(system_message.get("content", ""))

        lines = [
            "[SYSTEM PROMPT]",
            f"用户: {event.user_id}",
            f"会话: {self._get_conversation_key(event)}",
            "",
            system_content if system_content else "[空]",
        ]

        history_messages = related_history_messages or []
        if history_messages:
            lines.extend(["", "--- 关联历史 ---"])
            pair_index = 0
            pending_user = ""

            for message in history_messages:
                role = message.get("role", "user")
                content = self._format_prompt_message_content(message.get("content", ""))

                if role == "user":
                    pending_user = content
                    continue

                if role == "assistant":
                    pair_index += 1
                    lines.append(f"{pair_index}. 用户: {pending_user or '[空]'}")
                    lines.append(f"   助手: {content or '[空]'}")
                    pending_user = ""

            if pending_user:
                pair_index += 1
                lines.append(f"{pair_index}. 用户: {pending_user}")

        return "\n".join(lines).rstrip()

    def _get_conversation_key(self, event: MessageEvent) -> str:
        """获取会话键。"""
        if event.message_type == MessageType.PRIVATE.value:
            return f"private:{event.user_id}"
        return f"group:{event.group_id}:{event.user_id}"

    def _get_conversation(self, key: str) -> Conversation:
        """获取或创建对话对象。"""
        if key not in self.conversations:
            self.conversations[key] = Conversation()
        return self.conversations[key]

    def _clean_expired_conversations(self):
        """清理过期对话。"""
        expired_keys = [
            key for key, conv in self.conversations.items() if conv.is_expired()
        ]
        for key in expired_keys:
            del self.conversations[key]
            logger.debug(f"清理过期对话: {key}")

    def should_process(self, event: MessageEvent) -> bool:
        """判断是否需要处理该消息。"""
        if event.user_id == event.self_id:
            return False

        if event.message_type == MessageType.PRIVATE.value:
            return True
        if event.message_type == MessageType.GROUP.value:
            return event.is_at(event.self_id)
        return False

    def extract_user_message(self, event: MessageEvent) -> str:
        """提取用户发送的纯文本消息。"""
        text = event.extract_text()

        if event.message_type == MessageType.GROUP.value:
            text = re.sub(r"\[CQ:at,qq=\d+\]", "", text)
            text = text.replace(f"@{config.BOT_NAME}", "")

        return text.strip()

    async def download_images(self, event: MessageEvent) -> List[str]:
        """下载消息中的所有图片并转成 base64。"""
        image_segments = event.get_image_segments()
        if not image_segments:
            return []

        base64_images = []
        for seg in image_segments:
            try:
                base64_data = await self.image_client.process_image_segment(seg.data)
                if base64_data:
                    base64_images.append(base64_data)
                    logger.debug("图片处理成功")
                else:
                    logger.warning("图片处理失败")
            except Exception as e:
                logger.error(f"处理图片失败: {e}", exc_info=True)

        return base64_images

    def check_command(self, text: str, event: MessageEvent) -> Optional[str]:
        """检查是否为内置命令。"""
        text_lower = text.lower().strip()

        if text_lower in ["/reset", "/清除", "/清空"]:
            key = self._get_conversation_key(event)
            if key in self.conversations:
                del self.conversations[key]
            return "对话历史已清空"

        if text_lower in ["/help", "/帮助", "帮助"]:
            return self._get_help_text()

        if text_lower in ["/status", "/状态"]:
            return self._get_status_text()

        return None

    def _get_help_text(self) -> str:
        """获取帮助文本。"""
        return f"""{config.BOT_NAME} 使用帮助

基本使用：
私聊时直接发送消息即可
群聊时请先 @ 我

可用命令：
/reset 或 /清除：清空当前对话历史
/help 或 /帮助：显示帮助
/status 或 /状态：查看当前状态"""

    def _get_status_text(self) -> str:
        """获取状态文本。"""
        active_conversations = len(self.conversations)
        return f"""{config.BOT_NAME} 状态
运行状态：正常
活跃对话数：{active_conversations}
AI 服务：{config.OPENAI_API_BASE}
模型：{config.OPENAI_MODEL}
响应超时：{config.RESPONSE_TIMEOUT} 秒
消息长度限制：{config.MAX_MESSAGE_LENGTH} 字符"""

    async def check_rate_limit(self, target_id: str) -> bool:
        """检查并执行发送频率限制。"""
        async with self.rate_limit_lock:
            now = time.time()
            last_time = self.last_send_time.get(target_id, 0)

            if now - last_time < config.RATE_LIMIT_INTERVAL:
                wait_time = config.RATE_LIMIT_INTERVAL - (now - last_time)
                logger.debug(f"触发频率限制，等待 {wait_time:.2f} 秒")
                await asyncio.sleep(wait_time)

            self.last_send_time[target_id] = time.time()
            return True

    async def get_ai_response(self, event: MessageEvent) -> str:
        """调用 AI 接口生成回复。"""
        user_message = self.extract_user_message(event)

        command_result = self.check_command(user_message, event)
        if command_result is not None:
            return command_result

        has_images = event.has_image()
        base64_images = []
        if has_images:
            logger.info("检测到图片，开始处理")
            base64_images = await self.download_images(event)
            logger.info(f"图片处理完成: {len(base64_images)} 张")

        key = self._get_conversation_key(event)
        conversation = self._get_conversation(key)

        memory_context = ""
        important_context = ""
        related_history_messages: List[Dict[str, Any]] = []

        conversation_message_count = len(conversation.get_messages())
        is_first_turn = conversation_message_count == 0

        logger.debug(
            f"记忆检索准备: memory={'开' if self.memory_manager else '关'}, "
            f"消息={user_message[:50] if user_message else '空'}"
        )

        if self.memory_manager and user_message:
            try:
                logger.info(f"开始检索记忆: 用户={event.user_id}, 消息={user_message[:30]}")

                if is_first_turn:
                    important_memories = await self.memory_manager.get_important_memories(
                        user_id=str(event.user_id),
                        limit=5,
                    )
                    if important_memories:
                        important_context = "=== 重要事实（请务必记住并严格遵守）===\n"
                        for i, mem in enumerate(important_memories, 1):
                            important_context += f"{i}. {mem.content}\n"
                        important_context += "\n"
                        logger.info(f"新会话加载重要记忆: {len(important_memories)} 条")
                else:
                    logger.debug(f"非首轮，跳过重要记忆预加载: 轮数={conversation_message_count}")

                if is_first_turn:
                    memory_context = important_context
                    if important_context:
                        logger.info("新会话首轮仅注入重要记忆")
                else:
                    logger.info("按当前消息检索相关记忆")
                    search_result = await self.memory_manager.search_memories_with_context(
                        user_id=str(event.user_id),
                        query=user_message,
                        top_k=3,
                        include_conversations=True,
                    )

                    relevant_memories = search_result.get("memories", [])
                    related_history_messages = search_result.get("history_messages", [])

                    if relevant_memories:
                        dynamic_context = "=== 相关背景信息 ===\n"
                        for i, mem in enumerate(relevant_memories, 1):
                            mem_content = mem.get("content", "") if isinstance(mem, dict) else getattr(mem, "content", "")
                            if mem_content:
                                dynamic_context += f"{i}. {mem_content}\n"
                        dynamic_context += "\n"

                        memory_context = important_context + dynamic_context
                        logger.info(f"相关记忆命中: {len(relevant_memories)} 条")
                        if related_history_messages:
                            logger.info(f"已加载记忆关联对话: {len(related_history_messages) // 2} 轮")
                    else:
                        memory_context = important_context
                        if important_context:
                            logger.info("未命中普通记忆，仅保留重要记忆")

            except Exception as e:
                logger.warning(f"记忆检索失败: {e}")

        system_prompt = self._build_system_prompt()
        if memory_context:
            system_prompt = memory_context + "\n\n" + system_prompt
        system_prompt = self._augment_system_prompt_for_tools(
            system_prompt,
            self._build_memory_tools(),
        )

        messages = [
            self.ai_client.build_text_message("system", system_prompt),
        ]

        for hist_msg in related_history_messages:
            role = hist_msg.get("role", "user")
            content = hist_msg.get("content", "")
            messages.append(self.ai_client.build_text_message(role, content))

        if base64_images:
            messages.append(
                self.ai_client.build_multimodal_message(
                    role="user",
                    text=user_message or "请描述这张图片",
                    images=base64_images,
                )
            )
        else:
            messages.append(self.ai_client.build_text_message("user", user_message))

        if getattr(config, "LOG_FULL_PROMPT", False):
            logger.info(
                self._format_system_prompt_log_with_history(
                    event,
                    messages,
                    related_history_messages=related_history_messages,
                )
            )

        try:
            logger.info(
                f"请求 AI: 用户={event.user_id}, 图片={len(base64_images)}, "
                f"关联历史={len(related_history_messages)}"
            )
            response = await self._chat_with_tools(
                messages=messages,
                user_id=str(event.user_id),
                temperature=0.7,
            )

            conversation.add_message("user", user_message)
            conversation.add_message("assistant", response.content)

            if self.memory_manager:
                try:
                    logger.debug(f"登记对话轮次: 用户={event.user_id}")
                    self.memory_manager.register_dialogue_turn(
                        user_id=str(event.user_id),
                        user_message=user_message,
                        assistant_message=response.content,
                    )
                    logger.debug(f"对话轮次登记完成: 用户={event.user_id}")
                    logger.debug(f"检查是否触发记忆提取: 用户={event.user_id}")

                    async def _extract_with_error_handling():
                        try:
                            memories = await self.memory_manager.maybe_extract_memories(str(event.user_id))
                            if memories:
                                logger.info(f"记忆提取完成: {len(memories)} 条")
                        except Exception as e:
                            logger.error(f"记忆提取任务失败: {e}", exc_info=True)

                    asyncio.create_task(_extract_with_error_handling())
                except Exception as e:
                    logger.warning(f"记忆提取登记失败: {e}", exc_info=True)

            logger.info(f"AI 回复完成: 长度={len(response.content)}")
            return response.content

        except AIAPIError as e:
            logger.error(f"AI 请求失败: {e}")
            return f"AI 服务暂时不可用，请稍后重试\n错误信息: {str(e)}"
        except Exception as e:
            logger.error(f"调用 AI 失败: {e}", exc_info=True)
            return "处理消息时出错，请稍后重试"

    def split_long_message(self, message: str) -> List[str]:
        """把长消息拆成多段。"""
        max_length = config.MAX_MESSAGE_LENGTH

        if len(message) <= max_length:
            return [message]

        parts = []
        current_part = ""
        lines = message.split("\n")

        for line in lines:
            if len(line) > max_length:
                if current_part:
                    parts.append(current_part)
                    current_part = ""

                for i in range(0, len(line), max_length):
                    parts.append(line[i : i + max_length])
                continue

            if len(current_part) + len(line) + 1 > max_length:
                parts.append(current_part)
                current_part = line
            else:
                if current_part:
                    current_part += "\n" + line
                else:
                    current_part = line

        if current_part:
            parts.append(current_part)

        return parts
