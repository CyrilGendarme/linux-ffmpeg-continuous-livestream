"""Web editor for the stream's config.json, and the live show controls.

Reading and writing the config both require the instance secret: the config
contains stream keys. The pages themselves (and the field schema) are public.

The show controls (background loop, today's menu, countdown) are config
options too; the running stream picks them up without a restart.
"""

import json
import os
import re
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

from livestream.config import LIVE_OPTIONS, OPTIONS, OPTIONS_BY_NAME, option_value, schema, validate_config
from livestream.service import SERVICE_NAME

from .auth import check_secret, server_secret_problem

_write_lock = threading.Lock()
VIDEO_EXTENSIONS = {".mp4", ".webm"}


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


def _sync_schedule_js(config: dict) -> str:
    """Write the countdown time into the OBS overlays' startingSoonSchedule.js. Returns a problem or ''."""
    path = Path(settings.STARTING_SOON_SCHEDULE_PATH)
    try:
        source = path.read_text(encoding="utf-8")
        updated = source
        for constant, name in (("TARGET_HOUR", "countdown_hour"), ("TARGET_MINUTE", "countdown_minute")):
            value = option_value(OPTIONS_BY_NAME[name], config)
            updated = re.sub(rf"^(export const {constant} = )[^;\n]*;", rf"\g<1>{value};", updated, flags=re.M)
        if updated != source:
            path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return f" Could not update {path.name}: {exc.strerror or exc}."
    return ""


def _save_config(config: dict) -> str:
    """Write the config and everything derived from it. Returns problems to report, or ''."""
    _write_config(config)
    return _sync_schedule_js(config)


def _resolve(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path(settings.STREAM_WORKDIR) / path


def _as_config_path(path: Path) -> str:
    """A path as the config writes it: relative to the runner's working directory when inside it."""
    try:
        return str(path.relative_to(Path(settings.STREAM_WORKDIR).resolve()))
    except ValueError:
        return str(path)


def background_choices(current: str = "") -> list:
    """Videos offered as background loops: BACKGROUND_VIDEO_DIRS and the current background's folder."""
    folders = list(settings.BACKGROUND_VIDEO_DIRS)
    if current:
        folders.append(_resolve(current).parent)
    found = {}
    for folder in folders:
        try:
            entries = sorted(Path(folder).iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix.lower() in VIDEO_EXTENSIONS and entry.is_file():
                found.setdefault(entry.resolve(), None)
    return [_as_config_path(path) for path in found]


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
        return JsonResponse({
            "config": config,
            "service": _service_state(),
            "path": str(_config_path()),
            "backgrounds": background_choices(config.get("background_video") or ""),
        })

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
        problem = _save_config(clean)
    changed = _changed_keys(before, clean)

    restarted = False
    message = ("Saved." if changed else "Saved (nothing changed).") + problem
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


# ------------------------------------------------------------- show controls


def _show_target(config: dict, name: str) -> dict:
    """The settings dict where `name` takes effect: the active profile's if it overrides it."""
    profile = (config.get("profiles") or {}).get(config.get("profile") or "")
    return profile if isinstance(profile, dict) and name in profile else config


def show_state(config: dict) -> dict:
    return {name: option_value(OPTIONS_BY_NAME[name], _show_target(config, name)) for name in LIVE_OPTIONS}


def update_show(changes: dict):
    """Validate and save show settings; returns a JsonResponse."""
    unknown = sorted(set(changes) - set(LIVE_OPTIONS))
    if unknown:
        return JsonResponse({"detail": "Unknown show settings.", "errors": {key: "unknown setting" for key in unknown}},
                            status=400)
    with _write_lock:
        try:
            before = _read_config()
        except (OSError, ValueError) as exc:
            return JsonResponse({"detail": f"Could not read the config: {exc}"}, status=500)
        config = json.loads(json.dumps(before))  # deep copy
        for name, value in changes.items():
            _show_target(config, name)[name] = value
        clean, errors = validate_config(config, check_paths=True, base_dir=settings.STREAM_WORKDIR)
        if errors:
            return JsonResponse({"detail": "Some settings are invalid.", "errors": errors}, status=400)
        problem = _save_config(clean)
    changed = [name for name in LIVE_OPTIONS if show_state(before)[name] != show_state(clean)[name]]
    message = "Applied to the stream." if changed else "Nothing changed."
    if "background_video" in changed:
        message += " The new background goes on air as soon as it is prepared."
    return JsonResponse({"ok": True, "show": show_state(clean), "changed": changed, "message": message + problem})


@never_cache
def show_page(request):
    return render(request, "control_api/show.html", {"server_problem": server_secret_problem()})


@csrf_exempt  # authenticated by a header secret, not by cookies
@never_cache
@require_http_methods(["GET", "POST"])
def show_api(request):
    """GET: show settings and background choices. POST: change any of them, e.g. {"countdown_enabled": true}."""
    denied = check_secret(request)
    if denied is not None:
        return denied

    if request.method == "GET":
        try:
            config = _read_config()
        except (OSError, ValueError) as exc:
            return JsonResponse({"detail": f"Could not read the config: {exc}"}, status=500)
        show = show_state(config)
        return JsonResponse({"show": show, "backgrounds": background_choices(show["background_video"] or "")})

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError):
        return JsonResponse({"detail": "Request body must be valid JSON."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"detail": "Expected a JSON object of show settings."}, status=400)
    return update_show(payload)
