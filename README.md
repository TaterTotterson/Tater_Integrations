# Tater Integrations

Modular integration source for Tater. Tater downloads a module from `manifest.json` into its local `integrations/` runtime directory only when that integration is enabled in Tater.

## How this repo works

Each integration is a single Python module in `integrations/`. The module is self-contained: Tater should not need code changes when a new integration is added.

At boot and when integration settings change, Tater reads `manifest.json`. Only enabled integrations are downloaded into Tater's local runtime `integrations/` folder. If an integration is disabled, nothing should import it and Tater should continue to run without it.

## Build a basic integration

1. Create a module at `integrations/example.py`.
2. Add `__version__` and an `INTEGRATION` metadata block.
3. Implement settings helpers so the Integration Settings page can read and save values.
4. Implement `integration_status()` so Tater can show whether it is configured.
5. Implement `integration_devices()` if the integration exposes devices.
6. Implement `run_integration_action()` for shop/settings actions such as `test`, `discover`, or `pair`.
7. Implement `run_integration_device_action()` if devices can do things like snapshot, open, close, play audio, or turn on/off.
8. Add the integration entry to `manifest.json`, including version and SHA-256.
9. Compile-check the module and verify the manifest hash.

The smallest useful integration usually looks like this:

```python
from __future__ import annotations

__version__ = "1.0.0"

from typing import Any, Dict, List

import requests

from helpers import redis_client

EXAMPLE_SETTINGS_KEY = "example_settings"
EXAMPLE_DEFAULT_BASE_URL = "https://api.example.com"
EXAMPLE_DEFAULT_TIMEOUT_SECONDS = 12

INTEGRATION = {
    "id": "example",
    "name": "Example",
    "description": "Example integration used as a blueprint.",
    "badge": "EX",
    "order": 100,
    "fields": [
        {
            "key": "example_base_url",
            "label": "Base URL",
            "type": "text",
            "default": EXAMPLE_DEFAULT_BASE_URL,
        },
        {
            "key": "example_api_key",
            "label": "API Key",
            "type": "password",
            "default": "",
        },
    ],
    "actions": [
        {
            "id": "test",
            "label": "Test Example",
            "status": "Checking the Example API.",
        },
    ],
}


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "ignore").strip()
    return str(value or "").strip()


def _read_raw(client: Any = None) -> Dict[str, Any]:
    store = client or redis_client
    try:
        raw = store.hgetall(EXAMPLE_SETTINGS_KEY) or {}
    except Exception:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def read_example_settings(client: Any = None) -> Dict[str, Any]:
    raw = _read_raw(client)
    return {
        "EXAMPLE_BASE_URL": _text(raw.get("EXAMPLE_BASE_URL")) or EXAMPLE_DEFAULT_BASE_URL,
        "EXAMPLE_API_KEY": _text(raw.get("EXAMPLE_API_KEY")),
        "EXAMPLE_TIMEOUT_SECONDS": EXAMPLE_DEFAULT_TIMEOUT_SECONDS,
    }


def save_example_settings(*, base_url: Any = None, api_key: Any = None, client: Any = None) -> Dict[str, Any]:
    store = client or redis_client
    current = read_example_settings(store)
    next_settings = {
        "EXAMPLE_BASE_URL": _text(current.get("EXAMPLE_BASE_URL") if base_url is None else base_url)
        or EXAMPLE_DEFAULT_BASE_URL,
        "EXAMPLE_API_KEY": _text(current.get("EXAMPLE_API_KEY") if api_key is None else api_key),
    }
    store.hset(EXAMPLE_SETTINGS_KEY, mapping=next_settings)
    return read_example_settings(store)


def read_integration_settings() -> Dict[str, Any]:
    settings = read_example_settings()
    return {
        "example_base_url": settings.get("EXAMPLE_BASE_URL") or EXAMPLE_DEFAULT_BASE_URL,
        "example_api_key": settings.get("EXAMPLE_API_KEY", ""),
    }


def save_integration_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload or {}
    saved = save_example_settings(
        base_url=data.get("example_base_url"),
        api_key=data.get("example_api_key"),
    )
    return {
        "example_base_url": saved.get("EXAMPLE_BASE_URL") or EXAMPLE_DEFAULT_BASE_URL,
        "example_api_key": saved.get("EXAMPLE_API_KEY", ""),
    }


def integration_status() -> Dict[str, Any]:
    configured = bool(_text(read_example_settings().get("EXAMPLE_API_KEY")))
    return {
        "configured": configured,
        "message": "Example is configured." if configured else "Example API key is missing.",
    }


def _example_headers(api_key: str) -> Dict[str, str]:
    if not api_key:
        raise ValueError("Example API key is required.")
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def integration_devices() -> Dict[str, Any]:
    settings = read_example_settings()
    api_key = _text(settings.get("EXAMPLE_API_KEY"))
    if not api_key:
        return {"devices": [], "message": "Example is not configured."}

    # Replace this with the provider's real device inventory call.
    devices: List[Dict[str, Any]] = [
        {
            "id": "example_light",
            "name": "Example Light",
            "type": "light",
            "ref": "light:example_light",
            "capabilities": ["light", "switch"],
            "actions": ["turn_on", "turn_off"],
            "event_sources": [],
            "state": "unknown",
            "details": {},
        }
    ]
    return {"devices": devices, "message": f"Example returned {len(devices)} device(s)."}


def run_integration_action(action_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if _text(action_id) != "test":
        raise KeyError(f"Unsupported Example action: {action_id}")
    current = read_integration_settings()
    base_url = _text((payload or {}).get("example_base_url") or current.get("example_base_url"))
    api_key = _text((payload or {}).get("example_api_key") or current.get("example_api_key"))
    response = requests.get(
        f"{base_url.rstrip('/')}/health",
        headers=_example_headers(api_key),
        timeout=EXAMPLE_DEFAULT_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Example test failed: HTTP {response.status_code}: {response.text[:200]}")
    return {"ok": True, "message": "Example connection worked."}


def run_integration_device_action(action_id: str, device_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    action = _text(action_id).lower()
    target = _text(device_id)
    if action not in {"turn_on", "turn_off"}:
        raise KeyError(f"Unsupported Example device action: {action_id}")
    if not target:
        raise ValueError("Example device id is required.")
    # Replace this with the provider's real device command call.
    return {"ok": True, "action": action, "device_id": target}
```

