from __future__ import annotations
__version__ = "1.3.3"

import ipaddress
import json
import socket
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List
from urllib.parse import urlparse, urlunparse

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

from helpers import redis_client

HUE_SETTINGS_KEY = "hue_settings"
HUE_DEFAULT_BRIDGE_HOST = "http://philips-hue.local"
HUE_DEFAULT_DEVICE_TYPE = "tater_shop#tater"
HUE_DEFAULT_TIMEOUT_SECONDS = 10
HUE_DISCOVERY_URL = "https://discovery.meethue.com/"

INTEGRATION = {
    "id": "hue",
    "name": "Philips Hue",
    "description": "Hue Bridge pairing and app key used by Tater lighting actions.",
    "badge": "HUE",
    "order": 20,
    "capabilities": ["light", "switch", "sensor", "entry_sensor", "motion", "temperature", "humidity", "illuminance", "battery"],
    "fields": [
        {
            "key": "hue_bridge_host",
            "label": "Bridge Host or URL",
            "type": "text",
            "default": HUE_DEFAULT_BRIDGE_HOST,
            "placeholder": HUE_DEFAULT_BRIDGE_HOST,
        },
        {
            "key": "hue_app_key",
            "label": "Hue App Key",
            "type": "password",
            "default": "",
        },
        {
            "key": "hue_device_type",
            "label": "Device Type",
            "type": "text",
            "default": HUE_DEFAULT_DEVICE_TYPE,
        },
        {
            "key": "hue_timeout_seconds",
            "label": "Timeout Seconds",
            "type": "number",
            "default": HUE_DEFAULT_TIMEOUT_SECONDS,
            "min": 2,
            "max": 60,
        },
    ],
    "actions": [
        {
            "id": "link_bridge",
            "label": "Link Hue Bridge",
            "status": "Press the Hue Bridge button, then link. Tater can auto-detect the bridge.",
        },
    ],
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        parsed = int(default)
    return max(int(minimum), min(int(maximum), parsed))


def normalize_hue_bridge_root(raw_host: Any) -> str:
    text = _text(raw_host) or HUE_DEFAULT_BRIDGE_HOST
    if "://" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)
    if not parsed.netloc and parsed.path:
        parsed = urlparse(f"http://{text}")
    scheme = parsed.scheme or "http"
    netloc = parsed.netloc or parsed.path
    root = urlunparse((scheme, netloc, "", "", "", "")).rstrip("/")
    return root or HUE_DEFAULT_BRIDGE_HOST


def hue_clip_v2_root(raw_host: Any) -> str:
    base = normalize_hue_bridge_root(raw_host)
    parsed = urlparse(base)
    host = parsed.hostname or parsed.netloc or parsed.path
    try:
        port = int(parsed.port or 0)
    except Exception:
        port = 0
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if port and port not in {80, 443}:
        netloc = f"{host}:{port}"
    return urlunparse(("https", netloc, "", "", "", "")).rstrip("/")


def _root_from_url(raw_url: Any) -> str:
    text = _text(raw_url)
    if not text:
        return ""
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")).rstrip("/")


def _unique_bridge_roots(values: List[Any]) -> List[str]:
    roots: List[str] = []
    seen: set[str] = set()
    for value in values or []:
        root = normalize_hue_bridge_root(value)
        key = root.lower()
        if not root or key in seen:
            continue
        roots.append(root)
        seen.add(key)
    return roots


def _discover_bridges_broker(timeout: int) -> List[str]:
    try:
        response = requests.get(HUE_DISCOVERY_URL, timeout=max(2, min(10, int(timeout))))
        if response.status_code >= 400:
            return []
        payload = response.json()
    except Exception:
        return []

    roots: List[str] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        ip = _text(item.get("internalipaddress"))
        if not ip:
            continue
        roots.append(f"http://{ip}")
        port = _text(item.get("port"))
        if port and port not in {"80", "443"}:
            roots.append(f"http://{ip}:{port}")
    return _unique_bridge_roots(roots)


