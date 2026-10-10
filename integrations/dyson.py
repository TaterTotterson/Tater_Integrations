from __future__ import annotations

__version__ = "1.0.0"

import atexit
import contextlib
import json
import math
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from helpers import redis_client


DYSON_SETTINGS_KEY = "dyson_settings"
DYSON_DEFAULT_ENABLED = True
DYSON_DEFAULT_PORT = 1883
DYSON_DEFAULT_TIMEOUT_SECONDS = 8
DYSON_RUNTIME_POLL_SECONDS = 30
DYSON_LEGACY_FILTER_LIFE_HOURS = 4300

INTEGRATION_RUNTIME_POLL_SECONDS = DYSON_RUNTIME_POLL_SECONDS

INTEGRATION = {
    "id": "dyson",
    "name": "Dyson",
    "description": "Direct local Dyson fan and purifier sensor readings and controls over MQTT.",
    "badge": "DY",
    "order": 45,
    "runtime_poll_seconds": DYSON_RUNTIME_POLL_SECONDS,
    "capabilities": [
        "fan",
        "switch",
        "sensor",
        "air_quality",
        "temperature",
        "humidity",
    ],
    "fields": [
        {
            "key": "dyson_enabled",
            "label": "Enable Dyson",
            "type": "checkbox",
            "default": DYSON_DEFAULT_ENABLED,
        },
        {
            "key": "dyson_serial_number",
            "label": "Primary Device Serial Number",
            "type": "text",
            "default": "",
            "placeholder": "VS6-US-ABC1234A",
            "description": "The serial number printed on the fan or purifier sticker.",
        },
        {
            "key": "dyson_host",
            "label": "Primary Device IP Address or Hostname",
            "type": "text",
            "default": "",
            "placeholder": "192.168.1.42",
            "description": "A reserved IP address is recommended. When blank, Tater tries <serial>.local.",
        },
        {
            "key": "dyson_credential",
            "label": "Primary Device Password",
            "type": "password",
            "default": "",
            "description": "The local device password from the product sticker; this is not your Dyson account password.",
        },
        {
            "key": "dyson_mqtt_prefix",
            "label": "Primary MQTT Prefix / Product Type",
            "type": "text",
            "default": "",
            "placeholder": "438M",
            "description": "The product type or MQTT prefix from the sticker or a Dyson discovery tool (for example 438M or 475).",
        },
        {
            "key": "dyson_name",
            "label": "Primary Device Name",
            "type": "text",
            "default": "",
            "placeholder": "Living Room Dyson",
        },
        {
            "key": "dyson_room",
            "label": "Primary Device Room",
            "type": "text",
            "default": "",
            "placeholder": "Living Room",
        },
        {
            "key": "dyson_additional_devices",
            "label": "Additional Dyson Devices",
            "type": "textarea",
            "default": "",
            "rows": 5,
            "placeholder": "HP07-US-ABC1234A | 192.168.1.43 | 527 | Bedroom Dyson | Bedroom",
            "description": "Optional. One device per line: serial | host | MQTT prefix | name | room. Name and room are optional; host may be blank to use <serial>.local.",
            "full_width": True,
        },
        {
            "key": "dyson_additional_credentials",
            "label": "Additional Device Passwords",
            "type": "password",
            "default": "",
            "placeholder": "HP07-US-ABC1234A=device-password; TP09-US-XYZ9876=device-password",
            "description": "A semicolon-separated serial=password entry for each additional device.",
            "full_width": True,
        },
        {
            "key": "dyson_timeout_seconds",
            "label": "Connection Timeout Seconds",
            "type": "number",
            "default": DYSON_DEFAULT_TIMEOUT_SECONDS,
            "min": 2,
            "max": 30,
        },
    ],
    "actions": [
        {
            "id": "test",
            "label": "Test Dyson",
            "status": "Connects directly to the configured Dyson and requests its current state.",
        },
        {
            "id": "refresh",
            "label": "Refresh Dyson Sensors",
            "status": "Requests fresh fan and environmental sensor readings.",
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


def _number(value: Any) -> Optional[float]:
    token = _text(value)
    if not token or token.upper() in {"OFF", "INIT", "FAIL", "NONE", "INV", "N/A"}:
        return None
    try:
        return float(token)
    except (TypeError, ValueError):
        return None


def _display_number(value: float) -> Any:
    rounded = round(float(value), 3)
    return int(rounded) if rounded.is_integer() else rounded


def _slug(value: Any) -> str:
    output: List[str] = []
    for char in _text(value).lower():
        if char.isalnum():
            output.append(char)
        elif output and output[-1] != "_":
            output.append("_")
    return "".join(output).strip("_")


def _topic_component(value: Any, label: str) -> str:
    token = _text(value)
    if not token:
        raise ValueError(f"Dyson {label} is required.")
    if any(char in token for char in ("/", "+", "#", "\x00")):
        raise ValueError(f"Dyson {label} contains an invalid MQTT topic character.")
    return token


def _host_and_port(value: Any, serial_number: str) -> Tuple[str, int]:
    raw = _text(value) or f"{serial_number}.local"
    parsed = urlparse(raw if "://" in raw else f"mqtt://{raw}")
    host = _text(parsed.hostname)
    try:
        port = int(parsed.port or DYSON_DEFAULT_PORT)
    except ValueError as exc:
        raise ValueError("Dyson MQTT port is invalid.") from exc
    if not host:
        raise ValueError("Dyson device host is required.")
    if port < 1 or port > 65535:
        raise ValueError("Dyson MQTT port must be between 1 and 65535.")
    return host, port


def _read_raw(client: Any = None) -> Dict[str, Any]:
    store = client or redis_client
    try:
        raw = store.hgetall(DYSON_SETTINGS_KEY) or {}
    except Exception:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def read_dyson_settings(client: Any = None) -> Dict[str, Any]:
    raw = _read_raw(client)
    return {
        "DYSON_ENABLED": _as_bool(raw.get("DYSON_ENABLED"), DYSON_DEFAULT_ENABLED),
        "DYSON_SERIAL_NUMBER": _text(raw.get("DYSON_SERIAL_NUMBER")),
        "DYSON_HOST": _text(raw.get("DYSON_HOST")),
        "DYSON_CREDENTIAL": _text(raw.get("DYSON_CREDENTIAL")),
        "DYSON_MQTT_PREFIX": _text(raw.get("DYSON_MQTT_PREFIX")),
        "DYSON_NAME": _text(raw.get("DYSON_NAME")),
        "DYSON_ROOM": _text(raw.get("DYSON_ROOM")),
        "DYSON_ADDITIONAL_DEVICES": _text(raw.get("DYSON_ADDITIONAL_DEVICES")),
        "DYSON_ADDITIONAL_CREDENTIALS": _text(raw.get("DYSON_ADDITIONAL_CREDENTIALS")),
        "DYSON_TIMEOUT_SECONDS": str(
            _bounded_int(
                raw.get("DYSON_TIMEOUT_SECONDS"),
                default=DYSON_DEFAULT_TIMEOUT_SECONDS,
                minimum=2,
                maximum=30,
            )
        ),
    }


def save_dyson_settings(
    *,
    enabled: Any = None,
    serial_number: Any = None,
    host: Any = None,
    credential: Any = None,
    mqtt_prefix: Any = None,
    name: Any = None,
    room: Any = None,
    additional_devices: Any = None,
    additional_credentials: Any = None,
    timeout_seconds: Any = None,
    client: Any = None,
) -> Dict[str, Any]:
    store = client or redis_client
    current = read_dyson_settings(store)
    next_settings = {
        "DYSON_ENABLED": "true"
        if _as_bool(current.get("DYSON_ENABLED") if enabled is None else enabled, DYSON_DEFAULT_ENABLED)
        else "false",
        "DYSON_SERIAL_NUMBER": _text(
            current.get("DYSON_SERIAL_NUMBER") if serial_number is None else serial_number
        ),
        "DYSON_HOST": _text(current.get("DYSON_HOST") if host is None else host),
        "DYSON_CREDENTIAL": _text(
            current.get("DYSON_CREDENTIAL") if credential is None else credential
        ),
        "DYSON_MQTT_PREFIX": _text(
            current.get("DYSON_MQTT_PREFIX") if mqtt_prefix is None else mqtt_prefix
        ),
        "DYSON_NAME": _text(current.get("DYSON_NAME") if name is None else name),
        "DYSON_ROOM": _text(current.get("DYSON_ROOM") if room is None else room),
        "DYSON_ADDITIONAL_DEVICES": _text(
            current.get("DYSON_ADDITIONAL_DEVICES")
            if additional_devices is None
            else additional_devices
        ),
        "DYSON_ADDITIONAL_CREDENTIALS": _text(
            current.get("DYSON_ADDITIONAL_CREDENTIALS")
            if additional_credentials is None
            else additional_credentials
        ),
        "DYSON_TIMEOUT_SECONDS": str(
            _bounded_int(
                current.get("DYSON_TIMEOUT_SECONDS") if timeout_seconds is None else timeout_seconds,
                default=DYSON_DEFAULT_TIMEOUT_SECONDS,
                minimum=2,
                maximum=30,
            )
        ),
    }
    store.hset(DYSON_SETTINGS_KEY, mapping=next_settings)
    _shutdown_clients()
    return read_dyson_settings(store)


def _mqtt_utf8(value: Any) -> bytes:
    encoded = str(value).encode("utf-8")
    if len(encoded) > 65535:
        raise ValueError("MQTT string is too long.")
    return len(encoded).to_bytes(2, "big") + encoded


def _mqtt_remaining_length(length: int) -> bytes:
    if length < 0 or length > 268435455:
        raise ValueError("MQTT packet length is invalid.")
    output = bytearray()
    remaining = int(length)
    while True:
        digit = remaining % 128
        remaining //= 128
        if remaining:
            digit |= 0x80
        output.append(digit)
        if not remaining:
            return bytes(output)


def _mqtt_packet(first_byte: int, body: bytes = b"") -> bytes:
    return bytes([first_byte]) + _mqtt_remaining_length(len(body)) + body


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("Dyson closed the MQTT connection.")
        chunks.extend(chunk)
    return bytes(chunks)


def _recv_mqtt_packet(sock: socket.socket) -> Tuple[int, bytes]:
    first_byte = _recv_exact(sock, 1)[0]
    multiplier = 1
    remaining = 0
    for _index in range(4):
        digit = _recv_exact(sock, 1)[0]
        remaining += (digit & 0x7F) * multiplier
        if not digit & 0x80:
            return first_byte, _recv_exact(sock, remaining)
        multiplier *= 128
    raise ValueError("Dyson sent an invalid MQTT remaining length.")


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


class _DysonMQTTClient:
    """Small persistent MQTT 3.1.1 client for Dyson's local broker."""

    def __init__(self, settings: Dict[str, Any]) -> None:
        serial = _topic_component(settings.get("DYSON_SERIAL_NUMBER"), "serial number")
        prefix = _topic_component(settings.get("DYSON_MQTT_PREFIX"), "MQTT prefix")
        credential = _text(settings.get("DYSON_CREDENTIAL"))
        if not credential:
            raise ValueError("Dyson device password is required.")
        host, port = _host_and_port(settings.get("DYSON_HOST"), serial)

        self.serial_number = serial
        self.mqtt_prefix = prefix
        self.credential = credential
        self.host = host
        self.port = port
        self.timeout = _bounded_int(
            settings.get("DYSON_TIMEOUT_SECONDS"),
            default=DYSON_DEFAULT_TIMEOUT_SECONDS,
            minimum=2,
            maximum=30,
        )
        self.identity = (serial, prefix, credential, host, port, self.timeout)
        self._topic_root = f"{prefix}/{serial}"
        self._command_topic = f"{self._topic_root}/command"
        self._state_lock = threading.RLock()
        self._condition = threading.Condition(self._state_lock)
        self._write_lock = threading.Lock()
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        self._packet_id = 0
        self._generation = 0
        self._product_state: Dict[str, Any] = {}
        self._environment: Dict[str, Any] = {}
        self._faults: Dict[str, Any] = {}
        self._last_seen = 0.0
        self._last_error = ""

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def last_error(self) -> str:
        with self._state_lock:
            return self._last_error

    def start(self) -> None:
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"tater-dyson-{_slug(self.serial_number)}",
                daemon=True,
            )
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._connected.clear()
        with self._write_lock:
            sock = self._sock
            self._sock = None
            if sock is not None:
                with contextlib.suppress(Exception):
                    sock.sendall(_mqtt_packet(0xE0))
                with contextlib.suppress(Exception):
                    sock.shutdown(socket.SHUT_RDWR)
                with contextlib.suppress(Exception):
                    sock.close()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def ensure_connected(self, timeout: Optional[float] = None) -> None:
        self.start()
        wait_seconds = max(1.0, float(timeout or self.timeout))
        if self._connected.wait(wait_seconds):
            return
        error = self.last_error or f"Timed out connecting to {self.host}:{self.port}."
        raise RuntimeError(f"Dyson MQTT connection failed: {error}")

    def request_snapshot(self, *, timeout: Optional[float] = None, refresh: bool = True) -> Dict[str, Any]:
        self.ensure_connected(timeout)
        wait_seconds = max(1.0, float(timeout or self.timeout))
        with self._state_lock:
            previous_generation = self._generation
        if refresh:
            self._request_current_data()
            deadline = time.monotonic() + wait_seconds
            with self._condition:
                while self._generation <= previous_generation:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(timeout=remaining)
                # Dyson commonly sends product state and environmental data as
                # adjacent MQTT messages. Give the second message a brief chance
                # to arrive so the first inventory already contains sensors.
                settle_deadline = min(deadline, time.monotonic() + 0.25)
                observed_generation = self._generation
                while time.monotonic() < settle_deadline:
                    self._condition.wait(timeout=settle_deadline - time.monotonic())
                    if self._generation > observed_generation:
                        observed_generation = self._generation
        return self.snapshot()

    def snapshot(self) -> Dict[str, Any]:
        with self._state_lock:
            return {
                "serial_number": self.serial_number,
                "host": self.host,
                "port": self.port,
                "mqtt_prefix": self.mqtt_prefix,
                "online": self._connected.is_set(),
                "product_state": dict(self._product_state),
                "environment": dict(self._environment),
                "faults": dict(self._faults),
                "last_seen": self._last_seen,
                "last_error": self._last_error,
            }

    def send_state(self, values: Dict[str, Any]) -> None:
        if not values:
            raise ValueError("Dyson state command is empty.")
        self.ensure_connected()
        normalized = {str(key): str(value) for key, value in values.items()}
        self._publish_json(
            self._command_topic,
            {
                "msg": "STATE-SET",
                "time": _timestamp(),
                "mode-reason": "RAPP",
                "data": normalized,
            },
        )
        with self._condition:
            self._product_state.update(normalized)
            self._generation += 1
            self._condition.notify_all()

    def _run(self) -> None:
        retry_seconds = 1.0
        while not self._stop.is_set():
            try:
                self._connect_and_read()
                retry_seconds = 1.0
            except Exception as exc:
                if not self._stop.is_set():
                    with self._condition:
                        self._last_error = _text(exc) or exc.__class__.__name__
                        self._condition.notify_all()
            finally:
                self._connected.clear()
                with self._write_lock:
                    sock = self._sock
                    self._sock = None
                    if sock is not None:
                        with contextlib.suppress(Exception):
                            sock.close()
            if self._stop.wait(retry_seconds):
                break
            retry_seconds = min(30.0, retry_seconds * 2.0)

    def _connect_and_read(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(float(self.timeout))
        client_id = f"tater-{uuid.uuid4().hex}"[:23]
        connect_flags = 0x02 | 0x80 | 0x40  # clean session, username, password
        variable_header = _mqtt_utf8("MQTT") + bytes([4, connect_flags]) + (60).to_bytes(2, "big")
        payload = _mqtt_utf8(client_id) + _mqtt_utf8(self.serial_number) + _mqtt_utf8(self.credential)
        sock.sendall(_mqtt_packet(0x10, variable_header + payload))
        packet_type, body = _recv_mqtt_packet(sock)
        if packet_type >> 4 != 2 or len(body) < 2:
            raise RuntimeError("The Dyson broker did not return an MQTT CONNACK.")
        if body[1] != 0:
            reasons = {
                1: "unsupported MQTT protocol",
                2: "client identifier rejected",
                3: "broker unavailable",
                4: "bad serial number or device password",
                5: "not authorized",
            }
            raise RuntimeError(reasons.get(body[1], f"broker rejected the connection ({body[1]})"))

        sock.settimeout(1.0)
        with self._write_lock:
            if self._stop.is_set():
                sock.close()
                return
            self._sock = sock
        with self._condition:
            self._last_error = ""
            self._connected.set()
            self._condition.notify_all()

        self._subscribe(f"{self._topic_root}/#")
        self._request_current_data()
        last_ping = time.monotonic()
        last_refresh = last_ping

        while not self._stop.is_set():
            try:
                first_byte, packet_body = _recv_mqtt_packet(sock)
            except socket.timeout:
                now = time.monotonic()
                if now - last_ping >= 25.0:
                    self._send_packet(_mqtt_packet(0xC0))
                    last_ping = now
                if now - last_refresh >= DYSON_RUNTIME_POLL_SECONDS:
                    self._request_current_data()
                    last_refresh = now
                continue
            packet_kind = first_byte >> 4
            if packet_kind == 3:
                self._handle_publish(first_byte, packet_body)
            elif packet_kind == 14:
                raise ConnectionError("Dyson broker disconnected.")

    def _next_packet_id(self) -> int:
        with self._state_lock:
            self._packet_id = (self._packet_id % 65535) + 1
            return self._packet_id

    def _send_packet(self, packet: bytes) -> None:
        with self._write_lock:
            if not self._sock:
                raise ConnectionError("Dyson MQTT connection is not available.")
            self._sock.sendall(packet)

    def _subscribe(self, topic: str) -> None:
        packet_id = self._next_packet_id()
        body = packet_id.to_bytes(2, "big") + _mqtt_utf8(topic) + b"\x00"
        self._send_packet(_mqtt_packet(0x82, body))

    def _publish_json(self, topic: str, payload: Dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send_packet(_mqtt_packet(0x30, _mqtt_utf8(topic) + encoded))

    def _request_current_data(self) -> None:
        for command in (
            "REQUEST-CURRENT-STATE",
            "REQUEST-PRODUCT-ENVIRONMENT-CURRENT-SENSOR-DATA",
            "REQUEST-CURRENT-FAULTS",
        ):
            self._publish_json(
                self._command_topic,
                {"msg": command, "time": _timestamp(), "mode-reason": "RAPP"},
            )

    def _handle_publish(self, first_byte: int, body: bytes) -> None:
        if len(body) < 2:
            return
        topic_length = int.from_bytes(body[:2], "big")
        payload_offset = 2 + topic_length
        if payload_offset > len(body):
            return
        qos = (first_byte >> 1) & 0x03
        packet_id = None
        if qos:
            if payload_offset + 2 > len(body):
                return
            packet_id = int.from_bytes(body[payload_offset : payload_offset + 2], "big")
            payload_offset += 2
        try:
            payload = json.loads(body[payload_offset:].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            self._apply_message(payload)
        if qos == 1 and packet_id is not None:
            self._send_packet(_mqtt_packet(0x40, packet_id.to_bytes(2, "big")))

    def _apply_message(self, payload: Dict[str, Any]) -> None:
        message_type = _text(payload.get("msg")).upper()
        changed = False
        with self._condition:
            product_state = payload.get("product-state")
            if isinstance(product_state, dict):
                normalized: Dict[str, Any] = {}
                for key, value in product_state.items():
                    if isinstance(value, list) and value:
                        normalized[str(key)] = value[-1]
                    else:
                        normalized[str(key)] = value
                self._product_state.update(normalized)
                changed = bool(normalized) or changed

            if message_type == "ENVIRONMENTAL-CURRENT-SENSOR-DATA":
                environment = payload.get("data")
                if isinstance(environment, dict):
                    self._environment.update(environment)
                    changed = bool(environment) or changed
            elif message_type == "CURRENT-FAULTS":
                faults = payload.get("data")
                if isinstance(faults, dict):
                    self._faults = dict(faults)
                    changed = True

            if changed or message_type:
                self._last_seen = time.time()
                self._generation += 1
                self._condition.notify_all()


_clients_lock = threading.RLock()
_clients: Dict[str, _DysonMQTTClient] = {}


def _client_for(settings: Dict[str, Any]) -> _DysonMQTTClient:
    candidate = _DysonMQTTClient(settings)
    key = candidate.serial_number.lower()
    with _clients_lock:
        current = _clients.get(key)
        if current and current.identity == candidate.identity:
            return current
        if current:
            current.close()
        _clients[key] = candidate
        return candidate


def _shutdown_clients() -> None:
    with _clients_lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        with contextlib.suppress(Exception):
            client.close()


atexit.register(_shutdown_clients)


def _parse_additional_devices(settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    timeout = str(
        _bounded_int(
            settings.get("DYSON_TIMEOUT_SECONDS"),
            default=DYSON_DEFAULT_TIMEOUT_SECONDS,
            minimum=2,
            maximum=30,
        )
    )
    credentials: Dict[str, str] = {}
    credentials_text = _text(settings.get("DYSON_ADDITIONAL_CREDENTIALS")).replace(
        ";", "\n"
    )
    for raw_credential in credentials_text.splitlines():
        entry = raw_credential.strip()
        if not entry or entry.startswith("#"):
            continue
        if "=" not in entry:
            raise ValueError(
                "Additional Dyson device passwords must use serial=device-password entries."
            )
        serial_key, credential = entry.split("=", 1)
        serial_key = _text(serial_key).lower()
        credential = _text(credential)
        if not serial_key or not credential:
            raise ValueError(
                "Additional Dyson device passwords must include both a serial and password."
            )
        credentials[serial_key] = credential

    devices: List[Dict[str, Any]] = []
    for line_number, raw_line in enumerate(
        _text(settings.get("DYSON_ADDITIONAL_DEVICES")).splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 3:
            raise ValueError(
                f"Additional Dyson device line {line_number} needs serial | host | MQTT prefix."
            )
        serial, host, mqtt_prefix = parts[:3]
        name = parts[3] if len(parts) > 3 else ""
        room = parts[4] if len(parts) > 4 else ""
        credential = credentials.get(serial.lower(), "")
        if not serial or not credential or not mqtt_prefix:
            raise ValueError(
                f"Additional Dyson device line {line_number} is missing its serial, MQTT prefix, or matching password."
            )
        devices.append(
            {
                "DYSON_ENABLED": True,
                "DYSON_SERIAL_NUMBER": serial,
                "DYSON_HOST": host,
                "DYSON_CREDENTIAL": credential,
                "DYSON_MQTT_PREFIX": mqtt_prefix,
                "DYSON_NAME": name,
                "DYSON_ROOM": room,
                "DYSON_TIMEOUT_SECONDS": timeout,
            }
        )
    return devices


def _device_settings(settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    primary = {
        "DYSON_ENABLED": True,
        "DYSON_SERIAL_NUMBER": _text(settings.get("DYSON_SERIAL_NUMBER")),
        "DYSON_HOST": _text(settings.get("DYSON_HOST")),
        "DYSON_CREDENTIAL": _text(settings.get("DYSON_CREDENTIAL")),
        "DYSON_MQTT_PREFIX": _text(settings.get("DYSON_MQTT_PREFIX")),
        "DYSON_NAME": _text(settings.get("DYSON_NAME")),
        "DYSON_ROOM": _text(settings.get("DYSON_ROOM")),
        "DYSON_TIMEOUT_SECONDS": str(
            _bounded_int(
                settings.get("DYSON_TIMEOUT_SECONDS"),
                default=DYSON_DEFAULT_TIMEOUT_SECONDS,
                minimum=2,
                maximum=30,
            )
        ),
    }
    devices = [primary, *_parse_additional_devices(settings)]
    seen: set[str] = set()
    for device in devices:
        serial = _topic_component(device.get("DYSON_SERIAL_NUMBER"), "serial number")
        _topic_component(device.get("DYSON_MQTT_PREFIX"), "MQTT prefix")
        if not _text(device.get("DYSON_CREDENTIAL")):
            raise ValueError(f"Dyson {serial} needs its local device password.")
        _host_and_port(device.get("DYSON_HOST"), serial)
        serial_key = serial.lower()
        if serial_key in seen:
            raise ValueError(f"Dyson serial number {serial} is configured more than once.")
        seen.add(serial_key)
    return devices


def _configuration_error(settings: Dict[str, Any]) -> str:
    if not _as_bool(settings.get("DYSON_ENABLED"), DYSON_DEFAULT_ENABLED):
        return "Dyson is disabled."
    missing = []
    if not _text(settings.get("DYSON_SERIAL_NUMBER")):
        missing.append("serial number")
    if not _text(settings.get("DYSON_CREDENTIAL")):
        missing.append("device password")
    if not _text(settings.get("DYSON_MQTT_PREFIX")):
        missing.append("MQTT prefix")
    if missing:
        return "Dyson needs " + ", ".join(missing) + "."
    try:
        _device_settings(settings)
    except ValueError as exc:
        return str(exc)
    return ""


def _power_control_key(product_state: Dict[str, Any]) -> str:
    return "fmod" if "fmod" in product_state and "fpwr" not in product_state else "fpwr"


def _fan_is_on(product_state: Dict[str, Any]) -> Optional[bool]:
    if "fpwr" in product_state:
        return _text(product_state.get("fpwr")).upper() == "ON"
    if "fmod" in product_state:
        return _text(product_state.get("fmod")).upper() in {"FAN", "AUTO"}
    if "fnst" in product_state:
        return _text(product_state.get("fnst")).upper() == "FAN"
    return None


def _fan_percentage(product_state: Dict[str, Any]) -> Optional[int]:
    raw = _text(product_state.get("fnsp")).upper()
    if not raw or raw == "AUTO":
        return None
    try:
        speed = max(0, min(10, int(raw)))
    except ValueError:
        return None
    return speed * 10


def _first_number(source: Dict[str, Any], keys: Tuple[str, ...]) -> Tuple[Optional[float], str]:
    for key in keys:
        if key not in source:
            continue
        value = _number(source.get(key))
        if value is not None:
            return value, key
    return None, ""


def _sensor_values(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    environment = snapshot.get("environment") if isinstance(snapshot.get("environment"), dict) else {}
    product_state = snapshot.get("product_state") if isinstance(snapshot.get("product_state"), dict) else {}
    sensors: List[Dict[str, Any]] = []

    def add(
        sensor_id: str,
        label: str,
        value: Optional[float],
        unit: str,
        capabilities: List[str],
        raw_key: str,
        *,
        scale: float = 1.0,
        offset: float = 0.0,
        device_type: str = "sensor",
    ) -> None:
        if value is None:
            return
        converted = _display_number((value * scale) + offset)
        sensors.append(
            {
                "sensor_id": sensor_id,
                "label": label,
                "value": converted,
                "unit": unit,
                "capabilities": ["sensor", *capabilities],
                "raw_key": raw_key,
                "type": device_type,
            }
        )

    value, key = _first_number(environment, ("tact", "temp"))
    add("temperature", "Temperature", value, "°C", ["temperature"], key, scale=0.1, offset=-273.15, device_type="temperature")
    value, key = _first_number(environment, ("hact", "humi"))
    if value is None:
        value, key = _first_number(product_state, ("humi",))
    add("humidity", "Humidity", value, "%", ["humidity"], key, device_type="humidity")
    value, key = _first_number(environment, ("p25r", "pm25", "pact"))
    add("pm25", "PM2.5", value, "µg/m³", ["air_quality", "pm25"], key)
    value, key = _first_number(environment, ("p10r", "pm10"))
    add("pm10", "PM10", value, "µg/m³", ["air_quality", "pm10"], key)
    value, key = _first_number(environment, ("va10", "vact"))
    add("voc", "VOC", value, "mg/m³", ["air_quality", "voc"], key, scale=0.001)
    value, key = _first_number(environment, ("noxl", "no2"))
    add("no2", "NO₂", value, "µg/m³", ["air_quality", "no2"], key)
    value, key = _first_number(environment, ("co2r", "co2"))
    add("co2", "CO₂", value, "ppm", ["air_quality", "co2"], key)
    value, key = _first_number(environment, ("hchr", "hcho"))
    add("formaldehyde", "Formaldehyde", value, "mg/m³", ["air_quality", "formaldehyde"], key, scale=0.001)

    value, key = _first_number(product_state, ("fflr", "hflr"))
    if value is None and "filf" in product_state:
        legacy = _number(product_state.get("filf"))
        if legacy is not None:
            value = max(0.0, min(100.0, legacy * 100.0 / DYSON_LEGACY_FILTER_LIFE_HOURS))
            key = "filf"
    add("hepa_filter_life", "HEPA Filter Life", value, "%", ["filter_life"], key)
    value, key = _first_number(product_state, ("cflr",))
    add("carbon_filter_life", "Carbon Filter Life", value, "%", ["filter_life"], key)
    return sensors


def _build_devices(settings: Dict[str, Any], snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    serial = _text(settings.get("DYSON_SERIAL_NUMBER"))
    serial_slug = _slug(serial) or "device"
    name = _text(settings.get("DYSON_NAME")) or f"Dyson {serial}"
    room = _text(settings.get("DYSON_ROOM"))
    product_state = snapshot.get("product_state") if isinstance(snapshot.get("product_state"), dict) else {}
    is_on = _fan_is_on(product_state)
    state = "on" if is_on is True else "off" if is_on is False else "unavailable" if not snapshot.get("online") else "unknown"
    percentage = _fan_percentage(product_state)
    fan_ref = f"dyson:{serial_slug}:fan"
    details: Dict[str, Any] = {
        "manufacturer": "Dyson",
        "serial_number": serial,
        "host": snapshot.get("host"),
        "port": snapshot.get("port"),
        "mqtt_prefix": _text(settings.get("DYSON_MQTT_PREFIX")),
        "online": bool(snapshot.get("online")),
        "last_seen": snapshot.get("last_seen") or None,
        "fan_speed": int(percentage / 10) if percentage is not None else None,
        "percentage": percentage,
        "auto_mode": _text(product_state.get("auto")).upper() == "ON"
        or _text(product_state.get("fmod")).upper() == "AUTO",
        "night_mode": _text(product_state.get("nmod")).upper() == "ON",
        "oscillating": _text(product_state.get("oson")).upper() == "ON",
        "power_control_key": _power_control_key(product_state),
    }
    if snapshot.get("last_error"):
        details["connection_error"] = _text(snapshot.get("last_error"))
    devices: List[Dict[str, Any]] = [
        {
            "id": fan_ref,
            "name": name,
            "type": "fan",
            "ref": fan_ref,
            "capabilities": ["fan", "switch", "air_purifier", "variable_speed", "oscillation"],
            "actions": [
                "turn_on",
                "turn_off",
                "toggle",
                "set_percentage",
                "set_speed",
                "set_auto_mode",
                "set_night_mode",
                "set_oscillation",
            ],
            "event_sources": [{"type": "fan", "ref": fan_ref, "state_on": "on", "state_off": "off"}],
            "status": state,
            "state": state,
            "room": room,
            "area": room,
            "details": details,
        }
    ]
    for sensor in _sensor_values(snapshot):
        sensor_ref = f"dyson:{serial_slug}:{sensor['sensor_id']}"
        devices.append(
            {
                "id": sensor_ref,
                "name": f"{name} {sensor['label']}",
                "type": sensor["type"],
                "ref": sensor_ref,
                "capabilities": sensor["capabilities"],
                "actions": [],
                "event_sources": [],
                "status": sensor["value"],
                "state": sensor["value"],
                "room": room,
                "area": room,
                "details": {
                    "manufacturer": "Dyson",
                    "serial_number": serial,
                    "parent_ref": fan_ref,
                    "unit": sensor["unit"],
                    "raw_key": sensor["raw_key"],
                    "online": bool(snapshot.get("online")),
                    "last_seen": snapshot.get("last_seen") or None,
                },
            }
        )
    return devices


def read_integration_settings() -> Dict[str, Any]:
    settings = read_dyson_settings()
    return {
        "dyson_enabled": _as_bool(settings.get("DYSON_ENABLED"), DYSON_DEFAULT_ENABLED),
        "dyson_serial_number": settings.get("DYSON_SERIAL_NUMBER", ""),
        "dyson_host": settings.get("DYSON_HOST", ""),
        "dyson_credential": settings.get("DYSON_CREDENTIAL", ""),
        "dyson_mqtt_prefix": settings.get("DYSON_MQTT_PREFIX", ""),
        "dyson_name": settings.get("DYSON_NAME", ""),
        "dyson_room": settings.get("DYSON_ROOM", ""),
        "dyson_additional_devices": settings.get("DYSON_ADDITIONAL_DEVICES", ""),
        "dyson_additional_credentials": settings.get("DYSON_ADDITIONAL_CREDENTIALS", ""),
        "dyson_timeout_seconds": int(settings.get("DYSON_TIMEOUT_SECONDS") or DYSON_DEFAULT_TIMEOUT_SECONDS),
    }


def save_integration_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload or {}
    saved = save_dyson_settings(
        enabled=data.get("dyson_enabled"),
        serial_number=data.get("dyson_serial_number"),
        host=data.get("dyson_host"),
        credential=data.get("dyson_credential"),
        mqtt_prefix=data.get("dyson_mqtt_prefix"),
        name=data.get("dyson_name"),
        room=data.get("dyson_room"),
        additional_devices=data.get("dyson_additional_devices"),
        additional_credentials=data.get("dyson_additional_credentials"),
        timeout_seconds=data.get("dyson_timeout_seconds"),
    )
    return {
        "dyson_enabled": _as_bool(saved.get("DYSON_ENABLED"), DYSON_DEFAULT_ENABLED),
        "dyson_serial_number": saved.get("DYSON_SERIAL_NUMBER", ""),
        "dyson_host": saved.get("DYSON_HOST", ""),
        "dyson_credential": saved.get("DYSON_CREDENTIAL", ""),
        "dyson_mqtt_prefix": saved.get("DYSON_MQTT_PREFIX", ""),
        "dyson_name": saved.get("DYSON_NAME", ""),
        "dyson_room": saved.get("DYSON_ROOM", ""),
        "dyson_additional_devices": saved.get("DYSON_ADDITIONAL_DEVICES", ""),
        "dyson_additional_credentials": saved.get("DYSON_ADDITIONAL_CREDENTIALS", ""),
        "dyson_timeout_seconds": int(saved.get("DYSON_TIMEOUT_SECONDS") or DYSON_DEFAULT_TIMEOUT_SECONDS),
    }


def integration_status() -> Dict[str, Any]:
    settings = read_dyson_settings()
    error = _configuration_error(settings)
    if error:
        return {
            "enabled": _as_bool(settings.get("DYSON_ENABLED"), DYSON_DEFAULT_ENABLED),
            "configured": False,
            "message": error,
        }
    try:
        devices = _device_settings(settings)
    except ValueError as exc:
        return {"enabled": True, "configured": False, "message": str(exc)}
    count = len(devices)
    return {
        "enabled": True,
        "configured": True,
        "message": f"{count} Dyson device{'s are' if count != 1 else ' is'} configured for local MQTT.",
    }


def _inventory(*, refresh: bool) -> Tuple[List[Dict[str, Any]], List[str]]:
    settings = read_dyson_settings()
    error = _configuration_error(settings)
    if error:
        return [], [error]
    configured_devices = _device_settings(settings)
    results: Dict[int, List[Dict[str, Any]]] = {}
    warnings: List[str] = []

    def fetch(index: int, device_settings: Dict[str, Any]) -> Tuple[int, List[Dict[str, Any]], str]:
        label = _text(device_settings.get("DYSON_NAME")) or _text(
            device_settings.get("DYSON_SERIAL_NUMBER")
        )
        try:
            mqtt_client = _client_for(device_settings)
        except Exception as exc:
            return index, [], f"{label}: {_text(exc) or exc.__class__.__name__}"
        warning = ""
        try:
            snapshot = mqtt_client.request_snapshot(
                timeout=float(
                    device_settings.get("DYSON_TIMEOUT_SECONDS")
                    or DYSON_DEFAULT_TIMEOUT_SECONDS
                ),
                refresh=refresh,
            )
        except Exception as exc:
            snapshot = mqtt_client.snapshot()
            warning = f"{label}: {_text(exc) or exc.__class__.__name__}"
        return index, _build_devices(device_settings, snapshot), warning

    with ThreadPoolExecutor(max_workers=min(8, len(configured_devices))) as executor:
        futures = [
            executor.submit(fetch, index, device_settings)
            for index, device_settings in enumerate(configured_devices)
        ]
        for future in as_completed(futures):
            index, device_rows, warning = future.result()
            results[index] = device_rows
            if warning:
                warnings.append(warning)

    devices = [
        row
        for index in range(len(configured_devices))
        for row in results.get(index, [])
    ]
    return devices, warnings


def integration_devices() -> Dict[str, Any]:
    settings = read_dyson_settings()
    error = _configuration_error(settings)
    if error:
        return {"devices": [], "message": error}
    devices, warnings = _inventory(refresh=True)
    fan_count = sum(1 for device in devices if _text(device.get("type")) == "fan")
    sensor_count = max(0, len(devices) - fan_count)
    result: Dict[str, Any] = {
        "devices": devices,
        "message": (
            f"Dyson returned {fan_count} fan{'s' if fan_count != 1 else ''} and "
            f"{sensor_count} sensor reading{'s' if sensor_count != 1 else ''}."
        ),
    }
    if warnings:
        result["warnings"] = warnings
    return result


def run_integration_action(action_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    action = _text(action_id).lower()
    if action not in {"test", "refresh"}:
        raise KeyError(f"Unsupported Dyson action: {action_id}")
    if payload:
        save_integration_settings(payload)
    settings = read_dyson_settings()
    error = _configuration_error(settings)
    if error:
        raise ValueError(error)
    devices, warnings = _inventory(refresh=True)
    fan_count = sum(1 for device in devices if _text(device.get("type")) == "fan")
    sensor_count = max(0, len(devices) - fan_count)
    return {
        "ok": bool(fan_count) and not warnings,
        "device_count": len(devices),
        "fan_count": fan_count,
        "sensor_count": sensor_count,
        "devices": devices,
        "warnings": warnings,
        "message": (
            f"Dyson reached {fan_count} fan{'s' if fan_count != 1 else ''} and received "
            f"{sensor_count} sensor reading{'s' if sensor_count != 1 else ''}."
        ),
    }


def _payload_bool(payload: Dict[str, Any], *, default: bool) -> bool:
    for key in ("enabled", "on", "state", "value"):
        if key in payload:
            return _as_bool(payload.get(key), default)
    mode = _text(payload.get("mode")).lower()
    if mode:
        return mode in {"on", "auto", "enabled", "true", "1"}
    return default


def _configured_target(device_id: Any, serial_number: str) -> bool:
    target = _text(device_id).lower()
    serial = _text(serial_number).lower()
    expected = f"dyson:{_slug(serial_number)}:fan"
    return target in {serial, expected.lower(), f"fan:{serial}"}


def run_integration_device_action(action_id: str, device_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    action = _text(action_id).lower()
    data = payload or {}
    settings = read_dyson_settings()
    error = _configuration_error(settings)
    if error:
        raise ValueError(error)
    device_settings = next(
        (
            item
            for item in _device_settings(settings)
            if _configured_target(device_id, _text(item.get("DYSON_SERIAL_NUMBER")))
        ),
        None,
    )
    if device_settings is None:
        raise KeyError(f"Unknown Dyson fan: {device_id}")
    serial = _text(device_settings.get("DYSON_SERIAL_NUMBER"))
    client = _client_for(device_settings)
    snapshot = client.snapshot()
    product_state = snapshot.get("product_state") if isinstance(snapshot.get("product_state"), dict) else {}
    if not product_state:
        snapshot = client.request_snapshot(
            timeout=float(
                device_settings.get("DYSON_TIMEOUT_SECONDS")
                or DYSON_DEFAULT_TIMEOUT_SECONDS
            ),
            refresh=True,
        )
        product_state = (
            snapshot.get("product_state")
            if isinstance(snapshot.get("product_state"), dict)
            else {}
        )
    power_key = _power_control_key(product_state)
    command: Dict[str, Any]

    if action in {"turn_on", "turn_off", "toggle"}:
        enabled = action == "turn_on"
        if action == "toggle":
            enabled = not bool(_fan_is_on(product_state))
        if power_key == "fmod":
            command = {"fmod": "FAN" if enabled else "OFF"}
        else:
            command = {"fpwr": "ON" if enabled else "OFF"}
        if enabled and any(key in data for key in ("percentage", "speed_pct", "speed")):
            percentage = _bounded_int(
                data.get("percentage", data.get("speed_pct", data.get("speed"))),
                default=100,
                minimum=0,
                maximum=100,
            )
            if percentage == 0:
                command = {power_key: "OFF"}
            else:
                command["fnsp"] = f"{max(1, math.ceil(percentage / 10)):04d}"
    elif action in {"set_percentage", "set_speed"}:
        raw_value = data.get("percentage", data.get("speed_pct", data.get("speed")))
        if raw_value in (None, ""):
            raise ValueError("Dyson fan percentage or speed is required.")
        if action == "set_speed":
            speed = _bounded_int(raw_value, default=1, minimum=0, maximum=10)
            percentage = speed * 10
        else:
            percentage = _bounded_int(raw_value, default=10, minimum=0, maximum=100)
            speed = max(1, math.ceil(percentage / 10)) if percentage else 0
        if speed == 0:
            command = {power_key: "OFF"}
        else:
            command = {power_key: "FAN" if power_key == "fmod" else "ON", "fnsp": f"{speed:04d}"}
    elif action == "set_auto_mode":
        enabled = _payload_bool(data, default=True)
        command = {"fmod": "AUTO" if enabled else "FAN"} if power_key == "fmod" else {"auto": "ON" if enabled else "OFF"}
    elif action == "set_night_mode":
        command = {"nmod": "ON" if _payload_bool(data, default=True) else "OFF"}
    elif action == "set_oscillation":
        command = {"oson": "ON" if _payload_bool(data, default=True) else "OFF"}
        angle = data.get("angle", data.get("oscillation_angle"))
        if command["oson"] == "ON" and angle not in (None, ""):
            command["ancp"] = f"{_bounded_int(angle, default=90, minimum=0, maximum=350):04d}"
    else:
        raise KeyError(f"Unsupported Dyson device action: {action_id}")

    client.send_state(command)
    return {
        "ok": True,
        "action": action,
        "device_id": _text(device_id),
        "serial_number": serial,
        "command": command,
        "message": f"Sent {action.replace('_', ' ')} to {_text(device_settings.get('DYSON_NAME')) or serial}.",
    }


def integration_poll_events(client: Any = None, cursor: Any = None) -> Dict[str, Any]:
    del client
    settings = read_dyson_settings()
    error = _configuration_error(settings)
    if error:
        return {"states": [], "events": [], "cursor": cursor or {}}
    devices, _warnings = _inventory(refresh=True)
    previous = cursor.get("states") if isinstance(cursor, dict) and isinstance(cursor.get("states"), dict) else {}
    current: Dict[str, Any] = {}
    states: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    for device in devices:
        ref = _text(device.get("ref") or device.get("id"))
        if not ref:
            continue
        state = device.get("state")
        current[ref] = state
        states.append(
            {
                "id": ref,
                "ref": ref,
                "name": _text(device.get("name")),
                "state": state,
                "room": _text(device.get("room")),
                "attributes": dict(device.get("details") or {}),
            }
        )
        if ref in previous and previous.get(ref) != state:
            events.append(
                {
                    "kind": "state_changed",
                    "ref": ref,
                    "device_id": ref,
                    "state": state,
                    "previous_state": previous.get(ref),
                }
            )
    return {"states": states, "events": events, "cursor": {"states": current}}
