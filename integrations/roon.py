from __future__ import annotations

__version__ = "1.1.5"

import contextlib
import json
import re
import socket
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from helpers import redis_client

ROON_SETTINGS_KEY = "roon_settings"
ROON_ZONE_CACHE_KEY = "tater:roon:zones:registry:v1"
ROON_ZONE_CACHE_TTL_SECONDS = 60
ROON_DEFAULT_ENABLED = True
ROON_DEFAULT_CORE_PORT = 9330
ROON_FALLBACK_CORE_PORTS = (9100,)
ROON_DEFAULT_TIMEOUT_SECONDS = 10
ROON_PAIRING_TIMEOUT_SECONDS = 120
ROON_DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 3
ROON_EXTENSION_ID = "com.taterassistant.roon"
ROON_EXTENSION_NAME = "Tater Roon"
ROON_TRANSPORT_SERVICE = "com.roonlabs.transport:2"
ROON_BROWSE_SERVICE = "com.roonlabs.browse:1"
ROON_PING_SERVICE = "com.roonlabs.ping:1"
ROON_REGISTRY_SERVICE = "com.roonlabs.registry:1"
ROON_DISCOVERY_SERVICE_ID = "00720724-5143-4a9b-abac-0e50cba674bb"
ROON_SOOD_PORT = 9003
ROON_SOOD_MULTICAST_IP = "239.255.90.90"
ROON_PUBLISHER = "Tater Assistant"
ROON_EMAIL = "support@taterassistant.com"
ROON_WEBSITE = "https://taterassistant.com"

INTEGRATION = {
    "id": "roon",
    "name": "Roon",
    "description": "Roon Core pairing, zone transport controls, and library browse playback targets.",
    "badge": "RO",
    "order": 55,
    "fields": [
        {
            "key": "roon_enabled",
            "label": "Enable Roon",
            "type": "checkbox",
            "default": ROON_DEFAULT_ENABLED,
        },
        {
            "key": "roon_core_host",
            "label": "Core Host",
            "type": "text",
            "default": "",
            "placeholder": "192.168.1.20",
            "description": "Optional. Leave blank and use Discover Roon Core, or enter the Roon Core IP/host manually.",
        },
        {
            "key": "roon_core_port",
            "label": "Core API Port",
            "type": "number",
            "default": ROON_DEFAULT_CORE_PORT,
            "min": 1,
            "max": 65535,
        },
        {
            "key": "roon_zone_aliases",
            "label": "Zone Names / Aliases",
            "type": "textarea",
            "default": "",
            "rows": 6,
            "placeholder": "Living Room = main sats\nKitchen = kitchen sats",
            "description": "Optional. One per line: Roon zone/output id or name = friendly name. Test adds blank entries for current zones.",
            "full_width": True,
        },
        {
            "key": "roon_timeout_seconds",
            "label": "Request Timeout Seconds",
            "type": "number",
            "default": ROON_DEFAULT_TIMEOUT_SECONDS,
            "min": 2,
            "max": 60,
        },
        {
            "key": "roon_discovery_timeout_seconds",
            "label": "Discovery Timeout Seconds",
            "type": "number",
            "default": ROON_DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
            "min": 1,
            "max": 10,
        },
    ],
    "actions": [
        {
            "id": "discover",
            "label": "Discover Roon Core",
            "status": "Uses Roon discovery and saves the first Core it finds.",
        },
        {
            "id": "test",
            "label": "Pair / Test Roon",
            "status": "Connects to the Roon Core, pairs this extension, and lists zones.",
        },
    ],
}


class RoonPairingRequired(RuntimeError):
    pass


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


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _text(value).lower())


def _decode_json(value: Any, default: Any) -> Any:
    raw = _text(value)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _read_raw(client: Any = None) -> Dict[str, Any]:
    store = client or redis_client
    try:
        raw = store.hgetall(ROON_SETTINGS_KEY) or {}
    except Exception:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def _parse_tokens(value: Any) -> Dict[str, str]:
    parsed = _decode_json(value, {})
    if not isinstance(parsed, dict):
        return {}
    return {_text(key): _text(token) for key, token in parsed.items() if _text(key) and _text(token)}


def _token_for_core(settings: Dict[str, Any], core_id: Any) -> str:
    core = _text(core_id)
    tokens = _parse_tokens(settings.get("ROON_TOKENS_JSON"))
    if core and tokens.get(core):
        return tokens[core]
    if core and _text(settings.get("ROON_CORE_ID")) == core:
        return _text(settings.get("ROON_TOKEN"))
    return ""


def _parse_zone_aliases(value: Any) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for raw_line in _text(value).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, alias = line.split("=", 1)
        elif ":" in line:
            key, alias = line.split(":", 1)
        else:
            continue
        clean_key = _text(key)
        clean_alias = _text(alias)
        if not clean_key or not clean_alias:
            continue
        aliases[clean_key.lower()] = clean_alias
        slug = _slug(clean_key)
        if slug:
            aliases[slug] = clean_alias
    return aliases


def _alias_keys_for_zone(zone: Dict[str, Any]) -> List[str]:
    keys: List[str] = []
    for value in (zone.get("zone_id"), zone.get("display_name")):
        token = _text(value)
        if token:
            keys.extend([token.lower(), _slug(token)])
    outputs = zone.get("outputs") if isinstance(zone.get("outputs"), list) else []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        for value in (output.get("output_id"), output.get("display_name")):
            token = _text(value)
            if token:
                keys.extend([token.lower(), _slug(token)])
    return [key for key in keys if key]


def _zone_alias(zone: Dict[str, Any], settings: Dict[str, Any]) -> str:
    aliases = _parse_zone_aliases(settings.get("ROON_ZONE_ALIASES"))
    for key in _alias_keys_for_zone(zone):
        if aliases.get(key):
            return aliases[key]
    return ""


def _merge_zone_alias_template(existing: Any, zones: List[Dict[str, Any]]) -> str:
    existing_text = _text(existing)
    lines = existing_text.splitlines() if existing_text else []
    known: set[str] = set()
    for line in lines:
        if "=" in line:
            key = _text(line.split("=", 1)[0])
        elif ":" in line:
            key = _text(line.split(":", 1)[0])
        else:
            key = _text(line)
        if key:
            known.add(key.lower())
            known.add(_slug(key))
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        key = _text(zone.get("display_name")) or _text(zone.get("zone_id"))
        if not key:
            continue
        if key.lower() in known or _slug(key) in known:
            continue
        lines.append(f"{key} =")
        known.add(key.lower())
        known.add(_slug(key))
    return "\n".join(lines).strip()


