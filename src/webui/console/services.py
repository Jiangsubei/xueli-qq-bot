from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterable, List, Optional

from django.conf import settings
from django.urls import reverse
from tomlkit import dumps

from src.core.config import (
    Config,
    ConfigValidationError,
    get_vision_service_status,
    is_ai_service_configured,
    is_memory_extraction_configured,
)
from src.core.toml_utils import (
    dumps_toml_document,
    parse_toml_document,
    prune_none_values,
    sync_toml_container,
    toml_to_plain_data,
)
from src.core.webui_runtime_registry import get_runtime_state, run_coro_threadsafe
from src.memory.internal.access_policy import MemoryAccessPolicy
from src.memory.storage.important_memory_store import ImportantMemoryItem, ImportantMemoryStore
from src.memory.storage.markdown_store import MarkdownMemoryStore, MemoryItem

MASKED_SECRET = "*****"
ALLOWED_AVATAR_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ALLOWED_AVATAR_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}
SNAPSHOT_FIELDS = (
    "assistant_name",
    "qq_connection",
    "vision_status",
    "memory_status",
    "messages_received",
    "messages_replied",
    "message_errors",
    "run_status",
    "connection_status",
    "uptime",
    "active_conversations",
    "active_tasks",
    "group_repeat_echo",
    "emoji_total",
    "emoji_pending",
    "memory_reads",
    "memory_writes",
    "snapshot_label",
    "online_label",
)

GROUP_STRATEGY_OPTIONS = [
    {"value": "smart", "label": "\u667a\u80fd\u56de\u590d", "help": "\u52a9\u624b\u4f1a\u81ea\u5df1\u5224\u65ad\u4ec0\u4e48\u65f6\u5019\u63a5\u8bdd\uff0c\u9002\u5408\u5927\u591a\u6570\u7fa4\u804a\u3002"},
    {"value": "mixed", "label": "\u6df7\u5408\u6a21\u5f0f", "help": "\u65e2\u4f1a\u81ea\u5df1\u5224\u65ad\uff0c\u4e5f\u4f1a\u4f18\u5148\u56de\u5e94 @ \u5b83\u7684\u6d88\u606f\u3002"},
    {"value": "at_only", "label": "\u4ec5@\u56de\u590d", "help": "\u53ea\u6709\u88ab\u70b9\u540d\u65f6\u624d\u8bf4\u8bdd\uff0c\u66f4\u5b89\u9759\u4e00\u4e9b\u3002"},
    {"value": "quiet", "label": "\u5b89\u9759\u6a21\u5f0f", "help": "\u5c3d\u91cf\u5c11\u63d2\u8bdd\uff0c\u53ea\u4fdd\u7559\u6700\u57fa\u672c\u7684\u5b58\u5728\u611f\u3002"},
]
MEMORY_SCOPE_OPTIONS = [
    {"value": "user", "label": "\u53ea\u770b\u81ea\u5df1", "help": "\u53ea\u8bfb\u53d6\u5f53\u524d\u7528\u6237\u81ea\u5df1\u7684\u8bb0\u5fc6\uff0c\u66f4\u79c1\u5bc6\u4e5f\u66f4\u7a33\u59a5\u3002"},
    {"value": "global", "label": "\u4e00\u8d77\u770b", "help": "\u4f1a\u628a\u5171\u4eab\u8bb0\u5fc6\u4e5f\u4e00\u8d77\u5e26\u4e0a\uff0c\u65b9\u4fbf\u7406\u89e3\u66f4\u591a\u4e0a\u4e0b\u6587\u3002"},
]
FIELD_HELP = {
    "network": {
        "ws_url": "填写 QQ 事件推送使用的 WebSocket 地址。",
        "http_url": "填写助手主动调用使用的 HTTP 地址。",
    },
    "model": {
        "api_base": "填写模型服务地址。",
        "model": "填写要使用的模型名称。",
        "api_key": "可留空；为空时不发送 Authorization，保留遮罩时不会覆盖原密钥。",
        "extra_params": "补充模型请求时需要附带的额外参数。",
        "extra_headers": "如果服务需要额外请求头，就在这里填写。",
        "response_path": "指定回复内容在返回 JSON 中的位置。",
        "temperature": "常用采样温度，越高越发散，越低越稳定。",
    },
    "assistant": {
        "name": "页面和对话里显示的助手名字。",
        "alias": "更顺口的别名，可选。",
        "max_context_length": "每次回复参考多少条最近消息。",
        "max_message_length": "限制单次回复的最大长度。",
        "response_timeout": "等待模型返回结果的最长时间。",
        "private_quote_reply_enabled": "打开后，私聊回复会引用原消息。",
        "group_strategy": "决定助手在群里什么时候开口。",
        "personality": "描述助手的性格。",
        "dialogue_style": "描述助手平时怎么说话。",
        "rate_limit_interval": "两次发送之间的最小间隔。",
        "log_full_prompt": "打开后会在日志里记录完整提示词。",
        "plan_request_interval": "同一群聊内主动规划请求的冷却时间。",
        "plan_request_max_parallel": "群聊规划请求的最大并行数。",
        "plan_context_message_count": "规划时附带的最近群聊条数。",
        "at_user_when_proactive_reply": "主动群聊回复时是否 @ 触发用户。",
        "repeat_echo_enabled": "打开后，群里短时间内重复出现的短消息会被复读一次。",
        "repeat_echo_window_seconds": "统计重复消息时使用的时间窗口。",
        "repeat_echo_min_count": "重复达到多少次后触发复读。",
        "repeat_echo_cooldown_seconds": "两次群聊复读之间的最小间隔。",
        "behavior": "约束助手能做什么、不能做什么。",
    },
    "emoji": {
        "enabled": "关闭后，不再处理表情相关功能。",
        "capture_enabled": "是否收集聊天里出现的新表情。",
        "classification_enabled": "是否自动给新表情分类。",
        "reply_enabled": "是否在合适的时候发送表情。",
        "idle_seconds_before_classify": "空闲多久后开始整理新表情。",
        "classification_interval_seconds": "两次自动整理之间的间隔。",
        "classification_windows": "仅在这些时间段内自动分类。",
        "emotion_labels": "表情分类时可选的情绪标签。",
        "reply_cooldown_seconds": "两次表情回复之间的最小间隔。",
        "storage_path": "表情图片和索引的保存位置。",
    },
    "memory": {
        "enabled": "关闭后，不再读写记忆。",
        "auto_extract": "打开后，助手会自动提取新记忆。",
        "read_scope": "决定只读当前用户，还是包含共享记忆。",
        "bm25_top_k": "本地初筛时最多取多少候选记忆。",
        "rerank_top_k": "送去重排的候选条数。",
        "extract_every_n_turns": "每聊多少轮后尝试提取一次记忆。",
        "ordinary_decay_enabled": "是否让普通记忆随时间衰减。",
        "ordinary_half_life_days": "普通记忆衰减一半所需的大致天数。",
        "ordinary_forget_threshold": "衰减到这个阈值后视为可遗忘。",
        "storage_path": "记忆数据的保存位置。",
        "pre_rerank_top_k": "本地预排序后送入重排的最大候选数。",
        "dynamic_memory_limit": "最终提示词里最多保留多少条动态记忆。",
        "dynamic_dedup_enabled": "是否对动态记忆做相似内容去重。",
        "dynamic_dedup_similarity_threshold": "动态记忆去重阈值，越高越严格。",
        "rerank_candidate_max_chars": "单条候选记忆进入重排提示词时的最大长度。",
        "rerank_total_prompt_budget": "重排提示词允许占用的总字符预算。",
        "local_bm25_weight": "本地排序里 BM25 分数的权重。",
        "local_importance_weight": "本地排序里重要度的权重。",
        "local_mention_weight": "本地排序里提及次数的权重。",
        "local_recency_weight": "本地排序里新近程度的权重。",
        "local_scene_weight": "本地排序里场景匹配的权重。",
    },
}

_CONFIG_CACHE_LOCK = Lock()
_CONFIG_CACHE: Dict[str, Any] = {
    "path": None,
    "mtime_ns": None,
    "config": None,
}

_OPTIONAL_MODEL_SECTIONS = {"group_reply_decision", "vision_service", "memory_rerank"}


def _repo_root() -> Path:
    config_path = Path(settings.WEBUI_CONFIG_PATH)
    return config_path.resolve().parent


def _config_path() -> Path:
    return Path(settings.WEBUI_CONFIG_PATH)


