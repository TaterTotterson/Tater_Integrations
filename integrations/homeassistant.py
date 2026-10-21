from __future__ import annotations
__version__ = "1.6.0"

import asyncio
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import quote, urljoin

import requests

from helpers import redis_client

HOMEASSISTANT_SETTINGS_KEY = "homeassistant_settings"
HOMEASSISTANT_RUNTIME_STATES_KEY = "tater:integration_runtime:states"
HOMEASSISTANT_DEFAULT_BASE_URL = "http://homeassistant.local:8123"
HOMEASSISTANT_ENTITY_SCOPE_ALL = "all"
HOMEASSISTANT_ENTITY_SCOPE_ASSIST = "assist"
HOMEASSISTANT_DEFAULT_ENTITY_SCOPE = HOMEASSISTANT_ENTITY_SCOPE_ALL
HOMEASSISTANT_ASSIST_POLICY_CACHE_SECONDS = 30
HOMEASSISTANT_CAMERA_STREAM_FEATURE = 2
HOMEASSISTANT_CAMERA_CLIP_MAX_SECONDS = 30

# Keep these aligned with Home Assistant's homeassistant.exposed_entities
# defaults. Explicit per-entity Assist choices always take precedence.
HOMEASSISTANT_ASSIST_DEFAULT_DOMAINS = {
    "climate",
    "cover",
    "fan",
    "humidifier",
    "light",
    "media_player",
    "scene",
    "switch",
    "todo",
    "vacuum",
    "water_heater",
}
HOMEASSISTANT_ASSIST_DEFAULT_BINARY_SENSOR_DEVICE_CLASSES = {
    "door",
    "garage_door",
    "lock",
    "motion",
    "opening",
    "presence",
    "window",
}
HOMEASSISTANT_ASSIST_DEFAULT_SENSOR_DEVICE_CLASSES = {
    "aqi",
    "carbon_monoxide",
    "carbon_dioxide",
    "humidity",
    "pm10",
    "pm25",
    "temperature",
    "volatile_organic_compounds",
}
_ASSIST_POLICY_CACHE_LOCK = threading.RLock()
_ASSIST_POLICY_CACHE: Dict[str, Any] = {
    "base": "",
    "token": "",
    "policy": None,
    "loaded_at": 0.0,
    "retry_at": 0.0,
    "error": "",
}

