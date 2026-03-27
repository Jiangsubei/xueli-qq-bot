from django.urls import path

from .views import (
    assistant_avatar,
    dashboard,
    dashboard_data,
    delete_memory,
    memory_items,
    recall_data,
    restart_runtime,
    save_assistant,
    save_emoji,
    save_memory,
    save_models,
    save_network,
    update_memory,
    upload_avatar,
)

urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("api/dashboard/", dashboard_data, name="dashboard-data"),
    path("api/runtime/restart/", restart_runtime, name="runtime-restart"),
    path("api/recall/", recall_data, name="recall-data"),
    path("api/memory/items/", memory_items, name="memory-items"),
    path("api/memory/items/update/", update_memory, name="memory-item-update"),
    path("api/memory/items/delete/", delete_memory, name="memory-item-delete"),
    path("api/config/network/", save_network, name="save-network-settings"),
    path("api/config/models/", save_models, name="save-model-settings"),
    path("api/config/assistant/", save_assistant, name="save-assistant-settings"),
    path("api/config/emoji/", save_emoji, name="save-emoji-settings"),
    path("api/config/memory/", save_memory, name="save-memory-settings"),
    path("api/avatar/", upload_avatar, name="assistant-avatar-upload"),
    path("media/avatar/current/", assistant_avatar, name="assistant-avatar"),
]