def read_roon_settings(client: Any = None) -> Dict[str, Any]:
    raw = _read_raw(client)
    port = _bounded_int(raw.get("ROON_CORE_PORT"), default=ROON_DEFAULT_CORE_PORT, minimum=1, maximum=65535)
    timeout = _bounded_int(raw.get("ROON_TIMEOUT_SECONDS"), default=ROON_DEFAULT_TIMEOUT_SECONDS, minimum=2, maximum=60)
    discovery_timeout = _bounded_int(
        raw.get("ROON_DISCOVERY_TIMEOUT_SECONDS"),
        default=ROON_DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
        minimum=1,
        maximum=10,
    )
    tokens = _parse_tokens(raw.get("ROON_TOKENS_JSON"))
    legacy_core_id = _text(raw.get("ROON_CORE_ID"))
    legacy_token = _text(raw.get("ROON_TOKEN"))
    if legacy_core_id and legacy_token and legacy_core_id not in tokens:
        tokens[legacy_core_id] = legacy_token
    return {
        "ROON_ENABLED": _as_bool(raw.get("ROON_ENABLED"), ROON_DEFAULT_ENABLED),
        "ROON_CORE_HOST": _text(raw.get("ROON_CORE_HOST")),
        "ROON_CORE_PORT": str(port),
        "ROON_TIMEOUT_SECONDS": str(timeout),
        "ROON_DISCOVERY_TIMEOUT_SECONDS": str(discovery_timeout),
        "ROON_ZONE_ALIASES": _text(raw.get("ROON_ZONE_ALIASES")),
        "ROON_CORE_ID": legacy_core_id,
        "ROON_CORE_NAME": _text(raw.get("ROON_CORE_NAME")),
        "ROON_TOKEN": legacy_token,
        "ROON_TOKENS_JSON": json.dumps(tokens, sort_keys=True),
    }


def save_roon_settings(
    *,
    enabled: Any = None,
    core_host: Any = None,
    core_port: Any = None,
    timeout_seconds: Any = None,
    discovery_timeout_seconds: Any = None,
    zone_aliases: Any = None,
    core_id: Any = None,
    core_name: Any = None,
    token: Any = None,
    client: Any = None,
) -> Dict[str, Any]:
    store = client or redis_client
    current = read_roon_settings(store)
    tokens = _parse_tokens(current.get("ROON_TOKENS_JSON"))
    next_core_id = _text(current.get("ROON_CORE_ID") if core_id is None else core_id)
    next_token = _text(current.get("ROON_TOKEN") if token is None else token)
    if next_core_id and next_token:
        tokens[next_core_id] = next_token
    mapping = {
        "ROON_ENABLED": "true"
        if _as_bool(current.get("ROON_ENABLED") if enabled is None else enabled, ROON_DEFAULT_ENABLED)
        else "false",
        "ROON_CORE_HOST": _text(current.get("ROON_CORE_HOST") if core_host is None else core_host),
        "ROON_CORE_PORT": str(
            _bounded_int(
                current.get("ROON_CORE_PORT") if core_port is None else core_port,
                default=ROON_DEFAULT_CORE_PORT,
                minimum=1,
                maximum=65535,
            )
        ),
        "ROON_TIMEOUT_SECONDS": str(
            _bounded_int(
                current.get("ROON_TIMEOUT_SECONDS") if timeout_seconds is None else timeout_seconds,
                default=ROON_DEFAULT_TIMEOUT_SECONDS,
                minimum=2,
                maximum=60,
            )
        ),
        "ROON_DISCOVERY_TIMEOUT_SECONDS": str(
            _bounded_int(
                current.get("ROON_DISCOVERY_TIMEOUT_SECONDS")
                if discovery_timeout_seconds is None
                else discovery_timeout_seconds,
                default=ROON_DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
                minimum=1,
                maximum=10,
            )
        ),
        "ROON_ZONE_ALIASES": _text(current.get("ROON_ZONE_ALIASES") if zone_aliases is None else zone_aliases),
        "ROON_CORE_ID": next_core_id,
        "ROON_CORE_NAME": _text(current.get("ROON_CORE_NAME") if core_name is None else core_name),
        "ROON_TOKEN": next_token,
        "ROON_TOKENS_JSON": json.dumps(tokens, sort_keys=True),
    }
    store.hset(ROON_SETTINGS_KEY, mapping=mapping)
    with contextlib.suppress(Exception):
        store.delete(ROON_ZONE_CACHE_KEY)
    return read_roon_settings(store)


def _encode_sood_query(props: Dict[str, str]) -> bytes:
    payload = bytearray(b"SOOD")
    payload.append(2)
    payload.extend(b"Q")
    for name, value in props.items():
        name_bytes = _text(name).encode("utf-8")
        value_bytes = _text(value).encode("utf-8")
        if not name_bytes or len(name_bytes) > 255 or len(value_bytes) > 65534:
            continue
        payload.append(len(name_bytes))
        payload.extend(name_bytes)
        payload.extend(len(value_bytes).to_bytes(2, "big"))
        payload.extend(value_bytes)
    return bytes(payload)


def _parse_sood_message(data: bytes, addr: Tuple[str, int]) -> Optional[Dict[str, Any]]:
    if len(data) < 6 or data[:4] != b"SOOD" or data[4] != 2:
        return None
    props: Dict[str, Optional[str]] = {}
    pos = 6
    while pos < len(data):
        name_len = data[pos]
        pos += 1
        if name_len <= 0 or pos + name_len > len(data):
            return None
        name = data[pos : pos + name_len].decode("utf-8", "ignore")
        pos += name_len
        if pos + 2 > len(data):
            return None
        value_len = int.from_bytes(data[pos : pos + 2], "big")
        pos += 2
        if value_len == 65535:
            props[name] = None
            continue
        if pos + value_len > len(data):
            return None
        props[name] = data[pos : pos + value_len].decode("utf-8", "ignore")
        pos += value_len
    reply_host = _text(props.pop("_replyaddr", "") or addr[0])
    reply_port = _bounded_int(props.pop("_replyport", "") or addr[1], default=addr[1], minimum=1, maximum=65535)
    return {
        "type": chr(data[5]),
        "from": {"ip": reply_host, "port": reply_port},
        "props": props,
    }


