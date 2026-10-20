"""Web editor for the stream's config.json.

Reading and writing the config both require the instance secret: the config
contains stream keys. The page itself (and the field schema) are public.
"""

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from livestream.config import OPTIONS, schema, validate_config
from livestream.service import SERVICE_NAME

from .auth import check_secret, server_secret_problem

_write_lock = threading.Lock()


def _config_path() -> Path:
    return Path(settings.STREAM_CONFIG_PATH)


def _read_config() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_config(config: dict) -> None:
    """Atomic replace, keeping the previous version as config.json.bak."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.chmod(temporary, 0o600)  # contains stream keys
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    os.replace(temporary, path)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)


def _service_state() -> str:
    if shutil.which("systemctl") is None:
        return "unavailable"
    try:
        result = _systemctl("is-active", SERVICE_NAME)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    state = result.stdout.strip() or "unknown"
    if state in ("inactive", "unknown") and "could not be found" in result.stderr.lower():
        return "not installed"
    return state


def _changed_keys(before: dict, after: dict, prefix: str = "") -> list:
    changed = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if isinstance(old, dict) and isinstance(new, dict):
            changed += _changed_keys(old, new, f"{prefix}{key}.")
        elif old != new:
            changed.append(f"{prefix}{key}")
    return changed


@never_cache
def config_page(request):
    return render(request, "control_api/config.html", {"server_problem": server_secret_problem()})


@never_cache
@require_http_methods(["GET"])
def config_schema(request):
    return JsonResponse({"options": schema(), "groups": list(dict.fromkeys(option.group for option in OPTIONS))})


@csrf_exempt  # authenticated by a header secret, not by cookies
@never_cache
@require_http_methods(["GET", "POST"])
def config_api(request):
    denied = check_secret(request)
    if denied is not None:
        return denied

    if request.method == "GET":
        try:
            config = _read_config()
        except (OSError, ValueError) as exc:
            return JsonResponse({"detail": f"Could not read the config: {exc}"}, status=500)
        return JsonResponse({"config": config, "service": _service_state(), "path": str(_config_path())})

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError):
        return JsonResponse({"detail": "Request body must be valid JSON."}, status=400)
    if not isinstance(payload, dict) or not isinstance(payload.get("config"), dict):
        return JsonResponse({"detail": "Expected {\"config\": {...}, \"restart\": true|false}."}, status=400)

    clean, errors = validate_config(payload["config"], check_paths=True, base_dir=settings.STREAM_WORKDIR)
    if errors:
        return JsonResponse({"detail": "Some settings are invalid.", "errors": errors}, status=400)

    with _write_lock:
        try:
            before = _read_config()
        except (OSError, ValueError):
            before = {}
        _write_config(clean)
    changed = _changed_keys(before, clean)

    restarted = False
    message = "Saved." if changed else "Saved (nothing changed)."
    if payload.get("restart"):
        state = _service_state()
        if state in ("unavailable", "not installed"):
            message += " The stream service is not installed, so it was not restarted."
        else:
            result = _systemctl("restart", SERVICE_NAME)
            restarted = result.returncode == 0
            message += " Stream restarted." if restarted else f" Restart failed: {result.stderr.strip()[:300]}"
    return JsonResponse(
        {"ok": True, "changed": changed, "restarted": restarted, "message": message, "service": _service_state()}
    )
