from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.core.config import AppConfig, config, get_vision_service_status, is_vision_service_configured
from src.emoji.models import DEFAULT_REPLY_TONES
from src.services.ai_client import AIAPIError, AIClient

logger = logging.getLogger(__name__)


@dataclass
class ImageAnalysisResult:
    per_image_descriptions: List[str] = field(default_factory=list)
    merged_description: str = ""
    success_count: int = 0
    failure_count: int = 0
    source: str = "vision"
    error: str = ""
    sticker_flags: List[bool] = field(default_factory=list)
    sticker_confidences: List[float] = field(default_factory=list)
    sticker_reasons: List[str] = field(default_factory=list)

    @property
    def has_usable_description(self) -> bool:
        return bool(self.merged_description.strip() or any(item.strip() for item in self.per_image_descriptions))

    @property
    def sticker_count(self) -> int:
        return sum(1 for flag in self.sticker_flags if flag)

    def is_sticker(self, index: int) -> bool:
        return bool(self._safe_list_get(self.sticker_flags, index, False))

    def get_sticker_confidence(self, index: int) -> float:
        return float(self._safe_list_get(self.sticker_confidences, index, 0.0) or 0.0)

    def get_sticker_reason(self, index: int) -> str:
        return str(self._safe_list_get(self.sticker_reasons, index, "") or "")

    def get_description(self, index: int) -> str:
        return str(self._safe_list_get(self.per_image_descriptions, index, "") or "")

    def to_prompt_fields(self) -> Dict[str, Any]:
        return {
            "per_image_descriptions": list(self.per_image_descriptions),
            "merged_description": self.merged_description,
            "vision_success_count": self.success_count,
            "vision_failure_count": self.failure_count,
            "vision_source": self.source,
            "vision_error": self.error,
            "vision_available": self.has_usable_description,
            "sticker_flags": list(self.sticker_flags),
            "sticker_confidences": list(self.sticker_confidences),
            "sticker_reasons": list(self.sticker_reasons),
            "sticker_count": self.sticker_count,
        }

    @staticmethod
    def _safe_list_get(values: List[Any], index: int, default: Any) -> Any:
        if 0 <= index < len(values):
            return values[index]
        return default


