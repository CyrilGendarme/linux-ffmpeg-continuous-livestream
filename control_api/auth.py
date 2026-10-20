"""Shared-secret authentication for the control API, with brute-force lockout."""

import hmac
import threading
import time
from collections import defaultdict, deque

from django.conf import settings
from django.http import JsonResponse

MAX_FAILURES = 5
WINDOW_SECONDS = 600  # 5 wrong secrets within 10 minutes locks the client out for the rest of the window
MIN_SECRET_LENGTH = 16

_failures = defaultdict(deque)
_lock = threading.Lock()


def _client(request) -> str:
    # The server is meant to be exposed directly; X-Forwarded-For is not trusted.
    return request.META.get("REMOTE_ADDR", "unknown")


def _recent_failures(client: str) -> deque:
    attempts = _failures[client]
    cutoff = time.monotonic() - WINDOW_SECONDS
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    return attempts


def server_secret_problem() -> str:
    """Why the configured secret can't be used, or '' if it is fine."""
    secret = settings.INSTANCE_API_KEY or ""
    if not settings.DEBUG and (secret == "test-secret-key" or len(secret) < MIN_SECRET_LENGTH):
        return (
            f"The server's INSTANCE_API_KEY is missing, the default, or shorter than {MIN_SECRET_LENGTH} characters. "
            "Set a strong secret before using the control API."
        )
    return ""


def check_secret(request):
    """Return None if the request carries the right secret, otherwise an error response."""
    problem = server_secret_problem()
    if problem:
        return JsonResponse({"detail": problem}, status=503)

    client = _client(request)
    with _lock:
        attempts = _recent_failures(client)
        if len(attempts) >= MAX_FAILURES:
            retry = int(attempts[0] + WINDOW_SECONDS - time.monotonic()) + 1
            response = JsonResponse(
                {"detail": f"Too many wrong secrets. Try again in {retry // 60 + 1} minute(s)."}, status=429
            )
            response["Retry-After"] = str(retry)
            return response

    provided = request.headers.get("X-Instance-Key") or request.headers.get("X-API-Key") or ""
    if hmac.compare_digest(provided.encode(), (settings.INSTANCE_API_KEY or "").encode()):
        return None

    with _lock:
        _recent_failures(client).append(time.monotonic())
    return JsonResponse({"detail": "Missing or invalid secret."}, status=401)


def reset_failures() -> None:
    """For tests."""
    with _lock:
        _failures.clear()