def _clear_config_cache() -> None:
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE["path"] = None
        _CONFIG_CACHE["mtime_ns"] = None
        _CONFIG_CACHE["config"] = None


def _load_config_document() -> tuple[Any, Dict[str, Any]]:
    doc = parse_toml_document(_config_path())
    raw = toml_to_plain_data(doc)
    return doc, raw if isinstance(raw, dict) else {}


def _drop_empty_optional_model_sections(raw: Dict[str, Any]) -> None:
    for section_name in _OPTIONAL_MODEL_SECTIONS:
        section = raw.get(section_name)
        if not isinstance(section, dict):
            raw.pop(section_name, None)
            continue
        meaningful = {
            key: value
            for key, value in section.items()
            if value not in (None, "", [], {}) and not (section_name == "vision_service" and key == "enabled" and value is False)
        }
        if not meaningful:
            raw.pop(section_name, None)


def _snapshot_path() -> Path:
    return Path(settings.WEBUI_RUNTIME_SNAPSHOT_PATH)


def _avatar_root() -> Path:
    return Path(getattr(settings, "WEBUI_AVATAR_ROOT", _repo_root() / "data" / "webui" / "avatar"))


def _snapshot_ttl_seconds() -> int:
    return int(getattr(settings, "WEBUI_SNAPSHOT_TTL_SECONDS", 15))


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _mask_secret(value: Any) -> str:
    secret = str(value or "").strip()
    if not secret:
        return ""
    prefix = secret[:3]
    suffix = secret[-5:] if len(secret) > 5 else secret
    return f"{prefix}{MASKED_SECRET}{suffix}"


def _format_count(value: Any, online: bool) -> str:
    return str(_safe_int(value)) if online else "--"


def _format_optional_count(value: Any) -> str:
    if value in (None, ""):
        return "--"
    return str(_safe_int(value))


