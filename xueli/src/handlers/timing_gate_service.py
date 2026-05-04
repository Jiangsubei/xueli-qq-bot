from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, Optional

from src.core.config import AppConfig, config, is_group_reply_decision_configured
from src.core.message_trace import format_trace_log
from src.core.message_trace import get_execution_key
from src.core.model_invocation_router import ModelInvocationRouter, ModelInvocationType
from src.core.models import TimingDecision, TimingDecisionAction
from src.core.prompt_templates import PromptTemplateLoader
from src.handlers.message_context import MessageContext
from src.services.ai_client import AIAPIError, AIClient

logger = logging.getLogger(__name__)


class TimingGateService:
    """Secondary pacing gate between planning and visible reply generation."""

    def __init__(
        self,
        ai_client: Optional[AIClient] = None,
        *,
        app_config: Optional[AppConfig] = None,
        model_invocation_router: Optional[ModelInvocationRouter] = None,
    ) -> None:
        self.app_config = app_config or config.app
        self.ai_client = ai_client or self._create_ai_client()
        self._owns_ai_client = ai_client is None
        self.model_invocation_router = model_invocation_router
        self.template_loader = PromptTemplateLoader()

    def _create_ai_client(self) -> Optional[AIClient]:
        if not is_group_reply_decision_configured(self.app_config):
            return None
        decision = self.app_config.group_reply_decision
        return AIClient(
            api_base=decision.api_base,
            api_key=decision.api_key,
            model=decision.model,
            extra_params=dict(decision.extra_params or {}),
            extra_headers=dict(decision.extra_headers or {}),
            response_path=decision.response_path or "choices.0.message.content",
            log_label="timing_gate",
            app_config=self.app_config,
        )

    async def decide(self, *, event: Any, plan: Any, context: MessageContext) -> TimingDecision:
        fallback = self._fallback_decision(plan=plan, context=context)
        if self.ai_client is None:
            return fallback
        messages = [
            self.ai_client.build_text_message("system", await self._build_system_prompt()),
            self.ai_client.build_text_message("user", self._build_user_prompt(event=event, plan=plan, context=context)),
        ]
        try:
            async def run_chat():
                return await self.ai_client.chat_completion(messages=messages, temperature=0.1)

            if self.model_invocation_router is not None:
                response = await self.model_invocation_router.submit(
                    purpose=ModelInvocationType.TIMING_GATE,
                    trace_id=context.trace_id,
                    session_key=context.execution_key or get_execution_key(event),
                    message_id=getattr(event, "message_id", 0),
                    label="节奏判断",
                    runner=run_chat,
                )
            else:
                response = await run_chat()
            decision = self._parse_response(str(getattr(response, "content", "") or ""), fallback=fallback)
            if context.trace_id:
                logger.info(
                    "节奏判断完成：%s decision=%s reason=%s recent_gap_bucket=%s conversation_gap_bucket=%s session_gap_bucket=%s continuity_hint=%s",
                    format_trace_log(trace_id=context.trace_id, session_key=context.execution_key or get_execution_key(event), message_id=getattr(event, "message_id", 0)),
                    decision.decision,
                    str(getattr(decision, "reason", "") or ""),
                    str(getattr(context.temporal_context, "recent_gap_bucket", "unknown") or "unknown"),
                    str(getattr(context.temporal_context, "conversation_gap_bucket", "unknown") or "unknown"),
                    str(getattr(context.temporal_context, "session_gap_bucket", "unknown") or "unknown"),
                    str(getattr(context.temporal_context, "continuity_hint", "unknown") or "unknown"),
                )
                planner_reason = str(getattr(plan, "reason", "") or "")
                timing_reason = str(getattr(decision, "reason", "") or "")
                logger.info(
                    "[决策摘要] %s plan=%s timing=%s plan_reason=%s timing_reason=%s",
                    format_trace_log(trace_id=context.trace_id, session_key=context.execution_key or get_execution_key(event), message_id=getattr(event, "message_id", 0)),
                    str(getattr(plan, "action", "unknown")),
                    decision.decision,
                    planner_reason[:120],
                    timing_reason[:120],
                )
            return decision
        except asyncio.CancelledError:
            raise
        except (AIAPIError, asyncio.TimeoutError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("[节奏门] 节奏判断失败，回退到规则")
            return fallback
        except Exception as exc:
            logger.error("[节奏门] 节奏判断意外异常")
            return fallback

    async def decide_timing_only(self, *, event: Any, context: MessageContext) -> TimingDecision:
        fallback = self._fallback_decision(plan=None, context=context)
        if self.ai_client is None:
            return fallback
        messages = [
            self.ai_client.build_text_message("system", await self._build_system_prompt()),
            self.ai_client.build_text_message("user", self._build_user_prompt(event=event, plan=None, context=context)),
        ]
        try:
            async def run_chat():
                return await self.ai_client.chat_completion(messages=messages, temperature=0.1)

            if self.model_invocation_router is not None:
                response = await self.model_invocation_router.submit(
                    purpose=ModelInvocationType.TIMING_GATE,
                    trace_id=context.trace_id,
                    session_key=context.execution_key or get_execution_key(event),
                    message_id=getattr(event, "message_id", 0),
                    label="节奏判断",
                    runner=run_chat,
                )
            else:
                response = await run_chat()
            decision = self._parse_response(str(getattr(response, "content", "") or ""), fallback=fallback)
            if context.trace_id:
                logger.info(
                    "节奏判断完成：%s decision=%s reason=%s",
                    format_trace_log(trace_id=context.trace_id, session_key=context.execution_key or get_execution_key(event), message_id=getattr(event, "message_id", 0)),
                    decision.decision,
                    str(getattr(decision, "reason", "") or ""),
                )
            return decision
        except asyncio.CancelledError:
            raise
        except (AIAPIError, asyncio.TimeoutError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("[节奏门] 节奏判断失败，回退到规则")
            return fallback
        except Exception as exc:
            logger.error("[节奏门] 节奏判断意外异常")
            return fallback

    async def _build_system_prompt(self) -> str:
        """Load timing_gate.prompt — fully static, no variables."""
        return await self.template_loader.load("timing_gate.prompt")

    def _build_user_prompt(self, *, event: Any, plan: Any, context: MessageContext) -> str:
        lines = [
            f"当前消息类型：{getattr(event, 'message_type', '') or 'private'}",
        ]
        if plan is not None:
            lines.append(f"planner决定：{getattr(plan, 'reason', '')}")
        current_message = context.user_message or ""
        vision_analysis = getattr(context, "vision_analysis", None) or {}
        merged_desc = str(vision_analysis.get("merged_description", "") or "").strip()
        failure_count = int(vision_analysis.get("vision_failure_count", 0) or 0)
        if merged_desc:
            image_part = f"[图片] {merged_desc}"
        elif failure_count > 0:
            image_part = "[图片]未成功识别"
        else:
            image_part = ""
        if image_part:
            current_message = f"{current_message.strip()}\n{image_part}" if current_message.strip() else image_part
        lines.append(f"当前消息：{current_message}")
        if context.rendered_recent_history:
            lines.append("聊天历史（含时间戳）：")
            lines.append(context.rendered_recent_history)
        return "\n".join(str(line or "").strip() for line in lines if str(line or "").strip())

    def _extract_json_object(self, content: str) -> Dict[str, Any]:
        text = str(content or "").strip()
        if not text:
            raise ValueError("empty timing gate response")
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
        fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
        if fenced_match:
            return json.loads(fenced_match.group(1))
        json_match = re.search(r"\{.*\}", text, re.S)
        if json_match:
            return json.loads(json_match.group(0))
        raise ValueError("invalid timing gate response")

    def _parse_response(self, content: str, *, fallback: TimingDecision) -> TimingDecision:
        payload = self._extract_json_object(content)
        decision = str(payload.get("decision", "") or "").strip().lower()
        aliases = {
            "continue": TimingDecisionAction.CONTINUE.value,
            "wait": TimingDecisionAction.WAIT.value,
            "no_reply": TimingDecisionAction.NO_REPLY.value,
            "ignore": TimingDecisionAction.NO_REPLY.value,
        }
        normalized = aliases.get(decision, "")
        if normalized not in {item.value for item in TimingDecisionAction}:
            raise ValueError(f"unsupported timing decision: {decision}")
        return TimingDecision(
            decision=normalized,
            reason=str(payload.get("reason", "") or fallback.reason or "").strip(),
            source="timing_gate",
            raw_decision=payload,
        )

    def _fallback_decision(self, *, plan: Any, context: MessageContext) -> TimingDecision:
        signals = dict(context.planning_signals or {})
        if bool(signals.get("_force_timing_continue")):
            return TimingDecision(
                decision=TimingDecisionAction.CONTINUE.value,
                reason="高优先级消息，强制继续",
                source="rule",
            )
        if bool(signals.get("has_image_without_text")):
            return TimingDecision(decision=TimingDecisionAction.WAIT.value, reason="当前更像是等待图片相关补充", source="rule")
        return TimingDecision(
            decision=TimingDecisionAction.CONTINUE.value,
            reason=str(getattr(plan, "reason", "") or "当前适合继续生成回复"),
            source="rule",
        )

    async def close(self) -> None:
        if self._owns_ai_client and self.ai_client:
            await self.ai_client.close()
