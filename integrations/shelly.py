from __future__ import annotations
__version__ = "1.1.0"

import contextlib
import ipaddress
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

from helpers import redis_client

SHELLY_SETTINGS_KEY = "shelly_settings"
SHELLY_DISCOVERY_CACHE_KEY = "tater:shelly:devices:registry:v1"
SHELLY_DISCOVERY_CACHE_TTL_SECONDS = 60.0
SHELLY_DEFAULT_ENABLED = True
SHELLY_DEFAULT_TIMEOUT_SECONDS = 5
SHELLY_DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 3
SHELLY_MAX_SCAN_WORKERS = 32

INTEGRATION = {
    "id": "shelly",
    "name": "Shelly",
    "description": "Local Shelly device discovery and direct HTTP control for switches, lights, covers, and sensors.",
    "badge": "SH",
    "order": 40,
    "fields": [
        {
            "key": "shelly_enabled",
            "label": "Enable Shelly",
            "type": "checkbox",
            "default": SHELLY_DEFAULT_ENABLED,
        },
        {
            "key": "shelly_device_hosts",
            "label": "Manual Device Hosts",
            "type": "textarea",
            "default": "",
            "rows": 4,
            "placeholder": "192.168.1.42\nshellyplus1pm-a8032ab12345.local",
            "description": "Optional. One Shelly IP, host, or URL per line. Discovery also scans local private /24 networks.",
            "full_width": True,
        },
        {
            "key": "shelly_device_aliases",
            "label": "Device Names / Aliases",
            "type": "textarea",
            "default": "",
            "rows": 6,
            "placeholder": "shellyplugus-d48afc77f360 = CRT TV\n10.4.20.231 = CRT TV",
            "description": "Optional. One per line: device id, MAC, IP, host, or URL = friendly name. Discovery adds blank entries for found devices.",
            "full_width": True,
        },
        {
            "key": "shelly_username",
            "label": "Username",
            "type": "text",
            "default": "",
            "placeholder": "admin",
        },
        {
            "key": "shelly_password",
            "label": "Password",
            "type": "password",
            "default": "",
        },
        {
            "key": "shelly_timeout_seconds",
            "label": "Request Timeout Seconds",
            "type": "number",
            "default": SHELLY_DEFAULT_TIMEOUT_SECONDS,
            "min": 1,
            "max": 30,
        },
        {
            "key": "shelly_discovery_timeout_seconds",
            "label": "Discovery Timeout Seconds",
            "type": "number",
            "default": SHELLY_DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
            "min": 1,
            "max": 10,
        },
    ],
    "actions": [
        {
            "id": "discover",
            "label": "Discover Shelly Devices",
            "status": "Scans local networks and saves discovered Shelly hosts.",
        },
        {
            "id": "test",
            "label": "Test Shelly",
            "status": "Checks the first configured or discovered Shelly device.",
        },
    ],
}


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "ignore").strip()
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


def _clamp_number(value: Any, *, minimum: float, maximum: float) -> Optional[float]:
    if value is None or _text(value) == "":
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    return max(float(minimum), min(float(maximum), parsed))


def _slug(value: Any) -> str:
    token = _text(value).lower()
    out = []
    for char in token:
        if char.isalnum():
            out.append(char)
        elif out and out[-1] != "_":
            out.append("_")
    return "".join(out).strip("_")


def normalize_shelly_root(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)
    netloc = parsed.netloc or parsed.path
    if not netloc:
        return ""
    scheme = parsed.scheme or "http"
    return urlunparse((scheme, netloc, "", "", "", "")).rstrip("/")


def _split_manual_hosts(value: Any) -> List[str]:
    items: List[str] = []
    seen: set[str] = set()
    for raw in _text(value).replace(",", "\n").splitlines():
        token = _text(raw)
        if not token or token.startswith("#"):
            continue
        root = normalize_shelly_root(token)
        key = root.lower()
        if not root or key in seen:
            continue
        seen.add(key)
        items.append(root)
    return items


def _alias_key(value: Any) -> str:
    return _slug(str(value or "").replace("://", "_"))


def _parse_device_aliases(value: Any) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for raw in _text(value).splitlines():
        line = _text(raw)
        if not line or line.startswith("#") or "=" not in line:
            continue
        raw_key, raw_name = line.split("=", 1)
        key = _alias_key(raw_key)
        name = _text(raw_name)
        if key and name:
            aliases[key] = name
    return aliases


def _alias_template_lines(value: Any) -> Tuple[List[str], set[str]]:
    lines: List[str] = []
    keys: set[str] = set()
    for raw in _text(value).splitlines():
        line = _text(raw)
        if not line:
            continue
        lines.append(line)
        if line.startswith("#") or "=" not in line:
            continue
        raw_key, _raw_name = line.split("=", 1)
        key = _alias_key(raw_key)
        if key:
            keys.add(key)
    return lines, keys


def _device_alias_candidates(root: str, info: Dict[str, Any]) -> List[str]:
    parsed = urlparse(normalize_shelly_root(root))
    return [
        _device_key(root, info),
        info.get("id"),
        info.get("mac"),
        info.get("hostname"),
        parsed.hostname,
        normalize_shelly_root(root),
        root,
        info.get("type"),
        info.get("model"),
        info.get("app"),
    ]


def _device_alias(root: str, info: Dict[str, Any], settings: Dict[str, Any]) -> str:
    aliases = _parse_device_aliases((settings or {}).get("SHELLY_DEVICE_ALIASES"))
    if not aliases:
        return ""
    for candidate in _device_alias_candidates(root, info):
        name = aliases.get(_alias_key(candidate))
        if name:
            return name
    return ""


def _device_alias_template_key(device: Dict[str, Any]) -> str:
    details = device.get("details") if isinstance(device.get("details"), dict) else {}
    for key in ("device_id", "mac", "root_url"):
        value = _text(details.get(key))
        if value:
            return value
    return _text(device.get("id"))


def _merge_alias_template(value: Any, devices: List[Dict[str, Any]]) -> str:
    lines, seen_keys = _alias_template_lines(value)
    for device in devices:
        if not isinstance(device, dict):
            continue
        key = _device_alias_template_key(device)
        normalized = _alias_key(key)
        if not key or not normalized or normalized in seen_keys:
            continue
        seen_keys.add(normalized)
        lines.append(f"{key} =")
    return "\n".join(lines)


