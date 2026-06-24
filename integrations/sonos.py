from __future__ import annotations
__version__ = "1.1.3"

import contextlib
import html
import json
import socket
import ssl
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse
from xml.sax.saxutils import escape as xml_escape

import requests

from helpers import redis_client

try:
    import websocket
except Exception:  # pragma: no cover - optional runtime dependency guard
    websocket = None

SONOS_SETTINGS_KEY = "sonos_settings"
SONOS_TARGET_PREFIX = "sonos:"
SONOS_DEFAULT_ENABLED = True
SONOS_DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 2
SONOS_DISCOVERY_CACHE_KEY = "tater:sonos:speakers:registry:v1"
SONOS_DISCOVERY_CACHE_TTL_SECONDS = 60.0
SONOS_AVTRANSPORT_SERVICE = "urn:schemas-upnp-org:service:AVTransport:1"
SONOS_RENDERING_CONTROL_SERVICE = "urn:schemas-upnp-org:service:RenderingControl:1"
SONOS_ZONE_GROUP_TOPOLOGY_SERVICE = "urn:schemas-upnp-org:service:ZoneGroupTopology:1"
SONOS_DEFAULT_PLAY_TIMEOUT_SECONDS = 30.0
SONOS_AUDIOCLIP_API_KEY = "123e4567-e89b-12d3-a456-426655440000"
SONOS_AUDIOCLIP_SUBPROTOCOL = "v1.api.smartspeaker.audio"
SONOS_AUDIOCLIP_APP_ID = "com.tatertotterson.tater"
SONOS_AUDIOCLIP_NAME = "Tater Announcement"