def _discover_bridges_ssdp(timeout: int) -> List[str]:
    roots: List[str] = []
    deadline = time.time() + max(1.0, min(6.0, float(timeout)))
    message = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: ssdp:all\r\n"
        "\r\n"
    ).encode("ascii")

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.settimeout(0.7)
        sock.sendto(message, ("239.255.255.250", 1900))
    except OSError:
        try:
            sock.close()
        except Exception:
            pass
        return []

    try:
        while time.time() < deadline:
            try:
                data, _addr = sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break

            raw = data.decode("utf-8", "ignore")
            location = ""
            for line in raw.splitlines():
                if line.lower().startswith("location:"):
                    location = line.split(":", 1)[1].strip()
                    break
            if not location:
                continue

            root = _root_from_url(location)
            if not root:
                continue

            raw_l = raw.lower()
            if "ipbridge" in raw_l or "philips hue" in raw_l or "hue bridge" in raw_l:
                roots.append(root)
                continue

            try:
                desc_response = requests.get(location, timeout=2)
                desc = str(desc_response.text or "").lower()
            except Exception:
                continue
            if "philips hue" in desc or "hue bridge" in desc or "ipbridge" in desc:
                roots.append(root)
    finally:
        try:
            sock.close()
        except Exception:
            pass

    return _unique_bridge_roots(roots)


def _local_scan_networks() -> List[ipaddress.IPv4Network]:
    ips: set[str] = set()

    def add_ip(value: Any) -> None:
        text = _text(value)
        if not text:
            return
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            return
        if not isinstance(ip, ipaddress.IPv4Address):
            return
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            return
        if not ip.is_private:
            return
        ips.add(str(ip))

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            add_ip(sock.getsockname()[0])
    except OSError:
        pass

    try:
        hostname = socket.gethostname()
        for item in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_DGRAM):
            add_ip(item[4][0])
    except OSError:
        pass

    networks: List[ipaddress.IPv4Network] = []
    seen: set[str] = set()
    for ip_text in sorted(ips):
        try:
            network = ipaddress.ip_interface(f"{ip_text}/24").network
        except ValueError:
            continue
        key = str(network)
        if key in seen:
            continue
        networks.append(network)
        seen.add(key)
    return networks[:4]


def _looks_like_bridge_root(root: str, timeout: float) -> bool:
    try:
        response = requests.get(f"{root}/description.xml", timeout=timeout)
        if response.status_code < 400:
            text = str(response.text or "").lower()
            if "philips hue" in text or "hue bridge" in text or "ipbridge" in text:
                return True
    except Exception:
        pass

    try:
        response = requests.get(f"{root}/api/config", timeout=timeout)
        if response.status_code < 400:
            payload = response.json()
            if isinstance(payload, dict) and (_text(payload.get("bridgeid")) or _text(payload.get("apiversion"))):
                return True
    except Exception:
        pass

    return False


def _discover_bridges_subnet_scan(timeout: int) -> List[str]:
    networks = _local_scan_networks()
    if not networks:
        return []

    roots: List[str] = []
    probe_timeout = max(0.25, min(0.9, float(timeout) / 4.0))
    max_workers = 32

    for network in networks:
        hosts = list(network.hosts())
        if len(hosts) > 1024:
            continue

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(_looks_like_bridge_root, f"http://{host}", probe_timeout): f"http://{host}"
                for host in hosts
            }
            for future in as_completed(future_map):
                root = future_map[future]
                try:
                    found = bool(future.result())
                except Exception:
                    found = False
                if found:
                    roots.append(root)
                    break

        if roots:
            break

    return _unique_bridge_roots(roots)


def discover_hue_bridge_roots(timeout: int) -> List[str]:
    roots = _discover_bridges_broker(timeout)
    if roots:
        return _unique_bridge_roots(roots)
    roots = _discover_bridges_ssdp(timeout)
    if roots:
        return _unique_bridge_roots(roots)
    return _unique_bridge_roots(_discover_bridges_subnet_scan(timeout))


def _legacy_settings(client: Any = None) -> Dict[str, str]:
    store = client or redis_client
    merged: Dict[str, str] = {}
    for key in (
        "verba_settings:Philips Hue Control",
        "verba_settings: Philips Hue Control",
        "verba_settings:Philips Hue",
        "verba_settings: Philips Hue",
    ):
        try:
            raw = store.hgetall(key) or {}
        except Exception:
            continue
        for field, value in raw.items():
            if _text(value) or field not in merged:
                merged[str(field)] = _text(value)
    return merged


