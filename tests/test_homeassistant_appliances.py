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

from integrations import homeassistant


class HomeAssistantApplianceTests(unittest.TestCase):
    def _registry_context(self, platform: str = "example_washer") -> dict:
        return {
            "entities": {
                "sensor.laundry_current_status": {
                    "entity_id": "sensor.laundry_current_status",
                    "device_id": "washer-device-1",
                    "platform": platform,
                    "original_name": "Current status",
                },
                "sensor.laundry_remaining_time": {
                    "entity_id": "sensor.laundry_remaining_time",
                    "device_id": "washer-device-1",
                    "platform": platform,
                    "original_name": "Remaining time",
                    "original_device_class": "timestamp",
                },
            },
            "devices": {
                "washer-device-1": {
                    "id": "washer-device-1",
                    "name": "Laundry Washer",
                    "manufacturer": "Example Appliances",
                    "model": "Wash 3000",
                    "area_id": "laundry_room",
                }
            },
            "areas": {
                "laundry_room": {"area_id": "laundry_room", "name": "Laundry Room"}
            },
        }

    def test_appliance_semantics_are_provider_independent(self) -> None:
        for platform in ("lg_thinq", "home_connect", "custom_local_washer"):
            with self.subTest(platform=platform):
                context = self._registry_context(platform)
                caps = homeassistant._homeassistant_entity_capabilities(
                    "sensor.laundry_remaining_time",
                    {"friendly_name": "Laundry Washer Remaining time"},
                    context,
                )

                self.assertIn("appliance", caps)
                self.assertIn("washer", caps)
                self.assertIn("remaining_time", caps)

    def test_discovery_links_sibling_entities_and_preserves_timestamp_metadata(self) -> None:
        states = [
            {
                "entity_id": "sensor.laundry_current_status",
                "state": "Rinsing",
                "attributes": {"friendly_name": "Laundry Washer Current status"},
            },
            {
                "entity_id": "sensor.laundry_remaining_time",
                "state": "2026-10-21T02:48:09+00:00",
                "attributes": {
                    "friendly_name": "Laundry Washer Remaining time",
                    "device_class": "timestamp",
                },
            },
        ]
        context = self._registry_context()
        with (
            patch.object(
                homeassistant,
                "load_homeassistant_config",
                return_value={"base": "http://ha:8123", "token": "secret", "entity_scope": "all"},
            ),
            patch.object(homeassistant, "_homeassistant_rest_get", return_value=states),
            patch.object(homeassistant, "_homeassistant_registry_context", return_value=context),
        ):
            result = homeassistant.integration_devices()

        by_id = {row["id"]: row for row in result["devices"]}
        remaining = by_id["sensor.laundry_remaining_time"]
        current = by_id["sensor.laundry_current_status"]

        self.assertEqual(remaining["details"]["entity_property"], "remaining_time")
        self.assertEqual(remaining["details"]["device_class"], "timestamp")
        self.assertEqual(remaining["details"]["parent_device_name"], "Laundry Washer")
        self.assertEqual(remaining["details"]["homeassistant_device_id"], "washer-device-1")
        self.assertEqual(
            set(remaining["details"]["related_entity_ids"]),
            {"sensor.laundry_current_status", "sensor.laundry_remaining_time"},
        )
        self.assertIn("Laundry Washer", remaining["aliases"])
        self.assertEqual(current["details"]["entity_property"], "current_status")
        self.assertIn("washer", current["capabilities"])


if __name__ == "__main__":
    unittest.main()
