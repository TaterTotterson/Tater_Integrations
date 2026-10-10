from __future__ import annotations

__version__ = "1.0.0"

import contextlib
import importlib
import ipaddress
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from helpers import redis_client

GOOGLE_CAST_SETTINGS_KEY = "google_cast_settings"
GOOGLE_CAST_DISCOVERY_CACHE_KEY = "tater:google_cast:players:registry:v1"
GOOGLE_CAST_DEFAULT_ENABLED = True
GOOGLE_CAST_DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 3
GOOGLE_CAST_DEFAULT_CONNECTION_TIMEOUT_SECONDS = 12
GOOGLE_CAST_DISCOVERY_CACHE_TTL_SECONDS = 60.0
GOOGLE_CAST_DISCOVERY_STALE_TTL_SECONDS = 24 * 60 * 60.0

_discovery_lock = threading.RLock()
_discovery_rows: List[Dict[str, Any]] = []
_discovery_ts = 0.0

INTEGRATION = {
    "id": "google_cast",
    "name": "Google Cast",
    "description": "Discover Google Cast devices and play Tater-generated audio on TVs, speakers, and groups.",
    "badge": "CAST",
    "order": 52,
    "capabilities": ["speaker", "media_player", "audio_output", "announcement_target"],
    "fields": [
        {
            "key": "google_cast_enabled",
            "label": "Enable Google Cast",
            "type": "checkbox",
            "default": GOOGLE_CAST_DEFAULT_ENABLED,
        },
        {
            "key": "google_cast_discovery_timeout_seconds",
            "label": "Discovery Timeout Seconds",
            "type": "number",
            "default": GOOGLE_CAST_DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
            "min": 1,
            "max": 10,
        },
        {
            "key": "google_cast_connection_timeout_seconds",
            "label": "Connection Timeout Seconds",
            "type": "number",
            "default": GOOGLE_CAST_DEFAULT_CONNECTION_TIMEOUT_SECONDS,
            "min": 2,
            "max": 30,
        },
        {
            "key": "google_cast_known_hosts",
            "label": "Manual Cast Hosts",
            "type": "textarea",
            "default": "",
            "rows": 3,
            "placeholder": "192.168.1.42\noffice-tv.local",
            "description": "Optional. One hostname or IP per line for devices mDNS does not discover.",
            "full_width": True,
        },
    ],
    "actions": [
        {
            "id": "discover",
            "label": "Discover Cast Devices",
            "status": "Searches the local network and any configured manual hosts.",
        },
    ],
}


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "ignore").strip()
    return str(value or "").strip()


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    token = _text(value).lower()
    if token in {"1", "true", "yes", "on", "enabled"}:
        return True
    if token in {"0", "false", "no", "off", "disabled"}:
        return False
    return bool(default)


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(_text(value)))
    except Exception:
        parsed = int(default)
    return max(int(minimum), min(int(maximum), parsed))


def _split_known_hosts(value: Any) -> List[str]:
    hosts: List[str] = []
    seen: set[str] = set()
    for raw in _text(value).replace(",", "\n").splitlines():
        host = _text(raw)
        if not host or host.startswith("#"):
            continue
        key = host.casefold()
        if key in seen:
            continue
        seen.add(key)
        hosts.append(host)
    return hosts


def _load_cast_dependencies(*, required: bool = True) -> Optional[Dict[str, Any]]:
    try:
        pychromecast = importlib.import_module("pychromecast")
        discovery = importlib.import_module("pychromecast.discovery")
    except Exception as exc:
        if required:
            raise RuntimeError(
                "Google Cast requires PyChromecast. Rerun Tater setup so PyChromecast==14.0.10 is installed."
            ) from exc
        return None
    return {
        "pychromecast": pychromecast,
        "CastBrowser": discovery.CastBrowser,
        "SimpleCastListener": pychromecast.SimpleCastListener,
        "get_chromecast_from_host": pychromecast.get_chromecast_from_host,
        "stream_type_buffered": pychromecast.STREAM_TYPE_BUFFERED,
    }


