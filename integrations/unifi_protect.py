from __future__ import annotations
__version__ = "1.2.1"

import base64
import contextlib
import io
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import warnings
import wave
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

from helpers import redis_client
from vision_settings import get_vision_settings as get_shared_vision_settings

UNIFI_PROTECT_BASE_URL_KEY = "tater:unifi_protect:base_url"
UNIFI_PROTECT_API_KEY_KEY = "tater:unifi_protect:api_key"
UNIFI_PROTECT_DEFAULT_BASE_URL = "https://10.4.20.127"
DEFAULT_UNIFI_PROTECT_AUDIO_TIMEOUT_SECONDS = 90.0
INTEGRATION_RUNTIME_POLL_SECONDS = 2
UNIFI_PROTECT_CAMERA_PATHS = [
    "/proxy/protect/integration/v1/cameras",
    "/proxy/protect/api/cameras",
    "/proxy/protect/api/bootstrap",
]
UNIFI_PROTECT_SENSOR_PATHS = [
    "/proxy/protect/integration/v1/sensors",
    "/proxy/protect/api/sensors",
    "/proxy/protect/api/bootstrap",
]

INTEGRATION = {
    "id": "unifi_protect",
    "name": "UniFi Protect",
    "description": "UniFi Protect API key for cameras, sensors, and direct speaker announcements.",
    "badge": "PRO",
    "order": 70,
    "fields": [
        {
            "key": "unifi_protect_base_url",
            "label": "Console Base URL",
            "type": "text",
            "default": UNIFI_PROTECT_DEFAULT_BASE_URL,
            "placeholder": UNIFI_PROTECT_DEFAULT_BASE_URL,
        },
        {
            "key": "unifi_protect_api_key",
            "label": "API Key",
            "type": "password",
            "default": "",
        },
    ],
    "actions": [
        {
            "id": "test",
            "label": "Test UniFi Protect",
            "status": "Checks the Protect API and counts cameras plus sensors.",
        },
    ],
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_int(value: Any, default: int, *, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    try:
        out = int(float(value))
    except Exception:
        out = int(default)
    if minimum is not None:
        out = max(minimum, out)
    if maximum is not None:
        out = min(maximum, out)
    return out


def read_unifi_protect_settings(client: Any = None) -> Dict[str, str]:
    store = client or redis_client
    base = _text(store.get(UNIFI_PROTECT_BASE_URL_KEY) or UNIFI_PROTECT_DEFAULT_BASE_URL).rstrip("/")
    api_key = _text(store.get(UNIFI_PROTECT_API_KEY_KEY))
    return {
        "base": base or UNIFI_PROTECT_DEFAULT_BASE_URL,
        "api_key": api_key,
        "UNIFI_PROTECT_BASE_URL": base or UNIFI_PROTECT_DEFAULT_BASE_URL,
        "UNIFI_PROTECT_API_KEY": api_key,
    }


def save_unifi_protect_settings(
    *,
    base_url: Any = None,
    api_key: Any = None,
    client: Any = None,
) -> Dict[str, str]:
    store = client or redis_client
    current = read_unifi_protect_settings(store)
    next_base = _text(current.get("base") if base_url is None else base_url) or UNIFI_PROTECT_DEFAULT_BASE_URL
    next_api_key = _text(current.get("api_key") if api_key is None else api_key)
    store.set(UNIFI_PROTECT_BASE_URL_KEY, next_base.rstrip("/"))
    store.set(UNIFI_PROTECT_API_KEY_KEY, next_api_key)
    return read_unifi_protect_settings(store)


def load_unifi_protect_config(*, required: bool = True, client: Any = None) -> Dict[str, str]:
    settings = read_unifi_protect_settings(client)
    if required and not _text(settings.get("api_key")):
        raise ValueError(
            "UniFi Protect API key is not set. Open WebUI -> Settings -> Integrations -> UniFi Protect and add API key."
        )
    return {"base": _text(settings.get("base")), "api_key": _text(settings.get("api_key"))}


def unifi_protect_configured(client: Any = None) -> bool:
    return bool(_text(read_unifi_protect_settings(client).get("api_key")))


def unifi_protect_headers(api_key: str, *, json_content: bool = True) -> Dict[str, str]:
    headers = {"X-API-KEY": api_key, "Accept": "application/json"}
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


def unifi_protect_request(
    method: str,
    path: str,
    *,
    base_url: Any = None,
    api_key: Any = None,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    stream: bool = False,
    timeout_s: float = 20.0,
) -> Any:
    if base_url is None or api_key is None:
        conf = load_unifi_protect_config(required=True)
        base = conf["base"]
        key = conf["api_key"]
    else:
        base = _text(base_url).rstrip("/")
        key = _text(api_key)
    url_path = path if _text(path).startswith("/") else f"/{path}"
    if not base:
        raise ValueError("UniFi Protect base URL is required.")
    if not key:
        raise ValueError("UniFi Protect API key is required.")
    req_headers = unifi_protect_headers(key, json_content=not stream)
    if headers:
        req_headers.update(headers)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", InsecureRequestWarning)
        resp = requests.request(
            method,
            f"{base}{url_path}",
            headers=req_headers,
            params=params,
            json=json_body,
            timeout=max(5.0, float(timeout_s or 20.0)),
            verify=False,
            stream=stream,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"UniFi Protect HTTP {resp.status_code}: {resp.text[:200]}")
    if stream:
        return resp.content, resp.headers
    try:
        return resp.json()
    except Exception:
        return {}


def _payload_rows(payload: Any, *preferred_keys: str) -> Tuple[List[Dict[str, Any]], bool]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)], True
    if not isinstance(payload, dict):
        return [], False

    keys = list(preferred_keys) + ["data", "items", "results"]
    seen: set[str] = set()
    for key in keys:
        if not key or key in seen or key not in payload:
            continue
        seen.add(key)
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)], True
        if isinstance(value, dict):
            return [item for item in value.values() if isinstance(item, dict)], True

    values = list(payload.values())
    if values and all(isinstance(item, dict) for item in values):
        return [item for item in values if isinstance(item, dict)], True
    return [], False


