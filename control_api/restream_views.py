"""Stream title on Restream: connect the account once, then set the title on every channel."""

import json

from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.utils.html import escape
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import restream
from .auth import check_secret
from .config_views import _read_config

MAX_TITLE_LENGTH = 140  # what the strictest platforms (e.g. Twitch) accept
MAX_DESCRIPTION_LENGTH = 5000


def _credentials():
    """(client id, client secret) from the config, or None when not set up."""
    try:
        config = _read_config()
    except (OSError, ValueError):
        return None
    client_id, client_secret = config.get("restream_client_id"), config.get("restream_client_secret")
    return (client_id, client_secret) if client_id and client_secret else None


def _redirect_uri(request) -> str:
    return settings.RESTREAM_REDIRECT_URI or request.build_absolute_uri("/restream/callback/")


def _status(request, credentials) -> dict:
    return {
        "configured": credentials is not None,
        "connected": credentials is not None and restream.is_connected(),
        "redirect_uri": _redirect_uri(request),
        "last_title": restream.last_title(),
    }


@csrf_exempt  # authenticated by a header secret, not by cookies
@never_cache
@require_http_methods(["GET", "POST", "DELETE"])
def restream_api(request):
    """GET: connection status and channels with their titles.
    POST metadata fields with optional channels, or {"channel_id": id, "active": bool}.
    DELETE: forget the Restream connection."""
    denied = check_secret(request)
    if denied is not None:
        return denied
    credentials = _credentials()

    if request.method == "DELETE":
        restream.disconnect()
        return JsonResponse(_status(request, credentials))

    if request.method == "GET":
        status = _status(request, credentials)
        status["channels"] = []
        if status["connected"]:
            try:
                status["channels"] = restream.channels_with_titles(credentials)
            except restream.RestreamError as exc:
                status["error"] = str(exc)
        return JsonResponse(status)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError):
        return JsonResponse({"detail": "Request body must be valid JSON."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"detail": "Expected {\"title\": \"...\"}."}, status=400)
    if "active" in payload or "channel_id" in payload:
        channel_id, active = payload.get("channel_id"), payload.get("active")
        if not isinstance(channel_id, int) or isinstance(channel_id, bool):
            return JsonResponse({"detail": "'channel_id' must be a channel id."}, status=400)
        if not isinstance(active, bool):
            return JsonResponse({"detail": "'active' must be true or false."}, status=400)
        if credentials is None:
            return JsonResponse({"detail": "Set the Restream client ID and secret in the stream settings first."},
                                status=409)
        try:
            restream.set_channel_active(credentials, channel_id, active)
        except restream.RestreamError as exc:
            return JsonResponse({"detail": str(exc)}, status=502)
        return JsonResponse({"ok": True, "channel_id": channel_id, "active": active})

    metadata = {}
    title = payload.get("title")
    if "title" in payload:
        if not isinstance(title, str) or not title.strip():
            return JsonResponse({"detail": "The title must be a non-empty string.",
                                 "errors": {"title": "is required"}}, status=400)
        title = " ".join(title.split())
        if len(title) > MAX_TITLE_LENGTH:
            return JsonResponse({"detail": f"The title must be at most {MAX_TITLE_LENGTH} characters.",
                                 "errors": {"title": f"must be at most {MAX_TITLE_LENGTH} characters"}}, status=400)
        metadata["title"] = title
    description = payload.get("description")
    if "description" in payload:
        if not isinstance(description, str):
            return JsonResponse({"detail": "The description must be a string.",
                                 "errors": {"description": "must be a string"}}, status=400)
        description = description.strip()
        if len(description) > MAX_DESCRIPTION_LENGTH:
            return JsonResponse({"detail": f"The description must be at most {MAX_DESCRIPTION_LENGTH} characters.",
                                 "errors": {"description": f"must be at most {MAX_DESCRIPTION_LENGTH} characters"}},
                                status=400)
        metadata["description"] = description
    if not metadata:
        return JsonResponse({"detail": "Provide a title or description to update."}, status=400)
    channel_ids = payload.get("channels")
    if channel_ids is not None and (
        not isinstance(channel_ids, list) or not all(isinstance(item, int) and not isinstance(item, bool) for item in channel_ids)
    ):
        return JsonResponse({"detail": "'channels' must be a list of channel ids."}, status=400)
    if credentials is None:
        return JsonResponse({"detail": "Set the Restream client ID and secret in the stream settings first."}, status=409)
    try:
        results = restream.set_channel_metadata(credentials, metadata, channel_ids)
    except restream.RestreamError as exc:
        return JsonResponse({"detail": str(exc)}, status=502)
    updated = [result for result in results if result["ok"]]
    skipped = [result for result in results if result.get("unsupported")]
    failed = [result for result in results if not result["ok"] and not result.get("unsupported")]
    if not updated and not failed:
        message = "No Restream channel supports this metadata." if skipped else "No Restream channel to update."
    else:
        message = f"Metadata set on {len(updated)} of {len(updated) + len(failed)} channel(s)."
    if skipped:
        message += f" Skipped (metadata unavailable): {', '.join(result['name'] for result in skipped)}."
    response = {"ok": bool(updated) and not failed, "results": results, "message": message}
    if "title" in metadata:
        response["title"] = metadata["title"]
    if "description" in metadata:
        response["description"] = metadata["description"]
    return JsonResponse(response)


@csrf_exempt  # authenticated by a header secret, not by cookies
@never_cache
@require_http_methods(["POST"])
def restream_connect(request):
    """Start connecting Restream: returns the URL to open in the browser."""
    denied = check_secret(request)
    if denied is not None:
        return denied
    credentials = _credentials()
    if credentials is None:
        return JsonResponse({"detail": "Set the Restream client ID and secret in the stream settings first."}, status=409)
    return JsonResponse({"url": restream.authorize_url(credentials, _redirect_uri(request))})


@never_cache
@require_http_methods(["GET"])
def restream_callback(request):
    """Restream sends the browser back here; the single-use `state` proves we started it."""
    code, state = request.GET.get("code"), request.GET.get("state")
    credentials = _credentials()
    if not code or not state:
        message = "Restream access was not granted."
    elif credentials is None:
        message = "The Restream client ID and secret are not set."
    else:
        try:
            restream.complete_authorization(credentials, _redirect_uri(request), code, state)
        except restream.RestreamError as exc:
            message = str(exc)
        else:
            return HttpResponseRedirect("/show/#restream-connected")
    return HttpResponse(
        f"<!doctype html><meta charset=utf-8><title>Restream</title><p>{escape(message)}</p>"
        '<p><a href="/show/">Back to the show controls</a></p>',
        status=400,
    )
