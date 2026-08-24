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

from integrations import hue


class HueBrightnessActionTests(unittest.TestCase):
    def _run(self, action: str, payload: dict):
        with (
            patch.object(
                hue,
                "read_hue_settings",
                return_value={
                    "HUE_BRIDGE_HOST": "http://hue.test",
                    "HUE_APP_KEY": "test-key",
                    "HUE_TIMEOUT_SECONDS": 10,
                },
            ),
            patch.object(hue, "_hue_v2_put", return_value={"data": []}) as put,
        ):
            result = hue.run_integration_device_action(action, "light:test-light", payload)
        return result, put.call_args

    def test_turn_on_honors_explicit_brightness(self) -> None:
        result, call = self._run("turn_on", {"brightness_pct": 20})

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "turn_on")
        self.assertEqual(
            call.args[3],
            {
                "on": {"on": True},
                "dimming": {"brightness": 20.0},
            },
        )

    def test_turn_on_without_brightness_keeps_plain_power_action(self) -> None:
        _result, call = self._run("turn_on", {})

        self.assertEqual(call.args[3], {"on": {"on": True}})

    def test_zero_brightness_turns_light_off(self) -> None:
        _result, call = self._run("turn_on", {"brightness": 0})

        self.assertEqual(call.args[3], {"on": {"on": False}})


if __name__ == "__main__":
    unittest.main()