def read_hue_settings(client: Any = None) -> Dict[str, str]:
    store = client or redis_client
    try:
        shared = store.hgetall(HUE_SETTINGS_KEY) or {}
    except Exception:
        shared = {}
    legacy = _legacy_settings(store)

    def pick(*keys: str) -> str:
        for source in (shared, legacy):
            for key in keys:
                value = _text(source.get(key))
                if value:
                    return value
        return ""

    timeout = _bounded_int(
        pick("HUE_TIMEOUT_SECONDS", "TIMEOUT_SECONDS"),
        default=HUE_DEFAULT_TIMEOUT_SECONDS,
        minimum=2,
        maximum=60,
    )
    return {
        "HUE_BRIDGE_HOST": normalize_hue_bridge_root(
            pick("HUE_BRIDGE_HOST", "HUE_BRIDGE_URL", "HUE_HOST", "BRIDGE_HOST") or HUE_DEFAULT_BRIDGE_HOST
        ),
        "HUE_APP_KEY": pick("HUE_APP_KEY", "HUE_USERNAME", "HUE_USER", "HUE_API_KEY"),
        "HUE_DEVICE_TYPE": (pick("HUE_DEVICE_TYPE", "DEVICE_TYPE") or HUE_DEFAULT_DEVICE_TYPE)[:40],
        "HUE_TIMEOUT_SECONDS": str(timeout),
    }


def save_hue_settings(
    *,
    bridge_host: Any = None,
    app_key: Any = None,
    device_type: Any = None,
    timeout_seconds: Any = None,
    client: Any = None,
) -> Dict[str, str]:
    store = client or redis_client
    current = read_hue_settings(store)
    next_settings = {
        "HUE_BRIDGE_HOST": normalize_hue_bridge_root(
            current.get("HUE_BRIDGE_HOST") if bridge_host is None else bridge_host
        ),
        "HUE_APP_KEY": _text(current.get("HUE_APP_KEY") if app_key is None else app_key),
        "HUE_DEVICE_TYPE": (
            _text(current.get("HUE_DEVICE_TYPE") if device_type is None else device_type)
            or HUE_DEFAULT_DEVICE_TYPE
        )[:40],
        "HUE_TIMEOUT_SECONDS": str(
            _bounded_int(
                current.get("HUE_TIMEOUT_SECONDS") if timeout_seconds is None else timeout_seconds,
                default=HUE_DEFAULT_TIMEOUT_SECONDS,
                minimum=2,
                maximum=60,
            )
        ),
    }
    store.hset(HUE_SETTINGS_KEY, mapping=next_settings)
    return next_settings


def _hue_v2_get(bridge: str, app_key: str, resource: str, *, timeout: int) -> List[Dict[str, Any]]:
    path = resource.strip("/")
    api_root = hue_clip_v2_root(bridge)
    headers = {"hue-application-key": app_key, "Accept": "application/json"}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", InsecureRequestWarning)
        response = requests.get(
            f"{api_root}/clip/v2/resource/{path}",
            headers=headers,
            timeout=max(2, int(timeout or HUE_DEFAULT_TIMEOUT_SECONDS)),
            verify=False,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"Hue HTTP {response.status_code} reading {path}: {response.text[:200]}")
    try:
        payload = response.json()
    except Exception:
        return []
    rows = payload.get("data") if isinstance(payload, dict) else []
    return rows if isinstance(rows, list) else []


def _existing_hue_link(bridge: str, app_key: str, *, timeout: int) -> Dict[str, Any]:
    if not _text(app_key):
        return {"ok": False, "message": "Hue app key is missing."}
    try:
        rows = _hue_v2_get(bridge, app_key, "bridge", timeout=timeout)
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    bridge_row = next((row for row in rows if isinstance(row, dict)), {})
    bridge_id = _text(bridge_row.get("id"))
    bridge_name = _hue_name(bridge_row, "Hue Bridge") if bridge_row else "Hue Bridge"
    return {
        "ok": True,
        "code": "already_linked",
        "message": f"Philips Hue is already linked to {bridge_name}. Tater reused the saved Hue app key.",
        "hue_bridge_host": bridge,
        "hue_app_key": _text(app_key),
        "bridge_id": bridge_id,
        "bridge_name": bridge_name,
    }


