from __future__ import annotations

import logging
import os
import re
import tomllib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.memory_limits import MIN_RERANK_CANDIDATE_MAX_CHARS, MIN_RERANK_TOTAL_PROMPT_BUDGET

logger = logging.getLogger(__name__)
_TIME_WINDOW_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d$")
_DEFAULT_EMOTION_LABELS = ["开心", "喜欢", "惊讶", "无语", "委屈", "生气", "伤心", "嘲讽", "害怕", "困惑"]


@dataclass(frozen=True)
class AdapterConnectionConfig:
    adapter: str = "napcat"
    platform: str = "qq"
    ws_url: str = "ws://0.0.0.0:8095"
    http_url: str = "http://127.0.0.1:6700"


@dataclass(frozen=True)
class AIServiceConfig:
    api_base: str = ""
    api_key: str = ""
    model: str = ""
    extra_params: Dict[str, Any] = field(default_factory=dict)
    extra_headers: Dict[str, str] = field(default_factory=dict)
    response_path: str = "choices.0.message.content"


@dataclass(frozen=True)
class VisionServiceConfig:
    enabled: bool = False
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    extra_params: Optional[Dict[str, Any]] = None
    extra_headers: Optional[Dict[str, str]] = None
    response_path: Optional[str] = None


@dataclass(frozen=True)
class EmojiConfig:
    enabled: bool = True
    storage_path: str = "../data/emojis"
    capture_enabled: bool = True
    classification_enabled: bool = True
    idle_seconds_before_classify: float = 45.0
    classification_interval_seconds: float = 30.0
    classification_windows: List[str] = field(default_factory=list)
    emotion_labels: List[str] = field(default_factory=lambda: list(_DEFAULT_EMOTION_LABELS))
    reply_enabled: bool = False
    reply_cooldown_seconds: float = 180.0


@dataclass(frozen=True)
class BotBehaviorConfig:
    max_context_length: int = 10
    max_message_length: int = 4000
    response_timeout: int = 60
    rate_limit_interval: float = 1.0
    log_full_prompt: bool = False
    private_quote_reply_enabled: bool = False
    private_batch_window_seconds: float = 1.2
    sentence_split_enabled: bool = True
    segmented_reply_enabled: bool = True
    max_segments: int = 3
    first_segment_delay_min_ms: int = 0
    first_segment_delay_max_ms: int = 600
    followup_delay_min_seconds: float = 3.0
    followup_delay_max_seconds: float = 10.0


@dataclass(frozen=True)
class PlanningWindowConfig:
    enabled: bool = True
    private_window_seconds: float = 1.2
    group_proactive_window_seconds: float = 0.45
    queue_expire_seconds: float = 60.0


@dataclass(frozen=True)
class MemoryDisputeConfig:
    enabled: bool = True
    high_confidence_threshold: float = 0.75
    normal_confidence_threshold: float = 0.45
    signal_ttl_hours: float = 168.0


@dataclass(frozen=True)
class CharacterGrowthConfig:
    enabled: bool = True
    explicit_feedback_threshold: int = 2
    stable_signal_threshold: int = 6
    core_trait_threshold: int = 5
    tone_preference_threshold: int = 3
    behavior_habit_threshold: int = 2


@dataclass(frozen=True)
class AssistantProfileConfig:
    name: str = "AI??"
    alias: str = ""
    avatar_path: str = ""


@dataclass(frozen=True)
class ContentSection:
    content: str = ""


@dataclass(frozen=True)
class GroupReplyConfig:
    only_reply_when_at: bool = True
    interest_reply_enabled: bool = True
    plan_request_interval: float = 3.0
    plan_request_max_parallel: int = 1
    at_user_when_proactive_reply: bool = False
    repeat_echo_enabled: bool = False
    repeat_echo_window_seconds: float = 20.0
    repeat_echo_min_count: int = 2
    repeat_echo_cooldown_seconds: float = 90.0


@dataclass(frozen=True)
class GroupReplyDecisionConfig:
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    extra_params: Optional[Dict[str, Any]] = None
    extra_headers: Optional[Dict[str, str]] = None
    response_path: Optional[str] = None


@dataclass(frozen=True)
class MemoryRerankConfig:
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    extra_params: Optional[Dict[str, Any]] = None
    extra_headers: Optional[Dict[str, str]] = None
    response_path: Optional[str] = None


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = False
    storage_path: str = "../data/memories"
    read_scope: str = "user"
    bm25_top_k: int = 100
    rerank_top_k: int = 20
    pre_rerank_top_k: int = 12
    dynamic_memory_limit: int = 8
    dynamic_dedup_enabled: bool = True
    dynamic_dedup_similarity_threshold: float = 0.72
    rerank_candidate_max_chars: int = 160
    rerank_total_prompt_budget: int = 2400
    auto_extract: bool = True
    extract_every_n_turns: int = 3
    extraction_api_base: Optional[str] = None
    extraction_api_key: Optional[str] = None
    extraction_model: Optional[str] = None
    extraction_extra_params: Optional[Dict[str, Any]] = None
    extraction_extra_headers: Optional[Dict[str, str]] = None
    extraction_response_path: Optional[str] = None
    ordinary_decay_enabled: bool = True
    ordinary_half_life_days: float = 30.0
    ordinary_forget_threshold: float = 0.5
    local_bm25_weight: float = 1.0
    local_importance_weight: float = 0.35
    local_mention_weight: float = 0.2
    local_recency_weight: float = 0.15
    local_scene_weight: float = 0.3