INTEGRATION = {
    "id": "sonos",
    "name": "Sonos",
    "description": "Sonos speaker discovery and direct playback targets for announcements.",
    "badge": "SON",
    "order": 50,
    "fields": [
        {
            "key": "sonos_enabled",
            "label": "Enable Sonos announcements",
            "type": "checkbox",
            "default": SONOS_DEFAULT_ENABLED,
        },
        {
            "key": "sonos_discovery_timeout_seconds",
            "label": "Discovery Timeout Seconds",
            "type": "number",
            "default": SONOS_DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
            "min": 1,
            "max": 10,
        },
        {
            "key": "sonos_speaker_hosts",
            "label": "Manual Speaker Hosts",
            "type": "textarea",
            "default": "",
            "rows": 3,
            "placeholder": "192.168.1.42\nsonos-living-room.local",
            "description": "Optional. One host, IP, or URL per line for speakers SSDP does not discover.",
            "full_width": True,
        },
    ],
    "actions": [
        {
            "id": "discover",
            "label": "Discover Speakers",
            "status": "Runs Sonos discovery using SSDP plus manual speaker hosts.",
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


def read_sonos_settings(client: Any = None) -> Dict[str, Any]:
    store = client or redis_client
    try:
        raw = store.hgetall(SONOS_SETTINGS_KEY) or {}
    except Exception:
        raw = {}
    timeout = _bounded_int(
        raw.get("SONOS_DISCOVERY_TIMEOUT_SECONDS"),
        default=SONOS_DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
        minimum=1,
        maximum=10,
    )
    return {
        "SONOS_ENABLED": _as_bool(raw.get("SONOS_ENABLED"), SONOS_DEFAULT_ENABLED),
        "SONOS_DISCOVERY_TIMEOUT_SECONDS": str(timeout),
        "SONOS_SPEAKER_HOSTS": _text(raw.get("SONOS_SPEAKER_HOSTS")),
    }


def save_sonos_settings(
    *,
    enabled: Any = None,
    discovery_timeout_seconds: Any = None,
    speaker_hosts: Any = None,
    client: Any = None,
) -> Dict[str, Any]:
    store = client or redis_client
    current = read_sonos_settings(store)
    next_settings = {
        "SONOS_ENABLED": "true"
        if _as_bool(current.get("SONOS_ENABLED") if enabled is None else enabled, SONOS_DEFAULT_ENABLED)
        else "false",
        "SONOS_DISCOVERY_TIMEOUT_SECONDS": str(
            _bounded_int(
                current.get("SONOS_DISCOVERY_TIMEOUT_SECONDS")
                if discovery_timeout_seconds is None
                else discovery_timeout_seconds,
                default=SONOS_DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
                minimum=1,
                maximum=10,
            )
        ),
        "SONOS_SPEAKER_HOSTS": _text(current.get("SONOS_SPEAKER_HOSTS") if speaker_hosts is None else speaker_hosts),
    }
    store.hset(SONOS_SETTINGS_KEY, mapping=next_settings)
    with contextlib.suppress(Exception):
        store.delete(SONOS_DISCOVERY_CACHE_KEY)
    return read_sonos_settings(store)


def _split_sonos_manual_hosts(value: Any) -> List[str]:
    items: List[str] = []
    seen: set[str] = set()
    for raw in _text(value).replace(",", "\n").splitlines():
        token = _text(raw)
        if not token or token.startswith("#"):
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(token)
    return items


def normalize_sonos_root(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)
    netloc = parsed.netloc or parsed.path
    if not netloc:
        return ""
    if ":" not in netloc.rsplit("@", 1)[-1]:
        netloc = f"{netloc}:1400"
    scheme = parsed.scheme or "http"
    return urlunparse((scheme, netloc, "", "", "", "")).rstrip("/")


def _root_from_url(raw_url: Any) -> str:
    parsed = urlparse(_text(raw_url))
    if not parsed.scheme or not parsed.netloc:
        return ""
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")).rstrip("/")


def _description_url_for_root(root_url: Any) -> str:
    root = normalize_sonos_root(root_url)
    return f"{root}/xml/device_description.xml" if root else ""


def _xml_find_text(root: ET.Element, tag: str) -> str:
    node = root.find(f".//{{*}}{tag}")
    return _text(node.text if node is not None else "")


def _xml_local_name(tag: Any) -> str:
    text = str(tag or "")
    return text.rsplit("}", 1)[-1] if "}" in text else text


def _xml_attr_bool(value: Any) -> bool:
    return _text(value).lower() in {"1", "true", "yes", "on"}


def _parse_sonos_description(location_url: str, *, timeout_s: float) -> Dict[str, str]:
    location = _text(location_url)
    if not location:
        return {}
    try:
        response = requests.get(location, timeout=max(1.0, float(timeout_s or 2.0)))
        response.raise_for_status()
    except Exception:
        return {}

    body = _text(response.text)
    if "sonos" not in body.lower() and "zoneplayer" not in body.lower():
        return {}

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return {}

    udn = _text(_xml_find_text(root, "UDN"))
    speaker_id = sonos_target_id(udn) or sonos_target_id(_root_from_url(response.url) or location)
    root_url = _root_from_url(response.url) or _root_from_url(location)
    if not root_url:
        root_url = normalize_sonos_root(location)
    parsed = urlparse(root_url)
    name = (
        _xml_find_text(root, "roomName")
        or _xml_find_text(root, "friendlyName")
        or _xml_find_text(root, "displayName")
        or _text(parsed.hostname)
        or speaker_id
    )
    model = _xml_find_text(root, "modelName") or _xml_find_text(root, "modelNumber")
    if not speaker_id or not root_url:
        return {}
    return {
        "id": speaker_id,
        "udn": udn,
        "name": name,
        "model": model,
        "root_url": root_url,
        "location": response.url or location,
        "host": _text(parsed.hostname),
    }


def _fetch_sonos_speaker(root_or_location: Any, *, timeout_s: float = 2.0) -> Dict[str, str]:
    token = _text(root_or_location)
    if not token:
        return {}
    if token.lower().endswith(".xml"):
        location = token
    else:
        location = _description_url_for_root(token)
    return _parse_sonos_description(location, timeout_s=timeout_s)


def _parse_ssdp_headers(payload: bytes) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    raw = payload.decode("utf-8", "ignore")
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[_text(key).lower()] = _text(value)
    return headers


def _discover_sonos_locations_ssdp(timeout_s: float) -> List[str]:
    locations: List[str] = []
    seen: set[str] = set()
    deadline = time.time() + max(1.0, min(10.0, float(timeout_s or SONOS_DEFAULT_DISCOVERY_TIMEOUT_SECONDS)))
    message = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: urn:schemas-upnp-org:device:ZonePlayer:1\r\n"
        "\r\n"
    ).encode("ascii")

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.settimeout(0.5)
        sock.sendto(message, ("239.255.255.250", 1900))
    except OSError:
        with contextlib.suppress(Exception):
            sock.close()
        return []

    try:
        while time.time() < deadline:
            try:
                data, _addr = sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            headers = _parse_ssdp_headers(data)
            raw_location = _text(headers.get("location"))
            if not raw_location:
                continue
            raw_payload = data.decode("utf-8", "ignore").lower()
            if "sonos" not in raw_payload and "zoneplayer" not in raw_payload:
                continue
            key = raw_location.lower()
            if key in seen:
                continue
            seen.add(key)
            locations.append(raw_location)
    finally:
        with contextlib.suppress(Exception):
            sock.close()

    return locations


def _cache_rows(rows: List[Dict[str, str]]) -> None:
    payload = {"ts": time.time(), "rows": rows}
    with contextlib.suppress(Exception):
        redis_client.set(SONOS_DISCOVERY_CACHE_KEY, json.dumps(payload, ensure_ascii=False))


def _cached_rows() -> Optional[List[Dict[str, str]]]:
    try:
        raw = redis_client.get(SONOS_DISCOVERY_CACHE_KEY)
        payload = json.loads(raw) if raw else {}
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        ts = float(payload.get("ts") or 0.0)
    except Exception:
        ts = 0.0
    if ts <= 0.0 or (time.time() - ts) > SONOS_DISCOVERY_CACHE_TTL_SECONDS:
        return None
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return None
    return [dict(item) for item in rows if isinstance(item, dict)]


def _dedupe_sonos_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        speaker_id = sonos_target_id(row.get("id") or row.get("udn") or row.get("root_url"))
        root_url = normalize_sonos_root(row.get("root_url"))
        key = (speaker_id or root_url).lower()
        if not key or key in seen:
            continue
        next_row = dict(row)
        next_row["id"] = speaker_id
        next_row["root_url"] = root_url
        seen.add(key)
        out.append(next_row)
    out.sort(key=lambda item: (_text(item.get("name")).lower(), _text(item.get("host")).lower()))
    return out


def _sonos_soap_action(
    root_url: str,
    *,
    action: str,
    inner_xml: str,
    timeout_s: float,
    service: str = SONOS_AVTRANSPORT_SERVICE,
    control_path: str = "/MediaRenderer/AVTransport/Control",
) -> str:
    root = normalize_sonos_root(root_url)
    if not root:
        raise RuntimeError("Sonos speaker root URL is missing.")
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action} xmlns:u="{service}">'
        f"{inner_xml}"
        f"</u:{action}>"
        "</s:Body>"
        "</s:Envelope>"
    )
    response = requests.post(
        f"{root}{control_path}",
        data=body.encode("utf-8"),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{service}#{action}"',
        },
        timeout=max(2.0, float(timeout_s or SONOS_DEFAULT_PLAY_TIMEOUT_SECONDS)),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Sonos {action} HTTP {response.status_code}: {_text(response.text)[:200]}")
    return _text(response.text)


def _sonos_response_values(response_xml: Any) -> Dict[str, str]:
    payload = _text(response_xml)
    if not payload:
        return {}
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return {}
    values: Dict[str, str] = {}
    for node in root.iter():
        name = _xml_local_name(node.tag)
        if not name or name in {"Envelope", "Body"} or name.endswith("Response"):
            continue
        text = _text(node.text)
        if text:
            values[name] = text
    return values


