from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

helpers = types.ModuleType("helpers")
helpers.redis_client = None
sys.modules.setdefault("helpers", helpers)

from integrations import sonos


class SonosVolumeMappingTests(unittest.TestCase):
    def tearDown(self) -> None:
        with sonos._sonos_music_groups_lock:
            sonos._sonos_music_groups.clear()

    def test_group_playback_applies_each_speaker_volume(self) -> None:
        speakers = [
            {"id": "left", "root_url": "http://left:1400"},
            {"id": "right", "root_url": "http://right:1400"},
        ]

        with (
            patch.object(sonos, "_restore_sonos_music_groups_for_targets", return_value=[]),
            patch.object(sonos, "_sonos_set_volume") as set_volume,
            patch.object(sonos, "_sonos_snapshot_player", return_value={}),
            patch.object(sonos, "_sonos_snapshot_uri", return_value=""),
            patch.object(sonos, "_sonos_set_transport_uri"),
            patch.object(sonos, "sonos_play_url_sync"),
        ):
            result = sonos._sonos_play_group_sync(
                speakers=speakers,
                source_url="http://tater.test/music.wav",
                timeout_s=5.0,
                start_position_seconds=0.0,
                volume_percent=50,
                volume_by_speaker={"left": 100, "right": 35},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            set_volume.call_args_list,
            [
                unittest.mock.call("http://left:1400", 100, timeout_s=5.0),
                unittest.mock.call("http://right:1400", 35, timeout_s=5.0),
            ],
        )

    def test_single_playback_uses_mapped_volume(self) -> None:
        speaker = {"id": "office", "root_url": "http://office:1400"}

        with (
            patch.object(sonos, "resolve_sonos_target", return_value=speaker),
            patch.object(sonos, "_restore_sonos_music_groups_for_targets", return_value=[]),
            patch.object(sonos, "_sonos_set_volume") as set_volume,
            patch.object(sonos, "sonos_play_url_sync") as play_url,
        ):
            result = sonos.sonos_play_media_sync(
                speakers=["sonos:office"],
                source_url="http://tater.test/music.wav",
                media_content_type="music",
                volume_percent=50,
                volume_by_speaker={"office": 72},
            )

        self.assertTrue(result["ok"])
        set_volume.assert_called_once_with("http://office:1400", 72, timeout_s=30.0)
        play_url.assert_called_once()


if __name__ == "__main__":
    unittest.main()
