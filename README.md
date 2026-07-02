# Tater Integrations

<div align="center">
  <a href="https://taterassistant.com">
    <img src="images/tater-repo-logo.png" alt="Tater Integrations" width="460"/>
  </a>
</div>
<h3 align="center">
  <a href="https://taterassistant.com">taterassistant.com</a>
</h3>

Modular integration source for Tater. Tater downloads a module from `manifest.json` into its local `integrations/` runtime directory only when that integration is enabled in Tater.

## How this repo works

Each integration is a single Python module in `integrations/`. The module is self-contained: Tater should not need code changes when a new integration is added.

At boot and when integration settings change, Tater reads `manifest.json`. Only enabled integrations are downloaded into Tater's local runtime `integrations/` folder. If an integration is disabled, nothing should import it and Tater should continue to run without it.

## Current integration model

Tater integrations expose provider devices into one shared device catalog. Tater then builds categories such as lights, plugs, switches, cameras, entry sensors, thermostats, garage doors, speakers, and network devices from each device's `type`, `capabilities`, `features`, and `actions`.

The important rule is that integrations describe what a device really is. Do not expose a device as a light, switch, speaker, or sensor because its name or room contains that word. A UniFi network client named "SonosZP" is still a `network_device`; a Shelly Plug is a `plug`; a Hue light is a `light`.

Generic category Verbas and cores use this shared catalog. Provider-specific Verbas should only be needed when the provider has provider-specific behavior, such as Roon library browsing or music search.

Tater owns user organization on top of the provider data. Integrations should report the native provider room/area and stable device names, while Tater's Integrations > Organize UI stores user aliases, room renames, room merges, device renames, and preferred media players. Do not write those Tater aliases back into integration modules.

## Build a basic integration

1. Create a module at `integrations/example.py`.
2. Add `__version__` and an `INTEGRATION` metadata block.
3. Implement settings helpers so the Integration Settings page can read and save values.
4. Implement `integration_status()` so Tater can show whether it is configured.
5. Implement `integration_devices()` if the integration exposes devices, sensors, players, or categories.
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
            "room": "Living Room",
            "area": "Living Room",
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

Provider-specific helper functions are fine too. Existing cores and Verbas can lazy-load them through Tater's integration store, but new generic behavior should prefer shared capability hooks such as the device contract, media playback contract, web search contract, and runtime polling contract.

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
                "state": "online",
                "room": "Porch",
                "area": "Porch",
                "event_sources": [
                    {"type": "motion", "ref": "binary_sensor.front_door_motion", "state_on": "on"},
                    {"type": "doorbell", "ref": "event.front_door_doorbell", "state_on": "on"},
                ],
                "details": {},
            }
        ]
    }