def _sonos_av_transport_values(root_url: str, *, action: str, timeout_s: float) -> Dict[str, str]:
    body = _sonos_soap_action(
        root_url,
        action=action,
        inner_xml="<InstanceID>0</InstanceID>",
        timeout_s=timeout_s,
    )
    return _sonos_response_values(body)


def _sonos_get_volume(root_url: str, *, timeout_s: float) -> Optional[int]:
    body = _sonos_soap_action(
        root_url,
        action="GetVolume",
        inner_xml="<InstanceID>0</InstanceID><Channel>Master</Channel>",
        timeout_s=timeout_s,
        service=SONOS_RENDERING_CONTROL_SERVICE,
        control_path="/MediaRenderer/RenderingControl/Control",
    )
    values = _sonos_response_values(body)
    try:
        return int(values.get("CurrentVolume") or "")
    except Exception:
        return None


def _sonos_set_volume(root_url: str, volume: Any, *, timeout_s: float) -> None:
    try:
        level = max(0, min(100, int(float(_text(volume)))))
    except Exception:
        return
    _sonos_soap_action(
        root_url,
        action="SetVolume",
        inner_xml=f"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>{level}</DesiredVolume>",
        timeout_s=timeout_s,
        service=SONOS_RENDERING_CONTROL_SERVICE,
        control_path="/MediaRenderer/RenderingControl/Control",
    )


def _sonos_snapshot_player(root_url: str, *, timeout_s: float) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {}
    with contextlib.suppress(Exception):
        snapshot["transport"] = _sonos_av_transport_values(root_url, action="GetTransportInfo", timeout_s=timeout_s)
    with contextlib.suppress(Exception):
        snapshot["media"] = _sonos_av_transport_values(root_url, action="GetMediaInfo", timeout_s=timeout_s)
    with contextlib.suppress(Exception):
        snapshot["position"] = _sonos_av_transport_values(root_url, action="GetPositionInfo", timeout_s=timeout_s)
    with contextlib.suppress(Exception):
        snapshot["volume"] = _sonos_get_volume(root_url, timeout_s=timeout_s)
    return snapshot


def _sonos_transport_state(snapshot: Dict[str, Any]) -> str:
    transport = snapshot.get("transport") if isinstance(snapshot.get("transport"), dict) else {}
    return _text(transport.get("CurrentTransportState")).upper()


def _sonos_snapshot_uri(snapshot: Dict[str, Any]) -> str:
    media = snapshot.get("media") if isinstance(snapshot.get("media"), dict) else {}
    position = snapshot.get("position") if isinstance(snapshot.get("position"), dict) else {}
    return _text(media.get("CurrentURI")) or _text(position.get("TrackURI"))


def _sonos_snapshot_metadata(snapshot: Dict[str, Any]) -> str:
    media = snapshot.get("media") if isinstance(snapshot.get("media"), dict) else {}
    position = snapshot.get("position") if isinstance(snapshot.get("position"), dict) else {}
    return _text(media.get("CurrentURIMetaData")) or _text(position.get("TrackMetaData"))


def _sonos_seek(root_url: str, rel_time: Any, *, timeout_s: float) -> None:
    target = _text(rel_time)
    if not target or target in {"0:00:00", "00:00:00", "NOT_IMPLEMENTED"}:
        return
    _sonos_soap_action(
        root_url,
        action="Seek",
        inner_xml=f"<InstanceID>0</InstanceID><Unit>REL_TIME</Unit><Target>{xml_escape(target)}</Target>",
        timeout_s=timeout_s,
    )


def _sonos_play(root_url: str, *, timeout_s: float) -> None:
    _sonos_soap_action(
        root_url,
        action="Play",
        inner_xml="<InstanceID>0</InstanceID><Speed>1</Speed>",
        timeout_s=timeout_s,
    )


def _sonos_pause(root_url: str, *, timeout_s: float) -> None:
    _sonos_soap_action(root_url, action="Pause", inner_xml="<InstanceID>0</InstanceID>", timeout_s=timeout_s)


def _sonos_stop(root_url: str, *, timeout_s: float) -> None:
    _sonos_soap_action(root_url, action="Stop", inner_xml="<InstanceID>0</InstanceID>", timeout_s=timeout_s)


def _sonos_current_transport_state(root_url: str, *, timeout_s: float) -> str:
    values = _sonos_av_transport_values(root_url, action="GetTransportInfo", timeout_s=timeout_s)
    return _text(values.get("CurrentTransportState")).upper()


def _sonos_host_from_value(value: Any) -> str:
    token = _text(value)
    if not token:
        return ""
    parsed = urlparse(token)
    if parsed.hostname:
        return parsed.hostname
    parsed = urlparse(f"//{token}")
    return _text(parsed.hostname or token.split(":", 1)[0])


def _sonos_audio_clip_host(speaker: Dict[str, Any]) -> str:
    return _sonos_host_from_value(speaker.get("host")) or _sonos_host_from_value(speaker.get("root_url"))


def _sonos_audio_clip_player_id(speaker: Dict[str, Any]) -> str:
    return (
        sonos_target_id(speaker.get("coordinator_id"))
        or sonos_target_id(speaker.get("id"))
        or sonos_target_id(speaker.get("udn"))
    )