## Integration module hooks

Tater looks for these functions by name. Only implement the ones your integration needs.

- `read_integration_settings()`: returns values for the Settings UI.
- `save_integration_settings(payload)`: saves Settings UI values and returns the saved UI-shaped values.
- `integration_status()`: returns `{"configured": bool, "message": str}`.
- `integration_devices()`: returns a device inventory using the device contract below.
- `integration_web_search(query, **options)`: returns web search results when the integration exposes the `web_search` capability.
- `run_integration_action(action_id, payload)`: runs settings/shop actions such as `test`, `discover`, `pair`, or `forget_pairing`.
- `run_integration_device_action(action_id, device_id, payload)`: runs actions against a specific exposed device.
- `integration_poll_events(client=None, cursor=None)`: optionally returns events/states for runtime polling.

Provider-specific helper functions are fine too. Existing cores and verba can lazy-load them through Tater's integration store, but new generic behavior should prefer shared capability hooks such as the device contract and web search contract.

## Settings fields

The `INTEGRATION["fields"]` list drives the Settings UI. Common field types are `text`, `password`, `number`, and `select`.

```python
{
    "key": "example_timeout_seconds",
    "label": "Timeout Seconds",
    "type": "number",
    "default": 12,
    "min": 2,
    "max": 60,
}
```

Use stable, integration-prefixed field keys such as `hue_app_key`, `unifi_network_api_key`, or `example_api_key`. Keep secrets in `password` fields.

## Device contract

Integrations can expose devices to Tater by defining `integration_devices()`:

```python
def integration_devices():
    return {
        "devices": [
            {
                "id": "front_door",
                "name": "Front Door Camera",
                "type": "camera",
                "ref": "camera:front_door",
                "capabilities": ["camera", "snapshot", "motion", "doorbell"],
                "actions": ["camera_snapshot"],
                "event_sources": [
                    {"type": "motion", "ref": "binary_sensor.front_door_motion", "state_on": "on"},
                    {"type": "doorbell", "ref": "event.front_door_doorbell", "state_on": "on"},
                ],
                "details": {},
            }
        ]
    }
```