def _hue_v2_put(bridge: str, app_key: str, path: str, payload: Dict[str, Any], *, timeout: int) -> Dict[str, Any]:
    api_root = hue_clip_v2_root(bridge)
    headers = {"hue-application-key": app_key, "Accept": "application/json", "Content-Type": "application/json"}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", InsecureRequestWarning)
        response = requests.put(
            f"{api_root}/clip/v2/resource/{path.strip('/')}",
            headers=headers,
            json=payload,
            timeout=max(2, int(timeout or HUE_DEFAULT_TIMEOUT_SECONDS)),
            verify=False,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"Hue HTTP {response.status_code} writing {path}: {response.text[:200]}")
    try:
        parsed = response.json()
    except Exception:
        return {"ok": True}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def _hue_name(row: Dict[str, Any], fallback: str) -> str:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return _text(metadata.get("name")) or _text(row.get("name")) or fallback


def _hue_owner(row: Dict[str, Any]) -> str:
    owner = row.get("owner") if isinstance(row.get("owner"), dict) else {}
    return _text(owner.get("rid"))


def _hue_room_map(bridge: str, app_key: str, *, timeout: int) -> Dict[str, str]:
    rooms: Dict[str, str] = {}
    for resource_type in ("room", "zone"):
        try:
            rows = _hue_v2_get(bridge, app_key, resource_type, timeout=timeout)
        except Exception:
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = _hue_name(row, resource_type.title())
            room_id = _text(row.get("id"))
            if room_id:
                rooms[room_id] = name
            children = row.get("children") if isinstance(row.get("children"), list) else []
            services = row.get("services") if isinstance(row.get("services"), list) else []
            for child in [*children, *services]:
                if not isinstance(child, dict):
                    continue
                rid = _text(child.get("rid"))
                if rid:
                    rooms.setdefault(rid, name)
    return rooms


def _hue_resource_details(row: Dict[str, Any], resource_type: str) -> Dict[str, Any]:
    details: Dict[str, Any] = {}
    for key in (
        "product_data",
        "services",
        "on",
        "dimming",
        "color_temperature",
        "temperature",
        "humidity",
        "motion",
        "contact",
        "light",
        "power_state",
    ):
        value = row.get(key)
        if value not in (None, "", [], {}):
            details[key] = value
    if _hue_owner(row):
        details["owner"] = _hue_owner(row)
    details["resource_type"] = resource_type
    return details


def _hue_resource_state(row: Dict[str, Any], resource_type: str) -> str:
    if resource_type == "light":
        on = row.get("on") if isinstance(row.get("on"), dict) else {}
        if "on" in on:
            return "on" if bool(on.get("on")) else "off"
    if resource_type == "temperature":
        temp = row.get("temperature") if isinstance(row.get("temperature"), dict) else {}
        value = temp.get("temperature")
        return f"{value} C" if value not in (None, "") else ""
    if resource_type == "relative_humidity":
        humidity = row.get("humidity") if isinstance(row.get("humidity"), dict) else {}
        value = humidity.get("humidity") or humidity.get("relative_humidity")
        return f"{value}%" if value not in (None, "") else ""
    if resource_type == "motion":
        motion = row.get("motion") if isinstance(row.get("motion"), dict) else {}
        if "motion" in motion:
            return "motion" if bool(motion.get("motion")) else "clear"
    if resource_type == "contact":
        contact = row.get("contact") if isinstance(row.get("contact"), dict) else {}
        state = contact.get("contact_report", {}).get("state") if isinstance(contact.get("contact_report"), dict) else ""
        return _text(state)
    if resource_type == "light_level":
        light = row.get("light") if isinstance(row.get("light"), dict) else {}
        value = light.get("light_level") or light.get("light_level_valid")
        return _text(value)
    if resource_type == "device_power":
        power = row.get("power_state") if isinstance(row.get("power_state"), dict) else {}
        value = power.get("battery_level") or power.get("battery_state")
        return f"{value}%" if isinstance(value, (int, float)) else _text(value)
    return ""


def _hue_resource_capabilities(row: Dict[str, Any], resource_type: str) -> List[str]:
    if resource_type == "light":
        caps = ["light"]
        if isinstance(row.get("dimming"), dict):
            caps.append("dimmable")
        if isinstance(row.get("color"), dict):
            caps.append("color")
        if isinstance(row.get("color_temperature"), dict):
            caps.append("color_temperature")
        return caps
    if resource_type == "temperature":
        return ["sensor", "temperature"]
    if resource_type == "relative_humidity":
        return ["sensor", "humidity", "relative_humidity"]
    if resource_type == "motion":
        return ["sensor", "motion"]
    if resource_type == "contact":
        return ["sensor", "contact", "entry_sensor", "open_close"]
    if resource_type == "light_level":
        return ["sensor", "illuminance", "light_sensor"]
    if resource_type == "device_power":
        return ["sensor", "battery"]
    return ["device"]