def _list_unifi_rows(
    paths: List[str],
    *payload_keys: str,
    base_url: Any = None,
    api_key: Any = None,
    timeout_s: float = 20.0,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    warnings_out: List[str] = []
    first_empty_success: Optional[List[Dict[str, Any]]] = None
    for path in paths:
        try:
            payload = unifi_protect_request(
                "GET",
                path,
                base_url=base_url,
                api_key=api_key,
                timeout_s=timeout_s,
            )
        except Exception as exc:
            warnings_out.append(f"{path}: {exc}")
            continue

        rows, matched = _payload_rows(payload, *payload_keys)
        if rows:
            return rows, warnings_out
        if matched and first_empty_success is None:
            first_empty_success = []

    if first_empty_success is not None:
        return first_empty_success, warnings_out
    return [], warnings_out


def unifi_camera_entity(camera_id: Any) -> str:
    token = _text(camera_id).lower()
    return f"camera.unifi_{token}" if token else ""


def unifi_camera_id_from_target(target: Any) -> str:
    token = _text(target)
    lower = token.lower()
    if lower.startswith("unifi:"):
        token = _text(token.split(":", 1)[1])
        lower = token.lower()
    if lower.startswith("camera."):
        object_id = lower.split(".", 1)[1]
        if object_id.startswith("unifi_"):
            return object_id[len("unifi_") :]
        return object_id
    if lower.startswith("unifi_"):
        return lower[len("unifi_") :]
    return lower


def unifi_camera_name(row: Dict[str, Any], camera_id: str) -> str:
    for key in ("name", "displayName", "display_name", "friendlyName", "friendly_name"):
        value = _text(row.get(key))
        if value:
            return value
    return camera_id


def unifi_camera_has_speaker_hint(row: Dict[str, Any]) -> bool:
    feature_flags = row.get("featureFlags") if isinstance(row.get("featureFlags"), dict) else {}
    if not feature_flags and isinstance(row.get("feature_flags"), dict):
        feature_flags = row.get("feature_flags")
    direct_keys = (
        "hasSpeaker",
        "has_speaker",
        "hasTwoWayAudio",
        "has_two_way_audio",
        "hasTalkback",
        "has_talkback",
    )
    for key in direct_keys:
        if key in row and bool(row.get(key)):
            return True
        if key in feature_flags and bool(feature_flags.get(key)):
            return True
    text = " ".join(
        _text(row.get(key)).lower()
        for key in ("type", "model", "modelKey", "marketName", "market_name", "name")
    )
    return "doorbell" in text


def list_unifi_cameras() -> List[Dict[str, Any]]:
    rows, _warnings = _list_unifi_rows(UNIFI_PROTECT_CAMERA_PATHS, "cameras", timeout_s=20.0)
    return rows


def list_unifi_sensors() -> List[Dict[str, Any]]:
    rows, _warnings = _list_unifi_rows(UNIFI_PROTECT_SENSOR_PATHS, "sensors", timeout_s=20.0)
    return rows


def _first_text(row: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _text(row.get(key))
        if value:
            return value
    return ""


def _protect_bool_status(row: Dict[str, Any]) -> str:
    for key in ("isConnected", "is_connected", "connected", "isOnline", "is_online"):
        if key in row:
            return "online" if bool(row.get(key)) else "offline"
    for key in ("state", "status", "connectionState", "connection_state"):
        value = _text(row.get(key))
        if not value:
            continue
        token = value.lower()
        if token in {"connected", "online", "up"}:
            return "online"
        if token in {"disconnected", "offline", "down"}:
            return "offline"
        return value
    return ""


def _protect_details(row: Dict[str, Any], keys: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            out[key] = value
    return out


def _nested_value(row: Dict[str, Any], path: str) -> Any:
    current: Any = row
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _first_value(row: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = _nested_value(row, key) if "." in key else row.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _bool_value(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    token = _text(value).lower()
    if token in {"1", "true", "yes", "on", "open", "opened", "active", "detected", "wet", "alarm"}:
        return True
    if token in {"0", "false", "no", "off", "closed", "inactive", "clear", "dry", "none"}:
        return False
    return None


def _sensor_bool(row: Dict[str, Any], *keys: str) -> Optional[bool]:
    value = _first_value(row, *keys)
    if value is None:
        return None
    return _bool_value(value)


def _sensor_stat_value(row: Dict[str, Any], stat: str) -> Any:
    camel = {
        "temperature": "temperature",
        "humidity": "humidity",
        "light": "light",
    }.get(stat, stat)
    return _first_value(
        row,
        f"stats.{stat}.value",
        f"stats.{camel}.value",
        f"{stat}Stats.value",
        f"{stat}_stats.value",
        stat,
    )


def _unifi_camera_is_doorbell(row: Dict[str, Any]) -> bool:
    hint = " ".join(
        _text(row.get(key)).lower()
        for key in ("name", "type", "model", "modelKey", "marketName", "market_name")
    )
    return "doorbell" in hint or "g4db" in hint or "g5db" in hint


def _unifi_normalize_smart_type(value: Any) -> str:
    token = _text(value).lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "people": "person",
        "human": "person",
        "humans": "person",
        "vehicles": "vehicle",
        "car": "vehicle",
        "cars": "vehicle",
        "animals": "animal",
        "pet": "animal",
        "pets": "animal",
        "packages": "package",
        "parcel": "package",
        "licenseplate": "license_plate",
        "licenseplates": "license_plate",
    }
    return aliases.get(token, token)


def _unifi_camera_smart_detect_types(row: Dict[str, Any]) -> List[str]:
    values: List[Any] = []
    for key in ("smartDetectTypes", "smart_detect_types", "smartDetectionTypes", "smart_detection_types"):
        raw = row.get(key)
        if isinstance(raw, list):
            values.extend(raw)
        elif raw:
            values.append(raw)
    feature_flags = row.get("featureFlags") if isinstance(row.get("featureFlags"), dict) else {}
    raw_flags = feature_flags.get("smartDetectTypes") or feature_flags.get("smart_detect_types")
    if isinstance(raw_flags, list):
        values.extend(raw_flags)
    out: List[str] = []
    for value in values:
        token = _unifi_normalize_smart_type(value)
        if token and token not in out:
            out.append(token)
    return out


def _unifi_camera_capabilities(row: Dict[str, Any]) -> List[str]:
    caps = ["camera", "snapshot", "motion"]
    for smart_type in _unifi_camera_smart_detect_types(row):
        token = f"smart_{smart_type}"
        if token not in caps:
            caps.append(token)
    if _unifi_camera_is_doorbell(row):
        caps.append("doorbell")
    if unifi_camera_has_speaker_hint(row):
        caps.append("speaker")
    return caps


def _unifi_camera_event_sources(camera_id: str, row: Dict[str, Any]) -> List[Dict[str, Any]]:
    camera_token = _text(camera_id).lower()
    if not camera_token:
        return []
    out = [
        {
            "type": "motion",
            "ref": f"binary_sensor.unifi_{camera_token}_motion",
            "state_on": "on",
            "state_off": "off",
        }
    ]
    for smart_type in _unifi_camera_smart_detect_types(row):
        out.append(
            {
                "type": f"smart_{smart_type}",
                "ref": f"binary_sensor.unifi_{camera_token}_smart_{smart_type}",
                "state_on": "on",
                "state_off": "off",
            }
        )
    if _unifi_camera_is_doorbell(row):
        out.append(
            {
                "type": "doorbell",
                "ref": f"event.unifi_{camera_token}_doorbell",
                "state_on": "on",
                "state_off": "off",
            }
        )
    return out


def _unifi_sensor_kind(row: Dict[str, Any]) -> str:
    name = _text(row.get("name")).lower()
    mount_type = _text(row.get("mountType") or row.get("mount_type")).lower()
    sensor_type = _text(row.get("sensorType") or row.get("sensor_type") or row.get("type")).lower()
    model = _text(row.get("model") or row.get("modelKey") or row.get("marketName")).lower()
    hint = f"{name} {mount_type} {sensor_type} {model}"
    if "garage" in hint:
        return "garage"
    if "window" in hint:
        return "window"
    if any(token in hint for token in ("door", "contact", "open")):
        return "door"
    if any(token in hint for token in ("leak", "flood", "water")):
        return "leak"
    if "motion" in hint or _sensor_bool(row, "isMotionDetected", "is_motion_detected") is not None:
        return "motion"
    if _sensor_bool(row, "isOpened", "is_opened", "isOpen", "is_open", "open") is not None:
        return "contact"
    return "sensor"


def _unifi_sensor_capabilities(row: Dict[str, Any]) -> List[str]:
    caps = ["sensor"]
    sensor_kind = _unifi_sensor_kind(row)
    if sensor_kind in {"door", "window", "garage", "contact"}:
        caps.extend(["entry_sensor", "contact", "open_close"])
        if sensor_kind in {"door", "window", "garage"}:
            caps.append(sensor_kind)
    text = " ".join(_text(row.get(key)).lower() for key in ("name", "type", "sensorType", "sensor_type", "model", "modelKey"))
    if "motion" in text or _sensor_bool(row, "isMotionDetected", "is_motion_detected") is not None:
        caps.append("motion")
    if "temp" in text or _sensor_stat_value(row, "temperature") is not None:
        caps.append("temperature")
    if "humid" in text or _sensor_stat_value(row, "humidity") is not None:
        caps.append("humidity")
    if any(token in text for token in ("light", "lux", "illuminance")) or _sensor_stat_value(row, "light") is not None:
        caps.extend(["illuminance", "light_sensor"])
    if sensor_kind == "leak" or _sensor_bool(row, "isLeakDetected", "is_leak_detected", "leakDetected", "leak_detected") is not None:
        caps.extend(["leak", "water"])
    if _first_value(row, "batteryStatus", "battery_status", "batteryLevel", "battery_level", "battery") is not None:
        caps.append("battery")
    if _sensor_bool(row, "isTamperingDetected", "is_tampering_detected", "tamperingDetected", "tampering_detected") is not None:
        caps.append("tamper")
    return list(dict.fromkeys(caps))


def _unifi_sensor_state(row: Dict[str, Any]) -> str:
    opened = _sensor_bool(row, "isOpened", "is_opened", "isOpen", "is_open", "open", "opened", "contactState", "contact_state")
    if opened is not None:
        return "open" if opened else "closed"
    motion = _sensor_bool(row, "isMotionDetected", "is_motion_detected", "motionDetected", "motion_detected")
    if motion is not None:
        return "motion" if motion else "clear"
    leak = _sensor_bool(row, "isLeakDetected", "is_leak_detected", "leakDetected", "leak_detected")
    if leak is None and _first_value(row, "leakDetectedAt", "leak_detected_at") is not None:
        leak = True
    if leak is not None:
        return "wet" if leak else "dry"
    alarm = _sensor_bool(row, "isAlarmTriggered", "is_alarm_triggered", "alarmTriggered", "alarm_triggered")
    if alarm is None and _first_value(row, "alarmTriggeredAt", "alarm_triggered_at") is not None:
        alarm = True
    if alarm is not None:
        return "alarm" if alarm else "clear"
    status = _protect_bool_status(row)
    return status or _first_text(row, "state", "status") or "unknown"


def _unifi_sensor_event_sources(row: Dict[str, Any], ref: str, sensor_kind: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if _sensor_bool(row, "isOpened", "is_opened", "isOpen", "is_open", "open", "opened", "contactState", "contact_state") is not None or sensor_kind in {"door", "window", "garage", "contact"}:
        out.append(
            {
                "type": sensor_kind if sensor_kind in {"door", "window", "garage"} else "contact",
                "ref": ref,
                "state_on": "open",
                "state_off": "closed",
            }
        )
    if _sensor_bool(row, "isMotionDetected", "is_motion_detected", "motionDetected", "motion_detected") is not None or "motion" in _unifi_sensor_capabilities(row):
        out.append({"type": "motion", "ref": ref, "state_on": "motion", "state_off": "clear"})
    if sensor_kind == "leak" or _sensor_bool(row, "isLeakDetected", "is_leak_detected", "leakDetected", "leak_detected") is not None:
        out.append({"type": "leak", "ref": ref, "state_on": "wet", "state_off": "dry"})
    if _sensor_bool(row, "isTamperingDetected", "is_tampering_detected", "tamperingDetected", "tampering_detected") is not None:
        out.append({"type": "tamper", "ref": ref, "state_on": "tamper", "state_off": "clear"})
    if not out:
        out.append({"type": "connectivity", "ref": ref, "state_on": "online", "state_off": "offline"})
    return out


def _unifi_sensor_ref(sensor_id: Any, fallback: Any = "") -> str:
    token = _text(sensor_id).lower()
    if token:
        return f"binary_sensor.unifi_sensor_{token}"
    name = _text(fallback)
    return f"sensor:{name}" if name else ""


def _runtime_cursor_map(cursor: Any) -> Dict[str, Dict[str, Any]]:
    if isinstance(cursor, str):
        try:
            cursor = json.loads(cursor)
        except Exception:
            cursor = {}
    if not isinstance(cursor, dict):
        return {}
    states = cursor.get("states") if isinstance(cursor.get("states"), dict) else cursor
    out: Dict[str, Dict[str, Any]] = {}
    for key, value in (states or {}).items():
        key_text = _text(key)
        if not key_text:
            continue
        out[key_text] = value if isinstance(value, dict) else {"state": _text(value)}
    return out


def _runtime_sensor_state_payload(row: Dict[str, Any], ref: str, sensor_kind: str) -> Dict[str, Any]:
    sensor_id = _first_text(row, "id", "_id", "uuid")
    name = _first_text(row, "name", "displayName", "friendlyName") or sensor_id or ref
    state = _unifi_sensor_state(row)
    attrs = {
        "friendly_name": name,
        "device_id": sensor_id,
        "sensor_kind": sensor_kind,
        "mount_type": _first_text(row, "mountType", "mount_type"),
        "model": _first_text(row, "model", "modelKey", "marketName"),
    }
    return {
        "entity_id": ref,
        "id": ref,
        "device_id": sensor_id,
        "name": name,
        "state": state,
        "attributes": {key: value for key, value in attrs.items() if value not in (None, "")},
    }


def integration_runtime_poll(*, client: Any = None, cursor: Any = None) -> Dict[str, Any]:
    """Publish contact sensor state edges from the sensor API as a websocket fallback."""
    del client
    if not unifi_protect_configured():
        return {"states": [], "events": [], "cursor": {"states": {}}}

    previous = _runtime_cursor_map(cursor)
    current: Dict[str, Dict[str, Any]] = {}
    states: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []

    for row in list_unifi_sensors():
        if not isinstance(row, dict):
            continue
        sensor_id = _first_text(row, "id", "_id", "uuid")
        name = _first_text(row, "name", "displayName", "friendlyName") or sensor_id
        sensor_kind = _unifi_sensor_kind(row)
        if sensor_kind not in {"door", "window", "garage", "contact"}:
            continue
        ref = _unifi_sensor_ref(sensor_id, name)
        if not ref:
            continue
        payload = _runtime_sensor_state_payload(row, ref, sensor_kind)
        state = _text(payload.get("state"))
        current[ref] = {
            "state": state,
            "name": _text(payload.get("name")),
            "sensor_kind": sensor_kind,
            "device_id": sensor_id,
        }
        states.append(payload)

        old = previous.get(ref)
        old_state = _text((old or {}).get("state"))
        if not old_state or old_state == state:
            continue
        attrs = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {}
        events.append(
            {
                "kind": "state_changed",
                "payload": {
                    "entity_id": ref,
                    "device_id": sensor_id,
                    "source": "poll",
                    "new_state": {"state": state, "attributes": attrs},
                    "old_state": {"state": old_state, "attributes": attrs},
                },
            }
        )

    return {"states": states, "events": events, "cursor": {"states": current}}


def integration_devices() -> Dict[str, Any]:
    if not unifi_protect_configured():
        return {"devices": [], "message": "UniFi Protect is not configured."}
    rows: List[Dict[str, Any]] = []
    warnings_out: List[str] = []
    cameras, camera_warnings = _list_unifi_rows(UNIFI_PROTECT_CAMERA_PATHS, "cameras", timeout_s=20.0)
    sensors, sensor_warnings = _list_unifi_rows(UNIFI_PROTECT_SENSOR_PATHS, "sensors", timeout_s=20.0)
    warnings_out.extend(camera_warnings)
    warnings_out.extend(sensor_warnings)
    for camera in cameras:
        if not isinstance(camera, dict):
            continue
        camera_id = _first_text(camera, "id", "_id", "uuid")
        name = unifi_camera_name(camera, camera_id)
        camera_entity = unifi_camera_entity(camera_id)
        rows.append(
            {
                "id": camera_id or name,
                "name": name or camera_id or "Camera",
                "type": "camera",
                "ref": camera_entity or f"camera:{camera_id}",
                "capabilities": _unifi_camera_capabilities(camera),
                "actions": ["camera_snapshot"],
                "event_sources": _unifi_camera_event_sources(camera_id, camera),
                "status": _protect_bool_status(camera),
                "state": _first_text(camera, "state", "status"),
                "details": _protect_details(
                    camera,
                    [
                        "model",
                        "modelKey",
                        "marketName",
                        "host",
                        "hostAddress",
                        "firmwareVersion",
                        "isConnected",
                        "isRecording",
                        "isMotionDetected",
                        "lastMotion",
                        "lastMotionAt",
                        "lastRing",
                        "lastRingAt",
                    ],
                ),
            }
        )
    for sensor in sensors:
        if not isinstance(sensor, dict):
            continue
        sensor_id = _first_text(sensor, "id", "_id", "uuid")
        name = _first_text(sensor, "name", "displayName", "friendlyName") or sensor_id
        sensor_kind = _unifi_sensor_kind(sensor)
        sensor_ref = f"binary_sensor.unifi_sensor_{_text(sensor_id).lower()}" if sensor_id else f"sensor:{name}"
        sensor_state = _unifi_sensor_state(sensor)
        rows.append(
            {
                "id": sensor_id or name,
                "name": name or "Sensor",
                "type": "entry_sensor" if sensor_kind in {"door", "window", "garage", "contact"} else "sensor",
                "ref": sensor_ref,
                "capabilities": _unifi_sensor_capabilities(sensor),
                "event_sources": _unifi_sensor_event_sources(sensor, sensor_ref, sensor_kind),
                "status": _protect_bool_status(sensor),
                "state": sensor_state,
                "details": _protect_details(
                    sensor,
                    [
                        "model",
                        "modelKey",
                        "marketName",
                        "type",
                        "sensorType",
                        "sensor_type",
                        "mountType",
                        "mount_type",
                        "batteryStatus",
                        "battery_status",
                        "batteryLevel",
                        "battery_level",
                        "isConnected",
                        "isOpened",
                        "is_opened",
                        "isMotionDetected",
                        "is_motion_detected",
                        "leakDetectedAt",
                        "leak_detected_at",
                        "alarmTriggeredAt",
                        "alarm_triggered_at",
                        "tamperingDetectedAt",
                        "tampering_detected_at",
                        "stats",
                        "lastSeen",
                        "last_seen",
                    ],
                ),
            }
        )
    result: Dict[str, Any] = {
        "devices": rows,
        "message": f"UniFi Protect returned {len(cameras)} camera{'s' if len(cameras) != 1 else ''} and {len(sensors)} sensor{'s' if len(sensors) != 1 else ''}.",
    }
    if warnings_out:
        result["warnings"] = warnings_out
    return result


def read_integration_settings() -> Dict[str, Any]:
    settings = read_unifi_protect_settings()
    return {
        "unifi_protect_base_url": settings.get("base") or UNIFI_PROTECT_DEFAULT_BASE_URL,
        "unifi_protect_api_key": settings.get("api_key", ""),
    }


def save_integration_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    saved = save_unifi_protect_settings(
        base_url=(payload or {}).get("unifi_protect_base_url"),
        api_key=(payload or {}).get("unifi_protect_api_key"),
    )
    return {
        "unifi_protect_base_url": saved.get("base") or UNIFI_PROTECT_DEFAULT_BASE_URL,
        "unifi_protect_api_key": saved.get("api_key", ""),
    }


def run_integration_action(action_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if _text(action_id) != "test":
        raise KeyError(f"Unsupported UniFi Protect action: {action_id}")
    current = read_integration_settings()
    base = _text((payload or {}).get("unifi_protect_base_url") or current.get("unifi_protect_base_url")).rstrip("/")
    api_key = _text((payload or {}).get("unifi_protect_api_key") or current.get("unifi_protect_api_key"))
    if not api_key:
        raise ValueError("UniFi Protect API key is required.")
    cameras, camera_warnings = _list_unifi_rows(
        UNIFI_PROTECT_CAMERA_PATHS,
        "cameras",
        base_url=base,
        api_key=api_key,
        timeout_s=20.0,
    )
    sensors, sensor_warnings = _list_unifi_rows(
        UNIFI_PROTECT_SENSOR_PATHS,
        "sensors",
        base_url=base,
        api_key=api_key,
        timeout_s=20.0,
    )
    if not cameras and camera_warnings:
        raise RuntimeError(f"UniFi Protect test failed: {camera_warnings[-1]}")
    return {
        "ok": True,
        "camera_count": len(cameras),
        "sensor_count": len(sensors),
        "warnings": sensor_warnings,
        "message": (
            f"UniFi Protect connection worked. Found {len(cameras)} camera{'s' if len(cameras) != 1 else ''} "
            f"and {len(sensors)} sensor{'s' if len(sensors) != 1 else ''}."
        ),
    }


def _wav_duration_seconds(wav_bytes: bytes) -> float:
    payload = bytes(wav_bytes or b"")
    if not payload:
        return 0.0
    try:
        with wave.open(io.BytesIO(payload), "rb") as wav_file:
            frames = wav_file.getnframes()
            rate = wav_file.getframerate()
            return float(frames) / float(rate) if rate > 0 else 0.0
    except Exception:
        return 0.0


def _talkback_audio_args(session: Dict[str, Any]) -> list[str]:
    codec = _text(session.get("codec")).lower().replace("-", "_")
    sample_rate = _as_int(
        session.get("samplingRate") or session.get("sampling_rate"),
        48000,
        minimum=8000,
        maximum=96000,
    )
    if "mulaw" in codec or "pcmu" in codec:
        return ["-ac", "1", "-ar", "8000", "-c:a", "pcm_mulaw"]
    if "alaw" in codec or "pcma" in codec:
        return ["-ac", "1", "-ar", "8000", "-c:a", "pcm_alaw"]
    return ["-ac", "1", "-ar", str(sample_rate or 48000), "-c:a", "libopus", "-application", "voip", "-b:a", "32k"]


def play_unifi_protect_audio_sync(
    *,
    cameras: list[str],
    audio_bytes: bytes,
    timeout_s: float = DEFAULT_UNIFI_PROTECT_AUDIO_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    clean_cameras = [unifi_camera_id_from_target(item) for item in list(cameras or []) if _text(item)]
    clean_cameras = [item for item in clean_cameras if item]
    if not clean_cameras:
        return {"ok": False, "sent_count": 0, "error": "No UniFi Protect cameras selected."}
    payload = bytes(audio_bytes or b"")
    if not payload:
        return {"ok": False, "sent_count": 0, "error": "Announcement audio is empty."}

    ffmpeg_path = _text(os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg"))
    if not ffmpeg_path:
        return {"ok": False, "sent_count": 0, "error": "ffmpeg is required for UniFi Protect audio playback."}

    duration_s = _wav_duration_seconds(payload)
    run_timeout = max(float(timeout_s or DEFAULT_UNIFI_PROTECT_AUDIO_TIMEOUT_SECONDS), duration_s + 20.0, 30.0)
    sent_count = 0
    failures: list[str] = []
    tmp_path = ""

    try:
        with tempfile.NamedTemporaryFile(prefix="tater-unifi-announcement-", suffix=".wav", delete=False) as tmp_file:
            tmp_file.write(payload)
            tmp_path = tmp_file.name

        for camera_id in clean_cameras:
            try:
                session = unifi_protect_request(
                    "POST",
                    f"/proxy/protect/integration/v1/cameras/{camera_id}/talkback-session",
                    timeout_s=20.0,
                )
                stream_url = _text(session.get("url") or session.get("streamUrl") or session.get("stream_url"))
                if not stream_url:
                    raise RuntimeError("talkback session did not return a stream URL")
                cmd = [
                    ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-re",
                    "-i",
                    tmp_path,
                    "-vn",
                    *_talkback_audio_args(session),
                    "-f",
                    "rtp",
                    stream_url,
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=run_timeout, check=False)
                if proc.returncode == 0:
                    sent_count += 1
                    continue
                detail = _text(proc.stderr) or _text(proc.stdout) or f"ffmpeg exited {proc.returncode}"
                failures.append(f"{camera_id} ({detail[:220]})")
            except Exception as exc:
                failures.append(f"{camera_id} ({exc})")
    finally:
        if tmp_path:
            with contextlib.suppress(Exception):
                os.unlink(tmp_path)

    if sent_count:
        result: Dict[str, Any] = {"ok": True, "sent_count": sent_count}
        if failures:
            result["warnings"] = failures
        return result
    return {"ok": False, "sent_count": 0, "error": "; ".join(failures) or "UniFi Protect playback failed."}


def _mime_from_filename(filename: str) -> str:
    mt, _ = mimetypes.guess_type(filename or "")
    return mt or "image/jpeg"


def _to_data_url(image_bytes: bytes, filename: str = "image.jpg") -> str:
    mime = _mime_from_filename(filename)
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


class ProtectClient:
    def __init__(self):
        conf = load_unifi_protect_config(required=True)

        vision_settings = get_shared_vision_settings(
            default_api_base="http://127.0.0.1:1234",
            default_model="qwen2.5-vl-7b-instruct",
        )
        self.verify_ssl = False
        self.timeout = 20
        self.base_url = conf["base"].rstrip("/")
        self.api_key = conf["api_key"]
        self.vision_api_base = str(vision_settings.get("api_base") or "http://127.0.0.1:1234").strip().rstrip("/")
        self.vision_model = str(vision_settings.get("model") or "qwen2.5-vl-7b-instruct").strip()
        self.vision_api_key = str(vision_settings.get("api_key") or "").strip()
        self.headers = {"X-API-KEY": self.api_key, "Accept": "application/json"}

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def _req(self, method: str, path: str, *, params=None, json_body=None, headers=None, stream=False) -> Any:
        url = self._url(path)
        hdrs = dict(self.headers)
        if headers:
            hdrs.update(headers)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", InsecureRequestWarning)
            resp = requests.request(
                method,
                url,
                headers=hdrs,
                params=params,
                json=json_body,
                timeout=self.timeout,
                verify=self.verify_ssl,
                stream=stream,
            )

        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")

        ctype = (resp.headers.get("Content-Type") or "").lower()
        if stream or "image/" in ctype:
            return resp.content, resp.headers

        try:
            return resp.json()
        except Exception:
            return resp.text

    def list_sensors(self) -> List[dict]:
        return self._req("GET", "/proxy/protect/integration/v1/sensors") or []

    def get_sensor(self, sensor_id: str) -> dict:
        return self._req("GET", f"/proxy/protect/integration/v1/sensors/{sensor_id}")

    def list_cameras(self) -> List[dict]:
        return self._req("GET", "/proxy/protect/integration/v1/cameras") or []

    def get_camera_snapshot(self, camera_id: str) -> Tuple[bytes, str]:
        candidates = [
            f"/proxy/protect/integration/v1/cameras/{camera_id}/snapshot",
            f"/proxy/protect/integration/v1/cameras/{camera_id}/snapshot.jpg",
            f"/proxy/protect/integration/v1/cameras/{camera_id}/snapshot?format=jpeg",
            f"/proxy/protect/integration/v1/cameras/{camera_id}/snapshot?force=true",
            f"/proxy/protect/integration/v1/cameras/{camera_id}/snapshot?force=true&format=jpeg",
        ]

        last_err = None
        for path in candidates:
            try:
                data, headers = self._req(
                    "GET",
                    path,
                    headers={"Accept": "image/jpeg,image/png,image/*,*/*"},
                    stream=True,
                )
                ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if not ctype:
                    ctype = "image/jpeg"
                if isinstance(data, (bytes, bytearray)) and len(data) > 1000:
                    return bytes(data), ctype
            except Exception as exc:
                last_err = exc
                continue

        raise RuntimeError(f"Snapshot not available for camera {camera_id}. Last error: {last_err}")

    def call_vision(self, image_bytes: bytes, *, prompt: str, filename: str = "image.jpg") -> str:
        url = f"{self.vision_api_base}/v1/chat/completions"
        data_url = _to_data_url(image_bytes, filename)

        payload = {
            "model": self.vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt or "Describe this image."},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "temperature": 0.2,
            "max_tokens": 500,
        }

        headers = {"Content-Type": "application/json"}
        if self.vision_api_key:
            headers["Authorization"] = f"Bearer {self.vision_api_key}"

        resp = requests.post(url, json=payload, headers=headers, timeout=90)
        if resp.status_code != 200:
            return f"(vision error {resp.status_code}) {resp.text[:200]}"

        payload = resp.json()
        try:
            return (payload["choices"][0]["message"]["content"] or "").strip()
        except Exception:
            return "(vision error) Unexpected response"


def get_camera_snapshot(device_id: Any) -> Tuple[bytes, str]:
    camera_id = unifi_camera_id_from_target(device_id)
    if not camera_id:
        raise ValueError("UniFi Protect camera id is required.")
    return ProtectClient().get_camera_snapshot(camera_id)


def run_integration_device_action(action_id: str, device_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if _text(action_id) not in {"camera_snapshot", "snapshot"}:
        raise KeyError(f"Unsupported UniFi Protect device action: {action_id}")
    content, content_type = get_camera_snapshot(device_id)
    return {"ok": True, "bytes": content, "content_type": content_type}