@dataclass(frozen=True)
class AppConfig:
    adapter_connection: AdapterConnectionConfig = field(default_factory=AdapterConnectionConfig)
    ai_service: AIServiceConfig = field(default_factory=AIServiceConfig)
    vision_service: VisionServiceConfig = field(default_factory=VisionServiceConfig)
    emoji: EmojiConfig = field(default_factory=EmojiConfig)
    bot_behavior: BotBehaviorConfig = field(default_factory=BotBehaviorConfig)
    planning_window: PlanningWindowConfig = field(default_factory=PlanningWindowConfig)
    memory_dispute: MemoryDisputeConfig = field(default_factory=MemoryDisputeConfig)
    character_growth: CharacterGrowthConfig = field(default_factory=CharacterGrowthConfig)
    assistant_profile: AssistantProfileConfig = field(default_factory=AssistantProfileConfig)
    group_reply: GroupReplyConfig = field(default_factory=GroupReplyConfig)
    group_reply_decision: GroupReplyDecisionConfig = field(default_factory=GroupReplyDecisionConfig)
    personality: ContentSection = field(default_factory=ContentSection)
    dialogue_style: ContentSection = field(default_factory=ContentSection)
    behavior: ContentSection = field(default_factory=ContentSection)
    memory_rerank: MemoryRerankConfig = field(default_factory=MemoryRerankConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)

    @property
    def napcat(self) -> AdapterConnectionConfig:
        return self.adapter_connection


def _is_model_endpoint_configured(api_base: Any, model: Any) -> bool:
    return all(str(value or "").strip() for value in (api_base, model))


def is_ai_service_configured(app_config: AppConfig) -> bool:
    ai_service = app_config.ai_service
    return _is_model_endpoint_configured(ai_service.api_base, ai_service.model)


def is_vision_service_configured(app_config: AppConfig) -> bool:
    vision = app_config.vision_service
    return _is_model_endpoint_configured(vision.api_base, vision.model)


def get_vision_service_status(app_config: AppConfig) -> str:
    if not is_vision_service_configured(app_config):
        return "disabled" if not app_config.vision_service.enabled else "unconfigured"
    return "enabled"


def is_group_reply_decision_configured(app_config: AppConfig) -> bool:
    decision = app_config.group_reply_decision
    return _is_model_endpoint_configured(decision.api_base, decision.model)


def is_memory_rerank_configured(app_config: AppConfig) -> bool:
    rerank = app_config.memory_rerank
    return _is_model_endpoint_configured(rerank.api_base, rerank.model)


def is_memory_extraction_configured(app_config: AppConfig) -> bool:
    memory = app_config.memory
    return _is_model_endpoint_configured(memory.extraction_api_base, memory.extraction_model)


class ConfigValidationError(ValueError):
    def __init__(self, errors: List[str]):
        self.errors = [error for error in errors if error]
        super().__init__("config validation failed:\n" + "\n".join(f"- {item}" for item in self.errors))

