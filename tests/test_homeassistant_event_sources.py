from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path


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

from integrations import homeassistant


class HomeAssistantEventSourceTests(unittest.TestCase):
    def test_door_binary_sensor_declares_open_and_close(self) -> None:
        sources = homeassistant._homeassistant_entity_event_sources(
            "binary_sensor.front_door",
            "off",
            {"device_class": "door"},
        )

        self.assertEqual(
            sources,
            [
                {
                    "type": "door",
                    "ref": "binary_sensor.front_door",
                    "trigger_events": ["opens", "closes"],
                    "state_on": "on",
                    "state_off": "off",
                }
            ],
        )

    def test_motion_binary_sensor_declares_only_motion(self) -> None:
        sources = homeassistant._homeassistant_entity_event_sources(
            "binary_sensor.laundry_motion",
            "off",
            {"device_class": "motion"},
        )

        self.assertEqual(sources[0]["trigger_events"], ["motion"])

    def test_washer_enum_uses_reported_states(self) -> None:
        sources = homeassistant._homeassistant_entity_event_sources(
            "sensor.washer_state",
            "washing",
            {
                "device_class": "enum",
                "options": ["inactive", "washing", "rinsing", "wash_done"],
            },
        )

        self.assertEqual(sources[0]["trigger_events"], ["changed", "equals"])
        self.assertEqual(
            sources[0]["state_options"],
            ["inactive", "washing", "rinsing", "wash_done"],
        )

    def test_text_sensor_without_options_allows_exact_state_match(self) -> None:
        sources = homeassistant._homeassistant_entity_event_sources(
            "sensor.washer_state",
            "wash_done",
            {},
        )

        self.assertEqual(sources[0]["trigger_events"], ["changed", "equals"])
        self.assertNotIn("state_options", sources[0])

    def test_numeric_sensor_declares_threshold_events(self) -> None:
        sources = homeassistant._homeassistant_entity_event_sources(
            "sensor.washer_power",
            "412.5",
            {"device_class": "power", "unit_of_measurement": "W"},
        )

        self.assertEqual(sources[0]["trigger_events"], ["changed", "above", "below"])
        self.assertEqual(sources[0]["unit"], "W")


if __name__ == "__main__":
    unittest.main()