```

Device rows should use stable identifiers:

- `id`: stable provider/device id used by `run_integration_device_action()`.
- `ref`: stable resource reference for UI, events, runtime state, and cross-system matching.
- `name`: provider-reported user-facing name.
- `type`: the primary category, such as `light`, `plug`, `camera`, `entry_sensor`, or `network_device`.
- `capabilities`: category and behavior tags used by generic Verbas and cores.
- `actions`: action ids accepted by `run_integration_device_action()`.
- `state` and `status`: current state if cheaply available.
- `room` and `area`: provider-reported native room/area only. Tater may override these in Organize.
- `details`: provider-specific metadata that is useful for UI or debugging.

Tater uses `type`, `capabilities`, `features`, and `actions` to build shared catalogs such as all cameras, entry sensors, temperature sensors, speakers, garage doors, web search providers, and network presence targets. Common capabilities include `battery`, `camera`, `snapshot`, `doorbell`, `motion`, `entry_sensor`, `contact`, `door`, `window`, `garage`, `garage_door`, `open_close`, `temperature`, `humidity`, `thermostat`, `hvac`, `light`, `switch`, `plug`, `cover`, `fan`, `lock`, `remote`, `scene`, `script`, `energy`, `illuminance`, `leak`, `speaker`, `media_player`, `audio_output`, `announcement_target`, `web_search`, `network_device`, `client`, `presence`, and `connectivity`.

Preferred `type` values include:

- `light`: lights and dimmable/color light resources.
- `plug`: controllable outlet or plug devices.
- `switch`: real switch devices that are not plugs, lights, or network switches.
- `cover`: blinds, shades, curtains, and similar open/close devices.
- `garage_door`: garage doors and openers.
- `camera`: cameras and doorbells with snapshots or streams.
- `entry_sensor`: contact/open-close sensors.
- `temperature`, `humidity`, `illuminance`, `leak`, `battery`, `energy`: sensor categories.
- `thermostat`: HVAC climate devices.
- `speaker`: speakers, zones, and audio output targets.
- `network_device`: routers, access points, switches, clients, and presence-style network inventory.

Keep provider semantics clear. For example, a network switch from UniFi should be `network_device`, not `switch`; a media player visible as a network client should remain `network_device` in the UniFi Network integration and should only be a `speaker` in the Sonos/Roon/media integration that can actually play audio.

## Organize, rooms, and aliases

Tater builds a master room list from integration-reported `room`, `area`, and device metadata. The Integrations > Organize UI lets users:

- Rename rooms.
- Merge provider rooms into one Tater room.
- Move devices between Tater rooms.
- Rename devices.
- Set a preferred media player for a room.

Integrations should not persist these Tater-side changes. Keep reporting the provider's native names and rooms. Tater stores the alias/override layer in its own registry and applies it when generic Verbas, cores, and the UI read the shared device catalog.

## Media playback contract

Generated-audio Verbas and cores do not call provider-specific speaker code directly. They call Tater's shared media dispatcher with a normalized target:

- `voice_core:<selector>` for a satellite.
- `sonos:<speaker_id>` for Tater's built-in Sonos paired-speaker path.
- `integration:<integration_id>:<url-encoded-device-id>` for future media integrations.
- `ha:<media_player.entity_id>` for legacy Home Assistant media player targets.

To appear as a generic integration media target, expose a device with `type: "speaker"` or `type: "media_player"` and capabilities such as `speaker`, `media_player`, `audio_output`. If the device can play an arbitrary URL, add `play_url` to `actions`. If it supports media playback through a generic media action, add `play_media` and include `announcement_target` in `capabilities`.

Example speaker device:

```python
{
    "id": "zone:family_room",
    "name": "Family Room Speaker",
    "type": "speaker",
    "ref": "speaker:family_room",
    "capabilities": ["speaker", "media_player", "audio_output", "announcement_target"],
    "actions": ["play_url", "play_media"],
    "state": "available",
    "room": "Family Room",
    "area": "Family Room",
}
```

The dispatcher calls `run_integration_device_action()` with a payload like this:

```python
{
    "source_url": "http://tater.local:8501/api/speech/tts/runtime/<asset_id>/audio.mp3",
    "url": "http://tater.local:8501/api/speech/tts/runtime/<asset_id>/audio.mp3",
    "media_url": "http://tater.local:8501/api/speech/tts/runtime/<asset_id>/audio.mp3",
    "media_content_id": "http://tater.local:8501/api/speech/tts/runtime/<asset_id>/audio.mp3",
    "media_content_type": "music",
    "media_type": "audio/mpeg",
    "timeout_s": 360.0,
}
```

Use the first available URL field. Return `{"ok": True, "sent_count": 1}` when playback starts. Return `{"ok": False, "error": "..."}` when playback cannot start.

Do not mark provider-specific library playback as generic URL playback. Roon is a good example: playing "the Eagles" is a music-library operation, not "play this URL". That should stay behind a provider-specific Verba or action unless the provider can also play arbitrary URLs.

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

## Runtime polling contract

Tater runs a live integration runtime. It periodically refreshes the device registry cache and calls optional provider pollers so the UI and Verbas can read recent device state quickly instead of rebuilding every provider list on demand.

Expose one of these functions if the integration can cheaply poll recent changes:

- `integration_poll_events(client=None, cursor=None)`
- `poll_integration_events(client=None, cursor=None)`
- `integration_runtime_poll(client=None, cursor=None)`

Set `INTEGRATION_RUNTIME_POLL_SECONDS` or `INTEGRATION["runtime_poll_seconds"]` when the default 30 second interval is not right.

Return:

```python
{
    "states": [
        {
            "id": "binary_sensor.front_door_contact",
            "ref": "binary_sensor.front_door_contact",
            "entity_id": "binary_sensor.front_door_contact",
            "name": "Front Door",
            "state": "closed",
            "room": "Entry",
        }
    ],
    "events": [
        {
            "kind": "state_changed",
            "entity_id": "binary_sensor.front_door_contact",
            "state": "open",
            "previous_state": "closed",
        }
    ],
    "cursor": {"last_seen": "provider-specific cursor"}
}
```

State and event payloads must include one of `entity_id`, `ref`, `device_ref`, `resource_ref`, `id`, or `device_id` matching a device row or event source. Tater stores current states and recent events in its runtime cache and the Settings UI shows recent changes from that cache.

Optional runtime polling can also return only `states`, only `events`, or a plain list of events. If there are no changes, return empty lists and the next cursor.