def _sonos_audio_clip_member_targets(speaker: Dict[str, Any]) -> List[Dict[str, Any]]:
    row = speaker if isinstance(speaker, dict) else {}
    kind = _text(row.get("sonos_target_kind")).lower()
    if kind != "stereo_pair":
        return [dict(row)]

    member_rows = row.get("member_rows") if isinstance(row.get("member_rows"), list) else []
    targets: List[Dict[str, Any]] = []
    if member_rows:
        for member in member_rows:
            if not isinstance(member, dict):
                continue
            member_id = sonos_target_id(member.get("id") or member.get("udn"))
            if not member_id:
                continue
            next_row = dict(row)
            next_row["id"] = member_id
            next_row["udn"] = _text(member.get("udn")) or f"uuid:{member_id}"
            next_row["coordinator_id"] = ""
            next_row["host"] = _text(member.get("host")) or _text(row.get("host"))
            next_row["root_url"] = normalize_sonos_root(member.get("root_url")) or _text(row.get("root_url"))
            targets.append(next_row)
    else:
        member_ids = row.get("member_ids") if isinstance(row.get("member_ids"), list) else []
        member_hosts = row.get("member_hosts") if isinstance(row.get("member_hosts"), list) else []
        member_roots = row.get("member_root_urls") if isinstance(row.get("member_root_urls"), list) else []
        for index, raw_id in enumerate(member_ids):
            member_id = sonos_target_id(raw_id)
            if not member_id:
                continue
            next_row = dict(row)
            next_row["id"] = member_id
            next_row["udn"] = f"uuid:{member_id}"
            next_row["coordinator_id"] = ""
            next_row["host"] = _text(member_hosts[index] if index < len(member_hosts) else "") or _text(row.get("host"))
            next_row["root_url"] = (
                normalize_sonos_root(member_roots[index] if index < len(member_roots) else "")
                or _text(row.get("root_url"))
            )
            targets.append(next_row)

    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for target in targets or [dict(row)]:
        player_id = _sonos_audio_clip_player_id(target)
        if not player_id or player_id in seen:
            continue
        seen.add(player_id)
        deduped.append(target)
    return deduped or [dict(row)]


def _sonos_websocket_command(
    host: str,
    command: Dict[str, Any],
    options: Dict[str, Any],
    *,
    timeout_s: float,
) -> List[Any]:
    if websocket is None:
        raise RuntimeError("websocket-client is not installed.")
    if not host:
        raise RuntimeError("Sonos speaker host is missing.")

    connection = websocket.create_connection(
        f"wss://{host}:1443/websocket/api",
        timeout=max(2.0, min(10.0, float(timeout_s or SONOS_DEFAULT_PLAY_TIMEOUT_SECONDS))),
        sslopt={"cert_reqs": ssl.CERT_NONE, "check_hostname": False},
        header=[f"X-Sonos-Api-Key: {SONOS_AUDIOCLIP_API_KEY}"],
        subprotocols=[SONOS_AUDIOCLIP_SUBPROTOCOL],
    )
    try:
        connection.send(json.dumps([command, options]))
        raw_response = connection.recv()
    finally:
        with contextlib.suppress(Exception):
            connection.close()

    try:
        response = json.loads(_text(raw_response))
    except Exception as exc:
        raise RuntimeError(f"Sonos audio clip returned an invalid response: {_text(raw_response)[:200]}") from exc
    if isinstance(response, list):
        return response
    return [response]


def _sonos_play_audio_clip_sync(
    speaker: Dict[str, Any],
    source_url: Any,
    *,
    timeout_s: float,
    volume: Any = None,
) -> List[Any]:
    url = _text(source_url)
    if not url:
        raise RuntimeError("Sonos source URL is missing.")
    host = _sonos_audio_clip_host(speaker)
    player_id = _sonos_audio_clip_player_id(speaker)
    if not player_id:
        raise RuntimeError("Sonos player id is missing.")

    command = {
        "namespace": "audioClip:1",
        "command": "loadAudioClip",
        "playerId": player_id,
    }
    options: Dict[str, Any] = {
        "name": SONOS_AUDIOCLIP_NAME,
        "appId": SONOS_AUDIOCLIP_APP_ID,
        "streamUrl": url,
    }
    if volume is not None:
        with contextlib.suppress(Exception):
            options["volume"] = max(0, min(100, int(float(_text(volume)))))

    response = _sonos_websocket_command(host, command, options, timeout_s=timeout_s)
    first = response[0] if response else {}
    if isinstance(first, dict) and first.get("success") is True:
        return response
    raise RuntimeError(f"Sonos audio clip failed: {_text(first or response)[:200]}")


def _sonos_play_audio_clip_targets_sync(
    speaker: Dict[str, Any],
    source_url: Any,
    *,
    timeout_s: float,
    volume: Any = None,
) -> List[Any]:
    responses: List[Any] = []
    failures: List[str] = []
    for target in _sonos_audio_clip_member_targets(speaker):
        player_id = _sonos_audio_clip_player_id(target)
        try:
            responses.extend(
                _sonos_play_audio_clip_sync(
                    target,
                    source_url,
                    timeout_s=timeout_s,
                    volume=volume,
                )
            )
        except Exception as exc:
            failures.append(f"{player_id or 'unknown'} ({exc})")
    if responses:
        return responses
    raise RuntimeError("; ".join(failures) or "Sonos audio clip failed.")


def _sonos_replace_transport_uri(root_url: str, source_url: Any, *, timeout_s: float) -> None:
    url = _text(source_url)
    if not root_url:
        raise RuntimeError("Sonos speaker root URL is missing.")
    if not url:
        raise RuntimeError("Sonos source URL is missing.")
    escaped_url = xml_escape(url, {'"': "&quot;"})
    _sonos_soap_action(
        root_url,
        action="SetAVTransportURI",
        inner_xml=(
            "<InstanceID>0</InstanceID>"
            f"<CurrentURI>{escaped_url}</CurrentURI>"
            "<CurrentURIMetaData></CurrentURIMetaData>"
        ),
        timeout_s=timeout_s,
    )
    _sonos_play(root_url, timeout_s=timeout_s)


def _sonos_wait_for_playback_end(root_url: str, *, timeout_s: float) -> None:
    deadline = time.monotonic() + max(1.0, float(timeout_s or SONOS_DEFAULT_PLAY_TIMEOUT_SECONDS))
    saw_playing = False
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            values = _sonos_av_transport_values(root_url, action="GetTransportInfo", timeout_s=min(3.0, timeout_s))
            state = _text(values.get("CurrentTransportState")).upper()
            if state == "PLAYING":
                saw_playing = True
            elif saw_playing and state not in {"TRANSITIONING"}:
                return
        time.sleep(0.25)