def read_google_cast_settings(client: Any = None) -> Dict[str, Any]:
    store = client or redis_client
    try:
        raw = store.hgetall(GOOGLE_CAST_SETTINGS_KEY) or {}
    except Exception:
        raw = {}
    return {
        "GOOGLE_CAST_ENABLED": _as_bool(raw.get("GOOGLE_CAST_ENABLED"), GOOGLE_CAST_DEFAULT_ENABLED),
        "GOOGLE_CAST_DISCOVERY_TIMEOUT_SECONDS": str(
            _bounded_int(
                raw.get("GOOGLE_CAST_DISCOVERY_TIMEOUT_SECONDS"),
                default=GOOGLE_CAST_DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
                minimum=1,
                maximum=10,
            )
        ),
        "GOOGLE_CAST_CONNECTION_TIMEOUT_SECONDS": str(
            _bounded_int(
                raw.get("GOOGLE_CAST_CONNECTION_TIMEOUT_SECONDS"),
                default=GOOGLE_CAST_DEFAULT_CONNECTION_TIMEOUT_SECONDS,
                minimum=2,
                maximum=30,
            )
        ),
        "GOOGLE_CAST_KNOWN_HOSTS": _text(raw.get("GOOGLE_CAST_KNOWN_HOSTS")),
    }


def save_google_cast_settings(
    *,
    enabled: Any = None,
    discovery_timeout_seconds: Any = None,
    connection_timeout_seconds: Any = None,
    known_hosts: Any = None,
    client: Any = None,
) -> Dict[str, Any]:
    global _discovery_rows, _discovery_ts
    store = client or redis_client
    current = read_google_cast_settings(store)
    next_settings = {
        "GOOGLE_CAST_ENABLED": "true"
        if _as_bool(
            current.get("GOOGLE_CAST_ENABLED") if enabled is None else enabled,
            GOOGLE_CAST_DEFAULT_ENABLED,
        )
        else "false",
        "GOOGLE_CAST_DISCOVERY_TIMEOUT_SECONDS": str(
            _bounded_int(
                current.get("GOOGLE_CAST_DISCOVERY_TIMEOUT_SECONDS")
                if discovery_timeout_seconds is None
                else discovery_timeout_seconds,
                default=GOOGLE_CAST_DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
                minimum=1,
                maximum=10,
            )
        ),
        "GOOGLE_CAST_CONNECTION_TIMEOUT_SECONDS": str(
            _bounded_int(
                current.get("GOOGLE_CAST_CONNECTION_TIMEOUT_SECONDS")
                if connection_timeout_seconds is None
                else connection_timeout_seconds,
                default=GOOGLE_CAST_DEFAULT_CONNECTION_TIMEOUT_SECONDS,
                minimum=2,
                maximum=30,
            )
        ),
        "GOOGLE_CAST_KNOWN_HOSTS": _text(
            current.get("GOOGLE_CAST_KNOWN_HOSTS") if known_hosts is None else known_hosts
        ),
    }
    store.hset(GOOGLE_CAST_SETTINGS_KEY, mapping=next_settings)
    with _discovery_lock:
        _discovery_rows = []
        _discovery_ts = 0.0
    with contextlib.suppress(Exception):
        store.delete(GOOGLE_CAST_DISCOVERY_CACHE_KEY)
    return read_google_cast_settings(store)


def read_integration_settings() -> Dict[str, Any]:
    settings = read_google_cast_settings()
    return {
        "google_cast_enabled": _as_bool(settings.get("GOOGLE_CAST_ENABLED"), GOOGLE_CAST_DEFAULT_ENABLED),
        "google_cast_discovery_timeout_seconds": int(
            settings.get("GOOGLE_CAST_DISCOVERY_TIMEOUT_SECONDS")
            or GOOGLE_CAST_DEFAULT_DISCOVERY_TIMEOUT_SECONDS
        ),
        "google_cast_connection_timeout_seconds": int(
            settings.get("GOOGLE_CAST_CONNECTION_TIMEOUT_SECONDS")
            or GOOGLE_CAST_DEFAULT_CONNECTION_TIMEOUT_SECONDS
        ),
        "google_cast_known_hosts": settings.get("GOOGLE_CAST_KNOWN_HOSTS", ""),
    }


