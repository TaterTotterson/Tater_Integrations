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
        self.assertEqual(
            [source["type"] for source in unifi_protect._unifi_camera_event_sources("front", row)],
            ["motion", "smart_person", "smart_animal", "doorbell"],
        )

    def test_feature_flag_chime_identifies_doorbell_without_model_name(self) -> None:
        row = {"name": "Front Entry", "featureFlags": {"hasChime": True}}

        self.assertTrue(unifi_protect._unifi_camera_is_doorbell(row))


if __name__ == "__main__":
    unittest.main()
