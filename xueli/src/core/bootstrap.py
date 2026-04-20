"""Bot bootstrap helpers for validation, dependency construction, and wiring."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from src.adapters import create_adapter
from src.adapters.base import PlatformAdapter
from src.core.config import (
    Config,
    get_vision_service_status,
    is_ai_service_configured,
    is_group_reply_decision_configured,
    is_memory_extraction_configured,
    is_memory_rerank_configured,
)
from src.core.lifecycle import close_resource
from src.core.model_invocation_router import ModelInvocationRouter, ModelInvocationType
from src.core.runtime_metrics import RuntimeMetrics
from src.handlers.conversation_planner import ConversationPlanner
from src.handlers.message_handler import MessageHandler

logger = logging.getLogger(__name__)


@dataclass
class BotRuntimeComponents:
    connection: PlatformAdapter
    message_handler: MessageHandler
    memory_manager: Optional[Any]


class BotBootstrapper:
    """Validate config and assemble runtime components."""

    def __init__(self, config_obj: Config | None = None):
        self.config = config_obj or Config()

    async def build(
        self,
        *,
        on_message: Callable[[Dict[str, Any]], Awaitable[None]],
        on_connect: Callable[[], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]],
        runtime_metrics: Optional[RuntimeMetrics] = None,
        status_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        model_invocation_router: Optional[ModelInvocationRouter] = None,
    ) -> BotRuntimeComponents:
        app_config = self.config.validate()
        logger.info("开始启动助手：%s", self.config.get_assistant_name())
        self._log_runtime_config()

        memory_manager = await self._initialize_memory_manager(
            runtime_metrics=runtime_metrics,
            model_invocation_router=model_invocation_router,
        )
        message_handler: Optional[MessageHandler] = None

        try:
            message_handler = MessageHandler(
                memory_manager=memory_manager,
                conversation_planner=ConversationPlanner(
                    app_config=app_config,
                    model_invocation_router=model_invocation_router,
                ),
                runtime_metrics=runtime_metrics,
                status_provider=status_provider,
                app_config=app_config,
                model_invocation_router=model_invocation_router,
            )
            await message_handler.initialize()
            connection = self._create_adapter(
                on_message=on_message,
                on_connect=on_connect,
                on_disconnect=on_disconnect,
            )
        except Exception:
            await close_resource(message_handler, label="message_handler")
            await close_resource(memory_manager, label="memory_manager")
            raise

        return BotRuntimeComponents(
            connection=connection,
            message_handler=message_handler,
            memory_manager=memory_manager,
        )

    def _log_runtime_config(self) -> None:
        app_config = self.config.app
        ai_service = app_config.ai_service
        vision_service = self.config.get_vision_client_config()
        vision_status = get_vision_service_status(app_config)
        group_reply = app_config.group_reply
        decision_config = self.config.get_group_reply_decision_client_config()

        logger.info(
            "运行配置：助手=%s，回复模型=%s，地址=%s",
            self.config.get_assistant_name(),
            ai_service.model,
            ai_service.api_base,
        )
        logger.info(
            "视觉服务：状态=%s，模型=%s",
            vision_status,
            vision_service.get("model"),
        )
        logger.info(
            "群聊规划：已配置=%s，模型=%s，仅@回复=%s，兴趣回复=%s，统一上下文条数=%s，复读回声=%s",
            is_group_reply_decision_configured(app_config),
            decision_config.get("model"),
            group_reply.only_reply_when_at,
            group_reply.interest_reply_enabled,
            app_config.bot_behavior.max_context_length,
            group_reply.repeat_echo_enabled,
        )
        if is_memory_extraction_configured(app_config):
            logger.info("记忆提取运行策略：优先使用专用提取模型，失败时回退主模型")
        elif is_ai_service_configured(app_config):
            logger.info("记忆提取运行策略：未配置专用提取模型，当前直接使用主模型")
        else:
            logger.warning("记忆提取运行策略：主模型与专用提取模型均不可用")

    async def _initialize_memory_manager(
        self,
        *,
        runtime_metrics: Optional[RuntimeMetrics] = None,
        model_invocation_router: Optional[ModelInvocationRouter] = None,
    ):
        app_config = self.config.app
        memory_config = app_config.memory
        logger.info("记忆模块：%s", "已启用" if memory_config.enabled else "未启用")

        if not memory_config.enabled:
            return None

        from src.memory import ExtractionConfig, MemoryManager, MemoryManagerConfig, RetrievalConfig

        main_ai_client_config = self.config.get_ai_service_client_config()
        memory_client_config = self.config.get_memory_extraction_client_config()
        memory_rerank_client_config = self.config.get_memory_rerank_client_config()
        dedicated_extraction_configured = is_memory_extraction_configured(app_config)
        main_ai_configured = is_ai_service_configured(app_config)

        if dedicated_extraction_configured:
            logger.info("记忆提取模型：专用模型=%s", memory_client_config.get("model"))
        elif main_ai_configured:
            logger.info("记忆提取模型：未配置专用模型，直接使用主模型=%s", main_ai_client_config.get("model"))
        else:
            logger.warning("记忆提取模型不可用：专用模型与主模型均未配置完成")
        logger.info(
            "记忆配置：自动提取=%s，每%s轮提取一次，读取范围=%s，重排模型=%s",
            memory_config.auto_extract,
            memory_config.extract_every_n_turns,
            memory_config.read_scope,
            memory_rerank_client_config.get("model"),
        )

        manager_config = MemoryManagerConfig(
            storage_base_path=memory_config.storage_path,
            memory_read_scope=memory_config.read_scope,
            retrieval_config=RetrievalConfig(
                bm25_top_k=memory_config.bm25_top_k,
                rerank_enabled=is_memory_rerank_configured(app_config),
                rerank_top_k=memory_config.rerank_top_k,
                pre_rerank_top_k=memory_config.pre_rerank_top_k,
                dynamic_memory_limit=memory_config.dynamic_memory_limit,
                dynamic_dedup_enabled=memory_config.dynamic_dedup_enabled,
                dynamic_dedup_similarity_threshold=memory_config.dynamic_dedup_similarity_threshold,
                rerank_candidate_max_chars=memory_config.rerank_candidate_max_chars,
                rerank_total_prompt_budget=memory_config.rerank_total_prompt_budget,
                reranker_type="api",
                api_endpoint=memory_rerank_client_config.get("api_base", ""),
                api_key=memory_rerank_client_config.get("api_key", ""),
                api_model=memory_rerank_client_config.get("model", ""),
                api_extra_params=memory_rerank_client_config.get("extra_params", {}),
                api_extra_headers=memory_rerank_client_config.get("extra_headers", {}),
                api_response_path=memory_rerank_client_config.get("response_path", "choices.0.message.content"),
                model_invocation_router=model_invocation_router,
                local_bm25_weight=memory_config.local_bm25_weight,
                local_importance_weight=memory_config.local_importance_weight,
                local_mention_weight=memory_config.local_mention_weight,
                local_recency_weight=memory_config.local_recency_weight,
                local_scene_weight=memory_config.local_scene_weight,
            ),
            extraction_config=ExtractionConfig(
                extract_every_n_turns=memory_config.extract_every_n_turns,
            ),
            ordinary_decay_enabled=memory_config.ordinary_decay_enabled,
            ordinary_half_life_days=memory_config.ordinary_half_life_days,
            ordinary_forget_threshold=memory_config.ordinary_forget_threshold,
            auto_extract_memory=memory_config.auto_extract,
            auto_build_index=True,
        )

        memory_manager = MemoryManager(
            llm_callback=self._build_memory_llm_callback(
                dedicated_client_config=memory_client_config,
                main_client_config=main_ai_client_config,
                use_dedicated_first=dedicated_extraction_configured,
                main_available=main_ai_configured,
                model_invocation_router=model_invocation_router,
            ),
            config=manager_config,
            runtime_metrics=runtime_metrics,
        )
        try:
            await memory_manager.initialize()
        except Exception:
            await close_resource(memory_manager, label="memory_manager")
            raise

        logger.info("记忆管理器初始化完成")
        return memory_manager

    def _build_memory_llm_callback(
        self,
        *,
        dedicated_client_config: Dict[str, Any],
        main_client_config: Dict[str, Any],
        use_dedicated_first: bool,
        main_available: bool,
        model_invocation_router: Optional[ModelInvocationRouter] = None,
    ):
        if not use_dedicated_first and not main_available:
            return None

        async def _invoke_client(
            *,
            provider: str,
            client_config: Dict[str, Any],
            system_prompt: str,
            messages: list,
        ) -> Dict[str, str]:
            from src.services.ai_client import AIClient

            client = AIClient(log_label="memory_extract", app_config=self.config.app, **client_config)
            try:
                full_messages = [client.build_text_message("system", system_prompt)]
                for message in messages:
                    full_messages.append(client.build_text_message(message["role"], message["content"]))

                async def run_chat():
                    return await client.chat_completion(
                        messages=full_messages,
                        temperature=0.3,
                        model=client_config.get("model"),
                    )

                if model_invocation_router is not None:
                    result = await model_invocation_router.submit(
                        purpose=ModelInvocationType.MEMORY_EXTRACTION,
                        trace_id="",
                        session_key="",
                        message_id="",
                        label=f"记忆提取-{provider}",
                        runner=run_chat,
                    )
                else:
                    result = await run_chat()
                content = result.content if hasattr(result, "content") else str(result)
                return {
                    "content": str(content or ""),
                    "provider": provider,
                    "model": str(client_config.get("model") or ""),
                }
            finally:
                await client.close()

        async def llm_callback(system_prompt: str, messages: list):
            if use_dedicated_first:
                try:
                    return await _invoke_client(
                        provider="memory_extraction",
                        client_config=dedicated_client_config,
                        system_prompt=system_prompt,
                        messages=messages,
                    )
                except Exception as exc:
                    if not main_available:
                        raise
                    logger.warning("专用记忆提取模型调用失败，回退主模型：%s", exc)

            if not main_available:
                raise RuntimeError("主模型未配置，无法执行记忆提取")

            return await _invoke_client(
                provider="ai_service",
                client_config=main_client_config,
                system_prompt=system_prompt,
                messages=messages,
            )

        return llm_callback

    def _parse_ws_endpoint(self) -> tuple[str, int]:
        ws_url = self.config.app.adapter_connection.ws_url
        normalized = ws_url.split("://", 1)[1] if "://" in ws_url else ws_url
        if ":" in normalized:
            host, port_str = normalized.rsplit(":", 1)
            return host, int(port_str)
        return normalized, 8095

    def _create_adapter(
        self,
        *,
        on_message: Callable[[Dict[str, Any]], Awaitable[None]],
        on_connect: Callable[[], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]],
    ) -> PlatformAdapter:
        adapter_config = self.config.app.adapter_connection
        adapter_name = str(getattr(adapter_config, "adapter", "napcat") or "napcat").strip().lower() or "napcat"
        if adapter_name in {"api", "openapi"}:
            logger.info("准备启动 adapter：%s（平台=%s）", adapter_name, getattr(adapter_config, "platform", "api") or "api")
            return create_adapter(adapter_name, on_connect=on_connect, on_disconnect=on_disconnect)

        host, port = self._parse_ws_endpoint()
        logger.info("准备启动 adapter：%s ws://%s:%s", adapter_name, host, port)
        return create_adapter(
            adapter_name,
            host=host,
            port=port,
            on_message=on_message,
            on_connect=on_connect,
            on_disconnect=on_disconnect,
        )
