from __future__ import annotations

import json
import sys
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

helpers = types.ModuleType("helpers")
helpers.redis_client = None
sys.modules.setdefault("helpers", helpers)

from integrations import google_cast


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.values: dict[str, str] = {}

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def hset(self, key: str, *, mapping: dict[str, str]) -> None:
        self.hashes[key] = dict(mapping)

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class FakeCastInfo:
    def __init__(self, device_id: str) -> None:
        self.uuid = uuid.UUID(device_id)
        self.host = "192.168.1.70"
        self.port = 8009
        self.friendly_name = "Office TV"
        self.model_name = "Chromecast"
        self.manufacturer = "Google Inc."
        self.cast_type = "cast"


class FakeZeroconf:
    instances: list["FakeZeroconf"] = []

    def __init__(self) -> None:
        self.closed = False
        self.instances.append(self)

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    last_instance = None

    def __init__(self, listener, zeroconf_instance=None, *, known_hosts=None) -> None:
        self.listener = listener
        self.zeroconf_instance = zeroconf_instance
        self.known_hosts = known_hosts
        info = FakeCastInfo("12345678-1234-5678-1234-567812345678")
        self.devices = {info.uuid: info}
        self.started = False
        self.stopped = False
        FakeBrowser.last_instance = self

    def start_discovery(self) -> None:
        self.started = True

    def stop_discovery(self) -> None:
        self.stopped = True


class FakeMediaController:
    def __init__(self) -> None:
        self.quick_play_calls: list[dict] = []
        self.pause_calls: list[float] = []

    def quick_play(self, **kwargs) -> None:
        self.quick_play_calls.append(dict(kwargs))

    def pause(self, *, timeout: float) -> None:
        self.pause_calls.append(timeout)


class FakeCast:
    def __init__(self) -> None:
        self.media_controller = FakeMediaController()
        self.volume_calls: list[tuple[float, float]] = []
        self.disconnect_calls: list[float] = []

    def set_volume(self, level: float, *, timeout: float) -> None:
        self.volume_calls.append((level, timeout))

    def disconnect(self, *, timeout: float) -> None:
        self.disconnect_calls.append(timeout)


class GoogleCastIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redis = FakeRedis()
        google_cast.redis_client = self.redis
        with google_cast._discovery_lock:
            google_cast._discovery_rows = []
            google_cast._discovery_ts = 0.0

    def test_missing_dependency_is_reported_without_import_failure(self) -> None:
        with patch.object(google_cast, "_load_cast_dependencies", return_value=None):
            status = google_cast.integration_status()
            devices = google_cast.integration_devices()

        self.assertFalse(status["configured"])
        self.assertIn("PyChromecast is missing", status["message"])
        self.assertEqual(devices["devices"], [])

    def test_discovery_exposes_generic_media_player(self) -> None:
        dependencies = {
            "CastBrowser": FakeBrowser,
            "SimpleCastListener": lambda: object(),
            "Zeroconf": FakeZeroconf,
        }
        self.redis.hashes[google_cast.GOOGLE_CAST_SETTINGS_KEY] = {
            "GOOGLE_CAST_DISCOVERY_TIMEOUT_SECONDS": "1",
            "GOOGLE_CAST_KNOWN_HOSTS": "192.168.1.70\noffice-tv.local",
        }
        with (
            patch.object(google_cast, "_load_cast_dependencies", return_value=dependencies),
            patch.object(google_cast.threading.Event, "wait", return_value=True),
        ):
            rows = google_cast.discover_google_cast_devices(force=True)
            result = google_cast.integration_devices()

        self.assertEqual(rows[0]["name"], "Office TV")
        self.assertIsNotNone(FakeBrowser.last_instance.zeroconf_instance)
        self.assertTrue(FakeBrowser.last_instance.zeroconf_instance.closed)
        self.assertEqual(result["devices"][0]["type"], "media_player")
        self.assertIn("play_media", result["devices"][0]["actions"])
        self.assertIn("media_player", result["devices"][0]["capabilities"])
        cached = json.loads(self.redis.values[google_cast.GOOGLE_CAST_DISCOVERY_CACHE_KEY])
        self.assertEqual(cached["rows"][0]["host"], "192.168.1.70")

    def test_play_media_uses_buffered_url_and_disconnects(self) -> None:
        target = {
            "id": "12345678-1234-5678-1234-567812345678",
            "name": "Office TV",
            "host": "192.168.1.70",
            "port": 8009,
            "available": True,
        }
        cast = FakeCast()
        dependencies = {"stream_type_buffered": "BUFFERED"}
        with (
            patch.object(google_cast, "resolve_google_cast_target", return_value=target),
            patch.object(google_cast, "_connect_cast", return_value=(cast, dependencies)),
        ):
            result = google_cast.run_integration_device_action(
                "play_media",
                target["id"],
                {
                    "source_url": "http://192.168.1.20:8501/runtime/song.mp3",
                    "media_type": "audio/mpeg",
                    "title": "Tater Song",
                    "volume_percent": 42,
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(cast.volume_calls, [(0.42, 12.0)])
        self.assertEqual(len(cast.media_controller.quick_play_calls), 1)
        request = cast.media_controller.quick_play_calls[0]
        self.assertEqual(request["media_id"], "http://192.168.1.20:8501/runtime/song.mp3")
        self.assertEqual(request["media_type"], "audio/mpeg")
        self.assertEqual(request["stream_type"], "BUFFERED")
        self.assertEqual(cast.disconnect_calls, [12.0])

    def test_play_media_rejects_non_http_source(self) -> None:
        target = {
            "id": "12345678-1234-5678-1234-567812345678",
            "host": "192.168.1.70",
            "available": True,
        }
        with patch.object(google_cast, "resolve_google_cast_target", return_value=target):
            with self.assertRaisesRegex(ValueError, "LAN-reachable HTTP"):
                google_cast.run_integration_device_action(
                    "play_media",
                    target["id"],
                    {"source_url": "file:///tmp/song.mp3"},
                )

    def test_play_media_rejects_loopback_source(self) -> None:
        target = {
            "id": "12345678-1234-5678-1234-567812345678",
            "host": "192.168.1.70",
            "available": True,
        }
        with patch.object(google_cast, "resolve_google_cast_target", return_value=target):
            with self.assertRaisesRegex(ValueError, "loopback media URL"):
                google_cast.run_integration_device_action(
                    "play_media",
                    target["id"],
                    {"source_url": "http://127.0.0.1:8501/song.mp3"},
                )


if __name__ == "__main__":
    unittest.main()
