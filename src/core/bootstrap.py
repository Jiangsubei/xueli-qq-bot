"""Bot bootstrap helpers for validation, dependency construction, and wiring."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from src.core.config import Config, config, get_vision_service_status, is_group_reply_decision_configured
from src.core.connection import NapCatConnection
from src.core.lifecycle import close_resource
from src.core.runtime_metrics import RuntimeMetrics
from src.handlers.group_reply_planner import GroupReplyPlanner
from src.handlers.message_handler import MessageHandler

logger = logging.getLogger(__name__)


@dataclass
class BotRuntimeComponents:
    connection: NapCatConnection
    message_handler: MessageHandler
    memory_manager: Optional[Any]


class BotBootstrapper:
    """Validate config and assemble runtime components."""

    def __init__(self, config_obj: Config = config):
        self.config = config_obj

    async def build(
        self,
        *,
        on_message: Callable[[Dict[str, Any]], Awaitable[None]],
        on_connect: Callable[[], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]],
        runtime_metrics: Optional[RuntimeMetrics] = None,
        status_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> BotRuntimeComponents:
        app_config = self.config.validate()
        logger.info("starting bot: %s", self.config.get_assistant_name())
        self._log_runtime_config()

        memory_manager = await self._initialize_memory_manager(runtime_metrics=runtime_metrics)
        message_handler: Optional[MessageHandler] = None

        try:
            message_handler = MessageHandler(
                memory_manager=memory_manager,
                group_reply_planner=GroupReplyPlanner(app_config=app_config),
                runtime_metrics=runtime_metrics,
                status_provider=status_provider,
                app_config=app_config,
            )
            await message_handler.initialize()
            connection = self._create_connection(
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

        logger.info("AI service: %s", ai_service.api_base)
        logger.info("default model: %s", ai_service.model)
        logger.info(
            "[vision] status=%s model=%s api_base=%s",
            vision_status,
            vision_service.get("model"),
            vision_service.get("api_base"),
        )
        logger.info("[assistant] name: %s", self.config.get_assistant_name())
        logger.info(
            "[planner] only_at=%s interest=%s interval=%s max_parallel=%s burst_enabled=%s burst_window=%s burst_min=%s burst_max=%s model=%s",
            group_reply.only_reply_when_at,
            group_reply.interest_reply_enabled,
            group_reply.plan_request_interval,
            group_reply.plan_request_max_parallel,
            group_reply.burst_merge_enabled,
            group_reply.burst_window_seconds,
            group_reply.burst_min_messages,
            group_reply.burst_max_messages,
            decision_config.get("model"),
        )
        logger.info("[planner] configured=%s", is_group_reply_decision_configured(app_config))

    async def _initialize_memory_manager(
        self,
        *,
        runtime_metrics: Optional[RuntimeMetrics] = None,
    ):
        app_config = self.config.app
        memory_config = app_config.memory
        logger.info("memory module: %s", "enabled" if memory_config.enabled else "disabled")

        if not memory_config.enabled:
            return None

        from src.memory import MemoryManager, MemoryManagerConfig, RetrievalConfig, ExtractionConfig

        memory_client_config = self.config.get_memory_extraction_client_config()
        memory_rerank_client_config = self.config.get_memory_rerank_client_config()
        logger.info("memory extraction model: %s", memory_client_config.get("model"))
        logger.info("memory rerank model: %s", memory_rerank_client_config.get("model"))

        manager_config = MemoryManagerConfig(
            storage_base_path=memory_config.storage_path,
            memory_read_scope=memory_config.read_scope,
            retrieval_config=RetrievalConfig(
                bm25_top_k=memory_config.bm25_top_k,
                rerank_enabled=bool(memory_rerank_client_config.get("api_base") and memory_rerank_client_config.get("api_key") and memory_rerank_client_config.get("model")),
                rerank_top_k=memory_config.rerank_top_k,
                reranker_type="api",
                api_endpoint=memory_rerank_client_config.get("api_base", ""),
                api_key=memory_rerank_client_config.get("api_key", ""),
                api_model=memory_rerank_client_config.get("model", ""),
                api_extra_params=memory_rerank_client_config.get("extra_params", {}),
                api_extra_headers=memory_rerank_client_config.get("extra_headers", {}),
                api_response_path=memory_rerank_client_config.get("response_path", "choices.0.message.content"),
            ),
            extraction_config=ExtractionConfig(
                extract_every_n_turns=memory_config.extract_every_n_turns,
            ),
            ordinary_decay_enabled=memory_config.ordinary_decay_enabled,
            ordinary_half_life_days=memory_config.ordinary_half_life_days,
            ordinary_forget_threshold=memory_config.ordinary_forget_threshold,
            conversation_save_interval=memory_config.conversation_save_interval,
            auto_extract_memory=memory_config.auto_extract,
            auto_build_index=True,
        )

        memory_manager = MemoryManager(
            llm_callback=self._build_memory_llm_callback(memory_client_config),
            config=manager_config,
            runtime_metrics=runtime_metrics,
        )
        try:
            await memory_manager.initialize()
        except Exception:
            await close_resource(memory_manager, label="memory_manager")
            raise

        logger.info("[memory] read_scope=%s", memory_config.read_scope)
        logger.info("memory manager initialized")
        return memory_manager

    def _build_memory_llm_callback(self, client_config: Dict[str, Any]):
        async def llm_callback(system_prompt: str, messages: list):
            from src.services.ai_client import AIClient

            client = AIClient(log_label="memory_extract", app_config=self.config.app, **client_config)
            try:
                full_messages = [client.build_text_message("system", system_prompt)]
                for message in messages:
                    full_messages.append(client.build_text_message(message["role"], message["content"]))

                result = await client.chat_completion(
                    messages=full_messages,
                    temperature=0.3,
                    model=client_config.get("model"),
                )
                return result.content if hasattr(result, "content") else str(result)
            finally:
                await client.close()

        return llm_callback

    def _parse_ws_endpoint(self) -> tuple[str, int]:
        ws_url = self.config.app.napcat.ws_url
        normalized = ws_url.split("://", 1)[1] if "://" in ws_url else ws_url
        if ":" in normalized:
            host, port_str = normalized.rsplit(":", 1)
            return host, int(port_str)
        return normalized, 8095

    def _create_connection(
        self,
        *,
        on_message: Callable[[Dict[str, Any]], Awaitable[None]],
        on_connect: Callable[[], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]],
    ) -> NapCatConnection:
        host, port = self._parse_ws_endpoint()
        logger.info("listen NapCat connection: ws://%s:%s", host, port)
        return NapCatConnection(
            host=host,
            port=port,
            on_message=on_message,
            on_connect=on_connect,
            on_disconnect=on_disconnect,
        )
