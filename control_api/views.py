import json

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

TARGET_TIME = {"hours": 0, "minutes": 0}


def _require_instance_key(request):
    provided = request.headers.get("X-Instance-Key") or request.headers.get("X-API-Key")
    if provided != settings.INSTANCE_API_KEY:
        return False
    return True


@require_http_methods(["GET", "POST"])
def target_time(request):
    if not _require_instance_key(request):
        return JsonResponse({"detail": "Missing or invalid instance key."}, status=401)

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

        TARGET_TIME["hours"] = hours
        TARGET_TIME["minutes"] = minutes

    return JsonResponse({"hours": TARGET_TIME["hours"], "minutes": TARGET_TIME["minutes"]})


@require_http_methods(["POST"])
def push_json(request):
    if not _require_instance_key(request):
        return JsonResponse({"detail": "Missing or invalid instance key."}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (TypeError, ValueError):
        return JsonResponse({"detail": "Request body must be valid JSON."}, status=400)

    return JsonResponse({"ok": True, "payload": payload})