class VisionClient:
    """Use a dedicated multimodal model to convert images into concise text."""

    def __init__(
        self,
        ai_client: Optional[AIClient] = None,
        *,
        app_config: Optional[AppConfig] = None,
    ) -> None:
        self.app_config = app_config or config.app
        self._owns_ai_client = ai_client is None
        self.ai_client = ai_client
        if self.ai_client is None and self.is_available():
            self.ai_client = self._create_ai_client()

    def _create_ai_client(self) -> AIClient:
        client_config = config.get_vision_client_config() if self.app_config is config.app else {
            "enabled": self.app_config.vision_service.enabled,
            "api_base": self.app_config.vision_service.api_base or "",
            "api_key": self.app_config.vision_service.api_key or "",
            "model": self.app_config.vision_service.model or "",
            "extra_params": dict(self.app_config.vision_service.extra_params or {}),
            "extra_headers": dict(self.app_config.vision_service.extra_headers or {}),
            "response_path": self.app_config.vision_service.response_path or "choices.0.message.content",
        }
        logger.info("初始化视觉模型：模型=%s", client_config.get("model"))
        return AIClient(
            api_base=client_config["api_base"],
            api_key=client_config["api_key"],
            model=client_config["model"],
            extra_params=client_config["extra_params"],
            extra_headers=client_config["extra_headers"],
            response_path=client_config["response_path"],
            log_label="vision",
            app_config=self.app_config,
        )

    def enabled(self) -> bool:
        return bool(self.app_config.vision_service.enabled)

    def is_configured(self) -> bool:
        return is_vision_service_configured(self.app_config)

    def is_available(self) -> bool:
        return self.enabled() and self.is_configured()

    def status(self) -> str:
        return get_vision_service_status(self.app_config)

    def _build_system_prompt(self) -> str:
        return (
            "你是图片理解助手。你的任务是输出简洁、可靠、可供聊天机器人继续使用的 JSON。\n"
            "请只输出 JSON 对象，不要输出 markdown 或解释。\n"
            "JSON 结构必须是：\n"
            '{"images":[{"description":"第1张的简洁描述","is_sticker":true,"sticker_confidence":0.95,"sticker_reason":"判断依据"}],"merged_description":"整组图片摘要"}\n'
            "要求：\n"
            "1. description 只描述可见内容，不要编造细节。\n"
            "2. is_sticker 用来判断图片是否属于聊天表情包、梗图、reaction image 或强表达性的截图拼图。\n"
            "3. 普通照片、风景图、商品图、一般截图默认不要判成表情包。\n"
            "4. sticker_confidence 取 0 到 1 之间的小数。\n"
            "5. merged_description 需要概括整组图片内容。"
        )

    def _build_user_text(self, user_text: str, image_count: int) -> str:
        clean_text = str(user_text or "").strip()
        lines = [f"图片数量: {image_count}"]
        if clean_text:
            lines.append(f"用户原始文字: {clean_text}")
            lines.append("请结合用户文字理解图片，但优先依据图片本身可见内容作答。")
        else:
            lines.append("用户原始文字为空，请直接描述图片内容。")
        return "\n".join(lines)

    def _build_emotion_system_prompt(self, emotion_labels: List[str]) -> str:
        labels_text = " / ".join(emotion_labels)
        tones_text = " / ".join(DEFAULT_REPLY_TONES)
        return (
            "你是表情包分类助手。请根据这张表情包图像，判断它最主要的情绪，以及适合用在哪类回复场景。\n"
            "请只输出 JSON 对象，不要输出额外说明。\n"
            'JSON 结构必须是：{"primary_emotion":"开心","confidence":0.9,"all_emotions":["开心","喜欢"],"reply_tones":["庆祝"],"reply_intents":["庆祝-开心"],"reason":"简短判断依据"}\n'
            f"可选情绪标签只有：{labels_text}\n"
            f"可选回复语气只有：{tones_text}\n"
            "要求：\n"
            "1. primary_emotion 必须从给定情绪标签中选择一个。\n"
            "2. all_emotions 最多保留 3 个标签，按相关度排序。\n"
            "3. reply_tones 最多保留 3 个标签，按适配度排序。\n"
            "4. reply_intents 使用 tone-emotion 组合，例如 安慰-委屈。\n"
            "5. confidence 取 0 到 1 之间的小数。"
        )

    def _extract_json_object(self, content: str) -> Dict[str, Any]:
        text = str(content or "").strip()
        if not text:
            return {}

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
        return {}

    def _parse_result(self, content: str, image_count: int) -> ImageAnalysisResult:
        try:
            data = self._extract_json_object(content)
        except Exception:
            data = {}

        descriptions: List[str] = []
        sticker_flags: List[bool] = []
        sticker_confidences: List[float] = []
        sticker_reasons: List[str] = []

        images_payload = data.get("images") if isinstance(data, dict) else None
        if isinstance(images_payload, list):
            for item in images_payload[:image_count]:
                if not isinstance(item, dict):
                    continue
                descriptions.append(str(item.get("description", "")).strip())
                sticker_flags.append(bool(item.get("is_sticker", False)))
                sticker_confidences.append(self._float_value(item.get("sticker_confidence"), default=0.0))
                sticker_reasons.append(str(item.get("sticker_reason", "")).strip())

        if not descriptions:
            per_image = data.get("per_image_descriptions") if isinstance(data, dict) else None
            if isinstance(per_image, list):
                descriptions = [str(item).strip() for item in per_image[:image_count] if str(item).strip()]

        merged = ""
        if isinstance(data, dict):
            merged = str(data.get("merged_description", "")).strip()

        if not descriptions and content.strip():
            fallback = content.strip()
            descriptions = [f"第1张: {fallback}"] if image_count == 1 else []
            merged = fallback

        descriptions = self._pad_list(descriptions, image_count, "")
        sticker_flags = self._pad_list(sticker_flags, image_count, False)
        sticker_confidences = self._pad_list(sticker_confidences, image_count, 0.0)
        sticker_reasons = self._pad_list(sticker_reasons, image_count, "")

        success_count = len([item for item in descriptions if str(item).strip()]) if descriptions else (1 if merged else 0)
        success_count = min(image_count, success_count)
        return ImageAnalysisResult(
            per_image_descriptions=descriptions,
            merged_description=merged,
            success_count=success_count,
            failure_count=max(0, image_count - success_count),
            sticker_flags=sticker_flags,
            sticker_confidences=sticker_confidences,
            sticker_reasons=sticker_reasons,
        )

    async def analyze_images(
        self,
        *,
        base64_images: List[str],
        user_text: str = "",
    ) -> ImageAnalysisResult:
        if not base64_images:
            return ImageAnalysisResult(source="vision")
        if not self.is_available() or self.ai_client is None:
            return ImageAnalysisResult(
                success_count=0,
                failure_count=len(base64_images),
                source=self.status(),
                error="视觉服务不可用",
            )

        messages = [
            self.ai_client.build_text_message("system", self._build_system_prompt()),
            self.ai_client.build_multimodal_message(
                role="user",
                text=self._build_user_text(user_text, len(base64_images)),
                images=base64_images,
            ),
        ]
        try:
            response = await self.ai_client.chat_completion(messages=messages, temperature=0.1)
            result = self._parse_result(response.content, len(base64_images))
            result.source = "vision"
            return result
        except AIAPIError as exc:
            logger.warning("图片分析失败：%s", exc)
            return ImageAnalysisResult(
                success_count=0,
                failure_count=len(base64_images),
                source="vision_error",
                error=str(exc),
            )
        except Exception as exc:
            logger.warning("图片分析出现异常：%s", exc, exc_info=True)
            return ImageAnalysisResult(
                success_count=0,
                failure_count=len(base64_images),
                source="vision_error",
                error=str(exc),
            )

    async def classify_sticker_emotion(
        self,
        *,
        image_base64: str,
        emotion_labels: List[str],
    ) -> Dict[str, Any]:
        labels = [label.strip() for label in emotion_labels if str(label).strip()]
        if not labels:
            labels = ["开心", "无语", "生气", "伤心"]
        if not self.is_available() or self.ai_client is None:
            raise RuntimeError("视觉服务不可用")

        messages = [
            self.ai_client.build_text_message("system", self._build_emotion_system_prompt(labels)),
            self.ai_client.build_multimodal_message(
                role="user",
                text="请为这张表情包输出情绪和适合的回复场景。",
                images=[image_base64],
            ),
        ]
        response = await self.ai_client.chat_completion(messages=messages, temperature=0.1)
        data = self._extract_json_object(response.content)

        all_emotions = data.get("all_emotions")
        if not isinstance(all_emotions, list):
            all_emotions = []
        normalized_all = [item for item in [str(item).strip() for item in all_emotions] if item in labels][:3]

        primary = str(data.get("primary_emotion", "")).strip()
        if primary not in labels:
            primary = normalized_all[0] if normalized_all else labels[0]
        if primary not in normalized_all:
            normalized_all.insert(0, primary)

        reply_tones = data.get("reply_tones")
        if not isinstance(reply_tones, list):
            reply_tones = []
        normalized_tones = [
            item for item in [str(item).strip() for item in reply_tones] if item in DEFAULT_REPLY_TONES
        ][:3]

        reply_intents = data.get("reply_intents")
        if not isinstance(reply_intents, list):
            reply_intents = []
        normalized_intents = []
        for item in reply_intents:
            text = str(item).strip()
            if not text or "-" not in text:
                continue
            tone, emotion = [part.strip() for part in text.split("-", 1)]
            if tone in DEFAULT_REPLY_TONES and emotion in labels:
                normalized_intents.append(f"{tone}-{emotion}")
        normalized_intents = normalized_intents[:3]
        if normalized_tones and primary:
            derived_intent = f"{normalized_tones[0]}-{primary}"
            if derived_intent not in normalized_intents:
                normalized_intents.insert(0, derived_intent)

        return {
            "primary_emotion": primary,
            "confidence": self._float_value(data.get("confidence"), default=0.0),
            "reason": str(data.get("reason", "")).strip(),
            "all_emotions": normalized_all[:3],
            "reply_tones": normalized_tones,
            "reply_intents": normalized_intents[:3],
        }

    def _pad_list(self, values: List[Any], size: int, default: Any) -> List[Any]:
        items = list(values[:size])
        while len(items) < size:
            items.append(default)
        return items

    def _float_value(self, value: Any, *, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def close(self) -> None:
        if self._owns_ai_client and self.ai_client:
            await self.ai_client.close()