def _dedupe_roots(values: List[Any]) -> List[str]:
    roots: List[str] = []
    seen: set[str] = set()
    for value in values or []:
        root = normalize_shelly_root(value)
        key = root.lower()
        if not root or key in seen:
            continue
        roots.append(root)
        seen.add(key)
    return roots


def read_shelly_settings(client: Any = None) -> Dict[str, Any]:
    store = client or redis_client
    try:
        raw = store.hgetall(SHELLY_SETTINGS_KEY) or {}
    except Exception:
        raw = {}
    timeout = _bounded_int(
        raw.get("SHELLY_TIMEOUT_SECONDS"),
        default=SHELLY_DEFAULT_TIMEOUT_SECONDS,
        minimum=1,
        maximum=30,
    )
    discovery_timeout = _bounded_int(
        raw.get("SHELLY_DISCOVERY_TIMEOUT_SECONDS"),
        default=SHELLY_DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
        minimum=1,
        maximum=10,
    )
    return {
        "SHELLY_ENABLED": _as_bool(raw.get("SHELLY_ENABLED"), SHELLY_DEFAULT_ENABLED),
        "SHELLY_DEVICE_HOSTS": _text(raw.get("SHELLY_DEVICE_HOSTS")),
        "SHELLY_DEVICE_ALIASES": _text(raw.get("SHELLY_DEVICE_ALIASES")),
        "SHELLY_USERNAME": _text(raw.get("SHELLY_USERNAME")),
        "SHELLY_PASSWORD": _text(raw.get("SHELLY_PASSWORD")),
        "SHELLY_TIMEOUT_SECONDS": str(timeout),
        "SHELLY_DISCOVERY_TIMEOUT_SECONDS": str(discovery_timeout),
    }


def save_shelly_settings(
    *,
    enabled: Any = None,
    device_hosts: Any = None,
    device_aliases: Any = None,
    username: Any = None,
    password: Any = None,
    timeout_seconds: Any = None,
    discovery_timeout_seconds: Any = None,
    client: Any = None,
) -> Dict[str, Any]:
    store = client or redis_client
    current = read_shelly_settings(store)
    next_settings = {
        "SHELLY_ENABLED": "true"
        if _as_bool(current.get("SHELLY_ENABLED") if enabled is None else enabled, SHELLY_DEFAULT_ENABLED)
        else "false",
        "SHELLY_DEVICE_HOSTS": _text(current.get("SHELLY_DEVICE_HOSTS") if device_hosts is None else device_hosts),
        "SHELLY_DEVICE_ALIASES": _text(
            current.get("SHELLY_DEVICE_ALIASES") if device_aliases is None else device_aliases
        ),
        "SHELLY_USERNAME": _text(current.get("SHELLY_USERNAME") if username is None else username),
        "SHELLY_PASSWORD": _text(current.get("SHELLY_PASSWORD") if password is None else password),
        "SHELLY_TIMEOUT_SECONDS": str(
            _bounded_int(
                current.get("SHELLY_TIMEOUT_SECONDS") if timeout_seconds is None else timeout_seconds,
                default=SHELLY_DEFAULT_TIMEOUT_SECONDS,
                minimum=1,
                maximum=30,
            )
        ),
        "SHELLY_DISCOVERY_TIMEOUT_SECONDS": str(
            _bounded_int(
                current.get("SHELLY_DISCOVERY_TIMEOUT_SECONDS")
                if discovery_timeout_seconds is None
                else discovery_timeout_seconds,
                default=SHELLY_DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
                minimum=1,
                maximum=10,
            )
        ),
    }
    store.hset(SHELLY_SETTINGS_KEY, mapping=next_settings)
    with contextlib.suppress(Exception):
        store.delete(SHELLY_DISCOVERY_CACHE_KEY)
    return read_shelly_settings(store)


def _request_auth(settings: Dict[str, Any]) -> Tuple[str, str]:
    username = _text((settings or {}).get("SHELLY_USERNAME"))
    password = _text((settings or {}).get("SHELLY_PASSWORD"))
    return username, password


def _request_timeout(settings: Dict[str, Any]) -> int:
    return _bounded_int(
        (settings or {}).get("SHELLY_TIMEOUT_SECONDS"),
        default=SHELLY_DEFAULT_TIMEOUT_SECONDS,
        minimum=1,
        maximum=30,
    )


def _shelly_request(
    method: str,
    root: Any,
    path: str,
    *,
    settings: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: Optional[float] = None,
) -> Any:
    base = normalize_shelly_root(root)
    if not base:
        raise ValueError("Shelly device host is required.")

    conf = settings or read_shelly_settings()
    username, password = _request_auth(conf)
    timeout_s = float(timeout if timeout is not None else _request_timeout(conf))
    url_path = path if _text(path).startswith("/") else f"/{path}"
    url = f"{base}{url_path}"
    kwargs: Dict[str, Any] = {
        "params": params,
        "json": json_body,
        "timeout": max(1.0, timeout_s),
        "headers": {"Accept": "application/json"},
    }
    if json_body is not None:
        kwargs["headers"]["Content-Type"] = "application/json"
    if username or password:
        kwargs["auth"] = HTTPDigestAuth(username or "admin", password)
    response = requests.request(method.upper(), url, **kwargs)
    if response.status_code == 401 and (username or password):
        kwargs["auth"] = HTTPBasicAuth(username or "admin", password)
        response = requests.request(method.upper(), url, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"Shelly HTTP {response.status_code} calling {url}: {(response.text or '')[:240]}")
    try:
        return response.json()
    except Exception:
        return response.text


def _shelly_rpc(
    root: Any,
    method_name: str,
    *,
    settings: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: Optional[float] = None,
) -> Any:
    query: Dict[str, Any] = {}
    for key, value in (params or {}).items():
        if isinstance(value, bool):
            query[key] = "true" if value else "false"
        elif value is not None:
            query[key] = value
    return _shelly_request("GET", root, f"/rpc/{method_name}", settings=settings, params=query, timeout=timeout)


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