def _hue_resource_actions(resource_type: str) -> List[str]:
    return ["turn_on", "turn_off", "set_brightness", "set_color"] if resource_type == "light" else []


_HUE_COLOR_RGB: Dict[str, List[int]] = {
    "red": [255, 0, 0],
    "orange": [255, 128, 0],
    "amber": [255, 191, 0],
    "yellow": [255, 255, 0],
    "green": [0, 180, 0],
    "lime": [128, 255, 0],
    "blue": [0, 70, 255],
    "cyan": [0, 255, 255],
    "teal": [0, 128, 128],
    "purple": [128, 0, 255],
    "violet": [143, 0, 255],
    "magenta": [255, 0, 255],
    "pink": [255, 105, 180],
    "white": [255, 244, 229],
    "warm white": [255, 197, 143],
    "cool white": [214, 225, 255],
    "daylight": [220, 235, 255],
}


def _normalize_rgb(value: Any) -> List[int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    out: List[int] = []
    for item in value:
        try:
            out.append(max(0, min(255, int(round(float(item))))))
        except Exception:
            return None
    return out


def _rgb_to_xy(rgb: List[int]) -> List[float] | None:
    normalized = _normalize_rgb(rgb)
    if normalized is None:
        return None

    def gamma(channel: int) -> float:
        c = channel / 255.0
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92

    r, g, b = [gamma(v) for v in normalized]
    x_val = r * 0.664511 + g * 0.154324 + b * 0.162028
    y_val = r * 0.283881 + g * 0.668433 + b * 0.047685
    z_val = r * 0.000088 + g * 0.072310 + b * 0.986039
    total = x_val + y_val + z_val
    if total <= 0:
        return None
    return [round(x_val / total, 4), round(y_val / total, 4)]


def _hue_xy_from_payload(payload: Dict[str, Any]) -> List[float] | None:
    xy_color = payload.get("xy_color")
    if isinstance(xy_color, (list, tuple)) and len(xy_color) == 2:
        try:
            return [round(max(0.0, min(1.0, float(xy_color[0]))), 4), round(max(0.0, min(1.0, float(xy_color[1]))), 4)]
        except Exception:
            pass
    rgb = _normalize_rgb(payload.get("rgb_color"))
    if rgb is not None:
        return _rgb_to_xy(rgb)
    color_name = " ".join(_text(payload.get("color_name")).lower().replace("_", " ").split())
    if color_name:
        if color_name in _HUE_COLOR_RGB:
            return _rgb_to_xy(_HUE_COLOR_RGB[color_name])
        for name, rgb_value in _HUE_COLOR_RGB.items():
            if name in color_name:
                return _rgb_to_xy(rgb_value)
    return None


def _hue_resource_event_sources(resource_type: str, ref: str) -> List[Dict[str, Any]]:
    if resource_type == "motion":
        return [{"type": "motion", "ref": ref, "state_on": "motion", "state_off": "clear"}]
    if resource_type == "contact":
        return [{"type": "contact", "ref": ref, "state_on": "no_contact", "state_off": "contact"}]
    return []


def integration_devices() -> Dict[str, Any]:
    settings = read_hue_settings()
    bridge = normalize_hue_bridge_root(settings.get("HUE_BRIDGE_HOST"))
    app_key = _text(settings.get("HUE_APP_KEY"))
    timeout = _bounded_int(
        settings.get("HUE_TIMEOUT_SECONDS"),
        default=HUE_DEFAULT_TIMEOUT_SECONDS,
        minimum=2,
        maximum=60,
    )
    if not app_key:
        return {"devices": [], "message": "Philips Hue is not linked."}
    devices: List[Dict[str, Any]] = []
    errors: List[str] = []
    seen_refs: set[str] = set()
    rooms = _hue_room_map(bridge, app_key, timeout=timeout)
    for resource_type in ("light", "temperature", "relative_humidity", "motion", "contact", "light_level", "device_power"):
        try:
            rows = _hue_v2_get(bridge, app_key, resource_type, timeout=timeout)
        except Exception as exc:
            errors.append(f"{resource_type}: {exc}")
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_id = _text(row.get("id"))
            name = _hue_name(row, row_id or resource_type.title())
            state = _hue_resource_state(row, resource_type)
            ref = f"{resource_type}:{row_id}" if row_id else ""
            ref_key = ref or f"{resource_type}:{name}"
            if ref_key in seen_refs:
                continue
            seen_refs.add(ref_key)
            room = rooms.get(row_id) or rooms.get(_hue_owner(row)) or ""
            devices.append(
                {
                    "id": row_id or name,
                    "name": name,
                    "type": resource_type,
                    "ref": ref,
                    "capabilities": _hue_resource_capabilities(row, resource_type),
                    "actions": _hue_resource_actions(resource_type),
                    "features": _hue_resource_actions(resource_type),
                    "event_sources": _hue_resource_event_sources(resource_type, ref),
                    "room": room,
                    "area": room,
                    "status": state,
                    "state": state,
                    "details": _hue_resource_details(row, resource_type),
                }
            )
    message = f"Philips Hue returned {len(devices)} current resources."
    if errors:
        message = f"{message} Some resource types could not be read: {'; '.join(errors[:4])}."
    return {"devices": devices, "message": message, "warnings": errors}


def _pair_bridge_once(bridge: str, *, device: str, timeout: int) -> Dict[str, Any]:
    try:
        response = requests.post(f"{bridge}/api", json={"devicetype": device}, timeout=timeout)
    except requests.RequestException as exc:
        return {"ok": False, "code": "network", "message": f"{bridge}: {exc}", "hue_bridge_host": bridge}

    if response.status_code >= 400:
        return {
            "ok": False,
            "code": "http",
            "message": f"{bridge}: HTTP {response.status_code}: {response.text}",
            "hue_bridge_host": bridge,
        }

    try:
        payload = response.json()
    except Exception:
        return {
            "ok": False,
            "code": "bad_json",
            "message": f"{bridge}: unreadable response: {response.text}",
            "hue_bridge_host": bridge,
        }

    username = ""
    errors: List[str] = []
    link_button_error = False
    for item in payload if isinstance(payload, list) else [payload]:
        if not isinstance(item, dict):
            continue
        success = item.get("success") if isinstance(item.get("success"), dict) else {}
        error = item.get("error") if isinstance(item.get("error"), dict) else {}
        if success.get("username"):
            username = _text(success.get("username"))
            break
        if error:
            err_type = _text(error.get("type"))
            desc = _text(error.get("description")) or json.dumps(error, ensure_ascii=False)
            if err_type == "101" or "link button" in desc.lower():
                link_button_error = True
                desc = "link button not pressed"
            errors.append(desc)

    if username:
        return {
            "ok": True,
            "code": "linked",
            "message": "Hue Bridge linked successfully. The Hue app key was saved.",
            "hue_bridge_host": bridge,
            "hue_app_key": username,
        }

    suffix = "; ".join(errors) if errors else "no username was returned"
    return {
        "ok": False,
        "code": "link_button" if link_button_error else "api_error",
        "message": f"{bridge}: {suffix}",
        "hue_bridge_host": bridge,
    }


def pair_hue_bridge(
    *,
    bridge_host: Any = None,
    device_type: Any = None,
    timeout_seconds: Any = None,
) -> Dict[str, Any]:
    current_settings = read_hue_settings()
    bridge = normalize_hue_bridge_root(bridge_host or current_settings.get("HUE_BRIDGE_HOST"))
    device = (_text(device_type) or HUE_DEFAULT_DEVICE_TYPE)[:40]
    timeout = _bounded_int(timeout_seconds, default=HUE_DEFAULT_TIMEOUT_SECONDS, minimum=2, maximum=60)
    existing_app_key = _text(current_settings.get("HUE_APP_KEY"))

    existing = _existing_hue_link(bridge, existing_app_key, timeout=timeout)
    if existing.get("ok"):
        saved = save_hue_settings(
            bridge_host=bridge,
            app_key=existing_app_key,
            device_type=device,
            timeout_seconds=timeout,
        )
        return {
            "ok": True,
            "code": "already_linked",
            "message": existing.get("message") or "Philips Hue is already linked. Tater reused the saved Hue app key.",
            "hue_bridge_host": saved.get("HUE_BRIDGE_HOST", bridge),
            "hue_app_key": saved.get("HUE_APP_KEY", existing_app_key),
            "bridge_id": existing.get("bridge_id"),
            "bridge_name": existing.get("bridge_name"),
            "discovered_bridge_hosts": [],
        }

    first_result = _pair_bridge_once(bridge, device=device, timeout=timeout)
    attempts: List[Dict[str, Any]] = [first_result]
    if first_result.get("ok"):
        saved = save_hue_settings(
            bridge_host=bridge,
            app_key=first_result.get("hue_app_key"),
            device_type=device,
            timeout_seconds=timeout,
        )
        return {
            "ok": True,
            "message": "Hue Bridge linked successfully. The Hue app key was saved.",
            "hue_bridge_host": saved.get("HUE_BRIDGE_HOST", bridge),
            "hue_app_key": saved.get("HUE_APP_KEY", first_result.get("hue_app_key")),
            "discovered_bridge_hosts": [],
        }

    if first_result.get("code") == "link_button":
        return {
            "ok": False,
            "message": (
                f"Hue Bridge found at {bridge}, but the link button was not pressed. "
                "Press the bridge button and try again within 30 seconds."
            ),
            "hue_bridge_host": bridge,
            "discovered_bridge_hosts": [],
        }

    discovered = discover_hue_bridge_roots(timeout)
    candidates = [root for root in _unique_bridge_roots(discovered) if root.lower() != bridge.lower()]
    for candidate in candidates:
        existing = _existing_hue_link(candidate, existing_app_key, timeout=timeout)
        if existing.get("ok"):
            saved = save_hue_settings(
                bridge_host=candidate,
                app_key=existing_app_key,
                device_type=device,
                timeout_seconds=timeout,
            )
            return {
                "ok": True,
                "code": "already_linked",
                "message": (
                    f"Hue Bridge auto-detected at {candidate} and is already linked. "
                    "Tater reused the saved Hue app key."
                ),
                "hue_bridge_host": saved.get("HUE_BRIDGE_HOST", candidate),
                "hue_app_key": saved.get("HUE_APP_KEY", existing_app_key),
                "bridge_id": existing.get("bridge_id"),
                "bridge_name": existing.get("bridge_name"),
                "discovered_bridge_hosts": discovered,
            }

        result = _pair_bridge_once(candidate, device=device, timeout=timeout)
        attempts.append(result)
        if not result.get("ok"):
            continue

        saved = save_hue_settings(
            bridge_host=candidate,
            app_key=result.get("hue_app_key"),
            device_type=device,
            timeout_seconds=timeout,
        )
        auto_detected = candidate.lower() != bridge.lower()
        return {
            "ok": True,
            "message": (
                f"Hue Bridge auto-detected at {candidate} and linked successfully. "
                "The Hue app key was saved."
                if auto_detected
                else "Hue Bridge linked successfully. The Hue app key was saved."
            ),
            "hue_bridge_host": saved.get("HUE_BRIDGE_HOST", candidate),
            "hue_app_key": saved.get("HUE_APP_KEY", result.get("hue_app_key")),
            "discovered_bridge_hosts": discovered,
        }

    preferred = next((item for item in attempts if item.get("code") == "link_button"), None)
    if preferred is None:
        preferred = next((item for item in attempts if item.get("code") in {"api_error", "http", "bad_json"}), None)
    if preferred is None and attempts:
        preferred = attempts[-1]

    if preferred and preferred.get("code") == "link_button":
        detected = _text(preferred.get("hue_bridge_host"))
        return {
            "ok": False,
            "message": (
                f"Hue Bridge auto-detected at {detected}, but the link button was not pressed. "
                "Press the bridge button and try again within 30 seconds."
            ),
            "hue_bridge_host": detected or bridge,
            "discovered_bridge_hosts": discovered,
        }

    if preferred and discovered:
        detected = _text(preferred.get("hue_bridge_host")) or discovered[0]
        return {
            "ok": False,
            "message": (
                f"Hue Bridge auto-discovery found {detected}, but pairing failed: "
                f"{preferred.get('message') or 'unknown error'}"
            ),
            "hue_bridge_host": detected,
            "discovered_bridge_hosts": discovered,
        }

    return {
        "ok": False,
        "message": (
            f"Hue Bridge pairing failed: {(preferred or {}).get('message') or 'bridge not reachable'}. "
            "Auto-discovery did not find another bridge. You can enter the bridge IP from the Hue app or router."
        ),
        "hue_bridge_host": bridge,
        "discovered_bridge_hosts": discovered,
    }


def read_integration_settings() -> Dict[str, Any]:
    settings = read_hue_settings()
    return {
        "hue_bridge_host": settings.get("HUE_BRIDGE_HOST", HUE_DEFAULT_BRIDGE_HOST),
        "hue_app_key": settings.get("HUE_APP_KEY", ""),
        "hue_device_type": settings.get("HUE_DEVICE_TYPE", HUE_DEFAULT_DEVICE_TYPE),
        "hue_timeout_seconds": int(settings.get("HUE_TIMEOUT_SECONDS") or HUE_DEFAULT_TIMEOUT_SECONDS),
    }


def save_integration_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    saved = save_hue_settings(
        bridge_host=(payload or {}).get("hue_bridge_host"),
        app_key=(payload or {}).get("hue_app_key"),
        device_type=(payload or {}).get("hue_device_type"),
        timeout_seconds=(payload or {}).get("hue_timeout_seconds"),
    )
    return {
        "hue_bridge_host": saved.get("HUE_BRIDGE_HOST", HUE_DEFAULT_BRIDGE_HOST),
        "hue_app_key": saved.get("HUE_APP_KEY", ""),
        "hue_device_type": saved.get("HUE_DEVICE_TYPE", HUE_DEFAULT_DEVICE_TYPE),
        "hue_timeout_seconds": int(saved.get("HUE_TIMEOUT_SECONDS") or HUE_DEFAULT_TIMEOUT_SECONDS),
    }


def run_integration_action(action_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if _text(action_id) != "link_bridge":
        raise KeyError(f"Unsupported Philips Hue action: {action_id}")
    return pair_hue_bridge(
        bridge_host=(payload or {}).get("hue_bridge_host"),
        device_type=(payload or {}).get("hue_device_type"),
        timeout_seconds=(payload or {}).get("hue_timeout_seconds"),
    )


def run_integration_device_action(action_id: str, device_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    action = _text(action_id).lower()
    aliases = {
        "turn_on": True,
        "light_on": True,
        "on": True,
        "turn_off": False,
        "light_off": False,
        "off": False,
    }
    if action not in aliases and action not in {"set_brightness", "set_color"}:
        raise KeyError(f"Unsupported Philips Hue device action: {action_id}")
    light_id = _text(device_id)
    if light_id.startswith("light:"):
        light_id = _text(light_id.split(":", 1)[1])
    if not light_id:
        raise ValueError("Hue light id is required.")
    settings = read_hue_settings()
    bridge = normalize_hue_bridge_root(settings.get("HUE_BRIDGE_HOST"))
    app_key = _text(settings.get("HUE_APP_KEY"))
    timeout = _bounded_int(settings.get("HUE_TIMEOUT_SECONDS"), default=HUE_DEFAULT_TIMEOUT_SECONDS, minimum=2, maximum=60)
    if not app_key:
        raise ValueError("Philips Hue is not linked.")
    body: Dict[str, Any] = {}
    if action in aliases:
        body["on"] = {"on": bool(aliases[action])}
    if action == "set_brightness":
        brightness = (payload or {}).get("brightness_pct", (payload or {}).get("brightness"))
        if brightness is None:
            raise ValueError("Hue brightness action requires brightness_pct.")
        pct = max(0.0, min(100.0, float(brightness)))
        body["on"] = {"on": pct > 0}
        if pct > 0:
            body["dimming"] = {"brightness": pct}
    if action == "set_color":
        xy = _hue_xy_from_payload(payload or {})
        if xy is None:
            raise ValueError("Hue color action requires color_name, rgb_color, or xy_color.")
        body["on"] = {"on": True}
        body["color"] = {"xy": {"x": xy[0], "y": xy[1]}}
    result = _hue_v2_put(bridge, app_key, f"light/{light_id}", body, timeout=timeout)
    return {
        "ok": True,
        "action": "turn_on" if aliases.get(action) is True else "turn_off" if aliases.get(action) is False else action,
        "device_id": light_id,
        "result": result,
    }
