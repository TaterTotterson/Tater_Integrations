from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if "requests" not in sys.modules:
    requests = types.ModuleType("requests")
    packages = types.ModuleType("requests.packages")
    urllib3 = types.ModuleType("requests.packages.urllib3")
    exceptions = types.ModuleType("requests.packages.urllib3.exceptions")

    class InsecureRequestWarning(Warning):
        pass

    exceptions.InsecureRequestWarning = InsecureRequestWarning
    urllib3.exceptions = exceptions
    packages.urllib3 = urllib3
    requests.packages = packages
    sys.modules["requests"] = requests
    sys.modules["requests.packages"] = packages
    sys.modules["requests.packages.urllib3"] = urllib3
    sys.modules["requests.packages.urllib3.exceptions"] = exceptions

helpers = types.ModuleType("helpers")
helpers.redis_client = None
sys.modules.setdefault("helpers", helpers)
vision_settings = types.ModuleType("vision_settings")
vision_settings.get_vision_settings = lambda **_kwargs: {}
sys.modules.setdefault("vision_settings", vision_settings)

from integrations import unifi_protect


class UnifiProtectDeviceCapabilityTests(unittest.TestCase):
    def test_explicit_doorbell_flag_adds_doorbell_capability_and_event(self) -> None:
        row = {
            "name": "Front Entry",
            "isDoorbell": True,
            "smartDetectTypes": ["person", "animal"],
        }

        self.assertTrue(unifi_protect._unifi_camera_is_doorbell(row))
        self.assertIn("doorbell", unifi_protect._unifi_camera_capabilities(row))
        self.assertIn(
            "video_clip",
            unifi_protect._unifi_camera_capabilities(row, clip_available=True),
        )
        self.assertEqual(
            [source["type"] for source in unifi_protect._unifi_camera_event_sources("front", row)],
            ["motion", "smart_person", "smart_animal", "doorbell"],
        )

    def test_feature_flag_chime_identifies_doorbell_without_model_name(self) -> None:
        row = {"name": "Front Entry", "featureFlags": {"hasChime": True}}

        self.assertTrue(unifi_protect._unifi_camera_is_doorbell(row))

    def test_clip_capability_requires_configured_private_media_access(self) -> None:
        self.assertNotIn("video_clip", unifi_protect._unifi_camera_capabilities({}))
        self.assertIn(
            "video_clip",
            unifi_protect._unifi_camera_capabilities({}, clip_available=True),
        )

    def test_talkback_profile_matches_unifi_codec_container(self) -> None:
        cases = (
            ("aac", "aac", "adts"),
            ("opus", "libopus", "rtp"),
            ("vorbis", "libvorbis", "ogg"),
        )
        for codec, encoder, output_format in cases:
            with self.subTest(codec=codec):
                profile = unifi_protect._talkback_audio_profile(
                    {
                        "codec": codec,
                        "samplingRate": 24000,
                        "bitsPerSample": 16,
                    }
                )
                self.assertEqual(profile["encoder"], encoder)
                self.assertEqual(profile["output_format"], output_format)
                self.assertEqual(profile["sample_rate"], 24000)
                self.assertEqual(profile["bits_per_sample"], 16)

    def test_talkback_profile_rejects_unknown_codec(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported UniFi Protect talkback codec"):
            unifi_protect._talkback_audio_profile({"codec": "mystery"})

    def test_playback_uses_session_codec_container_and_realtime_pacing(self) -> None:
        session = {
            "url": "udp://192.0.2.10:7004",
            "codec": "aac",
            "samplingRate": 24000,
            "bitsPerSample": 16,
        }
        completed = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.object(
                unifi_protect,
                "read_unifi_protect_settings",
                return_value={
                    "base": "https://protect.local",
                    "api_key": "key",
                    "username": "",
                    "password": "",
                    "announcement_volume": "100",
                },
            ),
            patch.object(unifi_protect, "unifi_protect_request", return_value=session),
            patch.object(unifi_protect.shutil, "which", return_value="/usr/bin/ffmpeg"),
            patch.object(unifi_protect.subprocess, "run", return_value=completed) as run,
        ):
            result = unifi_protect.play_unifi_protect_audio_sync(
                cameras=["front-door"],
                audio_bytes=b"test audio",
            )

        self.assertTrue(result["ok"])
        command = run.call_args.args[0]
        self.assertIn("-re", command)
        self.assertIn("aac", command)
        self.assertEqual(command[command.index("-f") + 1], "adts")
        self.assertEqual(command[-1], session["url"])

    def test_announcement_volume_is_boosted_restored_and_does_not_change_ring(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.camera = {
                    "speakerSettings": {
                        "speakerVolume": 35,
                        "ringVolume": 62,
                    }
                }
                self.volume_changes = []
                self.closed = False

            def login(self) -> None:
                pass

            def get_camera(self, camera_id: str):
                self.assert_camera_id = camera_id
                return self.camera

            def set_speaker_volume(self, camera_id: str, level: int) -> None:
                self.volume_changes.append((camera_id, level))
                self.camera["speakerSettings"]["speakerVolume"] = level

            def close(self) -> None:
                self.closed = True

        client = FakeClient()
        settings = {
            "base": "https://protect.local",
            "api_key": "key",
            "username": "tater-local",
            "password": "secret",
            "announcement_volume": "100",
        }
        with (
            patch.object(unifi_protect, "read_unifi_protect_settings", return_value=settings),
            patch.object(unifi_protect, "_private_protect_client", return_value=client),
            patch.object(unifi_protect.shutil, "which", return_value="/usr/bin/ffmpeg"),
            patch.object(
                unifi_protect,
                "_play_unifi_camera_audio_sync",
                return_value={"ok": True},
            ),
        ):
            result = unifi_protect.play_unifi_protect_audio_sync(
                cameras=["front-door"],
                audio_bytes=b"test audio",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(client.volume_changes, [("front-door", 100), ("front-door", 35)])
        self.assertEqual(client.camera["speakerSettings"]["ringVolume"], 62)
        self.assertTrue(client.closed)

    def test_announcement_volume_is_restored_when_playback_fails(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.camera = {
                    "speakerSettings": {
                        "speakerVolume": 40,
                        "ringVolume": 70,
                    }
                }
                self.volume_changes = []

            def login(self) -> None:
                pass

            def get_camera(self, camera_id: str):
                return self.camera

            def set_speaker_volume(self, camera_id: str, level: int) -> None:
                self.volume_changes.append((camera_id, level))
                self.camera["speakerSettings"]["speakerVolume"] = level

            def close(self) -> None:
                pass

        client = FakeClient()
        with (
            patch.object(
                unifi_protect,
                "read_unifi_protect_settings",
                return_value={
                    "base": "https://protect.local",
                    "api_key": "key",
                    "username": "tater-local",
                    "password": "secret",
                    "announcement_volume": "100",
                },
            ),
            patch.object(unifi_protect, "_private_protect_client", return_value=client),
            patch.object(unifi_protect.shutil, "which", return_value="/usr/bin/ffmpeg"),
            patch.object(
                unifi_protect,
                "_play_unifi_camera_audio_sync",
                return_value={"ok": False, "error": "stream failed"},
            ),
        ):
            result = unifi_protect.play_unifi_protect_audio_sync(
                cameras=["front-door"],
                audio_bytes=b"test audio",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(client.volume_changes, [("front-door", 100), ("front-door", 40)])
        self.assertEqual(client.camera["speakerSettings"]["ringVolume"], 70)

    def test_private_client_uses_local_login_and_only_patches_speaker_volume(self) -> None:
        class FakeResponse:
            status_code = 200
            headers = {"x-csrf-token": "csrf-token"}
            content = b"{}"

            def json(self):
                return {}

        class FakeSession:
            def __init__(self) -> None:
                self.calls = []
                self.closed = False

            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                return FakeResponse()

            def close(self) -> None:
                self.closed = True

        session = FakeSession()
        with patch.object(unifi_protect.requests, "Session", return_value=session, create=True):
            client = unifi_protect._private_protect_client(
                {
                    "base_url": "https://protect.local:443",
                    "username": "tater-local",
                    "password": "secret",
                }
            )
            client.login()
            client.set_speaker_volume("front-door", 100)
            client.close()

        login = session.calls[0]
        self.assertEqual(login[0], "POST")
        self.assertEqual(login[1], "https://protect.local:443/api/auth/login")
        self.assertEqual(
            login[2]["json"],
            {"username": "tater-local", "password": "secret", "rememberMe": False},
        )
        volume_patch = session.calls[1]
        self.assertEqual(volume_patch[0], "PATCH")
        self.assertEqual(
            volume_patch[2]["json"],
            {"speakerSettings": {"speakerVolume": 100}},
        )
        self.assertNotIn("ringVolume", str(volume_patch[2]["json"]))
        self.assertEqual(volume_patch[2]["headers"]["X-CSRF-Token"], "csrf-token")
        self.assertTrue(session.closed)

    def test_private_volume_login_upgrades_standard_http_console_to_https(self) -> None:
        config = unifi_protect._private_volume_config(
            {
                "base": "http://10.4.20.127",
                "username": "tater",
                "password": "secret",
                "announcement_volume": "100",
            }
        )

        self.assertEqual(config["base_url"], "https://10.4.20.127:443")

    def test_clip_window_uses_event_time_and_bounds_duration(self) -> None:
        with (
            patch.object(unifi_protect.time, "time", return_value=1_786_486_910.0),
            patch.object(unifi_protect.time, "sleep") as sleep,
        ):
            start_ms, end_ms, duration = unifi_protect._unifi_camera_clip_window(
                {
                    "event_start": 1_786_486_908_000,
                    "duration_seconds": 8,
                    "pre_event_seconds": 2,
                    "post_event_seconds": 4,
                }
            )

        sleep.assert_called_once_with(2.0)
        self.assertEqual(duration, 8)
        self.assertEqual(start_ms, 1_786_486_906_000)
        self.assertEqual(end_ms, 1_786_486_910_000)

    def test_camera_clip_action_returns_integration_media_contract(self) -> None:
        with patch.object(
            unifi_protect,
            "get_camera_clip",
            return_value=(b"video", "video/mp4", {"duration_seconds": 8}),
        ):
            result = unifi_protect.run_integration_device_action(
                "camera_clip",
                "front-door",
                {"duration_seconds": 8},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["bytes"], b"video")
        self.assertEqual(result["content_type"], "video/mp4")


if __name__ == "__main__":
    unittest.main()
