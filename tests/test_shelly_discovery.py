from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
helpers = types.ModuleType("helpers")
helpers.redis_client = None
sys.modules.setdefault("helpers", helpers)

# Other integration tests install their own requests stubs. Isolate Shelly's
# HTTP imports too; every device request is mocked in setUp below.
requests_stub = types.ModuleType("requests")
auth_stub = types.ModuleType("requests.auth")
auth_stub.HTTPBasicAuth = Mock()
auth_stub.HTTPDigestAuth = Mock()
requests_stub.auth = auth_stub
with patch.dict(sys.modules, {"requests": requests_stub, "requests.auth": auth_stub}):
    from integrations import shelly


KNOWN = "http://10.4.20.10"
OTHER = "http://10.4.20.11"
NEW = "http://10.4.20.12"


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.strings = {}

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, *, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    def get(self, key):
        return self.strings.get(key)

    def set(self, key, value):
        self.strings[key] = value

    def delete(self, key):
        self.strings.pop(key, None)


class ShellyDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeRedis()
        self.store.hashes[shelly.SHELLY_SETTINGS_KEY] = {}
        self.settings = self.store.hashes[shelly.SHELLY_SETTINGS_KEY]
        self.offline = set()
        self.output = True
        self.gen = 2
        self.http_calls = []
        self.ids = {KNOWN: "shellyplus1pm-a", OTHER: "shellyplus1pm-b", NEW: "shellyplus1pm-c"}
        self.addCleanup(patch.stopall)
        patch.object(shelly, "redis_client", self.store).start()
        self.scan = patch.object(
            shelly, "_discover_shelly_roots_subnet",
            side_effect=AssertionError("unexpected subnet discovery"),
        ).start()
        patch.object(shelly, "_shelly_request", side_effect=self.request).start()

    def known(self, roots=(KNOWN,), timestamp=1):
        self.store.set(shelly.SHELLY_DISCOVERY_CACHE_KEY, json.dumps({"ts": timestamp, "roots": list(roots)}))

    def request(self, method, root, path, **kwargs):
        self.http_calls.append((method, root, path, kwargs))
        if root in self.offline:
            raise ConnectionError("offline")
        if path == "/shelly":
            info = {"id": self.ids[root], "mac": "AABBCCDDEEFF", "model": "SNSW-001P16EU"}
            if self.gen == 2:
                info["gen"] = 2
            return info
        if path == "/rpc/Shelly.GetStatus":
            return {"switch:0": {"output": self.output, "apower": 12.0}}
        if path == "/rpc/Shelly.GetConfig":
            return {"sys": {"device": {"name": "Desk plug"}}}
        if path == "/status":
            return {"relays": [{"ison": self.output}]}
        if path == "/settings":
            return {"name": "Desk plug"}
        if path in ("/rpc/Switch.Set", "/relay/0"):
            return {"ok": True}
        raise AssertionError(f"unexpected request path {path}")

    def test_old_discovery_records_remain_known_without_rescanning(self):
        self.known(timestamp=1)
        with patch.object(shelly.time, "time", return_value=2_000_000_000):
            self.assertEqual(shelly.discover_shelly_roots(), [KNOWN])
        self.assertEqual(self.http_calls, [])
        self.scan.assert_not_called()

    def test_manual_hosts_work_without_any_discovery_record(self):
        self.settings["SHELLY_DEVICE_HOSTS"] = f"{KNOWN}\n{OTHER}\n{KNOWN}/"
        self.assertEqual(shelly.discover_shelly_roots(), [KNOWN, OTHER])
        self.assertEqual(self.http_calls, [])

    def test_no_known_hosts_is_quiet_and_explains_discover(self):
        self.assertEqual(shelly.discover_shelly_roots(), [])
        result = shelly.integration_devices()
        self.assertEqual(result["devices"], [])
        self.assertIn("Discover Shelly Devices", result["message"])
        self.assertIn("Discover Shelly Devices", shelly.integration_status()["message"])
        self.assertEqual(self.http_calls, [])
        self.scan.assert_not_called()

    def test_corrupt_or_empty_records_never_trigger_automatic_discovery(self):
        for value in ("bad json", "[]", '{"roots":{}}', '{"roots":[null,{},4]}', '{"roots":[]}'):
            with self.subTest(value=value):
                self.store.set(shelly.SHELLY_DISCOVERY_CACHE_KEY, value)
                self.settings["SHELLY_DEVICE_HOSTS"] = KNOWN
                self.assertEqual(shelly.discover_shelly_roots(), [KNOWN])
        self.assertEqual(self.http_calls, [])
        self.scan.assert_not_called()

    def test_inventory_polls_fresh_states_only_at_known_hosts(self):
        self.known()
        self.settings["SHELLY_DEVICE_ALIASES"] = "shellyplus1pm-a = Desk plug"
        self.settings["SHELLY_DEVICE_ROOMS"] = "shellyplus1pm-a = Office"
        first = shelly.integration_devices()["devices"]
        self.output = False
        with patch.object(shelly.time, "time", return_value=2_000_000_000):
            second = shelly.integration_devices()["devices"]
        self.assertEqual(first[0]["state"], "on")
        self.assertEqual(second[0]["state"], "off")
        self.assertEqual(first[0]["name"], "Desk plug")
        self.assertEqual(first[0]["room"], "Office")
        self.assertEqual({row[1] for row in self.http_calls}, {KNOWN})
        self.scan.assert_not_called()

    def test_offline_host_does_not_stop_others_or_trigger_discovery(self):
        self.known((KNOWN, OTHER))
        self.offline.add(KNOWN)
        result = shelly.integration_devices()
        self.assertTrue(result["devices"])
        self.assertEqual(result["devices"][0]["details"]["root_url"], OTHER)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(shelly._cached_roots(), [KNOWN, OTHER])
        self.scan.assert_not_called()

    def test_discover_scans_once_and_preserves_offline_hosts_and_settings(self):
        self.known((KNOWN,))
        self.offline.add(KNOWN)
        self.settings["SHELLY_DEVICE_HOSTS"] = KNOWN
        self.settings["SHELLY_DEVICE_ALIASES"] = "shellyplus1pm-a = Existing name"
        self.settings["SHELLY_DEVICE_ROOMS"] = "shellyplus1pm-a = Office"
        self.scan.side_effect = None
        self.scan.return_value = [NEW]
        result = shelly.run_integration_action("discover", {})
        self.assertTrue(result["ok"])
        self.scan.assert_called_once()
        self.assertEqual(result["added_hosts"], [NEW])
        self.assertEqual(shelly._split_manual_hosts(self.settings["SHELLY_DEVICE_HOSTS"]), [KNOWN, NEW])
        self.assertEqual(shelly._cached_roots(), [KNOWN, NEW])
        self.assertIn("shellyplus1pm-a = Existing name", self.settings["SHELLY_DEVICE_ALIASES"])
        self.assertIn("shellyplus1pm-a = Office", self.settings["SHELLY_DEVICE_ROOMS"])
        self.assertIn("shellyplus1pm-c =", self.settings["SHELLY_DEVICE_ALIASES"])
        self.assertIn("shellyplus1pm-c =", self.settings["SHELLY_DEVICE_ROOMS"])
        shelly.integration_devices()
        self.scan.assert_called_once()

    def test_empty_manual_discovery_keeps_previous_hosts(self):
        self.known()
        self.scan.side_effect = None
        self.scan.return_value = []
        self.assertEqual(shelly.discover_shelly_roots(force=True), [KNOWN])
        self.assertEqual(shelly._cached_roots(), [KNOWN])
        self.scan.assert_called_once()

    def test_test_action_only_contacts_known_hosts(self):
        self.known()
        result = shelly.run_integration_action("test", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["root_url"], KNOWN)
        self.assertEqual({row[1] for row in self.http_calls}, {KNOWN})
        self.scan.assert_not_called()

    def test_test_without_hosts_requires_discover_instead_of_scanning(self):
        with self.assertRaisesRegex(RuntimeError, "Discover Shelly Devices"):
            shelly.run_integration_action("test", {})
        self.assertEqual(self.http_calls, [])
        self.scan.assert_not_called()

    def test_missing_action_target_does_not_expand_to_a_network_scan(self):
        self.known()
        with self.assertRaisesRegex(ValueError, "Discover Shelly Devices"):
            shelly._find_root_for_target("missing-device", {}, shelly.read_shelly_settings())
        self.assertEqual([row[1] for row in self.http_calls], [KNOWN])
        self.scan.assert_not_called()

    def test_offline_action_target_never_triggers_discovery(self):
        self.known()
        self.offline.add(KNOWN)
        with self.assertRaisesRegex(ValueError, "Discover Shelly Devices"):
            shelly._find_root_for_target("shellyplus1pm-a", {}, shelly.read_shelly_settings())
        self.assertEqual(shelly._cached_roots(), [KNOWN])
        self.scan.assert_not_called()

    def test_gen1_and_gen2_switch_control_keep_working_without_discovery(self):
        self.known()
        for gen, path in ((1, "/relay/0"), (2, "/rpc/Switch.Set")):
            with self.subTest(gen=gen):
                self.gen = gen
                result = shelly.run_integration_device_action("turn_off", "shelly:shellyplus1pm-a:switch:0", {})
                self.assertTrue(result["ok"])
                self.assertEqual(result["root_url"], KNOWN)
                self.assertEqual(self.http_calls[-1][2], path)
        self.assertEqual({row[1] for row in self.http_calls}, {KNOWN})
        self.scan.assert_not_called()

    def test_alias_room_and_credentials_edits_preserve_discovered_hosts(self):
        self.known()
        previous = self.store.get(shelly.SHELLY_DISCOVERY_CACHE_KEY)
        shelly.save_shelly_settings(
            device_aliases="shellyplus1pm-a = Desk plug",
            device_rooms="shellyplus1pm-a = Office",
            username="admin", password="test-password", timeout_seconds=8,
        )
        self.assertEqual(self.store.get(shelly.SHELLY_DISCOVERY_CACHE_KEY), previous)
        self.assertEqual(shelly.discover_shelly_roots(), [KNOWN])

    def test_host_edits_forget_removed_hosts_without_triggering_discovery(self):
        self.known((KNOWN, OTHER))
        self.settings["SHELLY_DEVICE_HOSTS"] = f"{KNOWN}\n{OTHER}"
        shelly.save_shelly_settings(device_hosts=OTHER)
        self.assertEqual(shelly.discover_shelly_roots(), [OTHER])
        shelly.save_shelly_settings(device_hosts="")
        self.assertEqual(shelly.discover_shelly_roots(), [])
        self.assertEqual(self.http_calls, [])
        self.scan.assert_not_called()

    def test_reordered_host_list_does_not_forget_other_discovered_hosts(self):
        self.known((NEW,))
        self.settings["SHELLY_DEVICE_HOSTS"] = f"{KNOWN}\n{OTHER}"
        shelly.save_shelly_settings(device_hosts=f"{OTHER}/\n{KNOWN}/")
        self.assertEqual(shelly.discover_shelly_roots(), [OTHER, KNOWN, NEW])

    def test_disabled_integration_is_quiet_and_reenabling_keeps_known_hosts(self):
        self.known()
        shelly.save_shelly_settings(enabled=False)
        self.assertEqual(shelly.discover_shelly_roots(force=True), [])
        self.assertFalse(shelly.integration_status()["enabled"])
        self.assertEqual(shelly.integration_devices()["devices"], [])
        self.assertEqual(shelly._cached_roots(), [KNOWN])
        shelly.save_shelly_settings(enabled=True)
        self.assertEqual(shelly.discover_shelly_roots(), [KNOWN])
        self.scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