def discover_roon_cores(timeout_s: Any = None) -> List[Dict[str, Any]]:
    timeout = _bounded_int(
        timeout_s,
        default=ROON_DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
        minimum=1,
        maximum=10,
    )
    query = _encode_sood_query(
        {
            "query_service_id": ROON_DISCOVERY_SERVICE_ID,
            "_tid": str(uuid.uuid4()),
        }
    )
    cores: List[Dict[str, Any]] = []
    seen: set[str] = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        with contextlib.suppress(Exception):
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.bind(("", 0))
        for target in ((ROON_SOOD_MULTICAST_IP, ROON_SOOD_PORT), ("255.255.255.255", ROON_SOOD_PORT)):
            with contextlib.suppress(Exception):
                sock.sendto(query, target)
        deadline = time.monotonic() + float(timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(max(0.05, remaining))
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                break
            except OSError:
                break
            message = _parse_sood_message(data, addr)
            if not message:
                continue
            props = message.get("props") if isinstance(message.get("props"), dict) else {}
            if _text(props.get("service_id")) != ROON_DISCOVERY_SERVICE_ID:
                continue
            host = _text((message.get("from") or {}).get("ip"))
            port = _bounded_int(props.get("http_port"), default=ROON_DEFAULT_CORE_PORT, minimum=1, maximum=65535)
            if not host:
                continue
            key = _text(props.get("unique_id")) or f"{host}:{port}"
            if key in seen:
                continue
            seen.add(key)
            cores.append(
                {
                    "id": key,
                    "host": host,
                    "port": port,
                    "name": _text(props.get("name")) or _text(props.get("display_name")) or "Roon Core",
                    "unique_id": _text(props.get("unique_id")),
                    "service_id": _text(props.get("service_id")),
                    "source": "discovery",
                }
            )
    finally:
        with contextlib.suppress(Exception):
            sock.close()
    return cores


def _moo_request(name: str, request_id: int, body: Any = None) -> bytes:
    payload = b""
    header = f"MOO/1 REQUEST {name}\nRequest-Id: {request_id}\n"
    if body is not None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        header += f"Content-Length: {len(payload)}\nContent-Type: application/json\n"
    return header.encode("utf-8") + b"\n" + payload


def _moo_complete(name: str, request_id: Any, body: Any = None) -> bytes:
    payload = b""
    header = f"MOO/1 COMPLETE {name}\nRequest-Id: {_text(request_id)}\n"
    if body is not None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        header += f"Content-Length: {len(payload)}\nContent-Type: application/json\n"
    return header.encode("utf-8") + b"\n" + payload


def _parse_moo_message(data: Any) -> Dict[str, Any]:
    if isinstance(data, str):
        raw = data.encode("utf-8")
    else:
        raw = bytes(data or b"")
    if not raw:
        raise ValueError("Empty Roon MOO message.")
    header_end = raw.find(b"\n\n")
    if header_end < 0:
        raise ValueError("Roon MOO message is missing a header terminator.")
    header_lines = raw[:header_end].decode("utf-8", "ignore").split("\n")
    first = header_lines[0].rstrip("\r")
    match = re.match(r"^MOO/([0-9]+) ([A-Z]+) (.*)$", first)
    if not match:
        raise ValueError(f"Invalid Roon MOO first line: {first}")
    verb = match.group(2)
    name = match.group(3).strip()
    message: Dict[str, Any] = {"verb": verb, "name": name, "headers": {}}
    if verb == "REQUEST" and "/" in name:
        service, request_name = name.rsplit("/", 1)
        message["service"] = service
        message["name"] = request_name
    for line in header_lines[1:]:
        clean = line.rstrip("\r")
        if not clean or ":" not in clean:
            continue
        key, value = clean.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key == "Request-Id":
            message["request_id"] = value
        elif key == "Content-Length":
            with contextlib.suppress(Exception):
                message["content_length"] = int(value)
        elif key == "Content-Type":
            message["content_type"] = value
        else:
            message["headers"][key] = value
    body_start = header_end + 2
    content_length = int(message.get("content_length") or 0)
    if content_length > 0:
        body_bytes = raw[body_start : body_start + content_length]
        if _text(message.get("content_type")).lower() == "application/json":
            message["body"] = json.loads(body_bytes.decode("utf-8"))
        else:
            message["body"] = body_bytes
    return message


class RoonClient:
    def __init__(self, host: Any, port: Any, *, timeout_s: Any = None) -> None:
        self.host = _text(host)
        self.port = _bounded_int(port, default=ROON_DEFAULT_CORE_PORT, minimum=1, maximum=65535)
        self.timeout_s = float(
            _bounded_int(timeout_s, default=ROON_DEFAULT_TIMEOUT_SECONDS, minimum=2, maximum=60)
        )
        self._request_id = 0
        self._ws: Any = None

    def __enter__(self) -> "RoonClient":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def connect(self) -> None:
        if not self.host:
            raise ValueError("Roon Core host is required.")
        try:
            import websocket
        except Exception as exc:
            raise RuntimeError(f"websocket-client is required for Roon: {exc}") from exc
        self._websocket_module = websocket
        url = f"ws://{self.host}:{self.port}/api"
        self._ws = websocket.create_connection(url, timeout=self.timeout_s)

    def close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                self._ws.close()
        self._ws = None

    def _send(self, payload: bytes) -> None:
        if self._ws is None:
            raise RuntimeError("Roon websocket is not connected.")
        self._ws.send(payload, opcode=self._websocket_module.ABNF.OPCODE_BINARY)

    def _respond_to_request(self, message: Dict[str, Any]) -> None:
        request_id = message.get("request_id")
        service = _text(message.get("service"))
        name = _text(message.get("name"))
        if service == ROON_PING_SERVICE and name == "ping":
            self._send(_moo_complete("Success", request_id))
            return
        self._send(
            _moo_complete(
                "InvalidRequest",
                request_id,
                {"error": f"unknown service request: {service}/{name}"},
            )
        )

    def request(self, name: str, body: Any = None, *, timeout_s: Any = None) -> Dict[str, Any]:
        if self._ws is None:
            raise RuntimeError("Roon websocket is not connected.")
        self._request_id += 1
        request_id = self._request_id
        self._send(_moo_request(name, request_id, body))
        timeout = float(timeout_s or self.timeout_s)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for Roon response to {name}.")
            self._ws.settimeout(max(0.05, remaining))
            try:
                incoming = self._ws.recv()
            except (socket.timeout, TimeoutError) as exc:
                raise TimeoutError(f"Timed out waiting for Roon response to {name}.") from exc
            except Exception as exc:
                timeout_exc = getattr(self._websocket_module, "WebSocketTimeoutException", None)
                if timeout_exc is not None and isinstance(exc, timeout_exc):
                    raise TimeoutError(f"Timed out waiting for Roon response to {name}.") from exc
                raise
            message = _parse_moo_message(incoming)
            if _text(message.get("verb")) == "REQUEST":
                self._respond_to_request(message)
                continue
            if _text(message.get("request_id")) != str(request_id):
                continue
            verb = _text(message.get("verb"))
            if verb in {"COMPLETE", "CONTINUE"}:
                return message

    def register(self, *, token: Any = None, timeout_s: Any = None) -> Dict[str, Any]:
        extension = {
            "extension_id": ROON_EXTENSION_ID,
            "display_name": ROON_EXTENSION_NAME,
            "display_version": __version__,
            "publisher": ROON_PUBLISHER,
            "email": ROON_EMAIL,
            "website": ROON_WEBSITE,
            "required_services": [ROON_TRANSPORT_SERVICE, ROON_BROWSE_SERVICE],
            "optional_services": [],
            "provided_services": [ROON_PING_SERVICE],
        }
        saved_token = _text(token)
        if saved_token:
            extension["token"] = saved_token
        return self.request(f"{ROON_REGISTRY_SERVICE}/register", extension, timeout_s=timeout_s)

    def info(self) -> Dict[str, Any]:
        message = self.request(f"{ROON_REGISTRY_SERVICE}/info")
        if _text(message.get("name")) not in {"Success", "Core"}:
            raise RuntimeError(f"Roon Core info failed: {_text(message.get('name')) or message}")
        body = message.get("body")
        return body if isinstance(body, dict) else {}

    def connect_and_register(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        core = self.info()
        token = _token_for_core(settings, core.get("core_id"))
        register_timeout = max(float(self.timeout_s), float(ROON_PAIRING_TIMEOUT_SECONDS if not token else 30))
        try:
            registration = self.register(token=token, timeout_s=register_timeout)
        except TimeoutError as exc:
            raise RoonPairingRequired(
                "Roon found the Core, but Tater has not been enabled yet. Click Pair / Test Roon, then open "
                "Roon Settings > Setup > Extensions and enable Tater Roon while Tater is still waiting."
            ) from exc
        if _text(registration.get("name")) != "Registered":
            raise RoonPairingRequired(
                f"Roon did not authorize Tater yet ({_text(registration.get('name')) or 'no response'}). "
                "Open Roon Settings > Setup > Extensions, enable Tater Roon, then run Pair / Test Roon again."
            )
        body = registration.get("body") if isinstance(registration.get("body"), dict) else {}
        merged = dict(core)
        merged.update(body)
        return merged

    def transport(self, method: str, body: Any = None) -> Dict[str, Any]:
        message = self.request(f"{ROON_TRANSPORT_SERVICE}/{method}", body)
        if _text(message.get("name")) != "Success":
            raise RuntimeError(f"Roon transport {method} failed: {_text(message.get('name')) or message}")
        payload = message.get("body")
        return payload if isinstance(payload, dict) else {}

    def browse(self, method: str, body: Any = None) -> Dict[str, Any]:
        message = self.request(f"{ROON_BROWSE_SERVICE}/{method}", body)
        if _text(message.get("name")) != "Success":
            raise RuntimeError(f"Roon browse {method} failed: {_text(message.get('name')) or message}")
        payload = message.get("body")
        return payload if isinstance(payload, dict) else {}


def _cache_zones(zones: List[Dict[str, Any]], core: Optional[Dict[str, Any]] = None) -> None:
    payload = {
        "zones": zones,
        "core": core or {},
        "cached_at": time.time(),
    }
    with contextlib.suppress(Exception):
        redis_client.setex(ROON_ZONE_CACHE_KEY, ROON_ZONE_CACHE_TTL_SECONDS, json.dumps(payload))


def _cached_zones() -> List[Dict[str, Any]]:
    try:
        raw = redis_client.get(ROON_ZONE_CACHE_KEY)
    except Exception:
        raw = None
    payload = _decode_json(raw, {})
    zones = payload.get("zones") if isinstance(payload, dict) else []
    return zones if isinstance(zones, list) else []


def _configured_core(settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    host = _text(settings.get("ROON_CORE_HOST"))
    if not host:
        return None
    return {
        "host": host,
        "port": _bounded_int(settings.get("ROON_CORE_PORT"), default=ROON_DEFAULT_CORE_PORT, minimum=1, maximum=65535),
        "name": _text(settings.get("ROON_CORE_NAME")) or "Roon Core",
        "source": "settings",
    }


def _core_candidates(settings: Dict[str, Any], *, discover: bool = True) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    configured = _configured_core(settings)
    if configured:
        configured_port = _bounded_int(
            configured.get("port"),
            default=ROON_DEFAULT_CORE_PORT,
            minimum=1,
            maximum=65535,
        )
        candidates.append(configured)
        for fallback_port in ROON_FALLBACK_CORE_PORTS:
            if configured_port == fallback_port:
                continue
            fallback = dict(configured)
            fallback["port"] = fallback_port
            fallback["source"] = "settings-port-fallback"
            candidates.append(fallback)
    if discover:
        timeout = _bounded_int(
            settings.get("ROON_DISCOVERY_TIMEOUT_SECONDS"),
            default=ROON_DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
            minimum=1,
            maximum=10,
        )
        for core in discover_roon_cores(timeout):
            candidates.append(core)
    seen: set[str] = set()
    deduped: List[Dict[str, Any]] = []
    for core in candidates:
        key = f"{_text(core.get('host')).lower()}:{core.get('port')}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(core)
    return deduped


def _save_pairing_from_core(core: Dict[str, Any], host: Any, port: Any, settings: Dict[str, Any]) -> Dict[str, Any]:
    core_id = _text(core.get("core_id"))
    token = _text(core.get("token"))
    core_name = _text(core.get("display_name") or core.get("name") or settings.get("ROON_CORE_NAME"))
    return save_roon_settings(
        core_host=host,
        core_port=port,
        core_id=core_id or settings.get("ROON_CORE_ID"),
        core_name=core_name,
        token=token or _token_for_core(settings, core_id),
    )


def roon_get_zones(*, force: bool = False, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    current = settings or read_roon_settings()
    if not force:
        cached = _cached_zones()
        if cached:
            return {"zones": cached, "core": {}, "cached": True}
    errors: List[str] = []
    for core_candidate in _core_candidates(current, discover=not bool(_configured_core(current))):
        host = _text(core_candidate.get("host"))
        port = _bounded_int(core_candidate.get("port"), default=ROON_DEFAULT_CORE_PORT, minimum=1, maximum=65535)
        try:
            with RoonClient(
                host,
                port,
                timeout_s=current.get("ROON_TIMEOUT_SECONDS"),
            ) as client:
                core = client.connect_and_register(current)
                zones_payload = client.transport("get_zones")
                zones = zones_payload.get("zones") if isinstance(zones_payload.get("zones"), list) else []
                saved = _save_pairing_from_core(core, host, port, current)
                alias_template = _merge_zone_alias_template(saved.get("ROON_ZONE_ALIASES"), zones)
                if alias_template != _text(saved.get("ROON_ZONE_ALIASES")):
                    saved = save_roon_settings(zone_aliases=alias_template)
                _cache_zones(zones, core)
                return {"zones": zones, "core": core, "settings": saved, "cached": False}
        except RoonPairingRequired:
            raise
        except Exception as exc:
            errors.append(f"{host}:{port} - {exc}")
    if errors:
        raise RuntimeError("Roon connection failed. " + " | ".join(errors[:3]))
    raise RuntimeError("No Roon Core is configured or discovered. Enter the Core host manually or run Discover Roon Core.")


def _now_playing_text(now_playing: Any) -> str:
    if not isinstance(now_playing, dict):
        return ""
    for key in ("three_line", "two_line", "one_line"):
        group = now_playing.get(key)
        if not isinstance(group, dict):
            continue
        parts = [_text(group.get(line_key)) for line_key in ("line1", "line2", "line3")]
        text = " - ".join(part for part in parts if part)
        if text:
            return text
    return ""


def _zone_volume_summary(zone: Dict[str, Any]) -> Dict[str, Any]:
    outputs = zone.get("outputs") if isinstance(zone.get("outputs"), list) else []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        volume = output.get("volume") if isinstance(output.get("volume"), dict) else {}
        if volume:
            return {
                "output_id": output.get("output_id"),
                "output_name": output.get("display_name"),
                "type": volume.get("type"),
                "value": volume.get("value"),
                "min": volume.get("min"),
                "max": volume.get("max"),
                "step": volume.get("step"),
                "is_muted": volume.get("is_muted"),
            }
    return {}


def _zone_device(zone: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    zone_id = _text(zone.get("zone_id"))
    alias = _zone_alias(zone, settings)
    name = alias or _text(zone.get("display_name")) or zone_id or "Roon Zone"
    state = _text(zone.get("state")) or "unknown"
    outputs = zone.get("outputs") if isinstance(zone.get("outputs"), list) else []
    output_names = [_text(output.get("display_name")) for output in outputs if isinstance(output, dict)]
    output_names = [name for name in output_names if name]
    capabilities = ["speaker", "media_player", "audio_output", "music", "library_browse", "roon", "roon_zone"]
    if _zone_volume_summary(zone):
        capabilities.extend(["volume", "mute"])
    actions = ["play", "pause", "playpause", "stop", "next", "previous", "play_media", "queue_media"]
    if "volume" in capabilities:
        actions.extend(["set_volume", "volume_up", "volume_down", "mute", "unmute"])
    return {
        "id": zone_id,
        "name": name,
        "type": "roon_zone",
        "ref": f"zone:{zone_id}" if zone_id else "",
        "capabilities": capabilities,
        "actions": actions,
        "state": state,
        "status": state,
        "details": {
            "zone_id": zone_id,
            "alias": alias,
            "roon_display_name": zone.get("display_name"),
            "now_playing": _now_playing_text(zone.get("now_playing")),
            "outputs": outputs,
            "output_names": output_names,
            "volume": _zone_volume_summary(zone),
            "is_play_allowed": zone.get("is_play_allowed"),
            "is_pause_allowed": zone.get("is_pause_allowed"),
            "is_next_allowed": zone.get("is_next_allowed"),
            "is_previous_allowed": zone.get("is_previous_allowed"),
            "queue_items_remaining": zone.get("queue_items_remaining"),
            "queue_time_remaining": zone.get("queue_time_remaining"),
        },
    }


def _target_tokens_for_zone(zone: Dict[str, Any], settings: Dict[str, Any]) -> set[str]:
    tokens = set(_alias_keys_for_zone(zone))
    alias = _zone_alias(zone, settings)
    if alias:
        tokens.add(alias.lower())
        tokens.add(_slug(alias))
    zone_id = _text(zone.get("zone_id"))
    if zone_id:
        tokens.add(f"zone:{zone_id}".lower())
        tokens.add(f"roon:{zone_id}".lower())
    return {token for token in tokens if token}


def _find_zone(zones: List[Dict[str, Any]], target: Any, settings: Dict[str, Any]) -> Dict[str, Any]:
    raw = _text(target)
    candidates = {
        raw.lower(),
        _slug(raw),
    }
    for prefix in ("zone:", "roon:", "output:"):
        if raw.lower().startswith(prefix):
            stripped = raw[len(prefix) :]
            candidates.add(stripped.lower())
            candidates.add(_slug(stripped))
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        if candidates & _target_tokens_for_zone(zone, settings):
            return zone
    raise ValueError(f"Roon zone was not found: {target}")


def _volume_outputs(zone: Dict[str, Any], output_id: Any = None) -> List[Dict[str, Any]]:
    wanted = _text(output_id)
    outputs = zone.get("outputs") if isinstance(zone.get("outputs"), list) else []
    matches: List[Dict[str, Any]] = []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        volume = output.get("volume") if isinstance(output.get("volume"), dict) else {}
        if not volume:
            continue
        if wanted and _text(output.get("output_id")) != wanted and _text(output.get("display_name")) != wanted:
            continue
        matches.append(output)
    if not matches:
        raise ValueError("Roon zone has no controllable volume output.")
    return matches


def _norm_media_kind(value: Any) -> str:
    token = _text(value).lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "song": "track",
        "songs": "track",
        "tracks": "track",
        "band": "artist",
        "artists": "artist",
        "albums": "album",
        "genres": "genre",
        "playlists": "playlist",
        "stations": "radio",
        "station": "radio",
        "internet_radio": "radio",
    }
    token = aliases.get(token, token)
    return token if token in {"track", "artist", "album", "genre", "playlist", "radio", "any"} else "any"


def _media_hierarchy(media_kind: str) -> str:
    return {
        "artist": "artists",
        "album": "albums",
        "genre": "genres",
        "playlist": "playlists",
        "radio": "internet_radio",
    }.get(media_kind, "browse")


def _browse_load_items(
    client: RoonClient,
    *,
    hierarchy: str,
    session_key: str,
    offset: int = 0,
    count: int = 100,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    result = client.browse(
        "load",
        {
            "hierarchy": hierarchy,
            "multi_session_key": session_key,
            "offset": offset,
            "count": count,
            "set_display_offset": offset,
        },
    )
    items = result.get("items") if isinstance(result.get("items"), list) else []
    return [item for item in items if isinstance(item, dict)], result


def _browse_select(
    client: RoonClient,
    *,
    hierarchy: str,
    session_key: str,
    zone_id: str,
    item: Dict[str, Any],
    input_text: str = "",
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "hierarchy": hierarchy,
        "multi_session_key": session_key,
        "zone_or_output_id": zone_id,
        "item_key": item.get("item_key"),
    }
    if input_text:
        body["input"] = input_text
    return client.browse("browse", body)


def _browse_list(
    client: RoonClient,
    *,
    hierarchy: str,
    session_key: str,
    zone_id: str,
    pop_all: bool = True,
    input_text: str = "",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    body: Dict[str, Any] = {
        "hierarchy": hierarchy,
        "multi_session_key": session_key,
        "zone_or_output_id": zone_id,
    }
    if pop_all:
        body["pop_all"] = True
    if input_text:
        body["input"] = input_text
    result = client.browse("browse", body)
    if _text(result.get("action")) != "list":
        return [], result
    items, _loaded = _browse_load_items(client, hierarchy=hierarchy, session_key=session_key, count=100)
    return items, result


def _browse_item_label(item: Dict[str, Any]) -> str:
    title = _text(item.get("title"))
    subtitle = _text(item.get("subtitle"))
    return " - ".join(part for part in (title, subtitle) if part)


def _browse_words(value: Any) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", _text(value).lower()) if token}


def _is_browse_header(item: Dict[str, Any]) -> bool:
    return _text(item.get("hint")).lower() == "header" or not _text(item.get("item_key"))


def _action_item_score(item: Dict[str, Any], *, media_kind: str, randomize: bool, enqueue: bool) -> int:
    if _is_browse_header(item):
        return -1
    title = _text(item.get("title")).lower()
    hint = _text(item.get("hint")).lower()
    if hint not in {"action", "action_list"} and not any(
        token in title for token in ("play", "shuffle", "radio", "queue", "add")
    ):
        return -1
    score = 0
    if enqueue:
        if "add next" in title:
            score += 900
        elif "add" in title or "queue" in title:
            score += 800
    if randomize:
        if "shuffle" in title:
            score += 950
        elif "radio" in title:
            score += 780
    if "play now" in title:
        score += 760
    elif title == "play" or title.startswith("play "):
        score += 700
    elif "play" in title:
        score += 500
    if media_kind in {"artist", "genre", "radio"} and "radio" in title:
        score += 120
    if hint == "action":
        score += 60
    return score


def _choose_action_item(
    items: List[Dict[str, Any]],
    *,
    media_kind: str,
    randomize: bool,
    enqueue: bool,
) -> Optional[Dict[str, Any]]:
    scored = [(_action_item_score(item, media_kind=media_kind, randomize=randomize, enqueue=enqueue), item) for item in items]
    scored = [(score, item) for score, item in scored if score >= 500]
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1]


def _browse_item_score(item: Dict[str, Any], *, query: str, media_kind: str) -> int:
    if _is_browse_header(item):
        return -1
    label = _browse_item_label(item)
    low = label.lower()
    title = _text(item.get("title")).lower()
    hint = _text(item.get("hint")).lower()
    score = 0
    kind_terms = {
        "track": {"track", "tracks", "song", "songs"},
        "artist": {"artist", "artists"},
        "album": {"album", "albums"},
        "genre": {"genre", "genres"},
        "playlist": {"playlist", "playlists"},
        "radio": {"radio", "stations", "internet radio"},
    }
    if media_kind in kind_terms:
        if title in kind_terms[media_kind]:
            score += 240
        elif any(term in low for term in kind_terms[media_kind]):
            score += 90
    elif "top result" in low:
        score += 120

    query_text = _text(query).lower()
    if query_text:
        if title == query_text:
            score += 360
        elif query_text in title:
            score += 240
        elif query_text in low:
            score += 140
        overlap = len(_browse_words(query_text) & _browse_words(label))
        score += overlap * 35
    if hint == "action_list":
        score += 25
    elif hint == "list":
        score += 15
    return score


def _choose_browse_item(items: List[Dict[str, Any]], *, query: str, media_kind: str) -> Optional[Dict[str, Any]]:
    scored = [(_browse_item_score(item, query=query, media_kind=media_kind), item) for item in items]
    scored = [(score, item) for score, item in scored if score > 0]
    if not scored:
        candidates = [item for item in items if not _is_browse_header(item)]
        return candidates[0] if candidates else None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1]


def _browse_result_message(result: Dict[str, Any]) -> str:
    message = _text(result.get("message"))
    if message:
        return message
    action = _text(result.get("action"))
    return f"Roon browse action returned {action or 'success'}."


def _activate_browse_items(
    client: RoonClient,
    *,
    hierarchy: str,
    session_key: str,
    zone_id: str,
    items: List[Dict[str, Any]],
    query: str,
    media_kind: str,
    randomize: bool,
    enqueue: bool,
    depth: int = 0,
) -> Dict[str, Any]:
    if depth > 8:
        raise RuntimeError("Roon browse search was too deep to resolve a playable item.")

    action_item = _choose_action_item(items, media_kind=media_kind, randomize=randomize, enqueue=enqueue)
    if action_item:
        result = _browse_select(client, hierarchy=hierarchy, session_key=session_key, zone_id=zone_id, item=action_item)
        action = _text(result.get("action"))
        if action == "list":
            next_items, _loaded = _browse_load_items(client, hierarchy=hierarchy, session_key=session_key, count=100)
            return _activate_browse_items(
                client,
                hierarchy=hierarchy,
                session_key=session_key,
                zone_id=zone_id,
                items=next_items,
                query=query,
                media_kind=media_kind,
                randomize=randomize,
                enqueue=enqueue,
                depth=depth + 1,
            )
        if action == "message" and result.get("is_error"):
            raise RuntimeError(_browse_result_message(result))
        return {
            "ok": True,
            "action": "queue_media" if enqueue else "play_media",
            "selected": action_item,
            "message": _browse_result_message(result),
            "browse_result": result,
        }

    item = _choose_browse_item(items, query=query, media_kind=media_kind)
    if not item:
        raise RuntimeError("Roon did not return a playable match.")

    input_prompt = item.get("input_prompt") if isinstance(item.get("input_prompt"), dict) else {}
    result = _browse_select(
        client,
        hierarchy=hierarchy,
        session_key=session_key,
        zone_id=zone_id,
        item=item,
        input_text=query if input_prompt and query else "",
    )
    action = _text(result.get("action"))
    if action == "message":
        if result.get("is_error"):
            raise RuntimeError(_browse_result_message(result))
        return {
            "ok": True,
            "action": "queue_media" if enqueue else "play_media",
            "selected": item,
            "message": _browse_result_message(result),
            "browse_result": result,
        }
    if action in {"none", "replace_item", "remove_item"}:
        return {
            "ok": True,
            "action": "queue_media" if enqueue else "play_media",
            "selected": item,
            "message": _browse_result_message(result),
            "browse_result": result,
        }
    if action != "list":
        raise RuntimeError(_browse_result_message(result))

    next_items, _loaded = _browse_load_items(client, hierarchy=hierarchy, session_key=session_key, count=100)
    return _activate_browse_items(
        client,
        hierarchy=hierarchy,
        session_key=session_key,
        zone_id=zone_id,
        items=next_items,
        query=query,
        media_kind=media_kind,
        randomize=randomize,
        enqueue=enqueue,
        depth=depth + 1,
    )


def _search_browse_items(
    client: RoonClient,
    *,
    zone_id: str,
    session_key: str,
    query: str,
) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    errors: List[str] = []
    try:
        items, result = _browse_list(
            client,
            hierarchy="search",
            session_key=session_key,
            zone_id=zone_id,
            pop_all=True,
            input_text=query,
        )
        if items:
            return "search", items, result
    except Exception as exc:
        errors.append(str(exc))

    try:
        root_items, result = _browse_list(
            client,
            hierarchy="search",
            session_key=session_key,
            zone_id=zone_id,
            pop_all=True,
        )
        prompt_item = next(
            (
                item
                for item in root_items
                if isinstance(item.get("input_prompt"), dict) and _text(item.get("item_key"))
            ),
            None,
        )
        if prompt_item:
            browse_result = _browse_select(
                client,
                hierarchy="search",
                session_key=session_key,
                zone_id=zone_id,
                item=prompt_item,
                input_text=query,
            )
            if _text(browse_result.get("action")) == "list":
                items, _loaded = _browse_load_items(client, hierarchy="search", session_key=session_key, count=100)
                if items:
                    return "search", items, browse_result
            return "search", root_items, result
    except Exception as exc:
        errors.append(str(exc))

    if errors:
        raise RuntimeError("Roon search failed. " + " | ".join(errors[:2]))
    raise RuntimeError("Roon search returned no results.")


def _play_media_with_browse(
    client: RoonClient,
    *,
    zone_id: str,
    query: str,
    media_kind: str,
    randomize: bool,
    enqueue: bool,
) -> Dict[str, Any]:
    session_key = f"tater-{uuid.uuid4()}"
    clean_query = _text(query)
    kind = _norm_media_kind(media_kind)

    if clean_query:
        hierarchy, items, result = _search_browse_items(
            client,
            zone_id=zone_id,
            session_key=session_key,
            query=clean_query,
        )
        source = result
    else:
        hierarchy = _media_hierarchy(kind)
        items, source = _browse_list(
            client,
            hierarchy=hierarchy,
            session_key=session_key,
            zone_id=zone_id,
            pop_all=True,
        )

    if not items:
        raise RuntimeError("Roon did not return any browse results.")

    result = _activate_browse_items(
        client,
        hierarchy=hierarchy,
        session_key=session_key,
        zone_id=zone_id,
        items=items,
        query=clean_query,
        media_kind=kind,
        randomize=randomize,
        enqueue=enqueue,
    )
    result.update(
        {
            "zone_id": zone_id,
            "query": clean_query,
            "media_kind": kind,
            "random": bool(randomize),
            "enqueue": bool(enqueue),
            "source_browse": source,
        }
    )
    return result


def read_integration_settings() -> Dict[str, Any]:
    settings = read_roon_settings()
    return {
        "roon_enabled": _as_bool(settings.get("ROON_ENABLED"), ROON_DEFAULT_ENABLED),
        "roon_core_host": settings.get("ROON_CORE_HOST", ""),
        "roon_core_port": int(settings.get("ROON_CORE_PORT") or ROON_DEFAULT_CORE_PORT),
        "roon_zone_aliases": settings.get("ROON_ZONE_ALIASES", ""),
        "roon_timeout_seconds": int(settings.get("ROON_TIMEOUT_SECONDS") or ROON_DEFAULT_TIMEOUT_SECONDS),
        "roon_discovery_timeout_seconds": int(
            settings.get("ROON_DISCOVERY_TIMEOUT_SECONDS") or ROON_DEFAULT_DISCOVERY_TIMEOUT_SECONDS
        ),
        "roon_paired": bool(_text(settings.get("ROON_TOKEN"))),
        "roon_core_id": settings.get("ROON_CORE_ID", ""),
        "roon_core_name": settings.get("ROON_CORE_NAME", ""),
    }


def save_integration_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload or {}
    saved = save_roon_settings(
        enabled=data.get("roon_enabled"),
        core_host=data.get("roon_core_host"),
        core_port=data.get("roon_core_port"),
        timeout_seconds=data.get("roon_timeout_seconds"),
        discovery_timeout_seconds=data.get("roon_discovery_timeout_seconds"),
        zone_aliases=data.get("roon_zone_aliases"),
    )
    return {
        "roon_enabled": _as_bool(saved.get("ROON_ENABLED"), ROON_DEFAULT_ENABLED),
        "roon_core_host": saved.get("ROON_CORE_HOST", ""),
        "roon_core_port": int(saved.get("ROON_CORE_PORT") or ROON_DEFAULT_CORE_PORT),
        "roon_zone_aliases": saved.get("ROON_ZONE_ALIASES", ""),
        "roon_timeout_seconds": int(saved.get("ROON_TIMEOUT_SECONDS") or ROON_DEFAULT_TIMEOUT_SECONDS),
        "roon_discovery_timeout_seconds": int(
            saved.get("ROON_DISCOVERY_TIMEOUT_SECONDS") or ROON_DEFAULT_DISCOVERY_TIMEOUT_SECONDS
        ),
        "roon_paired": bool(_text(saved.get("ROON_TOKEN"))),
        "roon_core_id": saved.get("ROON_CORE_ID", ""),
        "roon_core_name": saved.get("ROON_CORE_NAME", ""),
    }


def integration_status() -> Dict[str, Any]:
    settings = read_roon_settings()
    enabled = _as_bool(settings.get("ROON_ENABLED"), ROON_DEFAULT_ENABLED)
    if not enabled:
        return {"enabled": False, "configured": False, "message": "Roon is disabled."}
    host = _text(settings.get("ROON_CORE_HOST"))
    paired = bool(_text(settings.get("ROON_TOKEN")))
    if paired:
        core_name = _text(settings.get("ROON_CORE_NAME")) or host or "Roon Core"
        return {"enabled": True, "configured": True, "message": f"Roon is paired with {core_name}."}
    if host:
        return {
            "enabled": True,
            "configured": False,
            "message": "Roon Core is configured. Run Pair / Test Roon and approve Tater Roon in Roon settings.",
        }
    return {
        "enabled": True,
        "configured": False,
        "message": "Roon Core is not configured. Run Discover Roon Core or enter the Core host manually.",
    }


def integration_devices() -> Dict[str, Any]:
    settings = read_roon_settings()
    if not _as_bool(settings.get("ROON_ENABLED"), ROON_DEFAULT_ENABLED):
        return {"devices": [], "message": "Roon is disabled."}
    try:
        result = roon_get_zones(force=False, settings=settings)
    except RoonPairingRequired as exc:
        return {"devices": [], "message": str(exc), "needs_authorization": True}
    except Exception as exc:
        return {"devices": [], "message": f"Roon zones are unavailable: {exc}"}
    zones = result.get("zones") if isinstance(result.get("zones"), list) else []
    devices = [_zone_device(zone, read_roon_settings()) for zone in zones if isinstance(zone, dict)]
    return {"devices": devices, "message": f"Roon returned {len(devices)} zone{'s' if len(devices) != 1 else ''}."}


def run_integration_action(action_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    action = _text(action_id).lower()
    if action not in {"discover", "test"}:
        raise KeyError(f"Unsupported Roon action: {action_id}")
    if payload:
        save_integration_settings(payload)
    settings = read_roon_settings()
    if action == "discover":
        cores = discover_roon_cores(settings.get("ROON_DISCOVERY_TIMEOUT_SECONDS"))
        saved_settings = read_integration_settings()
        if cores:
            first = cores[0]
            save_roon_settings(core_host=first.get("host"), core_port=first.get("port"), core_name=first.get("name"))
            saved_settings = read_integration_settings()
        return {
            "ok": True,
            "core_count": len(cores),
            "cores": cores,
            "settings": saved_settings,
            "message": (
                f"Roon discovery found {len(cores)} Core{'s' if len(cores) != 1 else ''}."
                + (" Saved the first Core to settings." if cores else "")
            ),
        }

    try:
        result = roon_get_zones(force=True, settings=settings)
    except RoonPairingRequired as exc:
        return {
            "ok": False,
            "needs_authorization": True,
            "message": str(exc),
            "settings": read_integration_settings(),
        }
    zones = result.get("zones") if isinstance(result.get("zones"), list) else []
    core = result.get("core") if isinstance(result.get("core"), dict) else {}
    return {
        "ok": True,
        "zone_count": len(zones),
        "zones": zones,
        "core": core,
        "settings": read_integration_settings(),
        "message": f"Roon connection worked. Found {len(zones)} zone{'s' if len(zones) != 1 else ''}.",
    }


def run_integration_device_action(action_id: str, device_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    action = _text(action_id).lower()
    action = "playpause" if action in {"toggle", "play_pause"} else action
    action = "play_media" if action in {"play_query", "play_music", "play_library"} else action
    action = "queue_media" if action in {"queue_query", "queue_music"} else action
    transport_controls = {"play", "pause", "playpause", "stop", "previous", "next"}
    volume_actions = {"set_volume", "volume_up", "volume_down", "mute", "unmute"}
    media_actions = {"play_media", "queue_media"}
    if action not in transport_controls | volume_actions | media_actions:
        raise KeyError(f"Unsupported Roon device action: {action_id}")
    settings = read_roon_settings()
    result = roon_get_zones(force=True, settings=settings)
    zones = result.get("zones") if isinstance(result.get("zones"), list) else []
    zone = _find_zone(zones, device_id, read_roon_settings())
    core = _configured_core(read_roon_settings())
    if not core:
        raise RuntimeError("Roon Core is not configured.")
    host = _text(core.get("host"))
    port = _bounded_int(core.get("port"), default=ROON_DEFAULT_CORE_PORT, minimum=1, maximum=65535)
    with RoonClient(host, port, timeout_s=settings.get("ROON_TIMEOUT_SECONDS")) as client:
        client.connect_and_register(settings)
        if action in media_actions:
            target_id = _text((payload or {}).get("zone_or_output_id")) or _text(zone.get("zone_id"))
            media_query = _text(
                (payload or {}).get("query")
                or (payload or {}).get("request")
                or (payload or {}).get("media")
                or (payload or {}).get("title")
            )
            media_kind = _norm_media_kind(
                (payload or {}).get("media_kind")
                or (payload or {}).get("media_type")
                or (payload or {}).get("type")
            )
            payload_map = payload or {}
            if "random" in payload_map:
                randomize = _as_bool(payload_map.get("random"), False)
            else:
                randomize = _as_bool(payload_map.get("shuffle"), False)
            if media_kind in {"artist", "genre", "radio"} and "random" not in payload_map:
                randomize = True
            result = _play_media_with_browse(
                client,
                zone_id=target_id,
                query=media_query,
                media_kind=media_kind,
                randomize=randomize,
                enqueue=action == "queue_media",
            )
            result["device_id"] = _text(device_id)
            return result

        if action in transport_controls:
            target_id = _text((payload or {}).get("zone_or_output_id")) or _text(zone.get("zone_id"))
            client.transport("control", {"zone_or_output_id": target_id, "control": action})
            return {"ok": True, "action": action, "device_id": _text(device_id), "zone_id": zone.get("zone_id")}

        output_id = _text((payload or {}).get("output_id"))
        outputs = _volume_outputs(zone, output_id)
        changed: List[str] = []
        if action == "set_volume":
            if "volume" not in (payload or {}):
                raise ValueError("Roon set_volume requires a volume value.")
            value = float((payload or {}).get("volume"))
            for output in outputs:
                client.transport("change_volume", {"output_id": output.get("output_id"), "how": "absolute", "value": value})
                changed.append(_text(output.get("output_id")))
        elif action in {"volume_up", "volume_down"}:
            direction = 1 if action == "volume_up" else -1
            for output in outputs:
                volume = output.get("volume") if isinstance(output.get("volume"), dict) else {}
                how = "relative" if _text(volume.get("type")) == "incremental" else "relative_step"
                step = float((payload or {}).get("step") or 1)
                client.transport(
                    "change_volume",
                    {"output_id": output.get("output_id"), "how": how, "value": direction * step},
                )
                changed.append(_text(output.get("output_id")))
        else:
            how = "mute" if action == "mute" else "unmute"
            for output in outputs:
                client.transport("mute", {"output_id": output.get("output_id"), "how": how})
                changed.append(_text(output.get("output_id")))
        return {
            "ok": True,
            "action": action,
            "device_id": _text(device_id),
            "zone_id": zone.get("zone_id"),
            "output_ids": [item for item in changed if item],
        }