Tater uses `capabilities` to build shared catalogs such as all cameras, entry sensors, temperature sensors, speakers, garage doors, web search providers, and network presence targets. Common capabilities include `camera`, `snapshot`, `doorbell`, `motion`, `entry_sensor`, `contact`, `door`, `window`, `garage`, `garage_door`, `open_close`, `temperature`, `humidity`, `thermostat`, `hvac`, `light`, `switch`, `speaker`, `media_player`, `audio_output`, `announcement_target`, `web_search`, `network_device`, `client`, `presence`, and `connectivity`.

## Web search contract

Search providers should declare `capabilities: ["web_search"]` and implement `integration_web_search()`.

```python
def integration_web_search(
    query,
    *,
    num_results=5,
    start=1,
    site=None,
    safe="active",
    country=None,
    language=None,
    timeout_sec=15,
):
    return {
        "tool": "search_web",
        "ok": True,
        "provider": "example_search",
        "provider_label": "Example Search",
        "query": str(query),
        "start": int(start),
        "count": 1,
        "num_results": int(num_results),
        "results": [
            {
                "title": "Example result",
                "url": "https://example.com",
                "snippet": "Short summary text.",
                "display_url": "example.com",
            }
        ],
        "site_filter": site,
        "search_time_sec": None,
        "total_results": None,
        "has_more": False,
        "next_start": None,
    }
```

If the provider is not configured, return `{"tool": "search_web", "ok": False, "provider": "...", "error": "...", "needs": [...]}`. Keep the provider module self-contained: store credentials in an integration-specific Redis hash, expose settings fields through `INTEGRATION["fields"]`, and include a `test` action that runs a small search.

Camera integrations that support snapshots should also define:

```python
def run_integration_device_action(action_id, device_id, payload):
    if action_id in {"camera_snapshot", "snapshot"}:
        return {"ok": True, "bytes": image_bytes, "content_type": "image/jpeg"}
```

Other device actions use the same hook. For example, a garage door can expose `actions: ["open", "close"]`, a light can expose `actions: ["turn_on", "turn_off"]`, and a thermostat can expose `actions: ["set_temperature", "set_hvac_mode"]`.

Optional runtime polling can be exposed with `integration_poll_events(client=None, cursor=None)`. Return `{"events": [...], "states": [...], "cursor": next_cursor}` where event payloads include a `ref`, `entity_id`, or `id` matching one of the device/event refs.

## Manifest entry

Every integration must be listed in `manifest.json`:

```json
{
  "id": "example",
  "name": "Example",
  "description": "Example integration used as a blueprint.",
  "version": "1.0.0",
  "entry": "integrations/example.py",
  "sha256": "<sha256 of integrations/example.py>",
  "required": false
}
```

The manifest `version` should match the module `__version__`. Bump both whenever behavior changes. Keep `required` false unless Tater truly cannot boot without it.

Generate a SHA-256 from the repo root:

```bash
python3.11 -c 'import hashlib,pathlib; p=pathlib.Path("integrations/example.py"); print(hashlib.sha256(p.read_bytes()).hexdigest())'
```

## Validation

Before updating the manifest, compile-check the changed module:

```bash
python3.11 -m py_compile integrations/example.py
```

After updating `manifest.json`, verify all listed hashes:

```bash
python3.11 -c 'import hashlib,json,pathlib,sys; root=pathlib.Path("."); data=json.loads((root/"manifest.json").read_text()); bad=[]; [bad.append((item.get("id"), item.get("sha256"), hashlib.sha256((root/item.get("entry","")).read_bytes()).hexdigest())) for item in data.get("integrations", []) if item.get("entry") and item.get("sha256") and hashlib.sha256((root/item.get("entry","")).read_bytes()).hexdigest()!=item.get("sha256")]; print("integration-manifest-ok" if not bad else bad); sys.exit(1 if bad else 0)'
```

## Design rules

- Keep integrations optional. Missing settings should return an empty device list or a clear status message, not crash Tater at import time.
- Keep imports lightweight. Avoid network calls, discovery, or API login during module import.
- Put provider calls inside functions so disabled integrations stay dormant.
- Use clear errors for actions: `KeyError` for unsupported action ids, `ValueError` for missing inputs, and `RuntimeError` for provider/API failures.
- Prefer explicit `capabilities`, `actions`, `ref`, and `event_sources` over name inference.
- Do not add provider-specific code to Tater core for a new integration. Expose devices and actions from the integration module instead.