def save_integration_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload or {}
    saved = save_google_cast_settings(
        enabled=data.get("google_cast_enabled"),
        discovery_timeout_seconds=data.get("google_cast_discovery_timeout_seconds"),
        connection_timeout_seconds=data.get("google_cast_connection_timeout_seconds"),
        known_hosts=data.get("google_cast_known_hosts"),
    )
    return {
        "google_cast_enabled": _as_bool(saved.get("GOOGLE_CAST_ENABLED"), GOOGLE_CAST_DEFAULT_ENABLED),
        "google_cast_discovery_timeout_seconds": int(
            saved.get("GOOGLE_CAST_DISCOVERY_TIMEOUT_SECONDS")
            or GOOGLE_CAST_DEFAULT_DISCOVERY_TIMEOUT_SECONDS
        ),
        "google_cast_connection_timeout_seconds": int(
            saved.get("GOOGLE_CAST_CONNECTION_TIMEOUT_SECONDS")
            or GOOGLE_CAST_DEFAULT_CONNECTION_TIMEOUT_SECONDS
        ),
        "google_cast_known_hosts": saved.get("GOOGLE_CAST_KNOWN_HOSTS", ""),
    }


def integration_status() -> Dict[str, Any]:
    settings = read_google_cast_settings()
    enabled = _as_bool(settings.get("GOOGLE_CAST_ENABLED"), GOOGLE_CAST_DEFAULT_ENABLED)
    if not enabled:
        return {"configured": False, "enabled": False, "message": "Google Cast is disabled."}
    if _load_cast_dependencies(required=False) is None:
        return {
            "configured": False,
            "enabled": True,
            "message": "PyChromecast is missing. Rerun Tater setup, then restart Tater.",
        }
    return {"configured": True, "enabled": True, "message": "Google Cast is ready for discovery."}


def _cast_info_row(cast_info: Any) -> Dict[str, Any]:
    device_uuid = _text(getattr(cast_info, "uuid", ""))
    host = _text(getattr(cast_info, "host", ""))
    if not device_uuid or not host:
        return {}
    port = _bounded_int(getattr(cast_info, "port", 8009), default=8009, minimum=1, maximum=65535)
    name = _text(getattr(cast_info, "friendly_name", "")) or host
    return {
        "id": device_uuid.lower(),
        "name": name,
        "host": host,
        "port": port,
        "model": _text(getattr(cast_info, "model_name", "")),
        "manufacturer": _text(getattr(cast_info, "manufacturer", "")),
        "cast_type": _text(getattr(cast_info, "cast_type", "")),
        "available": True,
    }