INTEGRATION = {
    "id": "homeassistant",
    "name": "Home Assistant",
    "description": "Shared Home Assistant endpoint and token for devices, camera snapshots and short camera clips.",
    "badge": "HA",
    "order": 10,
    "capabilities": [
        "light",
        "switch",
        "plug",
        "sensor",
        "garage_door",
        "entry_sensor",
        "motion",
        "temperature",
        "humidity",
        "illuminance",
        "energy",
        "leak",
        "battery",
        "camera",
        "video_clip",
        "cover",
        "climate",
        "media_player",
        "appliance",
        "washer",
        "dryer",
        "dishwasher",
        "remaining_time",
    ],
    "fields": [
        {
            "key": "homeassistant_base_url",
            "label": "Base URL",
            "type": "text",
            "default": HOMEASSISTANT_DEFAULT_BASE_URL,
            "placeholder": HOMEASSISTANT_DEFAULT_BASE_URL,
        },
        {
            "key": "homeassistant_token",
            "label": "Long-Lived Access Token",
            "type": "password",
            "default": "",
        },
        {
            "key": "homeassistant_entity_scope",
            "label": "Devices Available to Tater",
            "type": "select",
            "options": [
                {"value": HOMEASSISTANT_ENTITY_SCOPE_ALL, "label": "All Home Assistant entities"},
                {
                    "value": HOMEASSISTANT_ENTITY_SCOPE_ASSIST,
                    "label": "Only entities exposed to Assist",
                },
            ],
            "default": HOMEASSISTANT_DEFAULT_ENTITY_SCOPE,
            "description": (
                "Uses Home Assistant Settings > Voice assistants > Expose choices. "
                "Assist-only mode requires a Home Assistant administrator token."
            ),
        },
    ],
    "actions": [
        {
            "id": "test",
            "label": "Test Home Assistant",
            "status": "Checks the Home Assistant REST API with the current form values.",
        },
    ],
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    token = _text(value).lower()
    if token in {"1", "true", "yes", "on", "enabled"}:
        return True
    if token in {"0", "false", "no", "off", "disabled"}:
        return False
    return bool(default)


def normalize_homeassistant_entity_scope(value: Any) -> str:
    token = _text(value).lower().replace("-", "_").replace(" ", "_")
    if token in {"assist", "assistant", "exposed", "assist_only", "voice_assistant"}:
        return HOMEASSISTANT_ENTITY_SCOPE_ASSIST
    return HOMEASSISTANT_ENTITY_SCOPE_ALL


def _invalidate_assist_policy_cache() -> None:
    with _ASSIST_POLICY_CACHE_LOCK:
        _ASSIST_POLICY_CACHE.update(
            {
                "base": "",
                "token": "",
                "policy": None,
                "loaded_at": 0.0,
                "retry_at": 0.0,
                "error": "",
            }
        )


def _clear_homeassistant_runtime_states(client: Any) -> int:
    if client is None:
        return 0
    try:
        rows = client.hgetall(HOMEASSISTANT_RUNTIME_STATES_KEY) or {}
    except Exception:
        return 0
    removed = 0
    for key, value in rows.items() if isinstance(rows, dict) else []:
        key_text = _text(key)
        provider = key_text.split(":", 1)[0].lower()
        if provider != "homeassistant":
            try:
                record = json.loads(_text(value))
            except Exception:
                record = {}
            provider = _text(record.get("provider") if isinstance(record, dict) else "").lower()
        if provider != "homeassistant":
            continue
        try:
            removed += int(client.hdel(HOMEASSISTANT_RUNTIME_STATES_KEY, key) or 0)
        except Exception:
            continue
    return removed


def load_homeassistant_config(*, required: bool = False, client: Any = None) -> Dict[str, str]:
    settings = (client or redis_client).hgetall(HOMEASSISTANT_SETTINGS_KEY) or {}
    base = _text(settings.get("HA_BASE_URL") or HOMEASSISTANT_DEFAULT_BASE_URL).rstrip("/")
    token = _text(settings.get("HA_TOKEN"))
    entity_scope = normalize_homeassistant_entity_scope(settings.get("HA_ENTITY_SCOPE"))
    if required and not token:
        raise ValueError(
            "Home Assistant token is not set. Open WebUI -> Settings -> Home Assistant Settings and add HA_TOKEN."
        )
    return {"base": base, "token": token, "entity_scope": entity_scope}


def save_homeassistant_config(
    *,
    base_url: Any = None,
    token: Any = None,
    entity_scope: Any = None,
    client: Any = None,
) -> Dict[str, str]:
    store = client or redis_client
    current = load_homeassistant_config(required=False, client=store)
    next_base = _text(current.get("base") if base_url is None else base_url) or HOMEASSISTANT_DEFAULT_BASE_URL
    next_token = _text(current.get("token") if token is None else token)
    next_entity_scope = normalize_homeassistant_entity_scope(
        current.get("entity_scope") if entity_scope is None else entity_scope
    )
    store.hset(
        HOMEASSISTANT_SETTINGS_KEY,
        mapping={
            "HA_BASE_URL": next_base.rstrip("/"),
            "HA_TOKEN": next_token,
            "HA_ENTITY_SCOPE": next_entity_scope,
        },
    )
    if normalize_homeassistant_entity_scope(current.get("entity_scope")) != next_entity_scope:
        _invalidate_assist_policy_cache()
        _clear_homeassistant_runtime_states(store)
    return load_homeassistant_config(required=False, client=store)


def read_integration_settings() -> Dict[str, Any]:
    config = load_homeassistant_config(required=False)
    return {
        "homeassistant_base_url": config.get("base") or HOMEASSISTANT_DEFAULT_BASE_URL,
        "homeassistant_token": config.get("token", ""),
        "homeassistant_entity_scope": normalize_homeassistant_entity_scope(config.get("entity_scope")),
    }


def save_integration_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    saved = save_homeassistant_config(
        base_url=(payload or {}).get("homeassistant_base_url"),
        token=(payload or {}).get("homeassistant_token"),
        entity_scope=(payload or {}).get("homeassistant_entity_scope"),
    )
    return {
        "homeassistant_base_url": saved.get("base") or HOMEASSISTANT_DEFAULT_BASE_URL,
        "homeassistant_token": saved.get("token", ""),
        "homeassistant_entity_scope": normalize_homeassistant_entity_scope(saved.get("entity_scope")),
    }


def run_integration_action(action_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if _text(action_id) != "test":
        raise KeyError(f"Unsupported Home Assistant action: {action_id}")

    current = read_integration_settings()
    base = _text((payload or {}).get("homeassistant_base_url") or current.get("homeassistant_base_url")).rstrip("/")
    token = _text((payload or {}).get("homeassistant_token") or current.get("homeassistant_token"))
    entity_scope = normalize_homeassistant_entity_scope(
        (payload or {}).get("homeassistant_entity_scope") or current.get("homeassistant_entity_scope")
    )
    if not base:
        raise ValueError("Home Assistant base URL is required.")
    if not token:
        raise ValueError("Home Assistant token is required.")

    response = requests.get(
        f"{base}/api/",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=10,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Home Assistant test failed: HTTP {response.status_code}: {response.text[:200]}")
    if entity_scope == HOMEASSISTANT_ENTITY_SCOPE_ASSIST:
        policy = assist_exposure_policy_sync(base, token, timeout_s=12.0)
        exposed_count = len(policy.get("explicitly_exposed") or set())
        return {
            "ok": True,
            "message": (
                "Home Assistant connection worked and Tater can read the Assist exposure settings "
                f"({exposed_count} explicitly exposed)."
            ),
        }
    return {"ok": True, "message": "Home Assistant connection worked."}


def ws_url(base_url: Any) -> str:
    base = _text(base_url).rstrip("/")
    if base.startswith("https://"):
        return base.replace("https://", "wss://", 1) + "/api/websocket"
    return base.replace("http://", "ws://", 1) + "/api/websocket"


async def _authenticate(ws: Any, token: str, *, timeout_s: float) -> None:
    hello = await ws.receive_json(timeout=timeout_s)
    hello_type = _text(hello.get("type"))
    if hello_type == "auth_required":
        await ws.send_json({"type": "auth", "access_token": token})
        auth = await ws.receive_json(timeout=timeout_s)
        if _text(auth.get("type")) != "auth_ok":
            raise RuntimeError(f"HA websocket auth failed: {auth}")
        return
    if hello_type == "auth_ok":
        return
    raise RuntimeError(f"Unexpected HA websocket hello/auth flow: {hello}")


async def call(base_url: Any, token: Any, payload: Dict[str, Any], *, timeout_s: float = 20.0) -> Any:
    try:
        import aiohttp
    except Exception as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(f"aiohttp is required for Home Assistant websocket calls: {exc}") from exc

    base = _text(base_url).rstrip("/")
    bearer = _text(token)
    if not base:
        raise ValueError("Home Assistant base URL is required.")
    if not bearer:
        raise ValueError("Home Assistant token is required.")

    message = dict(payload or {})
    message_type = _text(message.get("type"))
    if not message_type:
        raise ValueError("Home Assistant websocket payload must include a type.")
    request_id = int(message.get("id") or 1)
    message["id"] = request_id

    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(ws_url(base), heartbeat=30) as ws:
            await _authenticate(ws, bearer, timeout_s=timeout_s)
            await ws.send_json(message)

            loop = asyncio.get_running_loop()
            deadline = loop.time() + float(timeout_s)
            while True:
                remaining = max(0.1, deadline - loop.time())
                if remaining <= 0:
                    break
                msg = await ws.receive(timeout=remaining)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if _text(data.get("type")) != "result" or int(data.get("id") or 0) != request_id:
                        continue
                    if not data.get("success", False):
                        raise RuntimeError(f"HA websocket call failed: {data}")
                    return data.get("result")
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

    raise TimeoutError(f"Timed out waiting for Home Assistant websocket result: {message_type}")


async def entity_registry_list(base_url: Any, token: Any, *, timeout_s: float = 30.0) -> list[dict]:
    result = await call(
        base_url,
        token,
        {"type": "config/entity_registry/list", "id": 1},
        timeout_s=timeout_s,
    )
    return result if isinstance(result, list) else []


async def device_registry_list(base_url: Any, token: Any, *, timeout_s: float = 30.0) -> list[dict]:
    result = await call(
        base_url,
        token,
        {"type": "config/device_registry/list", "id": 1},
        timeout_s=timeout_s,
    )
    return result if isinstance(result, list) else []


async def area_registry_list(base_url: Any, token: Any, *, timeout_s: float = 30.0) -> list[dict]:
    result = await call(
        base_url,
        token,
        {"type": "config/area_registry/list", "id": 1},
        timeout_s=timeout_s,
    )
    return result if isinstance(result, list) else []


async def exposed_entity_list(base_url: Any, token: Any, *, timeout_s: float = 30.0) -> Dict[str, Any]:
    result = await call(
        base_url,
        token,
        {"type": "homeassistant/expose_entity/list", "id": 1},
        timeout_s=timeout_s,
    )
    return result if isinstance(result, dict) else {}


async def expose_new_entities_get(
    base_url: Any,
    token: Any,
    *,
    assistant: str = "conversation",
    timeout_s: float = 30.0,
) -> Dict[str, Any]:
    result = await call(
        base_url,
        token,
        {
            "type": "homeassistant/expose_new_entities/get",
            "id": 1,
            "assistant": _text(assistant) or "conversation",
        },
        timeout_s=timeout_s,
    )
    return result if isinstance(result, dict) else {}


def _registry_entity_map(value: Any) -> Dict[str, Dict[str, Any]]:
    if isinstance(value, dict):
        return {
            _text(entity_id): dict(row)
            for entity_id, row in value.items()
            if _text(entity_id) and isinstance(row, dict)
        }
    if isinstance(value, list):
        return {
            _text(row.get("entity_id")): dict(row)
            for row in value
            if isinstance(row, dict) and _text(row.get("entity_id"))
        }
    return {}


async def assist_exposure_policy(
    base_url: Any,
    token: Any,
    *,
    registry_entities: Any = None,
    timeout_s: float = 30.0,
) -> Dict[str, Any]:
    """Load enough data to mirror Home Assistant's effective Assist exposure."""
    jobs = [
        exposed_entity_list(base_url, token, timeout_s=timeout_s),
        expose_new_entities_get(base_url, token, timeout_s=timeout_s),
    ]
    if registry_entities is None:
        jobs.append(entity_registry_list(base_url, token, timeout_s=timeout_s))
    try:
        results = await asyncio.gather(*jobs)
    except Exception as exc:
        raise RuntimeError(
            "Could not read Home Assistant's Assist exposure settings. "
            "Use a Home Assistant administrator token. "
            f"{exc}"
        ) from exc
    exposed_result = results[0] if isinstance(results[0], dict) else {}
    expose_new_result = results[1] if isinstance(results[1], dict) else {}
    registry_rows = registry_entities if registry_entities is not None else results[2]

    exposed_map = exposed_result.get("exposed_entities")
    if not isinstance(exposed_map, dict):
        exposed_map = {}
    explicitly_exposed = {
        _text(entity_id)
        for entity_id, assistants in exposed_map.items()
        if _text(entity_id)
        and isinstance(assistants, dict)
        and _bool(assistants.get("conversation"), False)
    }
    registry_map = _registry_entity_map(registry_rows)
    explicitly_unexposed: set[str] = set()
    for entity_id, entry in registry_map.items():
        options = entry.get("options") if isinstance(entry.get("options"), dict) else {}
        conversation = options.get("conversation") if isinstance(options.get("conversation"), dict) else {}
        if "should_expose" in conversation and not _bool(conversation.get("should_expose"), False):
            explicitly_unexposed.add(entity_id)

    policy = {
        "assistant": "conversation",
        "expose_new": _bool(expose_new_result.get("expose_new"), True),
        "explicitly_exposed": explicitly_exposed,
        "explicitly_unexposed": explicitly_unexposed,
        "registry_entities": registry_map,
    }
    with _ASSIST_POLICY_CACHE_LOCK:
        _ASSIST_POLICY_CACHE.update(
            {
                "base": _text(base_url).rstrip("/"),
                "token": _text(token),
                "policy": policy,
                "loaded_at": time.monotonic(),
                "retry_at": 0.0,
                "error": "",
            }
        )
    return policy


async def _cached_assist_exposure_policy(
    base_url: Any,
    token: Any,
    *,
    timeout_s: float = 12.0,
) -> Dict[str, Any]:
    base = _text(base_url).rstrip("/")
    bearer = _text(token)
    now = time.monotonic()
    stale_policy: Optional[Dict[str, Any]] = None
    with _ASSIST_POLICY_CACHE_LOCK:
        same_connection = _ASSIST_POLICY_CACHE.get("base") == base and _ASSIST_POLICY_CACHE.get("token") == bearer
        cached = _ASSIST_POLICY_CACHE.get("policy") if same_connection else None
        if isinstance(cached, dict) and cached:
            stale_policy = cached
            if now - float(_ASSIST_POLICY_CACHE.get("loaded_at") or 0.0) < HOMEASSISTANT_ASSIST_POLICY_CACHE_SECONDS:
                return cached
        retry_at = float(_ASSIST_POLICY_CACHE.get("retry_at") or 0.0) if same_connection else 0.0
        cached_error = _text(_ASSIST_POLICY_CACHE.get("error")) if same_connection else ""
    if retry_at > now:
        if stale_policy is not None:
            return stale_policy
        raise RuntimeError(cached_error or "Home Assistant Assist exposure refresh is waiting to retry.")
    try:
        return await assist_exposure_policy(base, bearer, timeout_s=timeout_s)
    except Exception as exc:
        with _ASSIST_POLICY_CACHE_LOCK:
            _ASSIST_POLICY_CACHE.update(
                {
                    "base": base,
                    "token": bearer,
                    "policy": stale_policy,
                    "retry_at": now + HOMEASSISTANT_ASSIST_POLICY_CACHE_SECONDS,
                    "error": str(exc),
                }
            )
        if stale_policy is not None:
            return stale_policy
        raise


def _homeassistant_default_assist_exposure(
    entity_id: Any,
    attributes: Optional[Dict[str, Any]] = None,
    registry_entry: Optional[Dict[str, Any]] = None,
) -> bool:
    entity = _text(entity_id)
    if not entity:
        return False
    entry = registry_entry if isinstance(registry_entry, dict) else {}
    if entry and (entry.get("entity_category") not in (None, "") or entry.get("hidden_by") not in (None, "")):
        return False
    domain = entity.split(".", 1)[0] if "." in entity else ""
    if domain in HOMEASSISTANT_ASSIST_DEFAULT_DOMAINS:
        return True
    attrs = attributes if isinstance(attributes, dict) else {}
    device_class = _text(
        attrs.get("device_class")
        or entry.get("device_class")
        or entry.get("original_device_class")
    ).lower()
    if domain == "binary_sensor":
        return device_class in HOMEASSISTANT_ASSIST_DEFAULT_BINARY_SENSOR_DEVICE_CLASSES
    if domain == "sensor":
        return device_class in HOMEASSISTANT_ASSIST_DEFAULT_SENSOR_DEVICE_CLASSES
    return False


def homeassistant_entity_in_scope(
    entity_id: Any,
    attributes: Optional[Dict[str, Any]] = None,
    *,
    config: Optional[Dict[str, Any]] = None,
    assist_policy: Optional[Dict[str, Any]] = None,
) -> bool:
    conf = config if isinstance(config, dict) else {}
    if normalize_homeassistant_entity_scope(conf.get("entity_scope")) != HOMEASSISTANT_ENTITY_SCOPE_ASSIST:
        return True
    entity = _text(entity_id)
    policy = assist_policy if isinstance(assist_policy, dict) else {}
    if not entity or not policy:
        return False
    explicitly_unexposed = policy.get("explicitly_unexposed") or set()
    if entity in explicitly_unexposed:
        return False
    explicitly_exposed = policy.get("explicitly_exposed") or set()
    if entity in explicitly_exposed:
        return True
    if not _bool(policy.get("expose_new"), True):
        return False
    registry_entities = policy.get("registry_entities") if isinstance(policy.get("registry_entities"), dict) else {}
    return _homeassistant_default_assist_exposure(
        entity,
        attributes,
        registry_entities.get(entity),
    )


def _homeassistant_runtime_entity(payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    row = payload if isinstance(payload, dict) else {}
    new_state = row.get("new_state") if isinstance(row.get("new_state"), dict) else {}
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    entity_id = _text(
        row.get("entity_id")
        or row.get("ref")
        or new_state.get("entity_id")
        or raw.get("entity_id")
    )
    attributes = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
    if not attributes and isinstance(new_state.get("attributes"), dict):
        attributes = new_state.get("attributes")
    if not attributes and isinstance(raw.get("attributes"), dict):
        attributes = raw.get("attributes")
    return entity_id, attributes


async def integration_runtime_event_allowed(
    *,
    kind: Any,
    payload: Dict[str, Any],
    client: Any = None,
) -> bool:
    """Filter live runtime data through the integration-owned entity scope."""
    del kind
    config = load_homeassistant_config(required=False, client=client)
    if normalize_homeassistant_entity_scope(config.get("entity_scope")) != HOMEASSISTANT_ENTITY_SCOPE_ASSIST:
        return True
    entity_id, attributes = _homeassistant_runtime_entity(payload)
    if not entity_id:
        return False
    policy = await _cached_assist_exposure_policy(
        config.get("base"),
        config.get("token"),
    )
    return homeassistant_entity_in_scope(
        entity_id,
        attributes,
        config=config,
        assist_policy=policy,
    )


async def call_service(
    base_url: Any,
    token: Any,
    *,
    domain: str,
    service: str,
    service_data: Optional[Dict[str, Any]] = None,
    target: Optional[Dict[str, Any]] = None,
    return_response: bool = False,
    timeout_s: float = 20.0,
) -> Any:
    payload: Dict[str, Any] = {
        "type": "call_service",
        "id": 1,
        "domain": _text(domain),
        "service": _text(service),
        "service_data": dict(service_data or {}),
    }
    if isinstance(target, dict) and target:
        payload["target"] = dict(target)
    if return_response:
        payload["return_response"] = True
    return await call(base_url, token, payload, timeout_s=timeout_s)


def _run_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: Dict[str, Any] = {}
    error: Dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - handoff guard
            error["exc"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "exc" in error:
        raise error["exc"]
    return result.get("value")


def entity_registry_list_sync(base_url: Any, token: Any, *, timeout_s: float = 30.0) -> list[dict]:
    return _run_sync(
        entity_registry_list(base_url, token, timeout_s=timeout_s)
    )


def device_registry_list_sync(base_url: Any, token: Any, *, timeout_s: float = 30.0) -> list[dict]:
    return _run_sync(
        device_registry_list(base_url, token, timeout_s=timeout_s)
    )


def area_registry_list_sync(base_url: Any, token: Any, *, timeout_s: float = 30.0) -> list[dict]:
    return _run_sync(
        area_registry_list(base_url, token, timeout_s=timeout_s)
    )


def exposed_entity_list_sync(base_url: Any, token: Any, *, timeout_s: float = 30.0) -> Dict[str, Any]:
    return _run_sync(exposed_entity_list(base_url, token, timeout_s=timeout_s))


def expose_new_entities_get_sync(
    base_url: Any,
    token: Any,
    *,
    assistant: str = "conversation",
    timeout_s: float = 30.0,
) -> Dict[str, Any]:
    return _run_sync(
        expose_new_entities_get(
            base_url,
            token,
            assistant=assistant,
            timeout_s=timeout_s,
        )
    )


def assist_exposure_policy_sync(
    base_url: Any,
    token: Any,
    *,
    registry_entities: Any = None,
    timeout_s: float = 30.0,
) -> Dict[str, Any]:
    return _run_sync(
        assist_exposure_policy(
            base_url,
            token,
            registry_entities=registry_entities,
            timeout_s=timeout_s,
        )
    )


def _homeassistant_rest_get(base_url: Any, token: Any, path: str, *, timeout_s: float = 20.0) -> Any:
    base = _text(base_url).rstrip("/")
    bearer = _text(token)
    if not base or not bearer:
        return []
    response = requests.get(
        f"{base}{path if _text(path).startswith('/') else '/' + _text(path)}",
        headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"},
        timeout=max(5.0, float(timeout_s or 20.0)),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Home Assistant HTTP {response.status_code}: {response.text[:200]}")
    try:
        return response.json()
    except Exception:
        return []


_HOMEASSISTANT_APPLIANCE_KIND_TERMS: Dict[str, tuple[str, ...]] = {
    "washer": ("washer", "washing machine", "clothes washer"),
    "dryer": ("dryer", "tumble dryer", "clothes dryer"),
    "dishwasher": ("dishwasher", "dish washer"),
}


def _homeassistant_semantic_text(*values: Any) -> str:
    return " ".join(
        token
        for token in (
            re.sub(r"[^a-z0-9]+", " ", _text(value).lower()).strip()
            for value in values
        )
        if token
    )


def _homeassistant_registry_rows(
    entity_id: str,
    registry_context: Optional[Dict[str, Dict[str, Any]]] = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    context = registry_context if isinstance(registry_context, dict) else {}
    entity = (context.get("entities") or {}).get(entity_id) or {}
    device = (context.get("devices") or {}).get(_text(entity.get("device_id"))) or {}
    return entity, device


def _homeassistant_entity_semantics(
    entity_id: str,
    attrs: Dict[str, Any],
    registry_context: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, str]:
    """Return provider-neutral appliance and property metadata for one HA entity."""
    entity, device = _homeassistant_registry_rows(entity_id, registry_context)
    property_text = _homeassistant_semantic_text(
        entity_id.split(".", 1)[-1],
        attrs.get("friendly_name"),
        entity.get("name"),
        entity.get("original_name"),
        entity.get("translation_key"),
    )
    appliance_text = _homeassistant_semantic_text(
        property_text,
        device.get("name_by_user"),
        device.get("name"),
        device.get("model"),
        device.get("model_id"),
    )

    appliance_type = ""
    for candidate, terms in _HOMEASSISTANT_APPLIANCE_KIND_TERMS.items():
        if any(re.search(rf"\b{re.escape(term)}\b", appliance_text) for term in terms):
            appliance_type = candidate
            break

    entity_property = ""
    if (
        ("time" in property_text and re.search(r"\b(remain(?:ing)?|left)\b", property_text))
        or re.search(r"\b(completion|finish|end) time\b", property_text)
    ):
        entity_property = "remaining_time"
    elif re.search(
        r"\b(current status|current state|cycle status|cycle state|machine status|machine state|operation status|operation state)\b",
        property_text,
    ):
        entity_property = "current_status"
    elif re.search(r"\b(progress|percent complete|percentage complete)\b", property_text):
        entity_property = "progress"
    elif re.search(r"\bcycle\b", property_text):
        entity_property = "cycle"

    return {
        "appliance_type": appliance_type,
        "entity_property": entity_property,
        "parent_device_id": _text(entity.get("device_id")),
        "parent_device_name": _text(device.get("name_by_user") or device.get("name")),
        "manufacturer": _text(device.get("manufacturer")),
        "model": _text(device.get("model") or device.get("model_id")),
        "entity_platform": _text(entity.get("platform")),
        "entity_original_name": _text(entity.get("original_name") or entity.get("name")),
    }


def _homeassistant_entity_details(
    row: Dict[str, Any],
    *,
    entity_id: str = "",
    registry_context: Optional[Dict[str, Dict[str, Any]]] = None,
    related_entity_ids: Optional[list[str]] = None,
) -> Dict[str, Any]:
    attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
    entity, _device = _homeassistant_registry_rows(entity_id, registry_context)
    details: Dict[str, Any] = {}
    for key in (
        "unit_of_measurement",
        "device_class",
        "state_class",
        "options",
        "battery_level",
        "last_changed",
        "last_updated",
        "supported_features",
    ):
        value = attrs.get(key) if key in attrs else row.get(key)
        if key == "device_class" and value in (None, ""):
            value = entity.get("device_class") or entity.get("original_device_class")
        if value not in (None, ""):
            details[key] = value
    for key in ("temperature", "humidity", "illuminance", "pressure", "voltage", "power", "energy"):
        value = attrs.get(key)
        if value not in (None, ""):
            details[key] = value
    semantics = _homeassistant_entity_semantics(entity_id, attrs, registry_context)
    semantic_keys = {
        "appliance_type": "appliance_type",
        "entity_property": "entity_property",
        "parent_device_id": "homeassistant_device_id",
        "parent_device_name": "parent_device_name",
        "manufacturer": "manufacturer",
        "model": "model",
        "entity_platform": "homeassistant_platform",
        "entity_original_name": "entity_original_name",
    }
    for source_key, target_key in semantic_keys.items():
        value = semantics.get(source_key)
        if value:
            details[target_key] = value
    related = [_text(value) for value in (related_entity_ids or []) if _text(value)]
    if related:
        details["related_entity_ids"] = related
    return details


def _homeassistant_state_options(attrs: Dict[str, Any]) -> list[str]:
    raw = attrs.get("options")
    if not isinstance(raw, (list, tuple, set)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in raw:
        token = _text(value)
        folded = token.casefold()
        if not token or folded in seen:
            continue
        seen.add(folded)
        out.append(token)
    return out


def _homeassistant_state_is_numeric(state: Any, attrs: Dict[str, Any]) -> bool:
    if _text(attrs.get("unit_of_measurement")):
        return True
    if _text(state).lower() in {"", "unknown", "unavailable", "none"}:
        return False
    try:
        float(state)
        return True
    except (TypeError, ValueError):
        return False


def _homeassistant_entity_event_sources(
    entity_id: str,
    state: Any,
    attrs: Dict[str, Any],
) -> list[Dict[str, Any]]:
    domain = entity_id.split(".", 1)[0] if "." in entity_id else "entity"
    device_class = _text(attrs.get("device_class")).lower()
    source_type = ""
    trigger_events: list[str] = []
    state_on = ""
    state_off = ""

    if domain == "binary_sensor":
        source_type = device_class or "binary"
        state_on, state_off = "on", "off"
        if device_class == "motion":
            trigger_events = ["motion"]
        elif device_class in {"door", "window", "opening", "garage_door"}:
            trigger_events = ["opens", "closes"]
        elif device_class == "connectivity":
            trigger_events = ["connects", "disconnects"]
        else:
            trigger_events = ["turns_on", "turns_off"]
    elif domain in {"switch", "input_boolean", "light", "fan", "automation"}:
        source_type = domain
        trigger_events = ["turns_on", "turns_off"]
        state_on, state_off = "on", "off"
    elif domain == "cover":
        source_type = device_class or "cover"
        trigger_events = ["opens", "closes"]
        state_on, state_off = "open", "closed"
    elif domain == "sensor":
        if _homeassistant_state_is_numeric(state, attrs):
            source_type = device_class or "measurement"
            trigger_events = ["changed", "above", "below"]
        else:
            source_type = device_class or "state"
            trigger_events = ["changed", "equals"]
    elif domain in {"select", "input_select"}:
        source_type = "state"
        trigger_events = ["changed", "equals"]
    elif domain in {
        "alarm_control_panel",
        "climate",
        "device_tracker",
        "humidifier",
        "lock",
        "media_player",
        "person",
        "vacuum",
        "water_heater",
    }:
        source_type = domain
        trigger_events = ["changed", "equals"]
    elif domain == "event":
        source_type = "event"
        trigger_events = ["changed"]

    if not source_type or not trigger_events:
        return []
    source: Dict[str, Any] = {
        "type": source_type,
        "ref": entity_id,
        "trigger_events": trigger_events,
    }
    if state_on:
        source["state_on"] = state_on
    if state_off:
        source["state_off"] = state_off
    state_options = _homeassistant_state_options(attrs)
    if state_options:
        source["state_options"] = state_options
    unit = _text(attrs.get("unit_of_measurement"))
    if unit:
        source["unit"] = unit
    return [source]


def _homeassistant_camera_supports_clip(attrs: Dict[str, Any]) -> bool:
    try:
        supported_features = int(float(attrs.get("supported_features") or 0))
    except (TypeError, ValueError):
        supported_features = 0
    return bool(supported_features & HOMEASSISTANT_CAMERA_STREAM_FEATURE) and bool(shutil.which("ffmpeg"))


def _homeassistant_entity_capabilities(
    entity_id: str,
    attrs: Dict[str, Any],
    registry_context: Optional[Dict[str, Dict[str, Any]]] = None,
) -> list[str]:
    domain = entity_id.split(".", 1)[0] if "." in entity_id else "entity"
    entity, _device = _homeassistant_registry_rows(entity_id, registry_context)
    device_class = _text(
        attrs.get("device_class")
        or entity.get("device_class")
        or entity.get("original_device_class")
    ).lower()
    unit = _text(attrs.get("unit_of_measurement")).lower()
    name_hint = f"{entity_id} {_text(attrs.get('friendly_name'))}".lower()
    caps = [domain]

    def add(token: str) -> None:
        if token and token not in caps:
            caps.append(token)

    if domain == "camera":
        add("camera")
        add("snapshot")
        if _homeassistant_camera_supports_clip(attrs):
            add("video_clip")
    if domain == "light":
        add("switch")
        supported = attrs.get("supported_color_modes")
        supported_text = " ".join(_text(item).lower() for item in supported) if isinstance(supported, list) else _text(supported).lower()
        if any(token in supported_text for token in ("brightness", "color_temp", "xy", "rgb", "hs")):
            add("dimmable")
        if any(token in supported_text for token in ("xy", "rgb", "hs")):
            add("color")
    if domain in {"switch", "input_boolean"}:
        add("switch")
        if any(token in name_hint for token in ("plug", "outlet")):
            add("plug")
    if domain == "fan":
        add("fan")
        add("switch")
    if domain == "cover":
        add("cover")
        add("open_close")
        if device_class == "garage" or "garage" in name_hint:
            add("garage_door")
    if domain == "climate":
        add("climate")
        add("temperature")
    if domain == "lock":
        add("lock")
    if domain == "media_player":
        add("media_player")
    if domain == "remote":
        add("remote")
    if domain == "scene":
        add("scene")
    if domain == "script":
        add("script")
    if domain in {"binary_sensor", "sensor", "cover"}:
        if device_class in {"door", "garage_door", "window", "opening"} or any(
            token in name_hint for token in ("door", "window", "garage", "contact", "opening")
        ):
            add("entry_sensor")
        if device_class == "garage_door" or "garage" in name_hint:
            add("garage_door")
        if device_class == "motion" or "motion" in name_hint:
            add("motion")
        if device_class == "temperature" or unit in {"°c", "°f", "c", "f"} or "temp" in name_hint:
            add("temperature")
        if device_class == "humidity" or "humidity" in name_hint:
            add("humidity")
        if device_class in {"illuminance", "light"} or unit in {"lx", "lux"} or any(
            token in name_hint for token in ("illuminance", "lux", "light level")
        ):
            add("illuminance")
        if device_class in {"power", "energy", "voltage", "current"} or unit in {"w", "kw", "kwh", "v", "a"}:
            add("energy")
        if device_class in {"battery"} or "battery" in name_hint:
            add("battery")
        if device_class in {"moisture", "water"} or any(token in name_hint for token in ("leak", "water", "flood", "moisture")):
            add("leak")
    if any(token in name_hint for token in ("doorbell", "ring", "button", "press")):
        add("doorbell")
    semantics = _homeassistant_entity_semantics(entity_id, attrs, registry_context)
    appliance_type = _text(semantics.get("appliance_type"))
    entity_property = _text(semantics.get("entity_property"))
    if appliance_type:
        add("appliance")
        add(appliance_type)
    if entity_property:
        add(entity_property)
    return caps


def _homeassistant_entity_actions(entity_id: str, capabilities: list[str]) -> list[str]:
    domain = entity_id.split(".", 1)[0] if "." in entity_id else "entity"
    caps = {str(item or "").strip().lower() for item in capabilities or [] if str(item or "").strip()}
    if domain == "light":
        actions = ["turn_on", "turn_off", "toggle"]
        if "dimmable" in caps:
            actions.append("set_brightness")
        if "color" in caps:
            actions.append("set_color")
        return actions
    if domain in {"switch", "input_boolean"}:
        return ["turn_on", "turn_off", "toggle"]
    if domain == "fan":
        return ["turn_on", "turn_off", "toggle", "set_percentage"]
    if domain == "cover":
        return ["open", "close", "stop", "set_position"]
    if domain == "lock":
        return ["lock", "unlock"]
    if domain == "climate":
        return ["set_temperature", "set_hvac_mode"]
    if domain == "media_player":
        return [
            "play",
            "pause",
            "playpause",
            "stop",
            "next",
            "previous",
            "set_volume",
            "volume_up",
            "volume_down",
            "mute",
            "unmute",
            "play_media",
            "announce",
        ]
    if domain == "remote":
        return ["turn_on", "turn_off", "send_command"]
    if domain == "scene":
        return ["activate"]
    if domain == "script":
        return ["run"]
    if domain == "camera" or "snapshot" in caps:
        actions = ["camera_snapshot"]
        if "video_clip" in caps:
            actions.append("camera_clip")
        return actions
    return []


def _homeassistant_registry_context(
    base: str,
    token: str,
    *,
    require_entities: bool = False,
) -> Dict[str, Dict[str, Any]]:
    context: Dict[str, Dict[str, Any]] = {
        "entities": {},
        "devices": {},
        "areas": {},
    }
    try:
        areas = area_registry_list_sync(base, token, timeout_s=8.0)
        context["areas"] = {
            _text(row.get("area_id") or row.get("id")): dict(row)
            for row in areas
            if isinstance(row, dict) and _text(row.get("area_id") or row.get("id"))
        }
    except Exception:
        pass
    try:
        devices = device_registry_list_sync(base, token, timeout_s=8.0)
        context["devices"] = {
            _text(row.get("id")): dict(row)
            for row in devices
            if isinstance(row, dict) and _text(row.get("id"))
        }
    except Exception:
        pass
    try:
        entities = entity_registry_list_sync(base, token, timeout_s=8.0)
        context["entities"] = {
            _text(row.get("entity_id")): dict(row)
            for row in entities
            if isinstance(row, dict) and _text(row.get("entity_id"))
        }
    except Exception:
        if require_entities:
            raise
    return context


def _homeassistant_entity_room(entity_id: str, attrs: Dict[str, Any], registry_context: Dict[str, Dict[str, Any]]) -> str:
    entity = (registry_context.get("entities") or {}).get(entity_id) or {}
    devices = registry_context.get("devices") or {}
    areas = registry_context.get("areas") or {}
    device = devices.get(_text(entity.get("device_id"))) or {}
    area_id = (
        _text(entity.get("area_id"))
        or _text(device.get("area_id"))
        or _text(attrs.get("area_id"))
        or _text(attrs.get("room"))
        or _text(attrs.get("area"))
    )
    if not area_id:
        return ""
    area = areas.get(area_id) or {}
    return _text(area.get("name")) or area_id.replace("_", " ").title()


def integration_devices() -> Dict[str, Any]:
    config = load_homeassistant_config(required=False)
    base = _text(config.get("base"))
    token = _text(config.get("token"))
    if not token:
        return {"devices": [], "message": "Home Assistant is not configured."}
    states = _homeassistant_rest_get(base, token, "/api/states", timeout_s=20.0)
    entity_scope = normalize_homeassistant_entity_scope(config.get("entity_scope"))
    try:
        registry_context = _homeassistant_registry_context(
            base,
            token,
            require_entities=entity_scope == HOMEASSISTANT_ENTITY_SCOPE_ASSIST,
        )
    except Exception as exc:
        message = (
            "Tater could not read Home Assistant's entity and Assist exposure settings. "
            "Assist-only mode requires a Home Assistant administrator token. "
            f"{exc}"
        )
        return {"devices": [], "error": message, "message": message}
    assist_policy: Optional[Dict[str, Any]] = None
    if entity_scope == HOMEASSISTANT_ENTITY_SCOPE_ASSIST:
        try:
            assist_policy = assist_exposure_policy_sync(
                base,
                token,
                registry_entities=registry_context.get("entities"),
                timeout_s=12.0,
            )
        except Exception as exc:
            message = (
                "Tater could not read Home Assistant's Assist exposure settings. "
                "Assist-only mode requires a Home Assistant administrator token. "
                f"{exc}"
            )
            return {"devices": [], "error": message, "message": message}
    devices: list[dict] = []
    visible_rows: list[tuple[Dict[str, Any], str, Dict[str, Any]]] = []
    state_count = 0
    for row in states if isinstance(states, list) else []:
        if not isinstance(row, dict):
            continue
        entity_id = _text(row.get("entity_id"))
        if not entity_id:
            continue
        attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
        state_count += 1
        if not homeassistant_entity_in_scope(
            entity_id,
            attrs,
            config=config,
            assist_policy=assist_policy,
        ):
            continue
        visible_rows.append((row, entity_id, attrs))

    related_by_device: Dict[str, list[str]] = {}
    for _row, entity_id, _attrs in visible_rows:
        registry_entity, _registry_device = _homeassistant_registry_rows(entity_id, registry_context)
        parent_device_id = _text(registry_entity.get("device_id"))
        if parent_device_id:
            related_by_device.setdefault(parent_device_id, []).append(entity_id)

    for row, entity_id, attrs in visible_rows:
        domain = entity_id.split(".", 1)[0] if "." in entity_id else "entity"
        state = _text(row.get("state"))
        registry_entity, registry_device = _homeassistant_registry_rows(entity_id, registry_context)
        parent_device_id = _text(registry_entity.get("device_id"))
        capabilities = _homeassistant_entity_capabilities(entity_id, attrs, registry_context)
        actions = _homeassistant_entity_actions(entity_id, capabilities)
        room = _homeassistant_entity_room(entity_id, attrs, registry_context)
        aliases: list[str] = []
        seen_aliases: set[str] = set()
        for value in (
            registry_device.get("name_by_user"),
            registry_device.get("name"),
            registry_entity.get("name"),
            registry_entity.get("original_name"),
        ):
            alias = _text(value)
            alias_key = alias.casefold()
            if alias and alias_key not in seen_aliases:
                seen_aliases.add(alias_key)
                aliases.append(alias)
        devices.append(
            {
                "id": entity_id,
                "name": _text(attrs.get("friendly_name")) or entity_id,
                "aliases": aliases,
                "type": domain,
                "ref": entity_id,
                "capabilities": capabilities,
                "actions": actions,
                "features": actions,
                "event_sources": _homeassistant_entity_event_sources(entity_id, state, attrs),
                "room": room,
                "area": room,
                "state": state,
                "status": state,
                "details": _homeassistant_entity_details(
                    row,
                    entity_id=entity_id,
                    registry_context=registry_context,
                    related_entity_ids=related_by_device.get(parent_device_id) or [],
                ),
            }
        )
    if entity_scope == HOMEASSISTANT_ENTITY_SCOPE_ASSIST:
        return {
            "devices": devices,
            "message": (
                f"Home Assistant returned {len(devices)} of {state_count} current entities exposed to Assist."
            ),
        }
    return {"devices": devices, "message": f"Home Assistant returned {len(devices)} current entities."}


def get_camera_snapshot(device_id: Any) -> tuple[bytes, str]:
    config = load_homeassistant_config(required=True)
    entity_id = _text(device_id)
    if not entity_id:
        raise ValueError("Home Assistant camera entity is required.")
    response = requests.get(
        f"{config['base']}/api/camera_proxy/{quote(entity_id, safe='')}",
        headers={"Authorization": f"Bearer {config['token']}"},
        timeout=12,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Home Assistant camera snapshot failed: HTTP {response.status_code}: {response.text[:200]}")
    content_type = _text(response.headers.get("Content-Type")) or "image/jpeg"
    return response.content, content_type.split(";", 1)[0].strip() or "image/jpeg"


def _homeassistant_camera_stream_url(device_id: Any) -> str:
    config = load_homeassistant_config(required=True)
    entity_id = _text(device_id)
    if not entity_id.startswith("camera."):
        raise ValueError("Home Assistant camera entity is required.")
    result = _run_sync(
        call(
            config["base"],
            config["token"],
            {"type": "camera/stream", "id": 1, "entity_id": entity_id, "format": "hls"},
            timeout_s=30.0,
        )
    )
    raw_url = _text(result.get("url")) if isinstance(result, dict) else ""
    if not raw_url:
        raise RuntimeError(f"Home Assistant did not provide an HLS stream for {entity_id}.")
    return urljoin(f"{config['base'].rstrip('/')}/", raw_url)


def _homeassistant_clip_duration(payload: Dict[str, Any]) -> int:
    try:
        value = int(float((payload or {}).get("duration_seconds") or 8))
    except (TypeError, ValueError):
        value = 8
    return max(1, min(HOMEASSISTANT_CAMERA_CLIP_MAX_SECONDS, value))


def get_camera_clip(device_id: Any, payload: Optional[Dict[str, Any]] = None) -> tuple[bytes, str, Dict[str, Any]]:
    entity_id = _text(device_id)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to capture Home Assistant camera clips.")
    duration = _homeassistant_clip_duration(dict(payload or {}))
    stream_url = _homeassistant_camera_stream_url(entity_id)
    fd, output_path = tempfile.mkstemp(prefix="tater-ha-camera-", suffix=".mp4")
    os.close(fd)
    try:
        copy_command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            stream_url,
            "-t",
            str(duration),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
            "-movflags",
            "+faststart",
            output_path,
        ]
        completed = subprocess.run(
            copy_command,
            capture_output=True,
            text=True,
            timeout=duration + 35,
            check=False,
        )
        if completed.returncode != 0 or not Path(output_path).is_file() or Path(output_path).stat().st_size < 1000:
            transcode_command = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                stream_url,
                "-t",
                str(duration),
                "-map",
                "0:v:0",
                "-an",
                "-r",
                "4",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "28",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                output_path,
            ]
            completed = subprocess.run(
                transcode_command,
                capture_output=True,
                text=True,
                timeout=duration + 45,
                check=False,
            )
        if completed.returncode != 0:
            error = _text(completed.stderr)[-400:]
            raise RuntimeError(f"Home Assistant camera clip capture failed: {error or 'ffmpeg failed'}")
        content = Path(output_path).read_bytes()
        if len(content) < 1000:
            raise RuntimeError("Home Assistant camera clip capture returned an empty video.")
        return content, "video/mp4", {"duration_seconds": duration, "source": "homeassistant_hls"}
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


def run_integration_device_action(action_id: str, device_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    action = _text(action_id).lower()
    entity_id = _text(device_id)
    if action in {"camera_snapshot", "snapshot"}:
        content, content_type = get_camera_snapshot(entity_id)
        return {"ok": True, "bytes": content, "content_type": content_type}
    if action in {"camera_clip", "video_clip", "clip"}:
        content, content_type, metadata = get_camera_clip(entity_id, payload)
        return {"ok": True, "bytes": content, "content_type": content_type, **metadata}

    if "." not in entity_id:
        raise ValueError("Home Assistant entity id is required.")
    domain = entity_id.split(".", 1)[0]
    service = ""
    service_data: Dict[str, Any] = {}
    target = {"entity_id": entity_id}
    payload = dict(payload or {})

    if action in {"turn_on", "on"}:
        service = "turn_on"
    elif action in {"turn_off", "off"}:
        service = "turn_off"
    elif action == "toggle":
        service = "toggle"
    elif action == "set_brightness":
        service = "turn_on"
        brightness = payload.get("brightness_pct", payload.get("brightness"))
        if brightness is None:
            raise ValueError("Home Assistant brightness action requires brightness_pct.")
        service_data["brightness_pct"] = int(max(0, min(100, round(float(brightness)))))
    elif action == "set_color":
        service = "turn_on"
        color_name = _text(payload.get("color_name"))
        rgb_color = payload.get("rgb_color")
        xy_color = payload.get("xy_color")
        if color_name:
            service_data["color_name"] = color_name
        if isinstance(rgb_color, list) and len(rgb_color) == 3:
            service_data["rgb_color"] = [int(max(0, min(255, round(float(item))))) for item in rgb_color]
        if isinstance(xy_color, list) and len(xy_color) == 2:
            service_data["xy_color"] = [float(xy_color[0]), float(xy_color[1])]
        if not service_data:
            raise ValueError("Home Assistant color action requires color_name, rgb_color, or xy_color.")
    elif action == "open":
        service = "open_cover"
        domain = "cover"
    elif action == "close":
        service = "close_cover"
        domain = "cover"
    elif action == "stop":
        service = "stop_cover"
        domain = "cover"
    elif action == "set_position":
        service = "set_cover_position"
        domain = "cover"
        position = payload.get("position")
        if position is None:
            raise ValueError("Home Assistant cover position action requires position.")
        service_data["position"] = int(max(0, min(100, round(float(position)))))
    elif action == "set_percentage":
        service = "set_percentage"
        domain = "fan"
        percentage = payload.get("percentage", payload.get("speed", payload.get("speed_pct")))
        if percentage is None:
            raise ValueError("Home Assistant fan speed action requires percentage.")
        service_data["percentage"] = int(max(0, min(100, round(float(percentage)))))
    elif action in {"lock", "unlock"}:
        service = action
        domain = "lock"
    elif action == "activate":
        service = "turn_on"
        domain = "scene"
    elif action == "run":
        service = "turn_on"
        domain = "script"
    elif action == "send_command":
        service = "send_command"
        domain = "remote"
        command = _text(payload.get("command"))
        if not command:
            raise ValueError("Home Assistant remote action requires command.")
        service_data["command"] = command
    elif action in {"set_temperature", "set_target_temperature", "target_temperature"}:
        service = "set_temperature"
        domain = "climate"
        temperature = payload.get("temperature", payload.get("target_temperature"))
        if temperature is None:
            raise ValueError("Home Assistant climate temperature action requires temperature.")
        service_data["temperature"] = float(temperature)
        hvac_mode = _text(payload.get("hvac_mode") or payload.get("mode"))
        if hvac_mode:
            service_data["hvac_mode"] = hvac_mode
    elif action in {"set_hvac_mode", "set_mode", "hvac_mode"}:
        service = "set_hvac_mode"
        domain = "climate"
        hvac_mode = _text(payload.get("hvac_mode") or payload.get("mode"))
        if not hvac_mode:
            raise ValueError("Home Assistant climate mode action requires hvac_mode.")
        service_data["hvac_mode"] = hvac_mode
    elif action in {"play", "media_play", "resume"}:
        service = "media_play"
        domain = "media_player"
    elif action in {"pause", "media_pause"}:
        service = "media_pause"
        domain = "media_player"
    elif action in {"playpause", "play_pause", "media_play_pause"}:
        service = "media_play_pause"
        domain = "media_player"
    elif action in {"stop", "media_stop"} and domain == "media_player":
        service = "media_stop"
        domain = "media_player"
    elif action in {"next", "media_next_track"}:
        service = "media_next_track"
        domain = "media_player"
    elif action in {"previous", "prev", "media_previous_track"}:
        service = "media_previous_track"
        domain = "media_player"
    elif action == "set_volume":
        service = "volume_set"
        domain = "media_player"
        volume = payload.get("volume", payload.get("volume_pct", payload.get("level")))
        if volume is None:
            raise ValueError("Home Assistant media volume action requires volume.")
        service_data["volume_level"] = max(0.0, min(1.0, float(volume) / 100.0))
    elif action == "volume_up":
        service = "volume_up"
        domain = "media_player"
    elif action == "volume_down":
        service = "volume_down"
        domain = "media_player"
    elif action in {"mute", "unmute"}:
        service = "volume_mute"
        domain = "media_player"
        service_data["is_volume_muted"] = action == "mute"
    elif action in {"play_media", "play_url", "announce"}:
        service = "play_media"
        domain = "media_player"
        media_content_id = _text(
            payload.get("media_content_id")
            or payload.get("source_url")
            or payload.get("url")
            or payload.get("media_url")
        )
        if not media_content_id:
            raise ValueError("Home Assistant media playback requires media_content_id or source_url.")
        service_data["media_content_id"] = media_content_id
        service_data["media_content_type"] = _text(payload.get("media_content_type") or payload.get("media_type") or "music")
        if action == "announce":
            service_data["announce"] = True
    else:
        raise KeyError(f"Unsupported Home Assistant device action: {action_id}")

    config = load_homeassistant_config(required=True)
    result = call_service_sync(
        config["base"],
        config["token"],
        domain=domain,
        service=service,
        service_data=service_data,
        target=target,
        timeout_s=20.0,
    )
    return {
        "ok": True,
        "action": action,
        "device_id": entity_id,
        "domain": domain,
        "service": service,
        "service_data": service_data,
        "result": result,
    }


def call_service_sync(
    base_url: Any,
    token: Any,
    *,
    domain: str,
    service: str,
    service_data: Optional[Dict[str, Any]] = None,
    target: Optional[Dict[str, Any]] = None,
    return_response: bool = False,
    timeout_s: float = 20.0,
) -> Any:
    return _run_sync(
        call_service(
            base_url,
            token,
            domain=domain,
            service=service,
            service_data=service_data,
            target=target,
            return_response=return_response,
            timeout_s=timeout_s,
        )
    )


def upload_local_media_source_file_sync(
    base_url: Any,
    token: Any,
    *,
    target_media_content_id: str,
    filename: str,
    content: bytes,
    content_type: str = "audio/wav",
    timeout_s: float = 60.0,
) -> str:
    base = _text(base_url).rstrip("/")
    bearer = _text(token)
    media_target = _text(target_media_content_id)
    file_name = _text(filename) or "audio.wav"
    payload = bytes(content or b"")
    if not base:
        raise ValueError("Home Assistant base URL is required.")
    if not bearer:
        raise ValueError("Home Assistant token is required.")
    if not media_target:
        raise ValueError("Home Assistant target media folder is required.")
    if not payload:
        raise ValueError("Media upload content is empty.")

    response = requests.post(
        f"{base}/api/media_source/local_source/upload",
        headers={"Authorization": f"Bearer {bearer}"},
        data={"media_content_id": media_target},
        files={"file": (file_name, io.BytesIO(payload), _text(content_type) or "audio/wav")},
        timeout=max(5.0, float(timeout_s)),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"HA media upload failed: HTTP {response.status_code}: {response.text}")
    try:
        parsed = response.json()
    except Exception as exc:
        raise RuntimeError(f"HA media upload returned invalid JSON: {exc}") from exc

    media_content_id = _text(parsed.get("media_content_id"))
    if not media_content_id:
        raise RuntimeError("HA media upload succeeded but returned no media_content_id.")
    return media_content_id


async def remove_local_media_source(
    base_url: Any,
    token: Any,
    *,
    media_content_id: str,
    timeout_s: float = 20.0,
) -> Any:
    return await call(
        base_url,
        token,
        {
            "type": "media_source/local_source/remove",
            "id": 1,
            "media_content_id": _text(media_content_id),
        },
        timeout_s=timeout_s,
    )


def remove_local_media_source_sync(
    base_url: Any,
    token: Any,
    *,
    media_content_id: str,
    timeout_s: float = 20.0,
) -> Any:
    return _run_sync(
        remove_local_media_source(
            base_url,
            token,
            media_content_id=media_content_id,
            timeout_s=timeout_s,
        )
    )