def _normalize_nullable_string(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.lower() in {"none", "null"}:
        return None
    return text


def _format_uptime(value: Any, online: bool) -> str:
    if not online:
        return "--"
    total_seconds = max(0, int(float(value or 0)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}\u5929 {hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _vision_status_label(status: str) -> str:
    mapping = {
        "enabled": "\u5df2\u542f\u7528",
        "unconfigured": "\u672a\u914d\u7f6e",
        "disabled": "\u672a\u542f\u7528",
    }
    return mapping.get(str(status or "").strip().lower(), "\u672a\u542f\u7528")


def _group_strategy_from_config(app_config) -> str:
    group_reply = app_config.group_reply
    only_at = bool(group_reply.only_reply_when_at)
    interest = bool(group_reply.interest_reply_enabled)
    if only_at and interest:
        return "mixed"
    if only_at:
        return "at_only"
    if interest:
        return "smart"
    return "quiet"


def _parse_group_strategy(strategy: str) -> Dict[str, bool]:
    normalized = str(strategy or "").strip().lower()
    mapping = {
        "mixed": {"only_reply_when_at": True, "interest_reply_enabled": True},
        "at_only": {"only_reply_when_at": True, "interest_reply_enabled": False},
        "smart": {"only_reply_when_at": False, "interest_reply_enabled": True},
        "quiet": {"only_reply_when_at": False, "interest_reply_enabled": False},
    }
    return mapping.get(normalized, mapping["smart"])


def _resolve_storage_path(raw_path: str) -> Path:
    path = Path(str(raw_path or "").strip())
    if path.is_absolute():
        return path
    return _repo_root() / path


def _load_json_file(path: Path, default: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = default or {}
    if not path.exists():
        return dict(payload)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(payload)
    return data if isinstance(data, dict) else dict(payload)


def _load_emoji_stats(app_config) -> Dict[str, int]:
    storage_path = _resolve_storage_path(app_config.emoji.storage_path)
    index_path = storage_path / "index.json"
    data = _load_json_file(index_path, {"items": {}})
    items = list((data.get("items") or {}).values())
    return {
        "emoji_total": len(items),
        "emoji_pending_classification": sum(
            1 for item in items if item.get("emotion_status") == "pending" and not item.get("disabled", False)
        ),
    }


def _format_snapshot_label(snapshot_at_text: str) -> str:
    if not snapshot_at_text:
        return "--"
    try:
        return datetime.fromisoformat(snapshot_at_text).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return "\u521a\u521a\u540c\u6b65"


def _avatar_absolute_path(avatar_path: str) -> Path | None:
    relative = str(avatar_path or "").strip()
    if not relative:
        return None
    path = Path(relative)
    if path.is_absolute():
        return path
    normalized = path.as_posix().lstrip("./")
    if normalized.startswith("data/webui/avatar/"):
        return (_avatar_root() / path.name).resolve()
    return (_repo_root() / path).resolve()


def _avatar_url(avatar_path: str) -> str | None:
    path = _avatar_absolute_path(avatar_path)
    if not path or not path.exists() or not path.is_file():
        return None
    stamp = int(path.stat().st_mtime_ns)
    return f"{reverse('assistant-avatar')}?v={stamp}"


def load_runtime_snapshot() -> Dict[str, Any]:
    path = _snapshot_path()
    if not path.exists():
        return {"available": False, "payload": {}, "reason": "missing"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False, "payload": {}, "reason": "invalid"}

    snapshot_at_text = str(payload.get("snapshot_at") or "").strip()
    if not snapshot_at_text:
        return {"available": False, "payload": payload, "reason": "missing_timestamp"}
    try:
        snapshot_at = datetime.fromisoformat(snapshot_at_text)
    except ValueError:
        return {"available": False, "payload": payload, "reason": "bad_timestamp"}
    if snapshot_at.tzinfo is None:
        snapshot_at = snapshot_at.replace(tzinfo=datetime.now().astimezone().tzinfo)
    age = datetime.now(snapshot_at.tzinfo) - snapshot_at
    if age > timedelta(seconds=_snapshot_ttl_seconds()):
        return {"available": False, "payload": payload, "reason": "stale"}
    return {"available": True, "payload": payload, "reason": ""}


def load_config() -> Config:
    path = _config_path().resolve()
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        mtime_ns = None

    with _CONFIG_CACHE_LOCK:
        cached_path = _CONFIG_CACHE.get("path")
        cached_mtime = _CONFIG_CACHE.get("mtime_ns")
        cached_config = _CONFIG_CACHE.get("config")
        if cached_config is not None and cached_path == path and cached_mtime == mtime_ns:
            return cached_config

        config_obj = Config(str(path))
        _CONFIG_CACHE["path"] = path
        _CONFIG_CACHE["mtime_ns"] = mtime_ns
        _CONFIG_CACHE["config"] = config_obj
        return config_obj


def _supervisor_runtime_state() -> Dict[str, str]:
    state = get_runtime_state()
    supervisor = state.get("supervisor")
    if supervisor is None or not hasattr(supervisor, "get_state"):
        return {"state": "", "last_error": ""}
    try:
        payload = supervisor.get_state()
    except Exception:
        return {"state": "", "last_error": ""}
    return payload if isinstance(payload, dict) else {"state": "", "last_error": ""}


def _build_runtime_payload(app_config, snapshot_state: Dict[str, Any]) -> Dict[str, Any]:
    available = bool(snapshot_state.get("available"))
    payload = dict(snapshot_state.get("payload") or {})
    messages = payload.get("messages") or {}
    activity = payload.get("activity") or {}
    services = payload.get("services") or {}
    memory = payload.get("memory") or {}
    assistant = payload.get("assistant") or {}
    emoji_stats = _load_emoji_stats(app_config)

    connected = bool(payload.get("connected", False)) if available else False
    ready = bool(payload.get("ready", False)) if available else False
    vision_status = services.get("vision_status") or get_vision_service_status(app_config)
    memory_enabled = bool(services.get("memory_enabled", app_config.memory.enabled))
    supervisor_state = _supervisor_runtime_state()
    lifecycle_state = str(supervisor_state.get("state") or "").strip().lower()

    if lifecycle_state == "restarting":
        run_status = "\u91cd\u542f\u4e2d"
        online_label = "\u91cd\u542f\u4e2d"
    elif lifecycle_state == "starting":
        run_status = "\u542f\u52a8\u4e2d"
        online_label = "\u542f\u52a8\u4e2d"
    elif lifecycle_state == "error":
        run_status = "\u542f\u52a8\u5931\u8d25"
        online_label = "\u542f\u52a8\u5931\u8d25"
    elif ready and connected:
        run_status = "\u8fd0\u884c\u4e2d"
        online_label = "\u5df2\u8fde\u63a5"
    elif connected:
        run_status = "\u8fde\u63a5\u4e2d"
        online_label = "\u5df2\u8fde\u63a5"
    else:
        run_status = "\u672a\u8fde\u63a5"
        online_label = "\u672a\u8fde\u63a5"

    snapshot_label = _format_snapshot_label(str(payload.get("snapshot_at") or ""))

    return {
        "online": available and lifecycle_state not in {"restarting", "starting", "error"},
        "online_label": online_label,
        "assistant_name": str(assistant.get("name") or app_config.assistant_profile.name),
        "qq_connection": "\u5df2\u8fde\u63a5" if connected else "\u672a\u8fde\u63a5",
        "vision_status": _vision_status_label(vision_status),
        "memory_status": "\u5df2\u5f00\u542f" if memory_enabled else "\u672a\u5f00\u542f",
        "messages_received": _format_count(messages.get("messages_received"), available),
        "messages_replied": _format_count(messages.get("reply_parts_sent"), available),
        "message_errors": _format_count(messages.get("message_errors"), available),
        "run_status": run_status if available or lifecycle_state in {"restarting", "starting", "error"} else "\u672a\u8fde\u63a5",
        "connection_status": "\u6b63\u5e38" if connected else "\u65ad\u5f00",
        "uptime": _format_uptime(payload.get("uptime_seconds"), available),
        "active_conversations": _format_count(activity.get("active_conversations"), available),
        "active_tasks": _format_count(activity.get("active_message_tasks"), available),
        "group_repeat_echo": _format_optional_count((payload.get("planner") or {}).get("group_repeat_echo")),
        "emoji_total": _format_optional_count(emoji_stats.get("emoji_total")),
        "emoji_pending": _format_optional_count(emoji_stats.get("emoji_pending_classification")),
        "memory_reads": _format_optional_count(memory.get("memory_reads")),
        "memory_writes": _format_optional_count(memory.get("memory_writes")),
        "snapshot_label": snapshot_label,
    }


def _build_client_config() -> Dict[str, Any]:
    return {
        "refreshIntervalMs": 5000,
        "urls": {
            "dashboard": reverse("dashboard-data"),
            "runtimeRestart": reverse("runtime-restart"),
            "networkSave": reverse("save-network-settings"),
            "modelSave": reverse("save-model-settings"),
            "assistantSave": reverse("save-assistant-settings"),
            "emojiSave": reverse("save-emoji-settings"),
            "memorySave": reverse("save-memory-settings"),
            "avatarUpload": reverse("assistant-avatar-upload"),
            "avatarCurrent": reverse("assistant-avatar"),
            "memoryItems": reverse("memory-items"),
            "memoryUpdate": reverse("memory-item-update"),
            "memoryDelete": reverse("memory-item-delete"),
            "recall": reverse("recall-data"),
        },
    }


def _get_live_memory_manager():
    state = get_runtime_state()
    manager = state.get("memory_manager")
    loop = state.get("loop")
    if manager is None or loop is None or not loop.is_running():
        return None
    return manager



def _call_live_memory(factory, *, timeout: float = 8.0):
    manager = _get_live_memory_manager()
    if manager is None:
        return None
    return run_coro_threadsafe(factory(manager), timeout=timeout)



def _build_memory_stores(app_config):
    base_path = _resolve_storage_path(app_config.memory.storage_path)
    storage = MarkdownMemoryStore(
        base_path=str(base_path),
        ordinary_decay_enabled=app_config.memory.ordinary_decay_enabled,
        ordinary_half_life_days=app_config.memory.ordinary_half_life_days,
        ordinary_forget_threshold=app_config.memory.ordinary_forget_threshold,
    )
    important = ImportantMemoryStore(base_path=str(base_path / "important"))
    access_policy = MemoryAccessPolicy()
    return storage, important, access_policy



def _scope_dict(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    scope = (metadata or {}).get("applicability_scope")
    return scope if isinstance(scope, dict) else {}



def _memory_group(metadata: Optional[Dict[str, Any]]) -> str:
    prepared = dict(metadata or {})
    scope = _scope_dict(prepared)
    scope_kind = str(scope.get("kind") or "").strip().lower()
    if scope_kind in {"group", "group_member"}:
        return "group"
    if str(prepared.get("source_message_type") or "").strip().lower() == "group":
        return "group"
    if prepared.get("group_id") or prepared.get("source_group_id"):
        return "group"
    return "private"



def _sort_memory_items(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def sort_key(item: Dict[str, Any]):
        updated_at = str(item.get("updated_at") or item.get("created_at") or "")
        priority = _safe_int(item.get("priority"), 0)
        return (priority, updated_at)

    return sorted(items, key=sort_key, reverse=True)



def _serialize_memory_item(item: MemoryItem | ImportantMemoryItem, *, kind: str, access_policy: MemoryAccessPolicy) -> Dict[str, Any]:
    metadata = dict(getattr(item, "metadata", {}) or {})
    owner_user_id = str(getattr(item, "owner_user_id", "") or metadata.get("owner_user_id") or "")
    scope = _scope_dict(metadata)
    group_id = str(scope.get("group_id") or metadata.get("group_id") or metadata.get("source_group_id") or "")
    group = "important" if kind == "important" else _memory_group(metadata)
    updated_at = str(getattr(item, "updated_at", "") or getattr(item, "created_at", "") or "")
    return {
        "id": str(getattr(item, "id", "") or ""),
        "kind": kind,
        "group": group,
        "content": str(getattr(item, "content", "") or "").strip(),
        "owner_user_id": owner_user_id,
        "is_shared": bool(access_policy.is_shared(metadata)),
        "group_id": group_id,
        "updated_at": updated_at,
        "created_at": str(getattr(item, "created_at", "") or ""),
        "source": str(getattr(item, "source", "") or ""),
        "priority": _safe_int(getattr(item, "priority", 0), 0),
        "bm25_score": getattr(item, "bm25_score", None),
        "local_score": getattr(item, "local_score", None),
        "rerank_score": getattr(item, "rerank_score", None),
        "combined_score": getattr(item, "combined_score", None),
        "ranking_stage": getattr(item, "ranking_stage", None),
    }



def _group_memory_sections(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets = {"important": [], "group": [], "private": []}
    for item in items:
        buckets[item.get("group") or "private"].append(item)

    return [
        {"key": "important", "title": "\u91cd\u8981\u8bb0\u5fc6", "empty": "\u5f53\u524d\u8fd8\u6ca1\u6709\u91cd\u8981\u8bb0\u5fc6\u3002", "items": _sort_memory_items(buckets["important"])},
        {"key": "group", "title": "\u7fa4\u804a\u8bb0\u5fc6", "empty": "\u5f53\u524d\u8fd8\u6ca1\u6709\u7fa4\u804a\u8bb0\u5fc6\u3002", "items": _sort_memory_items(buckets["group"])},
        {"key": "private", "title": "\u79c1\u804a\u8bb0\u5fc6", "empty": "\u5f53\u524d\u8fd8\u6ca1\u6709\u79c1\u804a\u8bb0\u5fc6\u3002", "items": _sort_memory_items(buckets["private"])},
    ]



def _collect_memory_items(app_config) -> List[Dict[str, Any]]:
    def collect_from_manager(manager):
        async def _runner():
            records: List[Dict[str, Any]] = []
            access_policy = getattr(manager, "access_policy", MemoryAccessPolicy())
            for user_id in manager.important_memory_store.get_user_ids():
                memories = await manager.important_memory_store.get_memories(user_id, min_priority=1)
                records.extend(_serialize_memory_item(memory, kind="important", access_policy=access_policy) for memory in memories)
            for user_id in manager.storage.get_user_ids():
                memories = await manager.storage.get_user_memories(user_id)
                records.extend(_serialize_memory_item(memory, kind="ordinary", access_policy=access_policy) for memory in memories)
            global_memories = await manager.storage.get_global_memories()
            records.extend(_serialize_memory_item(memory, kind="ordinary", access_policy=access_policy) for memory in global_memories)
            return records

        return _runner()

    live_records = _call_live_memory(collect_from_manager)
    if live_records is not None:
        return live_records

    storage, important_store, access_policy = _build_memory_stores(app_config)

    async def _runner():
        records: List[Dict[str, Any]] = []
        for user_id in important_store.get_user_ids():
            memories = await important_store.get_memories(user_id, min_priority=1)
            records.extend(_serialize_memory_item(memory, kind="important", access_policy=access_policy) for memory in memories)
        for user_id in storage.get_user_ids():
            memories = await storage.get_user_memories(user_id)
            records.extend(_serialize_memory_item(memory, kind="ordinary", access_policy=access_policy) for memory in memories)
        global_memories = await storage.get_global_memories()
        records.extend(_serialize_memory_item(memory, kind="ordinary", access_policy=access_policy) for memory in global_memories)
        return records

    return asyncio.run(_runner())



def build_memory_items_payload() -> Dict[str, Any]:
    config_obj = load_config()
    items = _collect_memory_items(config_obj.app)
    return {"ok": True, "sections": _group_memory_sections(items)}



def _memory_owner_from_payload(payload: Dict[str, Any]) -> Optional[str]:
    owner_user_id = str(payload.get("owner_user_id") or "").strip()
    return owner_user_id or None



def update_memory_item(payload: Dict[str, Any]) -> Dict[str, Any]:
    memory_id = str(payload.get("id") or "").strip()
    kind = str(payload.get("kind") or "ordinary").strip().lower()
    content = str(payload.get("content") or "").strip()
    owner_user_id = _memory_owner_from_payload(payload)
    if not memory_id:
        raise ValueError("\u7f3a\u5c11\u8bb0\u5fc6 ID")
    if not content:
        raise ValueError("\u672a\u627e\u5230\u5bf9\u5e94\u7684\u8bb0\u5fc6")

    def update_live(manager):
        if kind == "important":
            if not owner_user_id:
                raise ValueError("\u91cd\u8981\u8bb0\u5fc6\u7f3a\u5c11\u6240\u5c5e\u7528\u6237")
            return manager.update_important_memory(owner_user_id, memory_id, content)
        return manager.update_memory(memory_id, content, owner_user_id)

    live_result = _call_live_memory(lambda manager: update_live(manager))
    if live_result is not None:
        if not live_result:
            raise ValueError("\u672a\u627e\u5230\u5bf9\u5e94\u7684\u8bb0\u5fc6")
        return {"ok": True, "message": "\u8bb0\u5fc6\u5df2\u66f4\u65b0"}

    config_obj = load_config()
    storage, important_store, _ = _build_memory_stores(config_obj.app)
    if kind == "important":
        if not owner_user_id:
            raise ValueError("\u91cd\u8981\u8bb0\u5fc6\u7f3a\u5c11\u6240\u5c5e\u7528\u6237")
        result = asyncio.run(important_store.update_memory(owner_user_id, memory_id, content))
    else:
        result = asyncio.run(storage.update_memory(memory_id, content, owner_user_id))
    if not result:
        raise ValueError("\u672a\u627e\u5230\u5bf9\u5e94\u7684\u8bb0\u5fc6")
    return {"ok": True, "message": "\u8bb0\u5fc6\u5df2\u66f4\u65b0"}



def delete_memory_item(payload: Dict[str, Any]) -> Dict[str, Any]:
    memory_id = str(payload.get("id") or "").strip()
    kind = str(payload.get("kind") or "ordinary").strip().lower()
    owner_user_id = _memory_owner_from_payload(payload)
    if not memory_id:
        raise ValueError("\u7f3a\u5c11\u8bb0\u5fc6 ID")

    def delete_live(manager):
        if kind == "important":
            if not owner_user_id:
                raise ValueError("\u91cd\u8981\u8bb0\u5fc6\u7f3a\u5c11\u6240\u5c5e\u7528\u6237")
            return manager.delete_important_memory_by_id(owner_user_id, memory_id)
        return manager.delete_memory(memory_id, owner_user_id)

    live_result = _call_live_memory(lambda manager: delete_live(manager))
    if live_result is not None:
        if not live_result:
            raise ValueError("\u672a\u627e\u5230\u5bf9\u5e94\u7684\u8bb0\u5fc6")
        return {"ok": True, "message": "\u8bb0\u5fc6\u5df2\u5220\u9664"}

    config_obj = load_config()
    storage, important_store, _ = _build_memory_stores(config_obj.app)
    if kind == "important":
        if not owner_user_id:
            raise ValueError("\u91cd\u8981\u8bb0\u5fc6\u7f3a\u5c11\u6240\u5c5e\u7528\u6237")
        result = asyncio.run(important_store.delete_memory_by_id(owner_user_id, memory_id))
    else:
        result = asyncio.run(storage.delete_memory(memory_id, owner_user_id))
    if not result:
        raise ValueError("\u672a\u627e\u5230\u5bf9\u5e94\u7684\u8bb0\u5fc6")
    return {"ok": True, "message": "\u8bb0\u5fc6\u5df2\u5220\u9664"}


def build_runtime_api_payload() -> Dict[str, Any]:
    config_obj = load_config()
    runtime = _build_runtime_payload(config_obj.app, load_runtime_snapshot())
    return {"ok": True, "runtime": runtime, "fields": {key: runtime.get(key, "") for key in SNAPSHOT_FIELDS}}


def restart_backend_runtime() -> Dict[str, Any]:
    state = get_runtime_state()
    supervisor = state.get("supervisor")
    if supervisor is None or not hasattr(supervisor, "restart_bot"):
        raise RuntimeError("\u5f53\u524d\u8fd8\u4e0d\u80fd\u4ece\u9875\u9762\u91cd\u542f\u52a9\u624b")

    result = run_coro_threadsafe(supervisor.restart_bot(), timeout=60.0)
    message = str((result or {}).get("message") or "\u52a9\u624b\u670d\u52a1\u5df2\u91cd\u542f")
    return {"ok": True, "message": message, "state": (result or {}).get("state", "running")}


def _model_status(api_base: Any, model: Any, api_key: Any = None, *, enabled_label: str = "\u5df2\u542f\u7528") -> str:
    del api_key
    if all(str(value or "").strip() for value in (api_base, model)):
        return enabled_label
    return "\u672a\u542f\u7528"


def _secret_hint(api_key: Any, *, empty_hint: str = "可留空，不发送 Authorization") -> str:
    if str(api_key or "").strip():
        return "密钥已保存"
    return empty_hint


def _memory_extraction_status(app_config: Any) -> str:
    if is_memory_extraction_configured(app_config):
        return "专用提取模型"
    if is_ai_service_configured(app_config):
        return "调用主模型提取"
    return "无法提取"


def _memory_extraction_description(app_config: Any) -> str:
    if is_memory_extraction_configured(app_config):
        return "整理新记忆时会优先使用专用提取模型，失败后回退主模型。"
    if is_ai_service_configured(app_config):
        return "当前未单独配置提取模型，将直接调用主模型提取记忆。"
    return "当前未配置专用提取模型，且主模型不可用，暂时无法提取记忆。"


def _memory_extraction_secret_hint(app_config: Any, api_key: Any) -> str:
    if str(api_key or "").strip():
        return "密钥已保存"
    if is_memory_extraction_configured(app_config):
        return "可留空，不发送 Authorization"
    if is_ai_service_configured(app_config):
        return "未单独配置时将直接调用主模型"
    return "未配置专用提取模型，且主模型不可用"


def build_dashboard_context() -> Dict[str, Any]:
    config_obj = load_config()
    app_config = config_obj.app
    snapshot_state = load_runtime_snapshot()
    runtime = _build_runtime_payload(app_config, snapshot_state)
    vision_status = get_vision_service_status(app_config)
    emoji_stats = _load_emoji_stats(app_config)
    group_client = {
        "api_base": app_config.group_reply_decision.api_base,
        "api_key": app_config.group_reply_decision.api_key,
        "model": app_config.group_reply_decision.model,
        "extra_params": app_config.group_reply_decision.extra_params,
        "extra_headers": app_config.group_reply_decision.extra_headers,
        "response_path": app_config.group_reply_decision.response_path,
    }
    vision_client = config_obj.get_vision_client_config()
    memory_rerank_client = config_obj.get_memory_rerank_client_config()
    memory_extraction_client = {
        "api_base": app_config.memory.extraction_api_base,
        "api_key": app_config.memory.extraction_api_key,
        "model": app_config.memory.extraction_model,
        "extra_params": app_config.memory.extraction_extra_params,
        "extra_headers": app_config.memory.extraction_extra_headers,
        "response_path": app_config.memory.extraction_response_path,
    }
    assistant_avatar_url = _avatar_url(getattr(app_config.assistant_profile, "avatar_path", ""))

    return {
        "page_title": "WebUI - 控制台",
        "active_page": "page-home",
        "navigation": [
            {"id": "page-home", "icon": "fa-house", "label": "鎬昏"},
            {"id": "page-status", "icon": "fa-chart-line", "label": "运行状态"},
            {"id": "page-network", "icon": "fa-network-wired", "label": "杩炴帴璁剧疆"},
            {"id": "page-model", "icon": "fa-cube", "label": "妯″瀷璁剧疆"},
            {"id": "page-reply", "icon": "fa-user-gear", "label": "鍔╂墜璁剧疆"},
            {"id": "page-emoji", "icon": "fa-face-smile", "label": "琛ㄦ儏绠＄悊"},
            {"id": "page-memory", "icon": "fa-database", "label": "璁板繂绠＄悊"},
            {"id": "page-recall", "icon": "fa-clock-rotate-left", "label": "浣犵殑鍥炲繂"},
        ],
        "theme_colors": [
            {"label": "鐜孩", "rgb": "255, 77, 143", "active": True},
            {"label": "澶╄摑", "rgb": "77, 148, 255", "active": False},
            {"label": "缈犵豢", "rgb": "16, 185, 129", "active": False},
            {"label": "紫罗兰", "rgb": "139, 92, 246", "active": False},
        ],
        "runtime": runtime,
        "runtime_offline": not runtime["online"],
        "assistant_avatar_url": assistant_avatar_url,
        "field_help": FIELD_HELP,
        "network_form": {
            "ws_url": app_config.napcat.ws_url,
            "http_url": app_config.napcat.http_url,
        },
        "model_forms": [
            {
                "slug": "ai_service",
                "title": "鍥炲妯″瀷",
                "description": "平时聊天时主要用它来回复。",
                "api_base": app_config.ai_service.api_base,
                "model": app_config.ai_service.model,
                "api_key": _mask_secret(app_config.ai_service.api_key),
                "temperature": _temperature_value(app_config.ai_service.extra_params),
                "param_rows": _mapping_to_editor_rows(_mapping_without_temperature(app_config.ai_service.extra_params)),
                "header_rows": _mapping_to_editor_rows(app_config.ai_service.extra_headers),
                "response_path": app_config.ai_service.response_path,
                "status_label": _model_status(app_config.ai_service.api_base, app_config.ai_service.model, app_config.ai_service.api_key),
                "secret_saved": bool(str(app_config.ai_service.api_key or "").strip()),
                "secret_hint": _secret_hint(app_config.ai_service.api_key),
            },
            {
                "slug": "group_reply_decision",
                "title": "缇よ亰鍒ゆ柇妯″瀷",
                "description": "它帮助助手判断群里要不要接话。",
                "api_base": group_client["api_base"],
                "model": group_client["model"],
                "api_key": _mask_secret(group_client["api_key"]),
                "temperature": _temperature_value(group_client["extra_params"]),
                "param_rows": _mapping_to_editor_rows(_mapping_without_temperature(group_client["extra_params"])),
                "header_rows": _mapping_to_editor_rows(group_client["extra_headers"]),
                "response_path": group_client["response_path"] or "",
                "status_label": _model_status(group_client["api_base"], group_client["model"], group_client["api_key"]),
                "secret_saved": bool(str(group_client["api_key"] or "").strip()),
                "secret_hint": _secret_hint(group_client["api_key"]),
            },
            {
                "slug": "vision_service",
                "title": "璇嗗浘妯″瀷",
                "description": "看图和识图时会用到它。",
                "api_base": vision_client["api_base"],
                "model": vision_client["model"],
                "api_key": _mask_secret(vision_client["api_key"]),
                "temperature": _temperature_value(vision_client["extra_params"]),
                "param_rows": _mapping_to_editor_rows(_mapping_without_temperature(vision_client["extra_params"])),
                "header_rows": _mapping_to_editor_rows(vision_client["extra_headers"]),
                "response_path": vision_client["response_path"] or "",
                "status_label": _vision_status_label(vision_status),
                "secret_saved": bool(str(vision_client["api_key"] or "").strip()),
                "secret_hint": _secret_hint(vision_client["api_key"]),
            },
            {
                "slug": "memory_rerank",
                "title": "重排模型",
                "description": "整理候选记忆顺序时会用到它。",
                "api_base": memory_rerank_client["api_base"],
                "model": memory_rerank_client["model"],
                "api_key": _mask_secret(memory_rerank_client["api_key"]),
                "temperature": _temperature_value(memory_rerank_client["extra_params"]),
                "param_rows": _mapping_to_editor_rows(_mapping_without_temperature(memory_rerank_client["extra_params"])),
                "header_rows": _mapping_to_editor_rows(memory_rerank_client["extra_headers"]),
                "response_path": memory_rerank_client["response_path"] or "",
                "status_label": _model_status(memory_rerank_client["api_base"], memory_rerank_client["model"], memory_rerank_client["api_key"]),
                "secret_saved": bool(str(memory_rerank_client["api_key"] or "").strip()),
                "secret_hint": _secret_hint(memory_rerank_client["api_key"]),
            },
            {
                "slug": "memory_extraction",
                "title": "璁板繂鎻愬彇妯″瀷",
                "description": _memory_extraction_description(app_config),
                "api_base": memory_extraction_client["api_base"],
                "model": memory_extraction_client["model"],
                "api_key": _mask_secret(memory_extraction_client["api_key"]),
                "temperature": _temperature_value(memory_extraction_client["extra_params"]),
                "param_rows": _mapping_to_editor_rows(_mapping_without_temperature(memory_extraction_client["extra_params"])),
                "header_rows": _mapping_to_editor_rows(memory_extraction_client["extra_headers"]),
                "response_path": memory_extraction_client["response_path"] or "",
                "status_label": _memory_extraction_status(app_config),
                "secret_saved": bool(str(memory_extraction_client["api_key"] or "").strip()),
                "secret_hint": _memory_extraction_secret_hint(app_config, memory_extraction_client["api_key"]),
            },
        ],
        "model_advanced_forms": [
            {
                "slug": "ai_service",
                "title": "鍥炲妯″瀷",
                "param_rows": _mapping_to_editor_rows(app_config.ai_service.extra_params),
                "header_rows": _mapping_to_editor_rows(app_config.ai_service.extra_headers),
                "response_path": app_config.ai_service.response_path,
            },
            {
                "slug": "group_reply_decision",
                "title": "缇よ亰鍒ゆ柇妯″瀷",
                "param_rows": _mapping_to_editor_rows(group_client["extra_params"]),
                "header_rows": _mapping_to_editor_rows(group_client["extra_headers"]),
                "response_path": group_client["response_path"] or "",
            },
            {
                "slug": "vision_service",
                "title": "璇嗗浘妯″瀷",
                "param_rows": _mapping_to_editor_rows(vision_client["extra_params"]),
                "header_rows": _mapping_to_editor_rows(vision_client["extra_headers"]),
                "response_path": vision_client["response_path"] or "",
            },
            {
                "slug": "memory_rerank",
                "title": "重排模型",
                "param_rows": _mapping_to_editor_rows(memory_rerank_client["extra_params"]),
                "header_rows": _mapping_to_editor_rows(memory_rerank_client["extra_headers"]),
                "response_path": memory_rerank_client["response_path"] or "",
            },
            {
                "slug": "memory_extraction",
                "title": "璁板繂鎻愬彇妯″瀷",
                "param_rows": _mapping_to_editor_rows(memory_extraction_client["extra_params"]),
                "header_rows": _mapping_to_editor_rows(memory_extraction_client["extra_headers"]),
                "response_path": memory_extraction_client["response_path"] or "",
            },
        ],
        "assistant_form": {
            "name": app_config.assistant_profile.name,
            "alias": app_config.assistant_profile.alias,
            "max_context_length": app_config.bot_behavior.max_context_length,
            "max_message_length": app_config.bot_behavior.max_message_length,
            "response_timeout": app_config.bot_behavior.response_timeout,
            "group_strategy": _group_strategy_from_config(app_config),
            "personality": app_config.personality.content,
            "dialogue_style": app_config.dialogue_style.content,
            "group_strategy_options": GROUP_STRATEGY_OPTIONS,
        },
        "assistant_advanced_form": {
            "rate_limit_interval": app_config.bot_behavior.rate_limit_interval,
            "log_full_prompt": bool(app_config.bot_behavior.log_full_prompt),
            "private_quote_reply_enabled": bool(app_config.bot_behavior.private_quote_reply_enabled),
            "plan_request_interval": app_config.group_reply.plan_request_interval,
            "plan_request_max_parallel": app_config.group_reply.plan_request_max_parallel,
            "plan_context_message_count": app_config.group_reply.plan_context_message_count,
            "at_user_when_proactive_reply": bool(app_config.group_reply.at_user_when_proactive_reply),
            "repeat_echo_enabled": bool(app_config.group_reply.repeat_echo_enabled),
            "repeat_echo_window_seconds": app_config.group_reply.repeat_echo_window_seconds,
            "repeat_echo_min_count": app_config.group_reply.repeat_echo_min_count,
            "repeat_echo_cooldown_seconds": app_config.group_reply.repeat_echo_cooldown_seconds,
            "behavior": app_config.behavior.content,
        },
        "emoji_form": {
            "enabled": bool(app_config.emoji.enabled),
            "capture_enabled": bool(app_config.emoji.capture_enabled),
            "classification_enabled": bool(app_config.emoji.classification_enabled),
            "reply_enabled": bool(app_config.emoji.reply_enabled),
        },
        "emoji_advanced_form": {
            "idle_seconds_before_classify": app_config.emoji.idle_seconds_before_classify,
            "classification_interval_seconds": app_config.emoji.classification_interval_seconds,
            "classification_windows": _windows_to_editor_rows(app_config.emoji.classification_windows),
            "emotion_labels": _labels_to_items(app_config.emoji.emotion_labels),
            "reply_cooldown_seconds": app_config.emoji.reply_cooldown_seconds,
            "storage_path": app_config.emoji.storage_path,
        },
        "memory_form": {
            "enabled": bool(app_config.memory.enabled),
            "read_scope": app_config.memory.read_scope,
            "auto_extract": bool(app_config.memory.auto_extract),
            "extract_every_n_turns": app_config.memory.extract_every_n_turns,
            "read_scope_options": MEMORY_SCOPE_OPTIONS,
        },
        "memory_advanced_form": {
            "bm25_top_k": app_config.memory.bm25_top_k,
            "rerank_top_k": app_config.memory.rerank_top_k,
            "pre_rerank_top_k": app_config.memory.pre_rerank_top_k,
            "dynamic_memory_limit": app_config.memory.dynamic_memory_limit,
            "dynamic_dedup_enabled": bool(app_config.memory.dynamic_dedup_enabled),
            "dynamic_dedup_similarity_threshold": app_config.memory.dynamic_dedup_similarity_threshold,
            "rerank_candidate_max_chars": app_config.memory.rerank_candidate_max_chars,
            "rerank_total_prompt_budget": app_config.memory.rerank_total_prompt_budget,
            "ordinary_decay_enabled": bool(app_config.memory.ordinary_decay_enabled),
            "ordinary_half_life_days": app_config.memory.ordinary_half_life_days,
            "ordinary_forget_threshold": app_config.memory.ordinary_forget_threshold,
            "storage_path": app_config.memory.storage_path,
            "local_bm25_weight": app_config.memory.local_bm25_weight,
            "local_importance_weight": app_config.memory.local_importance_weight,
            "local_mention_weight": app_config.memory.local_mention_weight,
            "local_recency_weight": app_config.memory.local_recency_weight,
            "local_scene_weight": app_config.memory.local_scene_weight,
        },
        "emoji_stats": {
            "total": str(emoji_stats.get("emoji_total", 0)),
            "pending": str(emoji_stats.get("emoji_pending_classification", 0)),
        },
        "memory_sections": build_memory_items_payload()["sections"],
        "client_config": _build_client_config(),
        "runtime_payload": build_runtime_api_payload(),
    }


def build_recall_payload() -> Dict[str, Any]:
    return {"ok": True, "items": []}


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _stringify_mapping_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _mapping_to_editor_rows(mapping: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    payload = dict(mapping or {})
    if not payload:
        return [{"key": "", "value": ""}]
    return [{"key": str(key), "value": _stringify_mapping_value(value)} for key, value in payload.items()]


def _temperature_value(mapping: Optional[Dict[str, Any]]) -> str:
    payload = dict(mapping or {})
    if "temperature" not in payload:
        return ""
    return _stringify_mapping_value(payload.get("temperature"))


def _mapping_without_temperature(mapping: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if mapping is None:
        return None
    payload = dict(mapping)
    payload.pop("temperature", None)
    return payload


def _parse_editor_scalar(value: Any) -> Any:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"[-+]?\d+", text):
        try:
            return int(text)
        except ValueError:
            return text
    if re.fullmatch(r"[-+]?(?:\d+\.\d+|\d+\.\d*|\.\d+)", text):
        try:
            return float(text)
        except ValueError:
            return text
    return text


def _rows_to_mapping(rows: Any, *, smart_values: bool, empty_as_none: bool = False) -> Optional[Dict[str, Any]]:
    mapping: Dict[str, Any] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        if not key:
            continue
        raw_value = row.get("value")
        value = _parse_editor_scalar(raw_value) if smart_values else str(raw_value or "").strip()
        mapping[key] = value
    if not mapping and empty_as_none:
        return None
    return mapping


def _rows_with_temperature(
    rows: Any,
    temperature_value: Any,
    *,
    empty_as_none: bool,
) -> Optional[Dict[str, Any]]:
    mapping = _rows_to_mapping(rows, smart_values=True, empty_as_none=False) or {}
    temperature_text = "" if temperature_value is None else str(temperature_value).strip()
    if temperature_text:
        mapping.update(
            _rows_to_mapping(
                [{"key": "temperature", "value": temperature_text}],
                smart_values=True,
                empty_as_none=False,
            )
            or {}
        )
    else:
        mapping.pop("temperature", None)
    if not mapping and empty_as_none:
        return None
    return mapping


def _windows_to_editor_rows(windows: Optional[List[str]]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for item in windows or []:
        text = str(item or "").strip()
        if not text or "-" not in text:
            continue
        start, end = text.split("-", 1)
        rows.append({"start": start.strip(), "end": end.strip()})
    return rows or [{"start": "", "end": ""}]


def _rows_to_windows(rows: Any) -> List[str]:
    windows: List[str] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        start = str(row.get("start") or "").strip()
        end = str(row.get("end") or "").strip()
        if not start and not end:
            continue
        windows.append(f"{start}-{end}")
    return windows


def _labels_to_items(labels: Optional[List[str]]) -> List[str]:
    return [str(label).strip() for label in labels or [] if str(label).strip()]


def _clean_string_list(values: Any) -> List[str]:
    return [str(value).strip() for value in values or [] if str(value).strip()]


def _preserve_secret(current: Any, submitted: Any) -> Any:
    text = str(submitted or "").strip()
    if not text:
        return current
    if text in {MASKED_SECRET, _mask_secret(current)}:
        return current
    return text


def _write_validated_config(raw_data: Dict[str, Any]) -> None:
    normalized_raw = prune_none_values(dict(raw_data))
    config_path = _config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".toml") as temp_file:
        temp_file.write(dumps_toml_document(normalized_raw))
        temp_path = Path(temp_file.name)
    try:
        Config(str(temp_path)).validate()
        target_doc = parse_toml_document(config_path)
        sync_toml_container(target_doc, normalized_raw)
        target_tmp = config_path.with_suffix(".tmp")
        target_tmp.write_text(dumps(target_doc), encoding="utf-8")
        target_tmp.replace(config_path)
        _clear_config_cache()
    finally:
        if temp_path.exists():
            temp_path.unlink()


def save_network_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    _, raw = _load_config_document()
    napcat = dict(raw.get("napcat") or {})
    napcat["ws_url"] = str(payload.get("ws_url") or "").strip()
    napcat["http_url"] = str(payload.get("http_url") or "").strip()
    raw["napcat"] = napcat
    _write_validated_config(raw)
    return {"ok": True, "message": "\u5df2\u7ecf\u8bb0\u597d\u4e86\uff0c\u91cd\u542f\u52a9\u624b\u540e\u751f\u6548"}


def save_model_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    _, raw = _load_config_document()

    ai_service = dict(raw.get("ai_service") or {})
    ai_payload = dict(payload.get("ai_service") or {})
    ai_service["api_base"] = str(ai_payload.get("api_base") or ai_service.get("api_base") or "").strip()
    ai_service["model"] = str(ai_payload.get("model") or ai_service.get("model") or "").strip()
    ai_service["api_key"] = _preserve_secret(ai_service.get("api_key", ""), ai_payload.get("api_key"))
    ai_service["extra_params"] = _rows_with_temperature(
        ai_payload.get("extra_params_rows"),
        ai_payload.get("temperature"),
        empty_as_none=False,
    ) or {}
    ai_service["extra_headers"] = _rows_to_mapping(ai_payload.get("extra_headers_rows"), smart_values=False, empty_as_none=False) or {}
    ai_response_path = str(ai_payload.get("response_path") or "").strip()
    ai_service["response_path"] = ai_response_path or ai_service.get("response_path") or "choices.0.message.content"
    raw["ai_service"] = ai_service

    decision = dict(raw.get("group_reply_decision") or {})
    decision_payload = dict(payload.get("group_reply_decision") or {})
    decision["api_base"] = _normalize_nullable_string(decision_payload.get("api_base") or decision.get("api_base"))
    decision["model"] = _normalize_nullable_string(decision_payload.get("model") or decision.get("model"))
    decision["api_key"] = _preserve_secret(decision.get("api_key", ""), decision_payload.get("api_key")) or None
    decision["extra_params"] = _rows_with_temperature(
        decision_payload.get("extra_params_rows"),
        decision_payload.get("temperature"),
        empty_as_none=True,
    )
    decision["extra_headers"] = _rows_to_mapping(decision_payload.get("extra_headers_rows"), smart_values=False, empty_as_none=True)
    decision["response_path"] = _normalize_nullable_string(decision_payload.get("response_path"))
    raw["group_reply_decision"] = decision

    vision = dict(raw.get("vision_service") or {})
    vision_payload = dict(payload.get("vision_service") or {})
    vision_api_base = _normalize_nullable_string(vision_payload.get("api_base") or vision.get("api_base"))
    vision_model = _normalize_nullable_string(vision_payload.get("model") or vision.get("model"))
    vision_api_key = _preserve_secret(vision.get("api_key", ""), vision_payload.get("api_key")) or None
    vision["api_base"] = vision_api_base
    vision["model"] = vision_model
    vision["api_key"] = vision_api_key
    vision["extra_params"] = _rows_with_temperature(
        vision_payload.get("extra_params_rows"),
        vision_payload.get("temperature"),
        empty_as_none=True,
    )
    vision["extra_headers"] = _rows_to_mapping(vision_payload.get("extra_headers_rows"), smart_values=False, empty_as_none=True)
    vision["response_path"] = _normalize_nullable_string(vision_payload.get("response_path"))
    vision["enabled"] = bool(vision_api_base and vision_model)
    raw["vision_service"] = vision

    memory_rerank = dict(raw.get("memory_rerank") or {})
    rerank_payload = dict(payload.get("memory_rerank") or {})
    memory_rerank["api_base"] = _normalize_nullable_string(rerank_payload.get("api_base") or memory_rerank.get("api_base"))
    memory_rerank["model"] = _normalize_nullable_string(rerank_payload.get("model") or memory_rerank.get("model"))
    memory_rerank["api_key"] = _preserve_secret(memory_rerank.get("api_key", ""), rerank_payload.get("api_key")) or None
    memory_rerank["extra_params"] = _rows_with_temperature(
        rerank_payload.get("extra_params_rows"),
        rerank_payload.get("temperature"),
        empty_as_none=True,
    )
    memory_rerank["extra_headers"] = _rows_to_mapping(rerank_payload.get("extra_headers_rows"), smart_values=False, empty_as_none=True)
    memory_rerank["response_path"] = _normalize_nullable_string(rerank_payload.get("response_path"))
    raw["memory_rerank"] = memory_rerank

    memory = dict(raw.get("memory") or {})
    extraction_payload = dict(payload.get("memory_extraction") or {})
    memory["extraction_api_base"] = _normalize_nullable_string(extraction_payload.get("api_base") or memory.get("extraction_api_base"))
    memory["extraction_model"] = _normalize_nullable_string(extraction_payload.get("model") or memory.get("extraction_model"))
    memory["extraction_api_key"] = _preserve_secret(memory.get("extraction_api_key", ""), extraction_payload.get("api_key")) or None
    memory["extraction_extra_params"] = _rows_with_temperature(
        extraction_payload.get("extra_params_rows"),
        extraction_payload.get("temperature"),
        empty_as_none=True,
    )
    memory["extraction_extra_headers"] = _rows_to_mapping(extraction_payload.get("extra_headers_rows"), smart_values=False, empty_as_none=True)
    memory["extraction_response_path"] = _normalize_nullable_string(extraction_payload.get("response_path"))
    raw["memory"] = memory

    _drop_empty_optional_model_sections(raw)

    _write_validated_config(raw)
    return {"ok": True, "message": "已经记好了，重启助手后生效"}


def save_assistant_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    _, raw = _load_config_document()

    assistant_profile = dict(raw.get("assistant_profile") or {})
    assistant_profile["name"] = str(payload.get("name") or assistant_profile.get("name") or "").strip()
    assistant_profile["alias"] = str(payload.get("alias") or assistant_profile.get("alias") or "").strip()
    raw["assistant_profile"] = assistant_profile

    bot_behavior = dict(raw.get("bot_behavior") or {})
    bot_behavior["max_context_length"] = _coerce_int(payload.get("max_context_length"), default=_safe_int(bot_behavior.get("max_context_length"), 10))
    bot_behavior["max_message_length"] = _coerce_int(payload.get("max_message_length"), default=_safe_int(bot_behavior.get("max_message_length"), 4000))
    bot_behavior["response_timeout"] = _coerce_int(payload.get("response_timeout"), default=_safe_int(bot_behavior.get("response_timeout"), 60))
    bot_behavior["rate_limit_interval"] = _coerce_float(payload.get("rate_limit_interval"), default=float(bot_behavior.get("rate_limit_interval", 1.0) or 1.0))
    bot_behavior["log_full_prompt"] = _coerce_bool(payload.get("log_full_prompt"), default=bool(bot_behavior.get("log_full_prompt", False)))
    bot_behavior["private_quote_reply_enabled"] = _coerce_bool(payload.get("private_quote_reply_enabled"), default=bool(bot_behavior.get("private_quote_reply_enabled", False)))
    raw["bot_behavior"] = bot_behavior

    group_reply = dict(raw.get("group_reply") or {})
    group_reply.update(_parse_group_strategy(payload.get("group_strategy")))
    group_reply["plan_request_interval"] = _coerce_float(payload.get("plan_request_interval"), default=float(group_reply.get("plan_request_interval", 3.0) or 3.0))
    group_reply["plan_request_max_parallel"] = _coerce_int(payload.get("plan_request_max_parallel"), default=_safe_int(group_reply.get("plan_request_max_parallel"), 1))
    group_reply["plan_context_message_count"] = _coerce_int(payload.get("plan_context_message_count"), default=_safe_int(group_reply.get("plan_context_message_count"), 5))
    group_reply["at_user_when_proactive_reply"] = _coerce_bool(payload.get("at_user_when_proactive_reply"), default=bool(group_reply.get("at_user_when_proactive_reply", False)))
    group_reply.pop("burst_merge_enabled", None)
    group_reply.pop("burst_window_seconds", None)
    group_reply.pop("burst_min_messages", None)
    group_reply.pop("burst_max_messages", None)
    group_reply["repeat_echo_enabled"] = _coerce_bool(payload.get("repeat_echo_enabled"), default=bool(group_reply.get("repeat_echo_enabled", False)))
    group_reply["repeat_echo_window_seconds"] = _coerce_float(payload.get("repeat_echo_window_seconds"), default=float(group_reply.get("repeat_echo_window_seconds", 20.0) or 20.0))
    group_reply["repeat_echo_min_count"] = _coerce_int(payload.get("repeat_echo_min_count"), default=_safe_int(group_reply.get("repeat_echo_min_count"), 2))
    group_reply["repeat_echo_cooldown_seconds"] = _coerce_float(payload.get("repeat_echo_cooldown_seconds"), default=float(group_reply.get("repeat_echo_cooldown_seconds", 90.0) or 90.0))
    raw["group_reply"] = group_reply

    personality = dict(raw.get("personality") or {})
    personality["content"] = str(payload.get("personality") or personality.get("content") or "")
    raw["personality"] = personality

    dialogue_style = dict(raw.get("dialogue_style") or {})
    dialogue_style["content"] = str(payload.get("dialogue_style") or dialogue_style.get("content") or "")
    raw["dialogue_style"] = dialogue_style

    behavior = dict(raw.get("behavior") or {})
    behavior["content"] = str(payload.get("behavior") or behavior.get("content") or "")
    raw["behavior"] = behavior

    _write_validated_config(raw)
    return {"ok": True, "message": "已经记好了，重启助手后生效"}


def save_emoji_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    _, raw = _load_config_document()
    emoji = dict(raw.get("emoji") or {})
    emoji["enabled"] = _coerce_bool(payload.get("enabled"), default=bool(emoji.get("enabled", True)))
    emoji["capture_enabled"] = _coerce_bool(payload.get("capture_enabled"), default=bool(emoji.get("capture_enabled", True)))
    emoji["classification_enabled"] = _coerce_bool(payload.get("classification_enabled"), default=bool(emoji.get("classification_enabled", True)))
    emoji["reply_enabled"] = _coerce_bool(payload.get("reply_enabled"), default=bool(emoji.get("reply_enabled", False)))
    emoji["idle_seconds_before_classify"] = _coerce_float(payload.get("idle_seconds_before_classify"), default=float(emoji.get("idle_seconds_before_classify", 45.0) or 45.0))
    emoji["classification_interval_seconds"] = _coerce_float(payload.get("classification_interval_seconds"), default=float(emoji.get("classification_interval_seconds", 30.0) or 30.0))
    emoji["classification_windows"] = _rows_to_windows(payload.get("classification_windows"))
    emoji["emotion_labels"] = _clean_string_list(payload.get("emotion_labels"))
    emoji["reply_cooldown_seconds"] = _coerce_float(payload.get("reply_cooldown_seconds"), default=float(emoji.get("reply_cooldown_seconds", 180.0) or 180.0))
    emoji["storage_path"] = str(payload.get("storage_path") or emoji.get("storage_path") or "data/emojis").strip()
    raw["emoji"] = emoji
    _write_validated_config(raw)
    return {"ok": True, "message": "已经记好了，重启助手后生效"}


def save_memory_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    _, raw = _load_config_document()
    memory = dict(raw.get("memory") or {})
    memory["enabled"] = _coerce_bool(payload.get("enabled"), default=bool(memory.get("enabled", False)))
    memory["read_scope"] = str(payload.get("read_scope") or memory.get("read_scope") or "user").strip()
    memory["auto_extract"] = _coerce_bool(payload.get("auto_extract"), default=bool(memory.get("auto_extract", True)))
    memory["bm25_top_k"] = _coerce_int(payload.get("bm25_top_k"), default=_safe_int(memory.get("bm25_top_k"), 100))
    memory["rerank_top_k"] = _coerce_int(payload.get("rerank_top_k"), default=_safe_int(memory.get("rerank_top_k"), 20))
    memory["extract_every_n_turns"] = _coerce_int(payload.get("extract_every_n_turns"), default=_safe_int(memory.get("extract_every_n_turns"), 3))
    memory["pre_rerank_top_k"] = _coerce_int(payload.get("pre_rerank_top_k"), default=_safe_int(memory.get("pre_rerank_top_k"), 12))
    memory["dynamic_memory_limit"] = _coerce_int(payload.get("dynamic_memory_limit"), default=_safe_int(memory.get("dynamic_memory_limit"), 8))
    memory["dynamic_dedup_enabled"] = _coerce_bool(payload.get("dynamic_dedup_enabled"), default=bool(memory.get("dynamic_dedup_enabled", True)))
    memory["dynamic_dedup_similarity_threshold"] = _coerce_float(payload.get("dynamic_dedup_similarity_threshold"), default=float(memory.get("dynamic_dedup_similarity_threshold", 0.72) or 0.72))
    memory["rerank_candidate_max_chars"] = _coerce_int(payload.get("rerank_candidate_max_chars"), default=_safe_int(memory.get("rerank_candidate_max_chars"), 160))
    memory["rerank_total_prompt_budget"] = _coerce_int(payload.get("rerank_total_prompt_budget"), default=_safe_int(memory.get("rerank_total_prompt_budget"), 2400))
    memory.pop("conversation_save_interval", None)
    memory["ordinary_decay_enabled"] = _coerce_bool(payload.get("ordinary_decay_enabled"), default=bool(memory.get("ordinary_decay_enabled", True)))
    memory["ordinary_half_life_days"] = _coerce_float(payload.get("ordinary_half_life_days"), default=float(memory.get("ordinary_half_life_days", 30.0) or 30.0))
    memory["ordinary_forget_threshold"] = _coerce_float(payload.get("ordinary_forget_threshold"), default=float(memory.get("ordinary_forget_threshold", 0.5) or 0.5))
    memory["storage_path"] = str(payload.get("storage_path") or memory.get("storage_path") or "memories").strip()
    memory["local_bm25_weight"] = _coerce_float(payload.get("local_bm25_weight"), default=float(memory.get("local_bm25_weight", 1.0) or 1.0))
    memory["local_importance_weight"] = _coerce_float(payload.get("local_importance_weight"), default=float(memory.get("local_importance_weight", 0.35) or 0.35))
    memory["local_mention_weight"] = _coerce_float(payload.get("local_mention_weight"), default=float(memory.get("local_mention_weight", 0.2) or 0.2))
    memory["local_recency_weight"] = _coerce_float(payload.get("local_recency_weight"), default=float(memory.get("local_recency_weight", 0.15) or 0.15))
    memory["local_scene_weight"] = _coerce_float(payload.get("local_scene_weight"), default=float(memory.get("local_scene_weight", 0.3) or 0.3))
    raw["memory"] = memory
    _write_validated_config(raw)
    return {"ok": True, "message": "已经记好了，重启助手后生效"}


def save_assistant_avatar(uploaded_file) -> Dict[str, Any]:
    if uploaded_file is None:
        raise ValueError("\u8bf7\u9009\u62e9\u8981\u4e0a\u4f20\u7684\u5934\u50cf\u56fe\u7247")

    suffix = Path(str(getattr(uploaded_file, "name", "") or "")).suffix.lower()
    content_type = str(getattr(uploaded_file, "content_type", "") or "").lower()
    if suffix not in ALLOWED_AVATAR_EXTENSIONS or content_type not in ALLOWED_AVATAR_CONTENT_TYPES:
        raise ValueError("\u53ea\u652f\u6301 PNG\u3001JPG\u3001JPEG\u3001WEBP\u3001GIF \u683c\u5f0f\u7684\u56fe\u7247")

    max_bytes = int(getattr(settings, "WEBUI_AVATAR_MAX_BYTES", 3 * 1024 * 1024))
    size = int(getattr(uploaded_file, "size", 0) or 0)
    if size <= 0:
        raise ValueError("\u4e0a\u4f20\u7684\u5934\u50cf\u6587\u4ef6\u65e0\u6548")
    if size > max_bytes:
        raise ValueError("\u5934\u50cf\u56fe\u7247\u4e0d\u80fd\u8d85\u8fc7 3MB")

    avatar_root = _avatar_root()
    avatar_root.mkdir(parents=True, exist_ok=True)
    for existing in avatar_root.glob("assistant.*"):
        existing.unlink(missing_ok=True)

    file_path = avatar_root / f"assistant{suffix}"
    with file_path.open("wb") as handle:
        for chunk in uploaded_file.chunks():
            handle.write(chunk)

    relative_path = Path("data") / "webui" / "avatar" / file_path.name

    _, raw = _load_config_document()
    assistant_profile = dict(raw.get("assistant_profile") or {})
    assistant_profile["avatar_path"] = relative_path.as_posix()
    raw["assistant_profile"] = assistant_profile
    _write_validated_config(raw)

    return {
        "ok": True,
        "message": "\u5934\u50cf\u5df2\u7ecf\u6362\u597d\u4e86",
        "avatar_url": _avatar_url(relative_path.as_posix()),
    }


def get_assistant_avatar_file() -> Dict[str, Any]:
    config_obj = load_config()
    avatar_path = getattr(config_obj.app.assistant_profile, "avatar_path", "")
    resolved = _avatar_absolute_path(avatar_path)
    if not resolved or not resolved.exists() or not resolved.is_file():
        return {"exists": False}
    content_type, _ = mimetypes.guess_type(str(resolved))
    return {
        "exists": True,
        "path": resolved,
        "content_type": content_type or "application/octet-stream",
    }


def handle_save_error(exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, ConfigValidationError):
        return {"ok": False, "message": "\u8bbe\u7f6e\u6709\u95ee\u9898\uff0c\u8bf7\u68c0\u67e5\u540e\u518d\u8bd5", "errors": list(exc.errors)}
    return {"ok": False, "message": str(exc) or "\u4fdd\u5b58\u5931\u8d25", "errors": []}


