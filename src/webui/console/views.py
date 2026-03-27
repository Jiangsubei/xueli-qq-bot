import json

from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .context import build_dashboard_context
from .services import (
    build_memory_items_payload,
    build_recall_payload,
    build_runtime_api_payload,
    delete_memory_item,
    get_assistant_avatar_file,
    handle_save_error,
    restart_backend_runtime,
    save_assistant_avatar,
    save_assistant_settings,
    save_emoji_settings,
    save_memory_settings,
    save_model_settings,
    save_network_settings,
    update_memory_item,
)


def _parse_json_body(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("请求内容不是有效的 JSON") from exc


@ensure_csrf_cookie
@require_GET
def dashboard(request):
    return render(request, "console/dashboard.html", build_dashboard_context())


@require_GET
def dashboard_data(request):
    return JsonResponse(build_runtime_api_payload())


@require_POST
def restart_runtime(request):
    try:
        return JsonResponse(restart_backend_runtime())
    except Exception as exc:
        return JsonResponse(handle_save_error(exc), status=400)


@require_GET
def recall_data(request):
    return JsonResponse(build_recall_payload())


@require_GET
def memory_items(request):
    return JsonResponse(build_memory_items_payload())


@require_POST
def update_memory(request):
    try:
        return JsonResponse(update_memory_item(_parse_json_body(request)))
    except Exception as exc:
        return JsonResponse(handle_save_error(exc), status=400)


@require_POST
def delete_memory(request):
    try:
        return JsonResponse(delete_memory_item(_parse_json_body(request)))
    except Exception as exc:
        return JsonResponse(handle_save_error(exc), status=400)


@require_POST
def save_network(request):
    try:
        return JsonResponse(save_network_settings(_parse_json_body(request)))
    except Exception as exc:
        return JsonResponse(handle_save_error(exc), status=400)


@require_POST
def save_models(request):
    try:
        return JsonResponse(save_model_settings(_parse_json_body(request)))
    except Exception as exc:
        return JsonResponse(handle_save_error(exc), status=400)


@require_POST
def save_assistant(request):
    try:
        return JsonResponse(save_assistant_settings(_parse_json_body(request)))
    except Exception as exc:
        return JsonResponse(handle_save_error(exc), status=400)


@require_POST
def save_emoji(request):
    try:
        return JsonResponse(save_emoji_settings(_parse_json_body(request)))
    except Exception as exc:
        return JsonResponse(handle_save_error(exc), status=400)


@require_POST
def save_memory(request):
    try:
        return JsonResponse(save_memory_settings(_parse_json_body(request)))
    except Exception as exc:
        return JsonResponse(handle_save_error(exc), status=400)


@require_POST
def upload_avatar(request):
    try:
        return JsonResponse(save_assistant_avatar(request.FILES.get("avatar")))
    except Exception as exc:
        return JsonResponse(handle_save_error(exc), status=400)


@require_GET
def assistant_avatar(request):
    payload = get_assistant_avatar_file()
    if not payload.get("exists"):
        return HttpResponse(status=204)
    return FileResponse(payload["path"].open("rb"), content_type=payload["content_type"])
