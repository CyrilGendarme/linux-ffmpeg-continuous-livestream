"""Restream API client: OAuth connection and stream titles (https://developers.restream.io).

The OAuth app's client id/secret are config options (restream_client_id,
restream_client_secret). After a one-time "Connect Restream" in the browser,
the tokens live in settings.RESTREAM_STATE_PATH (mode 0600): access tokens
last 1 hour, refresh tokens 1 year and rotate on every refresh.
"""

import base64
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings

API = "https://api.restream.io"
AUTHORIZE_URL = f"{API}/login"
TOKEN_URL = f"{API}/oauth/token"
STATE_TTL_SECONDS = 600  # an authorization must come back within 10 minutes
REFRESH_MARGIN_SECONDS = 60

_lock = threading.Lock()
_pending_states: Dict[str, float] = {}  # OAuth `state` -> expiry (monotonic)


class RestreamError(Exception):
    pass


# ------------------------------------------------------------------ storage


def _state_path() -> Path:
    return Path(settings.RESTREAM_STATE_PATH)


def _load() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _store(data: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)  # holds the Restream tokens
    os.replace(temporary, path)


def last_title() -> str:
    return _load().get("last_title", "")


def is_connected() -> bool:
    data = _load()
    return bool(data.get("refresh_token")) and data.get("refresh_expires_at", 0) > time.time()


def disconnect() -> None:
    with _lock:
        data = _load()
        data.pop("access_token", None)
        data.pop("refresh_token", None)
        _store(data)


# ------------------------------------------------------------------ HTTP


def _request(method: str, url: str, body: Optional[bytes] = None, headers: Optional[dict] = None) -> dict:
    request = Request(url, data=body, method=method, headers=headers or {})
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read()
    except HTTPError as exc:
        try:
            error = json.loads(exc.read()).get("error", {})
            message = error.get("message") or exc.reason
        except (ValueError, AttributeError):
            message = exc.reason
        raise RestreamError(f"Restream refused the request ({exc.code}): {message}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RestreamError(f"Restream is unreachable: {exc}") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise RestreamError("Restream sent an unreadable response.") from exc


def _token_request(credentials: tuple, fields: dict) -> None:
    """Exchange a code or refresh token; stores the new token pair."""
    client_id, client_secret = credentials
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = _request(
        "POST", TOKEN_URL, urlencode(fields).encode(),
        {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    if not data.get("access_token") or not data.get("refresh_token"):
        raise RestreamError("Restream did not return tokens.")
    now = time.time()
    stored = _load()
    stored.update(
        access_token=data["access_token"],
        expires_at=now + float(data.get("expires_in") or 3600),
        refresh_token=data["refresh_token"],
        refresh_expires_at=now + float(data.get("refreshTokenExpiresIn") or 365 * 86400),
    )
    _store(stored)


# ------------------------------------------------------------------ OAuth


def authorize_url(credentials: tuple, redirect_uri: str) -> str:
    """Where to send the browser to connect Restream; remembers a single-use state."""
    state = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _lock:
        for key in [key for key, expiry in _pending_states.items() if expiry < now]:
            del _pending_states[key]
        _pending_states[state] = now + STATE_TTL_SECONDS
    query = urlencode({"response_type": "code", "client_id": credentials[0], "redirect_uri": redirect_uri, "state": state})
    return f"{AUTHORIZE_URL}?{query}"


def complete_authorization(credentials: tuple, redirect_uri: str, code: str, state: str) -> None:
    with _lock:
        expiry = _pending_states.pop(state, None)
    if expiry is None or expiry < time.monotonic():
        raise RestreamError("This authorization link is unknown or expired. Start again from the show page.")
    with _lock:
        _token_request(credentials, {"grant_type": "authorization_code", "redirect_uri": redirect_uri, "code": code})


def _access_token(credentials: tuple) -> str:
    with _lock:
        data = _load()
        if not data.get("refresh_token"):
            raise RestreamError("Restream is not connected.")
        if data.get("expires_at", 0) - REFRESH_MARGIN_SECONDS > time.time():
            return data["access_token"]
        _token_request(credentials, {"grant_type": "refresh_token", "refresh_token": data["refresh_token"]})
        return _load()["access_token"]


# ------------------------------------------------------------------ API


def _api(credentials: tuple, method: str, path: str, payload: Optional[dict] = None) -> dict:
    headers = {"Authorization": f"Bearer {_access_token(credentials)}"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    return _request(method, f"{API}/v2{path}", body, headers)


def channels(credentials: tuple) -> List[dict]:
    """The account's channels: [{id, displayName, platformId, channelUrl}]."""
    return _api(credentials, "GET", "/user/channels").get("channels", [])


def set_title(credentials: tuple, title: str, channel_ids: Optional[List[int]] = None) -> List[dict]:
    """Set the stream title on the given channels (default: all). Returns one result per channel."""
    targets = channels(credentials)
    if channel_ids is not None:
        targets = [channel for channel in targets if channel.get("id") in channel_ids]
    results = []
    for channel in targets:
        result = {"id": channel.get("id"), "name": channel.get("displayName") or str(channel.get("id")), "ok": True}
        try:
            _api(credentials, "PATCH", f"/user/channel-meta/{int(channel['id'])}", {"title": title})
        except RestreamError as exc:
            result.update(ok=False, error=str(exc))
        results.append(result)
    if any(result["ok"] for result in results):
        with _lock:
            stored = _load()
            stored["last_title"] = title
            _store(stored)
    return results