def _sonos_restore_player(root_url: str, snapshot: Dict[str, Any], *, timeout_s: float) -> None:
    if not isinstance(snapshot, dict) or not snapshot:
        return
    uri = _sonos_snapshot_uri(snapshot)
    metadata = _sonos_snapshot_metadata(snapshot)
    state = _sonos_transport_state(snapshot)
    if uri:
        escaped_uri = xml_escape(uri, {'"': "&quot;"})
        escaped_metadata = xml_escape(metadata, {'"': "&quot;"})
        _sonos_soap_action(
            root_url,
            action="SetAVTransportURI",
            inner_xml=(
                "<InstanceID>0</InstanceID>"
                f"<CurrentURI>{escaped_uri}</CurrentURI>"
                f"<CurrentURIMetaData>{escaped_metadata}</CurrentURIMetaData>"
            ),
            timeout_s=timeout_s,
        )
        position = snapshot.get("position") if isinstance(snapshot.get("position"), dict) else {}
        with contextlib.suppress(Exception):
            _sonos_seek(root_url, position.get("RelTime"), timeout_s=timeout_s)

    volume = snapshot.get("volume")
    if volume is not None:
        with contextlib.suppress(Exception):
            _sonos_set_volume(root_url, volume, timeout_s=timeout_s)

    if state == "PLAYING":
        with contextlib.suppress(Exception):
            _sonos_play(root_url, timeout_s=timeout_s)
    elif state == "PAUSED_PLAYBACK":
        with contextlib.suppress(Exception):
            _sonos_play(root_url, timeout_s=timeout_s)
            _sonos_pause(root_url, timeout_s=timeout_s)
    elif state == "STOPPED":
        with contextlib.suppress(Exception):
            _sonos_stop(root_url, timeout_s=timeout_s)


def _zone_group_state_xml(root_url: str, *, timeout_s: float) -> str:
    body = _sonos_soap_action(
        root_url,
        action="GetZoneGroupState",
        inner_xml="",
        timeout_s=timeout_s,
        service=SONOS_ZONE_GROUP_TOPOLOGY_SERVICE,
        control_path="/ZoneGroupTopology/Control",
    )
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return ""
    for node in root.iter():
        if _xml_local_name(node.tag) == "ZoneGroupState":
            return _text(node.text)
    return ""


def _parse_zone_groups(zone_group_state_xml: Any) -> List[Dict[str, Any]]:
    payload = html.unescape(_text(zone_group_state_xml))
    if not payload:
        return []
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return []

    groups: List[Dict[str, Any]] = []
    for group_node in root.iter():
        if _xml_local_name(group_node.tag) != "ZoneGroup":
            continue
        members: List[Dict[str, Any]] = []
        for member_node in list(group_node):
            if _xml_local_name(member_node.tag) != "ZoneGroupMember":
                continue
            attrs = {str(key): _text(value) for key, value in dict(member_node.attrib).items()}
            member_id = sonos_target_id(attrs.get("UUID"))
            location = _text(attrs.get("Location"))
            root_url = _root_from_url(location)
            parsed = urlparse(root_url or location)
            members.append(
                {
                    "id": member_id,
                    "udn": _text(attrs.get("UUID")),
                    "name": _text(attrs.get("ZoneName")),
                    "location": location,
                    "root_url": root_url,
                    "host": _text(parsed.hostname),
                    "invisible": _xml_attr_bool(attrs.get("Invisible")),
                    "channel_map_set": _text(attrs.get("ChannelMapSet")),
                    "ht_sat_chan_map_set": _text(attrs.get("HTSatChanMapSet")),
                    "configuration": _text(attrs.get("Configuration")),
                    "raw": attrs,
                }
            )
        groups.append(
            {
                "id": _text(group_node.attrib.get("ID")),
                "coordinator": sonos_target_id(group_node.attrib.get("Coordinator")),
                "members": members,
            }
        )
    return groups


