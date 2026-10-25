import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .auth import check_secret
from .config_views import _read_config, show_state, update_show


@csrf_exempt  # authenticated by a header secret, not by cookies
@require_http_methods(["GET", "POST"])
def target_time(request):
    """The countdown's start time (config countdown_hour / countdown_minute)."""
    denied = check_secret(request)
    if denied is not None:
        return denied

    if request.method == "POST":
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (TypeError, ValueError):
            return JsonResponse({"detail": "Request body must be valid JSON."}, status=400)

        hours = payload.get("hours")
        minutes = payload.get("minutes")
        if hours is None or minutes is None:
            return JsonResponse({"detail": "Both 'hours' and 'minutes' are required."}, status=400)

        if not isinstance(hours, int) or not isinstance(minutes, int):
            return JsonResponse({"detail": "'hours' and 'minutes' must be integers."}, status=400)

        if not 0 <= hours <= 23 or not 0 <= minutes <= 59:
            return JsonResponse({"detail": "'hours' must be 0-23 and 'minutes' must be 0-59."}, status=400)

        response = update_show({"countdown_hour": hours, "countdown_minute": minutes})
        if response.status_code != 200:
            return response

    try:
        show = show_state(_read_config())
    except (OSError, ValueError) as exc:
        return JsonResponse({"detail": f"Could not read the config: {exc}"}, status=500)
    return JsonResponse({"hours": show["countdown_hour"], "minutes": show["countdown_minute"]})


@csrf_exempt
@require_http_methods(["POST"])
def push_json(request):
    denied = check_secret(request)
    if denied is not None:
        return denied

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (TypeError, ValueError):
        return JsonResponse({"detail": "Request body must be valid JSON."}, status=400)

    return JsonResponse({"ok": True, "payload": payload})