def _dedupe_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    unique: Dict[str, Dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        device_id = _text(raw.get("id")).lower()
        host = _text(raw.get("host"))
        if not device_id or not host:
            continue
        row = dict(raw)
        row["id"] = device_id
        row["host"] = host
        unique[device_id] = row
    return sorted(
        unique.values(),
        key=lambda item: (_text(item.get("name")).casefold(), _text(item.get("host")).casefold()),
    )


def _cache_rows(rows: List[Dict[str, Any]], *, client: Any = None, now_ts: Optional[float] = None) -> None:
    store = client or redis_client
    payload = {"ts": float(now_ts if now_ts is not None else time.time()), "rows": rows}
    with contextlib.suppress(Exception):
        store.set(GOOGLE_CAST_DISCOVERY_CACHE_KEY, json.dumps(payload, ensure_ascii=False))


def _cached_rows(*, maximum_age_s: float, client: Any = None) -> tuple[float, List[Dict[str, Any]]]:
    store = client or redis_client
    try:
        raw = store.get(GOOGLE_CAST_DISCOVERY_CACHE_KEY)
        payload = json.loads(_text(raw)) if raw else {}
        timestamp = float(payload.get("ts") or 0.0)
        rows = _dedupe_rows([dict(row) for row in payload.get("rows") or [] if isinstance(row, dict)])
    except Exception:
        return 0.0, []
    if timestamp <= 0.0 or time.time() - timestamp > maximum_age_s:
        return timestamp, []
    return timestamp, rows


def discover_google_cast_devices(
    *,
    force: bool = False,
    client: Any = None,
) -> List[Dict[str, Any]]:
    global _discovery_rows, _discovery_ts
    settings = read_google_cast_settings(client)
    if not _as_bool(settings.get("GOOGLE_CAST_ENABLED"), GOOGLE_CAST_DEFAULT_ENABLED):
        return []
    dependencies = _load_cast_dependencies(required=True)
    assert dependencies is not None
    now = time.time()
    with _discovery_lock:
        if not force and _discovery_rows and now - _discovery_ts <= GOOGLE_CAST_DISCOVERY_CACHE_TTL_SECONDS:
            return [dict(row) for row in _discovery_rows]
        if not force:
            cached_ts, cached = _cached_rows(
                maximum_age_s=GOOGLE_CAST_DISCOVERY_CACHE_TTL_SECONDS,
                client=client,
            )
            if cached:
                _discovery_rows = cached
                _discovery_ts = cached_ts
                return [dict(row) for row in cached]

        browser = None
        rows: List[Dict[str, Any]] = []
        timeout_s = _bounded_int(
            settings.get("GOOGLE_CAST_DISCOVERY_TIMEOUT_SECONDS"),
            default=GOOGLE_CAST_DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
            minimum=1,
            maximum=10,
        )
        known_hosts = _split_known_hosts(settings.get("GOOGLE_CAST_KNOWN_HOSTS"))
        try:
            listener = dependencies["SimpleCastListener"]()
            browser = dependencies["CastBrowser"](
                listener,
                known_hosts=known_hosts or None,
            )
            browser.start_discovery()
            threading.Event().wait(float(timeout_s))
            rows = _dedupe_rows(
                [_cast_info_row(info) for info in list(getattr(browser, "devices", {}).values())]
            )
        finally:
            if browser is not None:
                with contextlib.suppress(Exception):
                    browser.stop_discovery()

        if rows:
            _discovery_rows = rows
            _discovery_ts = now
            _cache_rows(rows, client=client, now_ts=now)
            return [dict(row) for row in rows]

        _cached_ts, stale = _cached_rows(
            maximum_age_s=GOOGLE_CAST_DISCOVERY_STALE_TTL_SECONDS,
            client=client,
        )
        for row in stale:
            row["available"] = False
        return stale


def normalize_google_cast_id(value: Any) -> str:
    token = _text(value)
    for prefix in ("integration:google_cast:", "google_cast:", "cast:"):
        if token.lower().startswith(prefix):
            token = token[len(prefix) :]
            break
    with contextlib.suppress(ValueError, AttributeError):
        return str(uuid.UUID(token)).lower()
    return token.lower()


def resolve_google_cast_target(value: Any, *, refresh: bool = False, client: Any = None) -> Dict[str, Any]:
    device_id = normalize_google_cast_id(value)
    if not device_id:
        return {}
    rows = discover_google_cast_devices(force=refresh, client=client)
    row = next((item for item in rows if normalize_google_cast_id(item.get("id")) == device_id), None)
    if row or refresh:
        return dict(row or {})
    rows = discover_google_cast_devices(force=True, client=client)
    row = next((item for item in rows if normalize_google_cast_id(item.get("id")) == device_id), None)
    return dict(row or {})


def _device_type(row: Dict[str, Any]) -> str:
    return "speaker" if _text(row.get("cast_type")).lower() in {"audio", "group"} else "media_player"


def integration_devices() -> Dict[str, Any]:
    settings = read_google_cast_settings()
    if not _as_bool(settings.get("GOOGLE_CAST_ENABLED"), GOOGLE_CAST_DEFAULT_ENABLED):
        return {"devices": [], "message": "Google Cast is disabled."}
    if _load_cast_dependencies(required=False) is None:
        return {"devices": [], "message": "PyChromecast is missing. Rerun Tater setup, then restart Tater."}
    try:
        rows = discover_google_cast_devices(force=False)
    except Exception as exc:
        return {"devices": [], "message": f"Google Cast discovery failed: {_text(exc)}"}
    devices: List[Dict[str, Any]] = []
    for row in rows:
        device_id = normalize_google_cast_id(row.get("id"))
        if not device_id:
            continue
        available = bool(row.get("available", True))
        devices.append(
            {
                "id": device_id,
                "name": _text(row.get("name")) or _text(row.get("host")) or "Google Cast",
                "type": _device_type(row),
                "ref": f"media_player:{device_id}",
                "capabilities": [
                    "speaker",
                    "media_player",
                    "audio_output",
                    "announcement_target",
                    "play_media",
                    "volume",
                    "seek",
                ],
                "actions": [
                    "play_media",
                    "play_url",
                    "play",
                    "pause",
                    "stop",
                    "seek",
                    "set_volume",
                    "mute",
                    "unmute",
                ],
                "features": ["play_media", "speaker", "volume", "seek"],
                "status": "available" if available else "offline",
                "state": "available" if available else "offline",
                "room": "",
                "area": "",
                "details": {
                    "host": row.get("host"),
                    "port": row.get("port"),
                    "model": row.get("model"),
                    "manufacturer": row.get("manufacturer"),
                    "cast_type": row.get("cast_type"),
                },
            }
        )
    return {"devices": devices, "message": f"Google Cast returned {len(devices)} device(s)."}


def _media_mime_type(payload: Dict[str, Any], source_url: str) -> str:
    supplied = _text(payload.get("media_type") or payload.get("mime_type") or payload.get("content_type"))
    if "/" in supplied:
        return supplied.split(";", 1)[0].strip().lower()
    suffix = Path(urlparse(source_url).path).suffix.lower()
    return {
        ".aac": "audio/aac",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".mp4": "video/mp4",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".wav": "audio/wav",
        ".webm": "audio/webm",
    }.get(suffix, "audio/mpeg")


def _validate_cast_media_url(source_url: Any) -> str:
    url = _text(source_url)
    parsed = urlparse(url)
    host = _text(parsed.hostname).lower()
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Google Cast playback requires a LAN-reachable HTTP or HTTPS media URL.")
    is_loopback = host == "localhost"
    with contextlib.suppress(ValueError):
        is_loopback = is_loopback or ipaddress.ip_address(host).is_loopback
    if is_loopback:
        raise ValueError(
            "Google Cast cannot fetch a loopback media URL. Use Tater's LAN-reachable runtime media URL."
        )
    return url


def _connect_cast(row: Dict[str, Any], *, timeout_s: float) -> tuple[Any, Dict[str, Any]]:
    dependencies = _load_cast_dependencies(required=True)
    assert dependencies is not None
    device_uuid = uuid.UUID(normalize_google_cast_id(row.get("id")))
    host = _text(row.get("host"))
    if not host:
        raise RuntimeError("Google Cast device host is missing.")
    cast = dependencies["get_chromecast_from_host"](
        (
            host,
            _bounded_int(row.get("port"), default=8009, minimum=1, maximum=65535),
            device_uuid,
            _text(row.get("model")) or None,
            _text(row.get("name")) or None,
        ),
        tries=1,
        retry_wait=1.0,
        timeout=float(timeout_s),
    )
    cast.wait(timeout=float(timeout_s))
    return cast, dependencies


def _volume_percent(payload: Dict[str, Any]) -> Optional[int]:
    raw = payload.get("volume_percent", payload.get("volume"))
    if raw is None or _text(raw) == "":
        return None
    return _bounded_int(raw, default=100, minimum=0, maximum=100)


def run_integration_action(action_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if _text(action_id).lower() != "discover":
        raise KeyError(f"Unsupported Google Cast action: {action_id}")
    if payload:
        save_integration_settings(payload)
    rows = discover_google_cast_devices(force=True)
    return {
        "ok": True,
        "device_count": len(rows),
        "devices": rows,
        "message": f"Google Cast discovery found {len(rows)} device(s).",
    }


def run_integration_device_action(action_id: str, device_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    action = _text(action_id).lower()
    supported = {"play_media", "play_url", "play", "pause", "stop", "seek", "set_volume", "mute", "unmute"}
    if action not in supported:
        raise KeyError(f"Unsupported Google Cast device action: {action_id}")
    row = resolve_google_cast_target(device_id)
    if not row or not row.get("available", True):
        row = resolve_google_cast_target(device_id, refresh=True)
    if not row:
        raise RuntimeError("Google Cast device was not found.")

    data = payload or {}
    settings = read_google_cast_settings()
    timeout_s = float(
        _bounded_int(
            data.get("connection_timeout_seconds")
            or settings.get("GOOGLE_CAST_CONNECTION_TIMEOUT_SECONDS"),
            default=GOOGLE_CAST_DEFAULT_CONNECTION_TIMEOUT_SECONDS,
            minimum=2,
            maximum=30,
        )
    )
    source_url = ""
    if action in {"play_media", "play_url"}:
        source_url = _validate_cast_media_url(
            data.get("source_url")
            or data.get("url")
            or data.get("media_url")
            or data.get("media_content_id")
        )
    cast = None
    try:
        cast, dependencies = _connect_cast(row, timeout_s=timeout_s)
        controller = cast.media_controller
        if action in {"play_media", "play_url"}:
            volume = _volume_percent(data)
            if volume is not None:
                cast.set_volume(volume / 100.0, timeout=timeout_s)
            current_time = max(0.0, float(data.get("start_position_seconds") or 0.0))
            controller.quick_play(
                media_id=source_url,
                media_type=_media_mime_type(data, source_url),
                timeout=timeout_s,
                title=_text(data.get("title")) or None,
                thumb=_text(data.get("artwork_url") or data.get("thumbnail_url")) or None,
                current_time=current_time,
                stream_type=dependencies["stream_type_buffered"],
            )
        elif action == "play":
            controller.play(timeout=timeout_s)
        elif action == "pause":
            controller.pause(timeout=timeout_s)
        elif action == "stop":
            controller.stop(timeout=timeout_s)
        elif action == "seek":
            position = max(
                0.0,
                float(data.get("position_seconds") or data.get("start_position_seconds") or 0.0),
            )
            controller.seek(position, timeout=timeout_s)
        elif action == "set_volume":
            volume = _volume_percent(data)
            if volume is None:
                raise ValueError("Google Cast set_volume requires volume_percent or volume.")
            cast.set_volume(volume / 100.0, timeout=timeout_s)
        elif action in {"mute", "unmute"}:
            cast.set_volume_muted(action == "mute", timeout=timeout_s)
    finally:
        if cast is not None:
            with contextlib.suppress(Exception):
                cast.disconnect(timeout=timeout_s)

    return {
        "ok": True,
        "sent_count": 1,
        "device_id": normalize_google_cast_id(device_id),
        "action": action,
    }


__all__ = [
    "INTEGRATION",
    "discover_google_cast_devices",
    "integration_devices",
    "integration_status",
    "read_google_cast_settings",
    "read_integration_settings",
    "resolve_google_cast_target",
    "run_integration_action",
    "run_integration_device_action",
    "save_google_cast_settings",
    "save_integration_settings",
]