def _description_indexes(rows: List[Dict[str, str]]) -> Dict[str, Dict[str, Dict[str, str]]]:
    by_id: Dict[str, Dict[str, str]] = {}
    by_root: Dict[str, Dict[str, str]] = {}
    by_host: Dict[str, Dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for value in (row.get("id"), row.get("udn")):
            token = sonos_target_id(value).lower()
            if token:
                by_id[token] = row
        root_url = normalize_sonos_root(row.get("root_url"))
        if root_url:
            by_root[root_url.lower()] = row
        host = _text(row.get("host")).lower()
        if host:
            by_host[host] = row
    return {"id": by_id, "root": by_root, "host": by_host}


def _description_for_member(
    member: Dict[str, Any],
    indexes: Dict[str, Dict[str, Dict[str, str]]],
) -> Dict[str, str]:
    member_id = sonos_target_id(member.get("id") or member.get("udn")).lower()
    if member_id and member_id in indexes["id"]:
        return dict(indexes["id"][member_id])
    root_url = normalize_sonos_root(member.get("root_url") or member.get("location")).lower()
    if root_url and root_url in indexes["root"]:
        return dict(indexes["root"][root_url])
    host = _text(member.get("host")).lower()
    if host and host in indexes["host"]:
        return dict(indexes["host"][host])
    return {}


def _member_from_description(row: Dict[str, Any], member_id: str = "") -> Dict[str, Any]:
    speaker_id = sonos_target_id(member_id or row.get("id") or row.get("udn"))
    root_url = normalize_sonos_root(row.get("root_url") or row.get("location"))
    parsed = urlparse(root_url or _text(row.get("location")))
    return {
        "id": speaker_id,
        "udn": _text(row.get("udn")) or (f"uuid:{speaker_id}" if speaker_id else ""),
        "name": _text(row.get("name")),
        "location": _text(row.get("location")),
        "root_url": root_url,
        "host": _text(row.get("host")) or _text(parsed.hostname),
        "invisible": False,
        "channel_map_set": "",
        "ht_sat_chan_map_set": "",
        "configuration": "",
        "raw": {},
    }


def _sonos_ids_from_channel_map(value: Any) -> List[str]:
    ids: List[str] = []
    seen: set[str] = set()
    for part in _text(value).split(";"):
        raw_id = _text(part.split(":", 1)[0])
        speaker_id = sonos_target_id(raw_id)
        if not speaker_id or speaker_id in seen:
            continue
        seen.add(speaker_id)
        ids.append(speaker_id)
    return ids


def _member_is_bonded(member: Dict[str, Any], coordinator_id: str) -> bool:
    member_id = sonos_target_id(member.get("id") or member.get("udn"))
    if _xml_attr_bool(member.get("invisible")):
        return True
    if member_id and coordinator_id and member_id == coordinator_id:
        return False
    return bool(_text(member.get("channel_map_set")) or _text(member.get("ht_sat_chan_map_set")))


def _sonos_set_label(kind: str, member_count: int) -> str:
    if kind == "home_theater":
        return "Home Theater"
    if kind == "stereo_pair":
        return "Stereo Pair"
    return "Sonos Set" if member_count > 1 else "Speaker"


def _sonos_set_kind(members: List[Dict[str, Any]], descriptions: List[Dict[str, str]]) -> str:
    member_count = len(members)
    haystack = " ".join(
        _text(value).lower()
        for member in members
        for value in (
            member.get("ht_sat_chan_map_set"),
            member.get("channel_map_set"),
            member.get("configuration"),
        )
    )
    model_haystack = " ".join(_text(desc.get("model")).lower() for desc in descriptions)
    theater_hints = {"arc", "beam", "playbar", "playbase", "soundbar", "sub", "surround"}
    if "htsat" in haystack or member_count >= 3 or any(hint in model_haystack for hint in theater_hints):
        return "home_theater"
    if member_count == 2:
        return "stereo_pair"
    return "set"


def _apply_sonos_topology(rows: List[Dict[str, str]], *, timeout_s: float) -> List[Dict[str, str]]:
    physical_rows = _dedupe_sonos_rows(rows)
    if not physical_rows:
        return []

    groups: List[Dict[str, Any]] = []
    for row in physical_rows:
        root_url = normalize_sonos_root(row.get("root_url"))
        if not root_url:
            continue
        try:
            zone_xml = _zone_group_state_xml(root_url, timeout_s=timeout_s)
        except Exception:
            zone_xml = ""
        groups = _parse_zone_groups(zone_xml)
        if groups:
            break
    if not groups:
        return physical_rows

    indexes = _description_indexes(physical_rows)
    covered_ids: set[str] = set()
    target_rows: List[Dict[str, Any]] = []

    for group in groups:
        members = [dict(item) for item in list(group.get("members") or []) if isinstance(item, dict)]
        coordinator_id = sonos_target_id(group.get("coordinator"))
        members_by_id = {
            sonos_target_id(member.get("id") or member.get("udn")): member
            for member in members
            if sonos_target_id(member.get("id") or member.get("udn"))
        }
        mapped_member_ids: List[str] = []
        for member in members:
            mapped_member_ids.extend(_sonos_ids_from_channel_map(member.get("channel_map_set")))
            mapped_member_ids.extend(_sonos_ids_from_channel_map(member.get("ht_sat_chan_map_set")))
        for mapped_id in mapped_member_ids:
            if mapped_id in members_by_id:
                continue
            desc = indexes["id"].get(mapped_id.lower())
            if not isinstance(desc, dict):
                continue
            mapped_member = _member_from_description(desc, mapped_id)
            members_by_id[mapped_id] = mapped_member
            members.append(mapped_member)

        bonded_members = [member for member in members if _member_is_bonded(member, coordinator_id)]
        if mapped_member_ids:
            bonded_members.extend(
                member
                for member_id, member in members_by_id.items()
                if member_id != coordinator_id and member_id in set(mapped_member_ids)
            )
        if not bonded_members:
            continue

        coordinator_member = next(
            (member for member in members if sonos_target_id(member.get("id") or member.get("udn")) == coordinator_id),
            {},
        )
        if not coordinator_member:
            coordinator_member = next((member for member in members if not _member_is_bonded(member, coordinator_id)), {})
        if not coordinator_member and members:
            coordinator_member = members[0]
        if not coordinator_id:
            coordinator_id = sonos_target_id(coordinator_member.get("id") or coordinator_member.get("udn"))
        if not coordinator_id:
            continue

        set_members: List[Dict[str, Any]] = []
        seen_member_ids: set[str] = set()
        for member in [coordinator_member, *bonded_members]:
            member_id = sonos_target_id(member.get("id") or member.get("udn"))
            if not member_id or member_id in seen_member_ids:
                continue
            seen_member_ids.add(member_id)
            set_members.append(member)
        if len(set_members) <= 1:
            continue

        descriptions = [_description_for_member(member, indexes) for member in set_members]
        coordinator_desc = _description_for_member(coordinator_member, indexes)
        target = dict(coordinator_desc)
        target["id"] = coordinator_id
        target["udn"] = _text(coordinator_desc.get("udn")) or f"uuid:{coordinator_id}"
        target["root_url"] = normalize_sonos_root(coordinator_desc.get("root_url") or coordinator_member.get("root_url"))
        target["location"] = _text(coordinator_desc.get("location")) or _text(coordinator_member.get("location"))
        target["host"] = _text(coordinator_desc.get("host")) or _text(coordinator_member.get("host"))

        base_name = _text(coordinator_member.get("name")) or _text(coordinator_desc.get("name")) or coordinator_id
        kind = _sonos_set_kind(set_members, descriptions)
        kind_label = _sonos_set_label(kind, len(set_members))
        member_names = [_text(member.get("name")) for member in set_members if _text(member.get("name"))]
        member_ids = [sonos_target_id(member.get("id") or member.get("udn")) for member in set_members]
        member_hosts = [_text(member.get("host")) for member in set_members if _text(member.get("host"))]
        member_roots = [
            normalize_sonos_root(member.get("root_url") or member.get("location"))
            for member in set_members
            if normalize_sonos_root(member.get("root_url") or member.get("location"))
        ]
        member_rows = [
            {
                "id": sonos_target_id(member.get("id") or member.get("udn")),
                "udn": _text(member.get("udn")) or f"uuid:{sonos_target_id(member.get('id') or member.get('udn'))}",
                "name": _text(member.get("name")),
                "host": _text(member.get("host")),
                "root_url": normalize_sonos_root(member.get("root_url") or member.get("location")),
            }
            for member in set_members
            if sonos_target_id(member.get("id") or member.get("udn"))
        ]

        target.update(
            {
                "name": base_name,
                "display_name": f"{base_name} {kind_label}",
                "sonos_target_kind": kind,
                "sonos_set_label": kind_label,
                "member_count": str(len(set_members)),
                "member_ids": member_ids,
                "member_names": member_names,
                "member_hosts": member_hosts,
                "member_root_urls": member_roots,
                "member_rows": member_rows,
                "aliases": [*member_ids, *member_hosts, *member_roots],
                "coordinator_id": coordinator_id,
                "zone_group_id": _text(group.get("id")),
            }
        )
        target_rows.append(target)
        covered_ids.update(member_id for member_id in member_ids if member_id)

    for row in physical_rows:
        speaker_id = sonos_target_id(row.get("id") or row.get("udn"))
        if speaker_id and speaker_id in covered_ids:
            continue
        next_row = dict(row)
        next_row.setdefault("sonos_target_kind", "speaker")
        next_row.setdefault("member_count", "1")
        target_rows.append(next_row)

    return _dedupe_sonos_rows(target_rows)  # type: ignore[arg-type]


def discover_sonos_speakers(*, force: bool = False, timeout_s: Any = None) -> List[Dict[str, str]]:
    settings = read_sonos_settings()
    if not _as_bool(settings.get("SONOS_ENABLED"), SONOS_DEFAULT_ENABLED):
        return []
    if not force:
        cached = _cached_rows()
        if cached is not None:
            return _dedupe_sonos_rows(cached)

    timeout = _bounded_int(
        timeout_s if timeout_s is not None else settings.get("SONOS_DISCOVERY_TIMEOUT_SECONDS"),
        default=SONOS_DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
        minimum=1,
        maximum=10,
    )
    candidates = _discover_sonos_locations_ssdp(float(timeout))
    candidates.extend(_split_sonos_manual_hosts(settings.get("SONOS_SPEAKER_HOSTS")))

    rows: List[Dict[str, str]] = []
    seen_candidates: set[str] = set()
    unique_candidates: List[str] = []
    for item in candidates:
        token = _text(item)
        key = token.lower()
        if not token or key in seen_candidates:
            continue
        seen_candidates.add(key)
        unique_candidates.append(token)

    if unique_candidates:
        with ThreadPoolExecutor(max_workers=min(12, max(1, len(unique_candidates)))) as executor:
            future_map = {
                executor.submit(_fetch_sonos_speaker, candidate, timeout_s=max(1.0, float(timeout))): candidate
                for candidate in unique_candidates
            }
            for future in as_completed(future_map):
                try:
                    row = future.result()
                except Exception:
                    row = {}
                if row:
                    rows.append(row)

    rows = _apply_sonos_topology(rows, timeout_s=float(timeout))
    _cache_rows(rows)
    return rows


def sonos_target_id(value: Any) -> str:
    token = _text(value)
    if not token:
        return ""
    lower = token.lower()
    if lower.startswith(SONOS_TARGET_PREFIX):
        token = _text(token[len(SONOS_TARGET_PREFIX) :])
    if token.lower().startswith("uuid:"):
        token = _text(token[5:])
    return token


def resolve_sonos_target(target: Any) -> Dict[str, str]:
    token = sonos_target_id(target)
    if not token:
        return {}
    token_l = token.lower()
    for row in discover_sonos_speakers():
        aliases = {
            _text(row.get("id")).lower(),
            sonos_target_id(row.get("udn")).lower(),
            _text(row.get("root_url")).lower(),
            _text(row.get("host")).lower(),
            _text(row.get("name")).lower(),
            _text(row.get("display_name")).lower(),
        }
        for key in ("member_ids", "member_hosts", "member_root_urls", "aliases"):
            values = row.get(key) if isinstance(row.get(key), list) else []
            aliases.update(_text(item).lower() for item in values if _text(item))
            aliases.update(sonos_target_id(item).lower() for item in values if sonos_target_id(item))
        if token_l in aliases:
            return row
    if token_l.startswith(("http://", "https://")) or "." in token_l or ":" in token_l:
        return _fetch_sonos_speaker(token, timeout_s=2.0)
    return {}


def sonos_play_url_sync(
    *,
    speaker: Dict[str, Any],
    source_url: Any,
    timeout_s: float = SONOS_DEFAULT_PLAY_TIMEOUT_SECONDS,
    restore_after: bool = True,
    restore_wait_s: Optional[float] = None,
) -> None:
    root_url = _text(speaker.get("root_url")) if isinstance(speaker, dict) else ""
    url = _text(source_url)
    if not root_url:
        raise RuntimeError("Sonos speaker root URL is missing.")
    if not url:
        raise RuntimeError("Sonos source URL is missing.")

    state = ""
    with contextlib.suppress(Exception):
        state = _sonos_current_transport_state(root_url, timeout_s=min(5.0, timeout_s))
    try:
        _sonos_play_audio_clip_targets_sync(speaker, url, timeout_s=timeout_s)
        return
    except Exception as exc:
        if state not in {"STOPPED", "NO_MEDIA_PRESENT"}:
            raise RuntimeError(
                "Sonos audio clip failed while the speaker was playing or its state was unknown; "
                "refused to replace Sonos playback."
            ) from exc
        _sonos_replace_transport_uri(root_url, url, timeout_s=timeout_s)


def sonos_play_media_sync(
    *,
    speakers: List[str],
    source_url: Any,
    timeout_s: float = SONOS_DEFAULT_PLAY_TIMEOUT_SECONDS,
    restore_after: bool = True,
    restore_wait_s: Optional[float] = None,
) -> Dict[str, Any]:
    clean_speakers = [sonos_target_id(item) for item in list(speakers or []) if sonos_target_id(item)]
    if not clean_speakers:
        return {"ok": False, "sent_count": 0, "error": "No Sonos speakers selected."}
    url = _text(source_url)
    if not url:
        return {"ok": False, "sent_count": 0, "error": "Sonos announcement URL is missing."}

    sent_count = 0
    failures: List[str] = []
    for target in clean_speakers:
        try:
            speaker = resolve_sonos_target(target)
            if not speaker:
                raise RuntimeError("speaker was not found")
            sonos_play_url_sync(
                speaker=speaker,
                source_url=url,
                timeout_s=timeout_s,
                restore_after=restore_after,
                restore_wait_s=restore_wait_s,
            )
            sent_count += 1
        except Exception as exc:
            failures.append(f"{target} ({exc})")

    if sent_count:
        result: Dict[str, Any] = {"ok": True, "sent_count": sent_count}
        if failures:
            result["warnings"] = failures
        return result
    return {"ok": False, "sent_count": 0, "error": "; ".join(failures) or "Sonos playback failed."}


def read_integration_settings() -> Dict[str, Any]:
    settings = read_sonos_settings()
    return {
        "sonos_enabled": _as_bool(settings.get("SONOS_ENABLED"), SONOS_DEFAULT_ENABLED),
        "sonos_discovery_timeout_seconds": int(
            settings.get("SONOS_DISCOVERY_TIMEOUT_SECONDS") or SONOS_DEFAULT_DISCOVERY_TIMEOUT_SECONDS
        ),
        "sonos_speaker_hosts": settings.get("SONOS_SPEAKER_HOSTS", ""),
    }


def integration_status() -> Dict[str, Any]:
    settings = read_sonos_settings()
    enabled = _as_bool(settings.get("SONOS_ENABLED"), SONOS_DEFAULT_ENABLED)
    return {
        "enabled": enabled,
        "message": "Sonos announcements are enabled." if enabled else "Sonos announcements are disabled.",
    }


def integration_devices() -> Dict[str, Any]:
    settings = read_sonos_settings()
    if not _as_bool(settings.get("SONOS_ENABLED"), SONOS_DEFAULT_ENABLED):
        return {"devices": [], "message": "Sonos announcements are disabled."}
    speakers = discover_sonos_speakers(force=False)
    devices: List[Dict[str, Any]] = []
    for speaker in speakers:
        if not isinstance(speaker, dict):
            continue
        speaker_id = _text(speaker.get("id")) or _text(speaker.get("udn"))
        devices.append(
            {
                "id": speaker_id,
                "name": _text(speaker.get("display_name")) or _text(speaker.get("name")) or _text(speaker.get("host")) or "Sonos Speaker",
                "type": "speaker",
                "ref": f"speaker:{speaker_id}" if speaker_id else "",
                "capabilities": ["speaker", "media_player", "audio_output", "announcement_target", "play_media"],
                "actions": ["play_media", "play_url", "announce"],
                "status": "available",
                "state": "available",
                "details": {
                    "model": speaker.get("model"),
                    "host": speaker.get("host"),
                    "root_url": speaker.get("root_url"),
                    "udn": speaker.get("udn"),
                    "sonos_target_kind": speaker.get("sonos_target_kind"),
                    "member_count": speaker.get("member_count"),
                    "member_names": speaker.get("member_names"),
                    "coordinator_id": speaker.get("coordinator_id"),
                },
            }
        )
    return {"devices": devices, "message": f"Sonos returned {len(devices)} speakers."}


def save_integration_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    saved = save_sonos_settings(
        enabled=(payload or {}).get("sonos_enabled"),
        discovery_timeout_seconds=(payload or {}).get("sonos_discovery_timeout_seconds"),
        speaker_hosts=(payload or {}).get("sonos_speaker_hosts"),
    )
    return {
        "sonos_enabled": _as_bool(saved.get("SONOS_ENABLED"), SONOS_DEFAULT_ENABLED),
        "sonos_discovery_timeout_seconds": int(
            saved.get("SONOS_DISCOVERY_TIMEOUT_SECONDS") or SONOS_DEFAULT_DISCOVERY_TIMEOUT_SECONDS
        ),
        "sonos_speaker_hosts": saved.get("SONOS_SPEAKER_HOSTS", ""),
    }


def run_integration_action(action_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if _text(action_id) != "discover":
        raise KeyError(f"Unsupported Sonos action: {action_id}")
    if payload:
        save_integration_settings(payload)
    rows = discover_sonos_speakers(force=True)
    count = len(rows)
    return {
        "ok": True,
        "speaker_count": count,
        "speakers": rows,
        "message": f"Sonos discovery found {count} speaker{'s' if count != 1 else ''}.",
    }


def run_integration_device_action(action_id: str, device_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    action = _text(action_id).lower()
    if action not in {"play_media", "play_url", "announce"}:
        raise KeyError(f"Unsupported Sonos device action: {action_id}")
    source_url = _text((payload or {}).get("source_url") or (payload or {}).get("url") or (payload or {}).get("media_url"))
    if not source_url:
        raise ValueError("Sonos device action requires source_url.")
    timeout_s = float((payload or {}).get("timeout_s") or SONOS_DEFAULT_PLAY_TIMEOUT_SECONDS)
    result = sonos_play_media_sync(speakers=[device_id], source_url=source_url, timeout_s=timeout_s)
    result.setdefault("ok", bool(result.get("sent_count")))
    result.setdefault("device_id", sonos_target_id(device_id))
    result.setdefault("action", action)
    return result