def _looks_like_shelly_root(root: str, settings: Dict[str, Any], timeout_s: float) -> bool:
    try:
        payload = _shelly_request("GET", root, "/shelly", settings=settings, timeout=timeout_s)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    if _text(payload.get("id")).lower().startswith("shelly"):
        return True
    if "gen" in payload and (_text(payload.get("mac")) or _text(payload.get("model"))):
        return True
    family = _text(payload.get("type") or payload.get("app") or payload.get("model")).lower()
    return "shelly" in family or family.startswith("sh")


def _discover_shelly_roots_subnet(settings: Dict[str, Any], timeout_seconds: int) -> List[str]:
    networks = _local_scan_networks()
    if not networks:
        return []

    roots: List[str] = []
    probe_timeout = max(0.25, min(1.0, float(timeout_seconds) / 3.0))
    for network in networks:
        hosts = list(network.hosts())
        if len(hosts) > 1024:
            continue
        with ThreadPoolExecutor(max_workers=SHELLY_MAX_SCAN_WORKERS) as executor:
            future_map = {
                executor.submit(_looks_like_shelly_root, f"http://{host}", settings, probe_timeout): f"http://{host}"
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
    return _dedupe_roots(roots)


def _cache_roots(roots: List[str]) -> None:
    payload = {"ts": time.time(), "roots": _dedupe_roots(roots)}
    with contextlib.suppress(Exception):
        redis_client.set(SHELLY_DISCOVERY_CACHE_KEY, json.dumps(payload, ensure_ascii=False))


def _cached_roots() -> Optional[List[str]]:
    try:
        raw = redis_client.get(SHELLY_DISCOVERY_CACHE_KEY)
        payload = json.loads(raw) if raw else {}
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        ts = float(payload.get("ts") or 0.0)
    except Exception:
        ts = 0.0
    if ts <= 0.0 or (time.time() - ts) > SHELLY_DISCOVERY_CACHE_TTL_SECONDS:
        return None
    roots = payload.get("roots")
    if not isinstance(roots, list):
        return None
    return _dedupe_roots(roots)


def discover_shelly_roots(*, force: bool = False, settings: Optional[Dict[str, Any]] = None) -> List[str]:
    conf = settings or read_shelly_settings()
    if not _as_bool(conf.get("SHELLY_ENABLED"), SHELLY_DEFAULT_ENABLED):
        return []
    manual = _split_manual_hosts(conf.get("SHELLY_DEVICE_HOSTS"))
    if not force:
        cached = _cached_roots()
        if cached is not None:
            return _dedupe_roots(manual + cached)
    timeout = _bounded_int(
        conf.get("SHELLY_DISCOVERY_TIMEOUT_SECONDS"),
        default=SHELLY_DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
        minimum=1,
        maximum=10,
    )
    discovered = _discover_shelly_roots_subnet(conf, timeout)
    roots = _dedupe_roots(manual + discovered)
    _cache_roots(roots)
    return roots


def _fetch_shelly_device(root: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    info = _shelly_request("GET", root, "/shelly", settings=settings)
    if not isinstance(info, dict):
        raise RuntimeError(f"{root} did not return Shelly device information.")
    gen = _bounded_int(info.get("gen"), default=1, minimum=1, maximum=99) if "gen" in info else 1
    result: Dict[str, Any] = {"root_url": normalize_shelly_root(root), "gen": gen, "info": info}
    if gen >= 2:
        result["status"] = _shelly_rpc(root, "Shelly.GetStatus", settings=settings)
        try:
            result["config"] = _shelly_rpc(root, "Shelly.GetConfig", settings=settings)
        except Exception:
            result["config"] = {}
        return result
    result["status"] = _shelly_request("GET", root, "/status", settings=settings)
    try:
        result["config"] = _shelly_request("GET", root, "/settings", settings=settings)
    except Exception:
        result["config"] = {}
    return result


def _device_key(root: str, info: Dict[str, Any]) -> str:
    value = _text(info.get("id") or info.get("mac") or info.get("hostname") or info.get("type"))
    if not value:
        parsed = urlparse(normalize_shelly_root(root))
        value = _text(parsed.hostname) or root
    return _slug(value) or "shelly"


def _device_name(root: str, info: Dict[str, Any], config: Dict[str, Any], settings: Optional[Dict[str, Any]] = None) -> str:
    sys_config = config.get("sys") if isinstance(config.get("sys"), dict) else {}
    sys_device = sys_config.get("device") if isinstance(sys_config.get("device"), dict) else {}
    parsed = urlparse(normalize_shelly_root(root))
    alias = _device_alias(root, info, settings or {})
    return (
        alias
        or _text(sys_device.get("name"))
        or _text(config.get("name"))
        or _text(info.get("name"))
        or _text(info.get("id"))
        or _text(info.get("type") or info.get("model") or info.get("app"))
        or _text(parsed.hostname)
        or "Shelly Device"
    )


def _component_name(
    base_name: str,
    component_type: str,
    component_id: Any,
    component_config: Optional[Dict[str, Any]] = None,
    *,
    use_base_name: bool = False,
) -> str:
    conf = component_config if isinstance(component_config, dict) else {}
    name = _text(conf.get("name"))
    if name:
        return name
    if use_base_name:
        return _text(base_name) or "Shelly Device"
    suffix = _text(component_id)
    label = {
        "switch": "Switch",
        "relay": "Switch",
        "light": "Light",
        "cover": "Cover",
        "roller": "Cover",
        "input": "Input",
        "temperature": "Temperature",
        "humidity": "Humidity",
        "illuminance": "Illuminance",
        "meter": "Meter",
    }.get(component_type, component_type.title())
    return f"{base_name} {label} {suffix}".strip()


def _component_id(device_key: str, component_type: str, component_index: Any) -> str:
    return f"shelly:{device_key}:{component_type}:{_text(component_index) or '0'}"


def _details(
    root: str,
    info: Dict[str, Any],
    component: Dict[str, Any],
    extra: Optional[Dict[str, Any]] = None,
    *,
    alias: str = "",
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "root_url": normalize_shelly_root(root),
        "device_id": info.get("id"),
        "mac": info.get("mac"),
        "model": info.get("model") or info.get("type") or info.get("app"),
        "gen": info.get("gen") or 1,
        "component": component,
    }
    if _text(alias):
        out["alias"] = _text(alias)
        out["friendly_name"] = _text(alias)
    for key in ("ver", "fw", "fw_id", "profile"):
        value = info.get(key)
        if value not in (None, ""):
            out[key] = value
    for key, value in (extra or {}).items():
        if value not in (None, ""):
            out[key] = value
    return out


def _state_from_bool(value: Any, on_text: str = "on", off_text: str = "off") -> str:
    if isinstance(value, bool):
        return on_text if value else off_text
    token = _text(value).lower()
    if token in {"1", "true", "on", "open", "active"}:
        return on_text
    if token in {"0", "false", "off", "closed", "inactive"}:
        return off_text
    return _text(value)


def _meter_details_for_index(status: Dict[str, Any], index: int) -> Dict[str, Any]:
    for key in ("meters", "emeters"):
        rows = status.get(key)
        if isinstance(rows, list) and index < len(rows) and isinstance(rows[index], dict):
            return rows[index]
    return {}


def _gen2_component_config(config: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = config.get(key) if isinstance(config, dict) else {}
    return value if isinstance(value, dict) else {}


def _gen2_control_keys(status: Dict[str, Any]) -> set[str]:
    controls: set[str] = set()
    for key, row in (status or {}).items():
        if not isinstance(row, dict) or ":" not in _text(key):
            continue
        component_type, _component_id = _text(key).split(":", 1)
        if component_type in {"switch", "light", "rgb", "rgbw", "rgbcct", "cct", "cover"}:
            controls.add(_text(key))
    return controls


def _gen2_devices(
    root: str,
    info: Dict[str, Any],
    status: Dict[str, Any],
    config: Dict[str, Any],
    settings: Dict[str, Any],
) -> List[Dict[str, Any]]:
    devices: List[Dict[str, Any]] = []
    device_key = _device_key(root, info)
    alias = _device_alias(root, info, settings)
    base_name = _device_name(root, info, config, settings)
    single_control_keys = _gen2_control_keys(status)
    for key, row in sorted((status or {}).items()):
        if not isinstance(row, dict) or ":" not in _text(key):
            continue
        component_type, component_id = _text(key).split(":", 1)
        component_config = _gen2_component_config(config, key)
        ref = _component_id(device_key, component_type, component_id)
        name = _component_name(
            base_name,
            component_type,
            component_id,
            component_config,
            use_base_name=bool(alias and len(single_control_keys) == 1 and key in single_control_keys),
        )
        component = {"type": component_type, "id": component_id, "key": key}

        if component_type == "switch":
            state = _state_from_bool(row.get("output"))
            caps = ["switch"]
            if any(item in row for item in ("apower", "voltage", "current", "aenergy")):
                caps.extend(["power_meter", "energy"])
            devices.append(
                {
                    "id": ref,
                    "name": name,
                    "type": "switch",
                    "ref": ref,
                    "capabilities": caps,
                    "actions": ["turn_on", "turn_off", "toggle"],
                    "event_sources": [{"type": "switch", "ref": ref, "state_on": "on", "state_off": "off"}],
                    "status": state,
                    "state": state,
                    "details": _details(root, info, component, row, alias=alias),
                }
            )
            continue

        if component_type in {"light", "rgb", "rgbw", "rgbcct", "cct"}:
            state = _state_from_bool(row.get("output"))
            caps = ["light", "switch"]
            if "brightness" in row or "brightness" in component_config:
                caps.append("dimmable")
            devices.append(
                {
                    "id": ref,
                    "name": name,
                    "type": "light",
                    "ref": ref,
                    "capabilities": caps,
                    "actions": ["turn_on", "turn_off", "toggle", "set_brightness"],
                    "event_sources": [{"type": "light", "ref": ref, "state_on": "on", "state_off": "off"}],
                    "status": state,
                    "state": state,
                    "details": _details(root, info, component, row, alias=alias),
                }
            )
            continue

        if component_type == "cover":
            state = _text(row.get("state")) or _text(row.get("last_direction")) or "unknown"
            devices.append(
                {
                    "id": ref,
                    "name": name,
                    "type": "cover",
                    "ref": ref,
                    "capabilities": ["cover", "open_close"],
                    "actions": ["open", "close", "stop", "set_position"],
                    "event_sources": [{"type": "cover", "ref": ref, "state_on": "open", "state_off": "closed"}],
                    "status": state,
                    "state": state,
                    "details": _details(root, info, component, row, alias=alias),
                }
            )
            continue

        if component_type in {"input", "button", "boolean"}:
            state = _state_from_bool(row.get("state") if "state" in row else row.get("input"), "on", "off")
            devices.append(
                {
                    "id": ref,
                    "name": name,
                    "type": "binary_sensor",
                    "ref": ref,
                    "capabilities": ["sensor", "binary_sensor", "input"],
                    "actions": [],
                    "event_sources": [{"type": "input", "ref": ref, "state_on": "on", "state_off": "off"}],
                    "status": state,
                    "state": state,
                    "details": _details(root, info, component, row, alias=alias),
                }
            )
            continue

        sensor_type_map = {
            "temperature": ("temperature", ["sensor", "temperature"], "temperature"),
            "humidity": ("humidity", ["sensor", "humidity"], "humidity"),
            "illuminance": ("illuminance", ["sensor", "illuminance", "light_sensor"], "illuminance"),
            "voltmeter": ("voltmeter", ["sensor", "voltage"], "voltage"),
            "pm1": ("power_meter", ["sensor", "power", "energy"], "apower"),
            "em1": ("power_meter", ["sensor", "power", "energy"], "apower"),
            "em": ("power_meter", ["sensor", "power", "energy"], "total_act_power"),
        }
        if component_type in sensor_type_map:
            device_type, caps, value_key = sensor_type_map[component_type]
            value = row.get(value_key)
            if value is None and isinstance(row.get("temperature"), dict):
                value = row.get("temperature", {}).get("tC")
            if value is None:
                value = row.get("value")
            state = _text(value) if value not in (None, "") else "unknown"
            devices.append(
                {
                    "id": ref,
                    "name": name,
                    "type": device_type,
                    "ref": ref,
                    "capabilities": caps,
                    "actions": [],
                    "event_sources": [],
                    "status": state,
                    "state": state,
                    "details": _details(root, info, component, row, alias=alias),
                }
            )
    return devices


def _gen1_devices(
    root: str,
    info: Dict[str, Any],
    status: Dict[str, Any],
    config: Dict[str, Any],
    settings: Dict[str, Any],
) -> List[Dict[str, Any]]:
    devices: List[Dict[str, Any]] = []
    device_key = _device_key(root, info)
    alias = _device_alias(root, info, settings)
    base_name = _device_name(root, info, config, settings)
    control_count = sum(
        len(rows)
        for rows in (
            status.get("relays") if isinstance(status.get("relays"), list) else [],
            status.get("lights") if isinstance(status.get("lights"), list) else [],
            status.get("rollers") if isinstance(status.get("rollers"), list) else [],
        )
    )

    relays = status.get("relays") if isinstance(status.get("relays"), list) else []
    relay_configs = config.get("relays") if isinstance(config.get("relays"), list) else []
    for index, relay in enumerate(relays):
        if not isinstance(relay, dict):
            continue
        relay_config = relay_configs[index] if index < len(relay_configs) and isinstance(relay_configs[index], dict) else {}
        ref = _component_id(device_key, "relay", index)
        state = _state_from_bool(relay.get("ison"))
        meter = _meter_details_for_index(status, index)
        caps = ["switch"]
        if meter or any(item in relay for item in ("power", "energy")):
            caps.extend(["power_meter", "energy"])
        devices.append(
            {
                "id": ref,
                "name": _component_name(
                    base_name,
                    "relay",
                    index,
                    relay_config,
                    use_base_name=bool(alias and control_count == 1),
                ),
                "type": "switch",
                "ref": ref,
                "capabilities": caps,
                "actions": ["turn_on", "turn_off", "toggle"],
                "event_sources": [{"type": "switch", "ref": ref, "state_on": "on", "state_off": "off"}],
                "status": state,
                "state": state,
                "details": _details(root, info, {"type": "relay", "id": index}, {**relay, "meter": meter}, alias=alias),
            }
        )

    lights = status.get("lights") if isinstance(status.get("lights"), list) else []
    light_configs = config.get("lights") if isinstance(config.get("lights"), list) else []
    for index, light in enumerate(lights):
        if not isinstance(light, dict):
            continue
        light_config = light_configs[index] if index < len(light_configs) and isinstance(light_configs[index], dict) else {}
        ref = _component_id(device_key, "light", index)
        state = _state_from_bool(light.get("ison"))
        caps = ["light", "switch"]
        if "brightness" in light:
            caps.append("dimmable")
        devices.append(
            {
                "id": ref,
                "name": _component_name(
                    base_name,
                    "light",
                    index,
                    light_config,
                    use_base_name=bool(alias and control_count == 1),
                ),
                "type": "light",
                "ref": ref,
                "capabilities": caps,
                "actions": ["turn_on", "turn_off", "toggle", "set_brightness"],
                "event_sources": [{"type": "light", "ref": ref, "state_on": "on", "state_off": "off"}],
                "status": state,
                "state": state,
                "details": _details(root, info, {"type": "light", "id": index}, light, alias=alias),
            }
        )

    rollers = status.get("rollers") if isinstance(status.get("rollers"), list) else []
    roller_configs = config.get("rollers") if isinstance(config.get("rollers"), list) else []
    for index, roller in enumerate(rollers):
        if not isinstance(roller, dict):
            continue
        roller_config = roller_configs[index] if index < len(roller_configs) and isinstance(roller_configs[index], dict) else {}
        ref = _component_id(device_key, "roller", index)
        state = _text(roller.get("state")) or _text(roller.get("last_direction")) or "unknown"
        devices.append(
            {
                "id": ref,
                "name": _component_name(
                    base_name,
                    "roller",
                    index,
                    roller_config,
                    use_base_name=bool(alias and control_count == 1),
                ),
                "type": "cover",
                "ref": ref,
                "capabilities": ["cover", "open_close"],
                "actions": ["open", "close", "stop", "set_position"],
                "event_sources": [{"type": "cover", "ref": ref, "state_on": "open", "state_off": "closed"}],
                "status": state,
                "state": state,
                "details": _details(root, info, {"type": "roller", "id": index}, roller, alias=alias),
            }
        )

    inputs = status.get("inputs") if isinstance(status.get("inputs"), list) else []
    for index, input_row in enumerate(inputs):
        if not isinstance(input_row, dict):
            continue
        ref = _component_id(device_key, "input", index)
        state = _state_from_bool(input_row.get("input"), "on", "off")
        devices.append(
            {
                "id": ref,
                "name": _component_name(base_name, "input", index),
                "type": "binary_sensor",
                "ref": ref,
                "capabilities": ["sensor", "binary_sensor", "input"],
                "actions": [],
                "event_sources": [{"type": "input", "ref": ref, "state_on": "on", "state_off": "off"}],
                "status": state,
                "state": state,
                "details": _details(root, info, {"type": "input", "id": index}, input_row, alias=alias),
            }
        )

    sensor_specs = [
        ("temperature", status.get("temperature"), "temperature", ["sensor", "temperature"]),
        ("temperature_f", status.get("temperature_f"), "temperature", ["sensor", "temperature"]),
        ("humidity", status.get("humidity"), "humidity", ["sensor", "humidity"]),
        ("lux", status.get("lux"), "illuminance", ["sensor", "illuminance", "light_sensor"]),
        ("illumination", status.get("illumination"), "illuminance", ["sensor", "illuminance", "light_sensor"]),
        ("battery", status.get("bat", {}).get("value") if isinstance(status.get("bat"), dict) else None, "battery", ["sensor", "battery"]),
    ]
    for key, value, device_type, caps in sensor_specs:
        if value in (None, "", [], {}):
            continue
        ref = _component_id(device_key, key, 0)
        devices.append(
            {
                "id": ref,
                "name": _component_name(base_name, device_type, 0),
                "type": device_type,
                "ref": ref,
                "capabilities": caps,
                "actions": [],
                "event_sources": [],
                "status": _text(value),
                "state": _text(value),
                "details": _details(root, info, {"type": key, "id": 0}, {"value": value}, alias=alias),
            }
        )

    emeters = status.get("emeters") if isinstance(status.get("emeters"), list) else []
    for index, meter in enumerate(emeters):
        if not isinstance(meter, dict):
            continue
        ref = _component_id(device_key, "meter", index)
        devices.append(
            {
                "id": ref,
                "name": _component_name(base_name, "meter", index),
                "type": "power_meter",
                "ref": ref,
                "capabilities": ["sensor", "power", "energy"],
                "actions": [],
                "event_sources": [],
                "status": _text(meter.get("power")),
                "state": _text(meter.get("power")),
                "details": _details(root, info, {"type": "meter", "id": index}, meter, alias=alias),
            }
        )
    return devices


def _fallback_device(
    root: str,
    info: Dict[str, Any],
    status: Dict[str, Any],
    config: Dict[str, Any],
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    device_key = _device_key(root, info)
    alias = _device_alias(root, info, settings)
    name = _device_name(root, info, config, settings)
    state = "online"
    if isinstance(status.get("wifi_sta"), dict) and status.get("wifi_sta", {}).get("connected") is False:
        state = "offline"
    ref = _component_id(device_key, "device", 0)
    return {
        "id": ref,
        "name": name,
        "type": "shelly_device",
        "ref": ref,
        "capabilities": ["network_device", "connectivity"],
        "actions": [],
        "event_sources": [{"type": "connectivity", "ref": ref, "state_on": "online", "state_off": "offline"}],
        "status": state,
        "state": state,
        "details": _details(root, info, {"type": "device", "id": 0}, status if isinstance(status, dict) else {}, alias=alias),
    }


def shelly_inventory(*, force: bool = False) -> Tuple[List[Dict[str, Any]], List[str]]:
    settings = read_shelly_settings()
    roots = discover_shelly_roots(force=force, settings=settings)
    devices: List[Dict[str, Any]] = []
    failures: List[str] = []
    for root in roots:
        try:
            item = _fetch_shelly_device(root, settings)
            info = item.get("info") if isinstance(item.get("info"), dict) else {}
            status = item.get("status") if isinstance(item.get("status"), dict) else {}
            config = item.get("config") if isinstance(item.get("config"), dict) else {}
            gen = int(item.get("gen") or info.get("gen") or 1)
            rows = (
                _gen2_devices(root, info, status, config, settings)
                if gen >= 2
                else _gen1_devices(root, info, status, config, settings)
            )
            if not rows:
                rows = [_fallback_device(root, info, status, config, settings)]
            devices.extend(rows)
        except Exception as exc:
            failures.append(f"{root} ({exc})")
    devices.sort(key=lambda item: (_text(item.get("name")).lower(), _text(item.get("id")).lower()))
    return devices, failures


def _parse_target(target: Any) -> Tuple[str, str, str]:
    token = _text(target)
    parts = token.split(":")
    if len(parts) >= 4 and parts[0].lower() == "shelly":
        return _text(parts[1]), _text(parts[2]).lower(), _text(parts[3])
    if len(parts) >= 3:
        return _text(parts[0]), _text(parts[1]).lower(), _text(parts[2])
    if len(parts) == 2:
        return "", _text(parts[0]).lower(), _text(parts[1])
    return "", "", ""


def _find_root_for_target(device_key: str, payload: Dict[str, Any], settings: Dict[str, Any]) -> str:
    root = normalize_shelly_root((payload or {}).get("root_url") or (payload or {}).get("host"))
    if root:
        return root
    wanted = _slug(device_key)
    roots: List[str] = []
    for force in (False, True):
        roots = _dedupe_roots(roots + discover_shelly_roots(force=force, settings=settings))
        for candidate in roots:
            try:
                info = _shelly_request("GET", candidate, "/shelly", settings=settings)
            except Exception:
                continue
            if not isinstance(info, dict):
                continue
            aliases = {
                _slug(info.get("id")),
                _slug(info.get("mac")),
                _slug(info.get("type")),
                _slug(info.get("model")),
                _slug(urlparse(candidate).hostname),
            }
            friendly = _device_alias(candidate, info, settings)
            if friendly:
                aliases.add(_slug(friendly))
            if wanted and wanted in aliases:
                return candidate
        if len(roots) == 1 and not wanted:
            return roots[0]
    raise ValueError(f"Shelly device was not found: {device_key}")


def _action_to_on(action: str) -> Optional[bool]:
    if action in {"turn_on", "on", "light_on", "switch_on"}:
        return True
    if action in {"turn_off", "off", "light_off", "switch_off"}:
        return False
    return None


def _position_from_payload(payload: Dict[str, Any]) -> Optional[float]:
    for key in ("position", "pos", "roller_pos", "cover_pos"):
        value = _clamp_number((payload or {}).get(key), minimum=0, maximum=100)
        if value is not None:
            return value
    return None


def _brightness_from_payload(payload: Dict[str, Any]) -> Optional[float]:
    for key in ("brightness", "level", "percent"):
        value = _clamp_number((payload or {}).get(key), minimum=0, maximum=100)
        if value is not None:
            return value
    return None


def _run_gen2_action(root: str, component_type: str, component_id: int, action: str, payload: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    timer = _clamp_number((payload or {}).get("timer") or (payload or {}).get("toggle_after"), minimum=0, maximum=86400)
    if component_type in {"switch", "relay"}:
        if action in {"toggle"}:
            result = _shelly_rpc(root, "Switch.Toggle", settings=settings, params={"id": component_id})
            return {"ok": True, "action": "toggle", "result": result}
        on = _action_to_on(action)
        if on is None:
            raise KeyError(f"Unsupported Shelly switch action: {action}")
        params: Dict[str, Any] = {"id": component_id, "on": on}
        if timer is not None:
            params["toggle_after"] = timer
        result = _shelly_rpc(root, "Switch.Set", settings=settings, params=params)
        return {"ok": True, "action": "turn_on" if on else "turn_off", "result": result}

    if component_type in {"light", "rgb", "rgbw", "rgbcct", "cct"}:
        namespaces = {
            "light": "Light",
            "rgb": "RGB",
            "rgbw": "RGBW",
            "rgbcct": "RGBCCT",
            "cct": "CCT",
        }
        namespace = namespaces.get(component_type, "Light")
        if action == "toggle":
            result = _shelly_rpc(root, f"{namespace}.Toggle", settings=settings, params={"id": component_id})
            return {"ok": True, "action": "toggle", "result": result}
        on = _action_to_on(action)
        brightness = _brightness_from_payload(payload)
        if action == "set_brightness" and brightness is not None:
            on = True
        if on is None and brightness is None:
            raise KeyError(f"Unsupported Shelly light action: {action}")
        params = {"id": component_id}
        if on is not None:
            params["on"] = on
        if brightness is not None:
            params["brightness"] = int(round(brightness))
        if timer is not None:
            params["toggle_after"] = timer
        result = _shelly_rpc(root, f"{namespace}.Set", settings=settings, params=params)
        return {"ok": True, "action": action, "result": result}

    if component_type in {"cover", "roller"}:
        if action in {"open", "cover_open"}:
            result = _shelly_rpc(root, "Cover.Open", settings=settings, params={"id": component_id})
            return {"ok": True, "action": "open", "result": result}
        if action in {"close", "cover_close"}:
            result = _shelly_rpc(root, "Cover.Close", settings=settings, params={"id": component_id})
            return {"ok": True, "action": "close", "result": result}
        if action in {"stop", "cover_stop"}:
            result = _shelly_rpc(root, "Cover.Stop", settings=settings, params={"id": component_id})
            return {"ok": True, "action": "stop", "result": result}
        position = _position_from_payload(payload)
        if action in {"set_position", "position", "cover_position"} and position is not None:
            result = _shelly_rpc(root, "Cover.GoToPosition", settings=settings, params={"id": component_id, "pos": int(round(position))})
            return {"ok": True, "action": "set_position", "position": int(round(position)), "result": result}
        raise KeyError(f"Unsupported Shelly cover action: {action}")

    raise KeyError(f"Unsupported Shelly component type: {component_type}")


def _run_gen1_action(root: str, component_type: str, component_id: int, action: str, payload: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    timer = _clamp_number((payload or {}).get("timer") or (payload or {}).get("toggle_after"), minimum=0, maximum=86400)
    if component_type in {"switch", "relay"}:
        if action == "toggle":
            turn = "toggle"
        else:
            on = _action_to_on(action)
            if on is None:
                raise KeyError(f"Unsupported Shelly relay action: {action}")
            turn = "on" if on else "off"
        params: Dict[str, Any] = {"turn": turn}
        if timer is not None:
            params["timer"] = timer
        result = _shelly_request("GET", root, f"/relay/{component_id}", settings=settings, params=params)
        return {"ok": True, "action": "toggle" if turn == "toggle" else f"turn_{turn}", "result": result}

    if component_type == "light":
        if action == "toggle":
            turn = "toggle"
        else:
            on = _action_to_on(action)
            brightness = _brightness_from_payload(payload)
            if action == "set_brightness" and brightness is not None:
                on = True
            if on is None and brightness is None:
                raise KeyError(f"Unsupported Shelly light action: {action}")
            turn = "on" if on is True else "off" if on is False else ""
        params = {}
        if turn:
            params["turn"] = turn
        brightness = _brightness_from_payload(payload)
        if brightness is not None:
            params["brightness"] = int(round(brightness))
        if timer is not None:
            params["timer"] = timer
        result = _shelly_request("GET", root, f"/light/{component_id}", settings=settings, params=params)
        return {"ok": True, "action": action, "result": result}

    if component_type in {"cover", "roller"}:
        if action in {"open", "cover_open"}:
            params = {"go": "open"}
        elif action in {"close", "cover_close"}:
            params = {"go": "close"}
        elif action in {"stop", "cover_stop"}:
            params = {"go": "stop"}
        else:
            position = _position_from_payload(payload)
            if action not in {"set_position", "position", "cover_position"} or position is None:
                raise KeyError(f"Unsupported Shelly roller action: {action}")
            params = {"go": "to_pos", "roller_pos": int(round(position))}
        result = _shelly_request("GET", root, f"/roller/{component_id}", settings=settings, params=params)
        return {"ok": True, "action": action, "result": result}

    raise KeyError(f"Unsupported Shelly component type: {component_type}")


def read_integration_settings() -> Dict[str, Any]:
    settings = read_shelly_settings()
    return {
        "shelly_enabled": _as_bool(settings.get("SHELLY_ENABLED"), SHELLY_DEFAULT_ENABLED),
        "shelly_device_hosts": settings.get("SHELLY_DEVICE_HOSTS", ""),
        "shelly_device_aliases": settings.get("SHELLY_DEVICE_ALIASES", ""),
        "shelly_username": settings.get("SHELLY_USERNAME", ""),
        "shelly_password": settings.get("SHELLY_PASSWORD", ""),
        "shelly_timeout_seconds": int(settings.get("SHELLY_TIMEOUT_SECONDS") or SHELLY_DEFAULT_TIMEOUT_SECONDS),
        "shelly_discovery_timeout_seconds": int(
            settings.get("SHELLY_DISCOVERY_TIMEOUT_SECONDS") or SHELLY_DEFAULT_DISCOVERY_TIMEOUT_SECONDS
        ),
    }


def save_integration_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    saved = save_shelly_settings(
        enabled=(payload or {}).get("shelly_enabled"),
        device_hosts=(payload or {}).get("shelly_device_hosts"),
        device_aliases=(payload or {}).get("shelly_device_aliases"),
        username=(payload or {}).get("shelly_username"),
        password=(payload or {}).get("shelly_password"),
        timeout_seconds=(payload or {}).get("shelly_timeout_seconds"),
        discovery_timeout_seconds=(payload or {}).get("shelly_discovery_timeout_seconds"),
    )
    return {
        "shelly_enabled": _as_bool(saved.get("SHELLY_ENABLED"), SHELLY_DEFAULT_ENABLED),
        "shelly_device_hosts": saved.get("SHELLY_DEVICE_HOSTS", ""),
        "shelly_device_aliases": saved.get("SHELLY_DEVICE_ALIASES", ""),
        "shelly_username": saved.get("SHELLY_USERNAME", ""),
        "shelly_password": saved.get("SHELLY_PASSWORD", ""),
        "shelly_timeout_seconds": int(saved.get("SHELLY_TIMEOUT_SECONDS") or SHELLY_DEFAULT_TIMEOUT_SECONDS),
        "shelly_discovery_timeout_seconds": int(
            saved.get("SHELLY_DISCOVERY_TIMEOUT_SECONDS") or SHELLY_DEFAULT_DISCOVERY_TIMEOUT_SECONDS
        ),
    }


def integration_status() -> Dict[str, Any]:
    settings = read_shelly_settings()
    enabled = _as_bool(settings.get("SHELLY_ENABLED"), SHELLY_DEFAULT_ENABLED)
    manual_count = len(_split_manual_hosts(settings.get("SHELLY_DEVICE_HOSTS")))
    alias_count = len(_parse_device_aliases(settings.get("SHELLY_DEVICE_ALIASES")))
    alias_text = f", {alias_count} named" if alias_count else ""
    return {
        "enabled": enabled,
        "configured": enabled,
        "message": (
            f"Shelly is enabled with {manual_count} manual host{'s' if manual_count != 1 else ''}{alias_text}."
            if enabled
            else "Shelly is disabled."
        ),
    }


def integration_devices() -> Dict[str, Any]:
    settings = read_shelly_settings()
    if not _as_bool(settings.get("SHELLY_ENABLED"), SHELLY_DEFAULT_ENABLED):
        return {"devices": [], "message": "Shelly is disabled."}
    devices, failures = shelly_inventory(force=False)
    message = f"Shelly returned {len(devices)} controllable device resource{'s' if len(devices) != 1 else ''}."
    if failures:
        message += f" {len(failures)} host{'s' if len(failures) != 1 else ''} failed."
    return {"devices": devices, "message": message, "warnings": failures}


def run_integration_action(action_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    action = _text(action_id).lower()
    if action not in {"discover", "test"}:
        raise KeyError(f"Unsupported Shelly action: {action_id}")
    if payload:
        save_integration_settings(payload)
    if action == "discover":
        devices, failures = shelly_inventory(force=True)
        roots = sorted({_text((item.get("details") or {}).get("root_url")) for item in devices if isinstance(item.get("details"), dict)})
        roots = [root for root in roots if root]
        current_settings = read_shelly_settings()
        existing_hosts = _split_manual_hosts(current_settings.get("SHELLY_DEVICE_HOSTS"))
        existing_keys = {root.lower() for root in existing_hosts}
        merged_hosts = _dedupe_roots(existing_hosts + roots)
        added_hosts = [root for root in merged_hosts if root.lower() not in existing_keys]
        alias_template = _merge_alias_template(current_settings.get("SHELLY_DEVICE_ALIASES"), devices)
        if roots or alias_template != _text(current_settings.get("SHELLY_DEVICE_ALIASES")):
            save_shelly_settings(
                device_hosts="\n".join(merged_hosts),
                device_aliases=alias_template,
            )
        saved_settings = read_integration_settings()
        blank_alias_count = sum(
            1
            for line in _text(alias_template).splitlines()
            if "=" in line and not _text(line.split("=", 1)[1])
        )
        added_text = (
            f" Added {len(added_hosts)} new host{'s' if len(added_hosts) != 1 else ''} to Manual Device Hosts."
            if added_hosts
            else " No new hosts were added."
        )
        alias_text = (
            f" {blank_alias_count} device name entr{'y is' if blank_alias_count == 1 else 'ies are'} ready to fill in."
            if blank_alias_count
            else ""
        )
        return {
            "ok": True,
            "device_count": len(devices),
            "host_count": len(roots),
            "saved_host_count": len(merged_hosts),
            "added_host_count": len(added_hosts),
            "hosts": roots,
            "added_hosts": added_hosts,
            "settings": saved_settings,
            "devices": devices,
            "warnings": failures,
            "message": (
                f"Shelly discovery found {len(roots)} host{'s' if len(roots) != 1 else ''} "
                f"and {len(devices)} resource{'s' if len(devices) != 1 else ''}."
                f"{added_text}"
                f"{alias_text}"
            ),
        }

    roots = discover_shelly_roots(force=True)
    if not roots:
        raise RuntimeError("Shelly test did not find any devices. Add manual hosts or check local network discovery.")
    settings = read_shelly_settings()
    item = _fetch_shelly_device(roots[0], settings)
    info = item.get("info") if isinstance(item.get("info"), dict) else {}
    name = _device_name(roots[0], info, item.get("config") if isinstance(item.get("config"), dict) else {}, settings)
    return {
        "ok": True,
        "root_url": roots[0],
        "name": name,
        "gen": item.get("gen") or info.get("gen") or 1,
        "message": f"Shelly connection worked. First device: {name}.",
    }


def run_integration_device_action(action_id: str, device_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    action = _text(action_id).lower()
    device_key, component_type, component_id_text = _parse_target(device_id)
    payload_data = payload or {}
    if not component_type:
        component_type = _text(payload_data.get("component_type")).lower()
    if not component_id_text:
        component_id_text = _text(payload_data.get("component_id") or 0)
    if not component_type:
        raise ValueError("Shelly component type is required.")
    component_type = "switch" if component_type == "relay" else component_type
    try:
        component_id = int(float(component_id_text or 0))
    except Exception:
        raise ValueError(f"Shelly component id must be numeric: {component_id_text}")

    settings = read_shelly_settings()
    root = _find_root_for_target(device_key, payload_data, settings)
    info = _shelly_request("GET", root, "/shelly", settings=settings)
    gen = _bounded_int(info.get("gen"), default=1, minimum=1, maximum=99) if isinstance(info, dict) and "gen" in info else 1
    result = (
        _run_gen2_action(root, component_type, component_id, action, payload_data, settings)
        if gen >= 2
        else _run_gen1_action(root, component_type, component_id, action, payload_data, settings)
    )
    result.setdefault("ok", True)
    result.setdefault("action", action)
    result["device_id"] = _text(device_id)
    result["root_url"] = root
    result["component_type"] = component_type
    result["component_id"] = component_id
    return result