class Config:
    _MAPPING = {
        "NAPCAT_WS_URL": ("adapter_connection", "ws_url"),
        "NAPCAT_HTTP_URL": ("adapter_connection", "http_url"),
        "ADAPTER_NAME": ("adapter_connection", "adapter"),
        "ADAPTER_PLATFORM": ("adapter_connection", "platform"),
        "ADAPTER_CONNECTION_WS_URL": ("adapter_connection", "ws_url"),
        "ADAPTER_CONNECTION_HTTP_URL": ("adapter_connection", "http_url"),
        "ADAPTER_CONNECTION_ADAPTER": ("adapter_connection", "adapter"),
        "ADAPTER_CONNECTION_PLATFORM": ("adapter_connection", "platform"),
        "OPENAI_API_BASE": ("ai_service", "api_base"),
        "OPENAI_API_KEY": ("ai_service", "api_key"),
        "OPENAI_MODEL": ("ai_service", "model"),
        "OPENAI_EXTRA_PARAMS": ("ai_service", "extra_params"),
        "OPENAI_EXTRA_HEADERS": ("ai_service", "extra_headers"),
        "OPENAI_RESPONSE_PATH": ("ai_service", "response_path"),
        "VISION_SERVICE_ENABLED": ("vision_service", "enabled"),
        "VISION_SERVICE_API_BASE": ("vision_service", "api_base"),
        "VISION_SERVICE_API_KEY": ("vision_service", "api_key"),
        "VISION_SERVICE_MODEL": ("vision_service", "model"),
        "VISION_SERVICE_EXTRA_PARAMS": ("vision_service", "extra_params"),
        "VISION_SERVICE_EXTRA_HEADERS": ("vision_service", "extra_headers"),
        "VISION_SERVICE_RESPONSE_PATH": ("vision_service", "response_path"),
        "EMOJI_ENABLED": ("emoji", "enabled"),
        "EMOJI_STORAGE_PATH": ("emoji", "storage_path"),
        "EMOJI_CAPTURE_ENABLED": ("emoji", "capture_enabled"),
        "EMOJI_CLASSIFICATION_ENABLED": ("emoji", "classification_enabled"),
        "EMOJI_IDLE_SECONDS_BEFORE_CLASSIFY": ("emoji", "idle_seconds_before_classify"),
        "EMOJI_CLASSIFICATION_INTERVAL_SECONDS": ("emoji", "classification_interval_seconds"),
        "EMOJI_CLASSIFICATION_WINDOWS": ("emoji", "classification_windows"),
        "EMOJI_EMOTION_LABELS": ("emoji", "emotion_labels"),
        "EMOJI_REPLY_ENABLED": ("emoji", "reply_enabled"),
        "EMOJI_REPLY_COOLDOWN_SECONDS": ("emoji", "reply_cooldown_seconds"),
        "GROUP_REPLY_ONLY_AT": ("group_reply", "only_reply_when_at"),
        "GROUP_REPLY_INTEREST_REPLY_ENABLED": ("group_reply", "interest_reply_enabled"),
        "GROUP_REPLY_PLAN_REQUEST_INTERVAL": ("group_reply", "plan_request_interval"),
        "GROUP_REPLY_PLAN_MAX_PARALLEL": ("group_reply", "plan_request_max_parallel"),
        "GROUP_REPLY_AT_USER_WHEN_PROACTIVE_REPLY": ("group_reply", "at_user_when_proactive_reply"),
        "GROUP_REPLY_REPEAT_ECHO_ENABLED": ("group_reply", "repeat_echo_enabled"),
        "GROUP_REPLY_REPEAT_ECHO_WINDOW_SECONDS": ("group_reply", "repeat_echo_window_seconds"),
        "GROUP_REPLY_REPEAT_ECHO_MIN_COUNT": ("group_reply", "repeat_echo_min_count"),
        "GROUP_REPLY_REPEAT_ECHO_COOLDOWN_SECONDS": ("group_reply", "repeat_echo_cooldown_seconds"),
        "GROUP_REPLY_DECISION_API_BASE": ("group_reply_decision", "api_base"),
        "GROUP_REPLY_DECISION_API_KEY": ("group_reply_decision", "api_key"),
        "GROUP_REPLY_DECISION_MODEL": ("group_reply_decision", "model"),
        "GROUP_REPLY_DECISION_EXTRA_PARAMS": ("group_reply_decision", "extra_params"),
        "GROUP_REPLY_DECISION_EXTRA_HEADERS": ("group_reply_decision", "extra_headers"),
        "GROUP_REPLY_DECISION_RESPONSE_PATH": ("group_reply_decision", "response_path"),
        "MAX_CONTEXT_LENGTH": ("bot_behavior", "max_context_length"),
        "MAX_MESSAGE_LENGTH": ("bot_behavior", "max_message_length"),
        "RESPONSE_TIMEOUT": ("bot_behavior", "response_timeout"),
        "RATE_LIMIT_INTERVAL": ("bot_behavior", "rate_limit_interval"),
        "LOG_FULL_PROMPT": ("bot_behavior", "log_full_prompt"),
        "PRIVATE_QUOTE_REPLY_ENABLED": ("bot_behavior", "private_quote_reply_enabled"),
        "PRIVATE_BATCH_WINDOW_SECONDS": ("bot_behavior", "private_batch_window_seconds"),
        "SENTENCE_SPLIT_ENABLED": ("bot_behavior", "sentence_split_enabled"),
        "SEGMENTED_REPLY_ENABLED": ("bot_behavior", "segmented_reply_enabled"),
        "SEGMENTED_REPLY_MAX_SEGMENTS": ("bot_behavior", "max_segments"),
        "FIRST_SEGMENT_DELAY_MIN_MS": ("bot_behavior", "first_segment_delay_min_ms"),
        "FIRST_SEGMENT_DELAY_MAX_MS": ("bot_behavior", "first_segment_delay_max_ms"),
        "FOLLOWUP_DELAY_MIN_SECONDS": ("bot_behavior", "followup_delay_min_seconds"),
        "FOLLOWUP_DELAY_MAX_SECONDS": ("bot_behavior", "followup_delay_max_seconds"),
        "PLANNING_WINDOW_ENABLED": ("planning_window", "enabled"),
        "PLANNING_WINDOW_PRIVATE_SECONDS": ("planning_window", "private_window_seconds"),
        "PLANNING_WINDOW_GROUP_PROACTIVE_SECONDS": ("planning_window", "group_proactive_window_seconds"),
        "PLANNING_WINDOW_QUEUE_EXPIRE_SECONDS": ("planning_window", "queue_expire_seconds"),
        "MEMORY_DISPUTE_ENABLED": ("memory_dispute", "enabled"),
        "MEMORY_DISPUTE_HIGH_CONFIDENCE_THRESHOLD": ("memory_dispute", "high_confidence_threshold"),
        "MEMORY_DISPUTE_NORMAL_CONFIDENCE_THRESHOLD": ("memory_dispute", "normal_confidence_threshold"),
        "MEMORY_DISPUTE_SIGNAL_TTL_HOURS": ("memory_dispute", "signal_ttl_hours"),
        "CHARACTER_GROWTH_ENABLED": ("character_growth", "enabled"),
        "CHARACTER_GROWTH_EXPLICIT_FEEDBACK_THRESHOLD": ("character_growth", "explicit_feedback_threshold"),
        "CHARACTER_GROWTH_STABLE_SIGNAL_THRESHOLD": ("character_growth", "stable_signal_threshold"),
        "CHARACTER_GROWTH_CORE_TRAIT_THRESHOLD": ("character_growth", "core_trait_threshold"),
        "CHARACTER_GROWTH_TONE_PREFERENCE_THRESHOLD": ("character_growth", "tone_preference_threshold"),
        "CHARACTER_GROWTH_BEHAVIOR_HABIT_THRESHOLD": ("character_growth", "behavior_habit_threshold"),
        "ASSISTANT_NAME": ("assistant_profile", "name"),
        "ASSISTANT_ALIAS": ("assistant_profile", "alias"),
        "PERSONALITY": ("personality", "content"),
        "DIALOGUE_STYLE": ("dialogue_style", "content"),
        "BEHAVIOR": ("behavior", "content"),
        "MEMORY_ENABLED": ("memory", "enabled"),
        "MEMORY_STORAGE_PATH": ("memory", "storage_path"),
        "MEMORY_READ_SCOPE": ("memory", "read_scope"),
        "MEMORY_BM25_TOP_K": ("memory", "bm25_top_k"),
        "MEMORY_RERANK_TOP_K": ("memory", "rerank_top_k"),
        "MEMORY_RERANK_API_BASE": ("memory_rerank", "api_base"),
        "MEMORY_RERANK_API_KEY": ("memory_rerank", "api_key"),
        "MEMORY_RERANK_MODEL": ("memory_rerank", "model"),
        "MEMORY_RERANK_EXTRA_PARAMS": ("memory_rerank", "extra_params"),
        "MEMORY_RERANK_EXTRA_HEADERS": ("memory_rerank", "extra_headers"),
        "MEMORY_RERANK_RESPONSE_PATH": ("memory_rerank", "response_path"),
        "MEMORY_AUTO_EXTRACT": ("memory", "auto_extract"),
        "MEMORY_EXTRACT_EVERY_N_TURNS": ("memory", "extract_every_n_turns"),
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
    _JSON_STRING_KEYS = {
        "OPENAI_EXTRA_PARAMS", "OPENAI_EXTRA_HEADERS", "VISION_SERVICE_EXTRA_PARAMS", "VISION_SERVICE_EXTRA_HEADERS",
        "GROUP_REPLY_DECISION_EXTRA_PARAMS", "GROUP_REPLY_DECISION_EXTRA_HEADERS",
        "MEMORY_RERANK_EXTRA_PARAMS", "MEMORY_RERANK_EXTRA_HEADERS",
        "MEMORY_EXTRACTION_EXTRA_PARAMS", "MEMORY_EXTRACTION_EXTRA_HEADERS",
    }

    def __init__(self, path: str | None = None):
        self._path = self._resolve_path(path)
        self._raw_data: Dict[str, Any] = {}
        self._app = AppConfig()
        self._errors: List[str] = []
        self._load_error: Optional[str] = None
        self.reload()

    @property
    def app(self) -> AppConfig:
        return self._app

    @property
    def raw_data(self) -> Dict[str, Any]:
        return self._raw_data

    @property
    def path(self) -> str:
        return self._path

    def reload(self) -> None:
        self._errors = []
        self._load_error = None
        self._raw_data = {}
        self._app = AppConfig()
        try:
            with open(self._path, "rb") as file:
                payload = tomllib.load(file)
            self._raw_data = dict(payload) if isinstance(payload, dict) else {}
            logger.debug("配置加载完成：%s", self._path)
        except Exception as exc:
            self._load_error = f"加载配置失败 {self._path}: {exc}"
            logger.warning(self._load_error)
            return
        self._app = self._build_app_config()

    def validate(self) -> AppConfig:
        errors = self.get_validation_errors()
        if errors:
            raise ConfigValidationError(errors)
        return self._app

    def get_validation_errors(self) -> List[str]:
        return [self._load_error] if self._load_error else list(self._errors)

    def __getattr__(self, name: str) -> Any:
        if name == "BOT_NAME":
            return self.get_assistant_name()
        mapping = self._MAPPING.get(name)
        if mapping is None:
            raise AttributeError(f"'Config' object has no attribute '{name}'")
        section = getattr(self._app, mapping[0])
        value = getattr(section, mapping[1])
        if name in self._JSON_STRING_KEYS and isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
        return value

    def get_extra_params(self) -> Dict[str, Any]:
        return dict(self._app.ai_service.extra_params)

    def get_extra_headers(self) -> Dict[str, str]:
        return dict(self._app.ai_service.extra_headers)

    def get_assistant_name(self) -> str:
        name = self._app.assistant_profile.name.strip()
        if name:
            return name
        fallback = self._raw_data.get("bot_behavior", {}).get("name", "")
        return fallback.strip() if isinstance(fallback, str) and fallback.strip() else "AI助手"

    def get_assistant_alias(self) -> str:
        return self._app.assistant_profile.alias.strip()

    def get_memory_read_scope(self) -> str:
        return self._app.memory.read_scope

    def get_group_reply_decision_extra_params(self) -> Dict[str, Any]:
        value = self._app.group_reply_decision.extra_params
        return {} if value is None else dict(value)

    def get_group_reply_decision_extra_headers(self) -> Dict[str, str]:
        value = self._app.group_reply_decision.extra_headers
        return {} if value is None else dict(value)

    def get_vision_extra_params(self) -> Dict[str, Any]:
        return {} if self._app.vision_service.extra_params is None else dict(self._app.vision_service.extra_params)

    def get_vision_extra_headers(self) -> Dict[str, str]:
        return {} if self._app.vision_service.extra_headers is None else dict(self._app.vision_service.extra_headers)

    def get_memory_extraction_extra_params(self) -> Dict[str, Any]:
        value = self._app.memory.extraction_extra_params
        return {} if value is None else dict(value)

    def get_memory_extraction_extra_headers(self) -> Dict[str, str]:
        value = self._app.memory.extraction_extra_headers
        return {} if value is None else dict(value)

    def get_memory_rerank_extra_params(self) -> Dict[str, Any]:
        return {} if self._app.memory_rerank.extra_params is None else dict(self._app.memory_rerank.extra_params)

    def get_memory_rerank_extra_headers(self) -> Dict[str, str]:
        return {} if self._app.memory_rerank.extra_headers is None else dict(self._app.memory_rerank.extra_headers)

    def get_memory_rerank_client_config(self) -> Dict[str, Any]:
        rerank = self._app.memory_rerank
        return {
            "api_base": rerank.api_base or "",
            "api_key": rerank.api_key or "",
            "model": rerank.model or "",
            "extra_params": self.get_memory_rerank_extra_params(),
            "extra_headers": self.get_memory_rerank_extra_headers(),
            "response_path": rerank.response_path or AIServiceConfig().response_path,
        }

    def get_ai_service_client_config(self) -> Dict[str, Any]:
        ai_service = self._app.ai_service
        return {
            "api_base": ai_service.api_base,
            "api_key": ai_service.api_key,
            "model": ai_service.model,
            "extra_params": self.get_extra_params(),
            "extra_headers": self.get_extra_headers(),
            "response_path": ai_service.response_path,
        }

    def get_memory_extraction_client_config(self) -> Dict[str, Any]:
        memory = self._app.memory
        return {
            "api_base": memory.extraction_api_base or "",
            "api_key": memory.extraction_api_key or "",
            "model": memory.extraction_model or "",
            "extra_params": self.get_memory_extraction_extra_params(),
            "extra_headers": self.get_memory_extraction_extra_headers(),
            "response_path": memory.extraction_response_path or AIServiceConfig().response_path,
        }

    def get_group_reply_decision_client_config(self) -> Dict[str, Any]:
        decision = self._app.group_reply_decision
        return {
            "api_base": decision.api_base or "",
            "api_key": decision.api_key or "",
            "model": decision.model or "",
            "extra_params": self.get_group_reply_decision_extra_params(),
            "extra_headers": self.get_group_reply_decision_extra_headers(),
            "response_path": decision.response_path or AIServiceConfig().response_path,
        }

    def get_vision_client_config(self) -> Dict[str, Any]:
        vision = self._app.vision_service
        return {
            "enabled": vision.enabled,
            "api_base": vision.api_base or "",
            "api_key": vision.api_key or "",
            "model": vision.model or "",
            "extra_params": self.get_vision_extra_params(),
            "extra_headers": self.get_vision_extra_headers(),
            "response_path": vision.response_path or AIServiceConfig().response_path,
        }

    def _resolve_path(self, path: str | None) -> str:
        if path is not None:
            return os.path.normpath(path)
        current_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(current_dir, "..", "..", "config", "config.toml"),
            os.path.join(os.getcwd(), "config", "config.toml"),
            os.path.join(os.getcwd(), "config.toml"),
        ]
        for candidate in candidates:
            normalized = os.path.normpath(candidate)
            if os.path.exists(normalized):
                return normalized
        return os.path.normpath(candidates[0])

    def _build_app_config(self) -> AppConfig:
        return AppConfig(
            adapter_connection=self._build_adapter_connection_config(), ai_service=self._build_ai_service_config(),
            vision_service=self._build_vision_service_config(), emoji=self._build_emoji_config(),
            bot_behavior=self._build_bot_behavior_config(), planning_window=self._build_planning_window_config(),
            memory_dispute=self._build_memory_dispute_config(), character_growth=self._build_character_growth_config(),
            assistant_profile=self._build_assistant_profile_config(),
            group_reply=self._build_group_reply_config(), group_reply_decision=self._build_group_reply_decision_config(),
            memory_rerank=self._build_memory_rerank_config(),
            personality=self._build_content_section("personality"), dialogue_style=self._build_content_section("dialogue_style"),
            behavior=self._build_content_section("behavior"), memory=self._build_memory_config(),
        )

    def _build_adapter_connection_config(self) -> AdapterConnectionConfig:
        section = self._get_adapter_connection_section()
        return AdapterConnectionConfig(
            adapter=self._literal_string(section, "adapter_connection", "adapter", allowed={"napcat", "api", "openapi"}, default="napcat"),
            platform=self._optional_string(section, "adapter_connection", "platform", default="qq") or "qq",
            ws_url=self._require_string(section, "adapter_connection", "ws_url", default="ws://0.0.0.0:8095"),
            http_url=self._require_string(section, "adapter_connection", "http_url", default="http://127.0.0.1:6700"),
        )

    def _get_adapter_connection_section(self) -> Dict[str, Any]:
        section = self._get_section("adapter_connection")
        if section:
            return section
        return self._get_section("napcat")

    def _build_ai_service_config(self) -> AIServiceConfig:
        section = self._get_section("ai_service")
        return AIServiceConfig(
            api_base=self._require_string(section, "ai_service", "api_base"),
            api_key=self._optional_string(section, "ai_service", "api_key", default=""),
            model=self._require_string(section, "ai_service", "model"),
            extra_params=self._mapping_value(section, "ai_service", "extra_params", default={}),
            extra_headers=self._mapping_value(section, "ai_service", "extra_headers", default={}),
            response_path=self._optional_string(section, "ai_service", "response_path", default="choices.0.message.content"),
        )

    def _build_vision_service_config(self) -> VisionServiceConfig:
        section = self._get_section("vision_service")
        return VisionServiceConfig(
            enabled=self._bool_value(section, "vision_service", "enabled", default=False),
            api_base=self._nullable_string(section, "vision_service", "api_base"),
            api_key=self._nullable_string(section, "vision_service", "api_key"),
            model=self._nullable_string(section, "vision_service", "model"),
            extra_params=self._nullable_mapping(section, "vision_service", "extra_params"),
            extra_headers=self._nullable_mapping(section, "vision_service", "extra_headers"),
            response_path=self._nullable_string(section, "vision_service", "response_path"),
        )

    def _build_emoji_config(self) -> EmojiConfig:
        section = self._get_section("emoji")
        return EmojiConfig(
            enabled=self._bool_value(section, "emoji", "enabled", default=True),
            storage_path=self._optional_string(section, "emoji", "storage_path", default="../data/emojis"),
            capture_enabled=self._bool_value(section, "emoji", "capture_enabled", default=True),
            classification_enabled=self._bool_value(section, "emoji", "classification_enabled", default=True),
            idle_seconds_before_classify=self._bounded_float(section, "emoji", "idle_seconds_before_classify", default=45.0, minimum=0.0),
            classification_interval_seconds=self._bounded_float(section, "emoji", "classification_interval_seconds", default=30.0, minimum=0.0),
            classification_windows=self._time_window_list(section, "emoji", "classification_windows", default=[]),
            emotion_labels=self._string_list(section, "emoji", "emotion_labels", default=_DEFAULT_EMOTION_LABELS),
            reply_enabled=self._bool_value(section, "emoji", "reply_enabled", default=False),
            reply_cooldown_seconds=self._bounded_float(section, "emoji", "reply_cooldown_seconds", default=180.0, minimum=0.0),
        )

    def _build_bot_behavior_config(self) -> BotBehaviorConfig:
        section = self._get_section("bot_behavior")
        config = BotBehaviorConfig(
            max_context_length=self._bounded_int(section, "bot_behavior", "max_context_length", default=10, minimum=1),
            max_message_length=self._bounded_int(section, "bot_behavior", "max_message_length", default=4000, minimum=1),
            response_timeout=self._bounded_int(section, "bot_behavior", "response_timeout", default=60, minimum=1),
            rate_limit_interval=self._bounded_float(section, "bot_behavior", "rate_limit_interval", default=1.0, minimum=0.0),
            log_full_prompt=self._bool_value(section, "bot_behavior", "log_full_prompt", default=False),
            private_quote_reply_enabled=self._bool_value(section, "bot_behavior", "private_quote_reply_enabled", default=False),
            private_batch_window_seconds=self._bounded_float(section, "bot_behavior", "private_batch_window_seconds", default=1.2, minimum=0.0),
            sentence_split_enabled=self._bool_value(section, "bot_behavior", "sentence_split_enabled", default=True),
            segmented_reply_enabled=self._bool_value(section, "bot_behavior", "segmented_reply_enabled", default=True),
            max_segments=self._bounded_int(section, "bot_behavior", "max_segments", default=3, minimum=1),
            first_segment_delay_min_ms=self._bounded_int(section, "bot_behavior", "first_segment_delay_min_ms", default=0, minimum=0),
            first_segment_delay_max_ms=self._bounded_int(section, "bot_behavior", "first_segment_delay_max_ms", default=600, minimum=0),
            followup_delay_min_seconds=self._bounded_float(section, "bot_behavior", "followup_delay_min_seconds", default=3.0, minimum=0.0),
            followup_delay_max_seconds=self._bounded_float(section, "bot_behavior", "followup_delay_max_seconds", default=10.0, minimum=0.0),
        )
        if config.first_segment_delay_min_ms > config.first_segment_delay_max_ms:
            self._add_error("bot_behavior.first_segment_delay_min_ms cannot be greater than bot_behavior.first_segment_delay_max_ms")
            config = BotBehaviorConfig(**{**config.__dict__, "first_segment_delay_min_ms": 0, "first_segment_delay_max_ms": max(0, config.first_segment_delay_max_ms)})
        if config.followup_delay_min_seconds > config.followup_delay_max_seconds:
            self._add_error("bot_behavior.followup_delay_min_seconds cannot be greater than bot_behavior.followup_delay_max_seconds")
            config = BotBehaviorConfig(**{**config.__dict__, "followup_delay_min_seconds": 0.0, "followup_delay_max_seconds": max(0.0, config.followup_delay_max_seconds)})
        return config

    def _build_planning_window_config(self) -> PlanningWindowConfig:
        section = self._get_section("planning_window")
        fallback_private_window = self._bounded_float(
            self._get_section("bot_behavior"),
            "bot_behavior",
            "private_batch_window_seconds",
            default=1.2,
            minimum=0.0,
        )
        return PlanningWindowConfig(
            enabled=self._bool_value(section, "planning_window", "enabled", default=True),
            private_window_seconds=self._bounded_float(
                section,
                "planning_window",
                "private_window_seconds",
                default=fallback_private_window,
                minimum=0.0,
            ),
            group_proactive_window_seconds=self._bounded_float(
                section,
                "planning_window",
                "group_proactive_window_seconds",
                default=0.45,
                minimum=0.0,
            ),
            queue_expire_seconds=self._bounded_float(
                section,
                "planning_window",
                "queue_expire_seconds",
                default=60.0,
                minimum=0.0,
            ),
        )

    def _build_memory_dispute_config(self) -> MemoryDisputeConfig:
        section = self._get_section("memory_dispute")
        high = self._bounded_float(
            section,
            "memory_dispute",
            "high_confidence_threshold",
            default=0.75,
            minimum=0.0,
            maximum=1.0,
        )
        normal = self._bounded_float(
            section,
            "memory_dispute",
            "normal_confidence_threshold",
            default=0.45,
            minimum=0.0,
            maximum=1.0,
        )
        if normal > high:
            self._add_error("memory_dispute.normal_confidence_threshold cannot be greater than memory_dispute.high_confidence_threshold")
            normal = high
        return MemoryDisputeConfig(
            enabled=self._bool_value(section, "memory_dispute", "enabled", default=True),
            high_confidence_threshold=high,
            normal_confidence_threshold=normal,
            signal_ttl_hours=self._bounded_float(
                section,
                "memory_dispute",
                "signal_ttl_hours",
                default=168.0,
                minimum=1.0,
            ),
        )

    def _build_character_growth_config(self) -> CharacterGrowthConfig:
        section = self._get_section("character_growth")
        return CharacterGrowthConfig(
            enabled=self._bool_value(section, "character_growth", "enabled", default=True),
            explicit_feedback_threshold=self._bounded_int(section, "character_growth", "explicit_feedback_threshold", default=2, minimum=1),
            stable_signal_threshold=self._bounded_int(section, "character_growth", "stable_signal_threshold", default=6, minimum=1),
            core_trait_threshold=self._bounded_int(section, "character_growth", "core_trait_threshold", default=5, minimum=1),
            tone_preference_threshold=self._bounded_int(section, "character_growth", "tone_preference_threshold", default=3, minimum=1),
            behavior_habit_threshold=self._bounded_int(section, "character_growth", "behavior_habit_threshold", default=2, minimum=1),
        )

    def _build_assistant_profile_config(self) -> AssistantProfileConfig:
        section = self._get_section("assistant_profile")
        return AssistantProfileConfig(
            name=self._require_string(section, "assistant_profile", "name", default="AI??"),
            alias=self._optional_string(section, "assistant_profile", "alias", default=""),
            avatar_path=self._optional_string(section, "assistant_profile", "avatar_path", default=""),
        )

    def _build_group_reply_config(self) -> GroupReplyConfig:
        section = self._get_section("group_reply")
        config = GroupReplyConfig(
            only_reply_when_at=self._bool_value(section, "group_reply", "only_reply_when_at", default=True),
            interest_reply_enabled=self._bool_value(section, "group_reply", "interest_reply_enabled", default=True),
            plan_request_interval=self._bounded_float(section, "group_reply", "plan_request_interval", default=3.0, minimum=0.0),
            plan_request_max_parallel=self._bounded_int(section, "group_reply", "plan_request_max_parallel", default=1, minimum=1),
            at_user_when_proactive_reply=self._bool_value(section, "group_reply", "at_user_when_proactive_reply", default=False),
            repeat_echo_enabled=self._bool_value(section, "group_reply", "repeat_echo_enabled", default=False),
            repeat_echo_window_seconds=self._bounded_float(section, "group_reply", "repeat_echo_window_seconds", default=20.0, minimum=1.0),
            repeat_echo_min_count=self._bounded_int(section, "group_reply", "repeat_echo_min_count", default=2, minimum=2),
            repeat_echo_cooldown_seconds=self._bounded_float(section, "group_reply", "repeat_echo_cooldown_seconds", default=90.0, minimum=0.0),
        )
        return config

    def _build_group_reply_decision_config(self) -> GroupReplyDecisionConfig:
        section = self._get_section("group_reply_decision")
        return GroupReplyDecisionConfig(
            api_base=self._nullable_string(section, "group_reply_decision", "api_base"),
            api_key=self._nullable_string(section, "group_reply_decision", "api_key"),
            model=self._nullable_string(section, "group_reply_decision", "model"),
            extra_params=self._nullable_mapping(section, "group_reply_decision", "extra_params"),
            extra_headers=self._nullable_mapping(section, "group_reply_decision", "extra_headers"),
            response_path=self._nullable_string(section, "group_reply_decision", "response_path"),
        )

    def _build_memory_rerank_config(self) -> MemoryRerankConfig:
        section = self._get_section("memory_rerank")
        return MemoryRerankConfig(
            api_base=self._nullable_string(section, "memory_rerank", "api_base"),
            api_key=self._nullable_string(section, "memory_rerank", "api_key"),
            model=self._nullable_string(section, "memory_rerank", "model"),
            extra_params=self._nullable_mapping(section, "memory_rerank", "extra_params"),
            extra_headers=self._nullable_mapping(section, "memory_rerank", "extra_headers"),
            response_path=self._nullable_string(section, "memory_rerank", "response_path"),
        )

    def _build_content_section(self, section_name: str) -> ContentSection:
        return ContentSection(content=self._optional_string(self._get_section(section_name), section_name, "content", default=""))

    def _build_memory_config(self) -> MemoryConfig:
        section = self._get_section("memory")
        config = MemoryConfig(
            enabled=self._bool_value(section, "memory", "enabled", default=False),
            storage_path=self._optional_string(section, "memory", "storage_path", default="../data/memories"),
            read_scope=self._literal_string(section, "memory", "read_scope", allowed={"user", "global"}, default="user"),
            bm25_top_k=self._bounded_int(section, "memory", "bm25_top_k", default=100, minimum=1),
            rerank_top_k=self._bounded_int(section, "memory", "rerank_top_k", default=20, minimum=1),
            pre_rerank_top_k=self._bounded_int(section, "memory", "pre_rerank_top_k", default=12, minimum=1),
            dynamic_memory_limit=self._bounded_int(section, "memory", "dynamic_memory_limit", default=8, minimum=1),
            dynamic_dedup_enabled=self._bool_value(section, "memory", "dynamic_dedup_enabled", default=True),
            dynamic_dedup_similarity_threshold=self._bounded_float(section, "memory", "dynamic_dedup_similarity_threshold", default=0.72, minimum=0.0, maximum=1.0),
            rerank_candidate_max_chars=self._bounded_int(
                section,
                "memory",
                "rerank_candidate_max_chars",
                default=160,
                minimum=MIN_RERANK_CANDIDATE_MAX_CHARS,
            ),
            rerank_total_prompt_budget=self._bounded_int(
                section,
                "memory",
                "rerank_total_prompt_budget",
                default=2400,
                minimum=MIN_RERANK_TOTAL_PROMPT_BUDGET,
            ),
            auto_extract=self._bool_value(section, "memory", "auto_extract", default=True),
            extract_every_n_turns=self._bounded_int(section, "memory", "extract_every_n_turns", default=3, minimum=1),
            extraction_api_base=self._nullable_string(section, "memory", "extraction_api_base"),
            extraction_api_key=self._nullable_string(section, "memory", "extraction_api_key"),
            extraction_model=self._nullable_string(section, "memory", "extraction_model"),
            extraction_extra_params=self._nullable_mapping(section, "memory", "extraction_extra_params"),
            extraction_extra_headers=self._nullable_mapping(section, "memory", "extraction_extra_headers"),
            extraction_response_path=self._nullable_string(section, "memory", "extraction_response_path"),
            ordinary_decay_enabled=self._bool_value(section, "memory", "ordinary_decay_enabled", default=True),
            ordinary_half_life_days=self._bounded_float(section, "memory", "ordinary_half_life_days", default=30.0, minimum=0.000001),
            ordinary_forget_threshold=self._bounded_float(section, "memory", "ordinary_forget_threshold", default=0.5, minimum=0.0, maximum=1.0),
            local_bm25_weight=self._bounded_float(section, "memory", "local_bm25_weight", default=1.0, minimum=0.0),
            local_importance_weight=self._bounded_float(section, "memory", "local_importance_weight", default=0.35, minimum=0.0),
            local_mention_weight=self._bounded_float(section, "memory", "local_mention_weight", default=0.2, minimum=0.0),
            local_recency_weight=self._bounded_float(section, "memory", "local_recency_weight", default=0.15, minimum=0.0),
            local_scene_weight=self._bounded_float(section, "memory", "local_scene_weight", default=0.3, minimum=0.0),
        )
        rerank_top_k = config.rerank_top_k
        pre_rerank_top_k = config.pre_rerank_top_k
        if rerank_top_k > config.bm25_top_k:
            self._add_error("memory.rerank_top_k cannot be greater than memory.bm25_top_k")
        if pre_rerank_top_k > config.bm25_top_k:
            self._add_error("memory.pre_rerank_top_k cannot be greater than memory.bm25_top_k")
        if rerank_top_k > config.bm25_top_k or pre_rerank_top_k > config.bm25_top_k:
            config = MemoryConfig(
                enabled=config.enabled,
                storage_path=config.storage_path,
                read_scope=config.read_scope,
                bm25_top_k=config.bm25_top_k,
                rerank_top_k=min(rerank_top_k, config.bm25_top_k),
                pre_rerank_top_k=min(pre_rerank_top_k, config.bm25_top_k),
                dynamic_memory_limit=config.dynamic_memory_limit,
                dynamic_dedup_enabled=config.dynamic_dedup_enabled,
                dynamic_dedup_similarity_threshold=config.dynamic_dedup_similarity_threshold,
                rerank_candidate_max_chars=config.rerank_candidate_max_chars,
                rerank_total_prompt_budget=config.rerank_total_prompt_budget,
                auto_extract=config.auto_extract,
                extract_every_n_turns=config.extract_every_n_turns,
                extraction_api_base=config.extraction_api_base,
                extraction_api_key=config.extraction_api_key,
                extraction_model=config.extraction_model,
                extraction_extra_params=config.extraction_extra_params,
                extraction_extra_headers=config.extraction_extra_headers,
                extraction_response_path=config.extraction_response_path,
                ordinary_decay_enabled=config.ordinary_decay_enabled,
                ordinary_half_life_days=config.ordinary_half_life_days,
                ordinary_forget_threshold=config.ordinary_forget_threshold,
                local_bm25_weight=config.local_bm25_weight,
                local_importance_weight=config.local_importance_weight,
                local_mention_weight=config.local_mention_weight,
                local_recency_weight=config.local_recency_weight,
                local_scene_weight=config.local_scene_weight,
            )
        return config

    def _get_section(self, section_name: str) -> Dict[str, Any]:
        value = self._raw_data.get(section_name, {})
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        self._add_error(f"{section_name} must be an object")
        return {}

    def _require_string(self, section: Dict[str, Any], section_name: str, key: str, default: str = "") -> str:
        value = section.get(key)
        if value is None:
            self._add_error(f"{section_name}.{key} is required")
            return default
        if not isinstance(value, str):
            self._add_error(f"{section_name}.{key} must be a string")
            return default
        stripped = value.strip()
        if not stripped:
            self._add_error(f"{section_name}.{key} cannot be empty")
            return default
        return stripped

    def _optional_string(self, section: Dict[str, Any], section_name: str, key: str, default: str = "") -> str:
        value = section.get(key)
        if value is None:
            return default
        if not isinstance(value, str):
            self._add_error(f"{section_name}.{key} must be a string")
            return default
        return value.strip()

    def _nullable_string(self, section: Dict[str, Any], section_name: str, key: str) -> Optional[str]:
        value = section.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            self._add_error(f"{section_name}.{key} must be a string or null")
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.lower() in {"none", "null"}:
            return None
        return stripped

    def _mapping_value(self, section: Dict[str, Any], section_name: str, key: str, default: Dict[str, Any] | None = None) -> Dict[str, Any]:
        value = section.get(key)
        if value is None:
            return dict(default or {})
        if not isinstance(value, dict):
            self._add_error(f"{section_name}.{key} must be an object")
            return dict(default or {})
        return dict(value)

    def _nullable_mapping(self, section: Dict[str, Any], section_name: str, key: str) -> Optional[Dict[str, Any]]:
        value = section.get(key)
        if value is None:
            return None
        if not isinstance(value, dict):
            self._add_error(f"{section_name}.{key} must be an object or null")
            return None
        return dict(value)

    def _bool_value(self, section: Dict[str, Any], section_name: str, key: str, default: bool = False) -> bool:
        value = section.get(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        self._add_error(f"{section_name}.{key} must be a boolean")
        return default

    def _bounded_int(self, section: Dict[str, Any], section_name: str, key: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
        value = section.get(key)
        if value is None:
            return default
        if isinstance(value, bool):
            self._add_error(f"{section_name}.{key} must be an integer")
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            self._add_error(f"{section_name}.{key} must be an integer")
            return default
        if minimum is not None and parsed < minimum:
            self._add_error(f"{section_name}.{key} cannot be less than {minimum}")
            return default
        if maximum is not None and parsed > maximum:
            self._add_error(f"{section_name}.{key} cannot be greater than {maximum}")
            return default
        return parsed

    def _bounded_float(self, section: Dict[str, Any], section_name: str, key: str, default: float, minimum: float | None = None, maximum: float | None = None) -> float:
        value = section.get(key)
        if value is None:
            return default
        if isinstance(value, bool):
            self._add_error(f"{section_name}.{key} must be a number")
            return default
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            self._add_error(f"{section_name}.{key} must be a number")
            return default
        if minimum is not None and parsed < minimum:
            self._add_error(f"{section_name}.{key} cannot be less than {minimum}")
            return default
        if maximum is not None and parsed > maximum:
            self._add_error(f"{section_name}.{key} cannot be greater than {maximum}")
            return default
        return parsed

    def _literal_string(self, section: Dict[str, Any], section_name: str, key: str, allowed: set[str], default: str) -> str:
        value = section.get(key)
        if value is None:
            return default
        if not isinstance(value, str):
            self._add_error(f"{section_name}.{key} must be a string")
            return default
        normalized = value.strip().lower()
        if normalized not in allowed:
            self._add_error(f"{section_name}.{key} must be one of: {', '.join(sorted(allowed))}")
            return default
        return normalized

    def _string_list(self, section: Dict[str, Any], section_name: str, key: str, default: List[str]) -> List[str]:
        value = section.get(key)
        if value is None:
            return list(default)
        if not isinstance(value, list):
            self._add_error(f"{section_name}.{key} must be a list of strings")
            return list(default)
        result: List[str] = []
        for item in value:
            if not isinstance(item, str):
                self._add_error(f"{section_name}.{key} must be a list of strings")
                return list(default)
            normalized = item.strip()
            if normalized:
                result.append(normalized)
        return result or list(default)

    def _time_window_list(self, section: Dict[str, Any], section_name: str, key: str, default: List[str]) -> List[str]:
        values = self._string_list(section, section_name, key, default)
        if values == list(default) and section.get(key) is not None and not isinstance(section.get(key), list):
            return list(default)
        for value in values:
            if not _TIME_WINDOW_RE.match(value):
                self._add_error(f"{section_name}.{key} contains invalid window: {value}")
                return list(default)
        return values

    def _add_error(self, message: str) -> None:
        self._errors.append(message)


config = Config()
