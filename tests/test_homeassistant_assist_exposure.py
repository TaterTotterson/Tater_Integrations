from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


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


class FakeRedis:
    def __init__(self, values: dict | None = None) -> None:
        self.values = dict(values or {})

    def hgetall(self, _key: str) -> dict:
        return dict(self.values)

    def hset(self, _key: str, mapping: dict) -> None:
        self.values.update(mapping)


class HomeAssistantAssistExposureTests(unittest.TestCase):
    def test_existing_config_defaults_to_all_entities(self) -> None:
        client = FakeRedis({"HA_BASE_URL": "http://ha:8123", "HA_TOKEN": "secret"})

        config = homeassistant.load_homeassistant_config(client=client)

        self.assertEqual(config["entity_scope"], "all")
        self.assertTrue(
            homeassistant.homeassistant_entity_in_scope(
                "sensor.private_diagnostic",
                {},
                config=config,
            )
        )

    def test_scope_is_saved_without_losing_existing_connection(self) -> None:
        client = FakeRedis({"HA_BASE_URL": "http://ha:8123", "HA_TOKEN": "secret"})

        saved = homeassistant.save_homeassistant_config(entity_scope="assist", client=client)

        self.assertEqual(saved["base"], "http://ha:8123")
        self.assertEqual(saved["token"], "secret")
        self.assertEqual(saved["entity_scope"], "assist")

    def test_scope_change_clears_old_home_assistant_runtime_states(self) -> None:
        class RuntimeRedis:
            def __init__(self) -> None:
                self.hashes = {
                    homeassistant.HOMEASSISTANT_SETTINGS_KEY: {
                        "HA_BASE_URL": "http://ha:8123",
                        "HA_TOKEN": "secret",
                        "HA_ENTITY_SCOPE": "all",
                    },
                    homeassistant.HOMEASSISTANT_RUNTIME_STATES_KEY: {
                        "homeassistant:light.kitchen": "{}",
                        "hue:light.other": "{}",
                    },
                }

            def hgetall(self, key: str) -> dict:
                return dict(self.hashes.get(key) or {})

            def hset(self, key: str, mapping: dict) -> None:
                self.hashes.setdefault(key, {}).update(mapping)

            def hdel(self, key: str, field: str) -> int:
                if field not in self.hashes.get(key, {}):
                    return 0
                del self.hashes[key][field]
                return 1

        client = RuntimeRedis()

        homeassistant.save_homeassistant_config(entity_scope="assist", client=client)

        states = client.hashes[homeassistant.HOMEASSISTANT_RUNTIME_STATES_KEY]
        self.assertNotIn("homeassistant:light.kitchen", states)
        self.assertIn("hue:light.other", states)

    def test_explicit_exposure_overrides_default_rules(self) -> None:
        config = {"entity_scope": "assist"}
        policy = {
            "expose_new": False,
            "explicitly_exposed": {"sensor.custom_value"},
            "explicitly_unexposed": {"light.private_lamp"},
            "registry_entities": {},
        }

        self.assertTrue(
            homeassistant.homeassistant_entity_in_scope(
                "sensor.custom_value", {}, config=config, assist_policy=policy
            )
        )
        self.assertFalse(
            homeassistant.homeassistant_entity_in_scope(
                "light.private_lamp", {}, config=config, assist_policy=policy
            )
        )

    def test_default_exposure_matches_home_assistant_domains_and_device_classes(self) -> None:
        config = {"entity_scope": "assist"}
        policy = {
            "expose_new": True,
            "explicitly_exposed": set(),
            "explicitly_unexposed": set(),
            "registry_entities": {},
        }

        self.assertTrue(
            homeassistant.homeassistant_entity_in_scope(
                "light.kitchen", {}, config=config, assist_policy=policy
            )
        )
        self.assertTrue(
            homeassistant.homeassistant_entity_in_scope(
                "binary_sensor.hall_motion",
                {"device_class": "motion"},
                config=config,
                assist_policy=policy,
            )
        )
        self.assertFalse(
            homeassistant.homeassistant_entity_in_scope(
                "sensor.router_packets",
                {"device_class": "data_size"},
                config=config,
                assist_policy=policy,
            )
        )

    def test_hidden_or_diagnostic_entities_are_not_default_exposed(self) -> None:
        config = {"entity_scope": "assist"}
        policy = {
            "expose_new": True,
            "explicitly_exposed": set(),
            "explicitly_unexposed": set(),
            "registry_entities": {
                "switch.hidden": {"hidden_by": "integration"},
                "sensor.diagnostic_temperature": {
                    "entity_category": "diagnostic",
                    "original_device_class": "temperature",
                },
            },
        }

        self.assertFalse(
            homeassistant.homeassistant_entity_in_scope(
                "switch.hidden", {}, config=config, assist_policy=policy
            )
        )
        self.assertFalse(
            homeassistant.homeassistant_entity_in_scope(
                "sensor.diagnostic_temperature", {}, config=config, assist_policy=policy
            )
        )

    def test_policy_combines_exposure_endpoints_and_registry_overrides(self) -> None:
        registry = [
            {
                "entity_id": "light.private_lamp",
                "options": {"conversation": {"should_expose": False}},
            }
        ]
        with (
            patch.object(
                homeassistant,
                "exposed_entity_list",
                new=AsyncMock(
                    return_value={
                        "exposed_entities": {
                            "sensor.custom_value": {"conversation": True},
                            "light.alexa_only": {"cloud.alexa": True},
                        }
                    }
                ),
            ),
            patch.object(
                homeassistant,
                "expose_new_entities_get",
                new=AsyncMock(return_value={"expose_new": True}),
            ),
        ):
            policy = asyncio.run(
                homeassistant.assist_exposure_policy(
                    "http://ha:8123",
                    "secret",
                    registry_entities=registry,
                )
            )

        self.assertEqual(policy["explicitly_exposed"], {"sensor.custom_value"})
        self.assertEqual(policy["explicitly_unexposed"], {"light.private_lamp"})
        self.assertTrue(policy["expose_new"])

    def test_device_discovery_returns_only_entities_in_assist_scope(self) -> None:
        states = [
            {
                "entity_id": "light.kitchen",
                "state": "on",
                "attributes": {"friendly_name": "Kitchen"},
            },
            {
                "entity_id": "sensor.router_packets",
                "state": "100",
                "attributes": {"friendly_name": "Router Packets", "device_class": "data_size"},
            },
        ]
        policy = {
            "expose_new": True,
            "explicitly_exposed": set(),
            "explicitly_unexposed": set(),
            "registry_entities": {},
        }
        with (
            patch.object(
                homeassistant,
                "load_homeassistant_config",
                return_value={"base": "http://ha:8123", "token": "secret", "entity_scope": "assist"},
            ),
            patch.object(homeassistant, "_homeassistant_rest_get", return_value=states),
            patch.object(
                homeassistant,
                "_homeassistant_registry_context",
                return_value={"entities": {}, "devices": {}, "areas": {}},
            ),
            patch.object(homeassistant, "assist_exposure_policy_sync", return_value=policy),
        ):
            result = homeassistant.integration_devices()

        self.assertEqual([row["id"] for row in result["devices"]], ["light.kitchen"])
        self.assertIn("1 of 2", result["message"])

    def test_device_discovery_fails_closed_when_assist_policy_is_unavailable(self) -> None:
        with (
            patch.object(
                homeassistant,
                "load_homeassistant_config",
                return_value={"base": "http://ha:8123", "token": "secret", "entity_scope": "assist"},
            ),
            patch.object(homeassistant, "_homeassistant_rest_get", return_value=[]),
            patch.object(
                homeassistant,
                "_homeassistant_registry_context",
                return_value={"entities": {}, "devices": {}, "areas": {}},
            ),
            patch.object(
                homeassistant,
                "assist_exposure_policy_sync",
                side_effect=RuntimeError("Unauthorized"),
            ),
        ):
            result = homeassistant.integration_devices()

        self.assertEqual(result["devices"], [])
        self.assertIn("administrator token", result["error"])

    def test_runtime_filter_allows_everything_in_all_mode(self) -> None:
        with (
            patch.object(
                homeassistant,
                "load_homeassistant_config",
                return_value={"base": "http://ha:8123", "token": "secret", "entity_scope": "all"},
            ),
            patch.object(
                homeassistant,
                "_cached_assist_exposure_policy",
                new=AsyncMock(),
            ) as policy_loader,
        ):
            allowed = asyncio.run(
                homeassistant.integration_runtime_event_allowed(
                    kind="state_changed",
                    payload={"entity_id": "sensor.private"},
                )
            )

        self.assertTrue(allowed)
        policy_loader.assert_not_awaited()

    def test_runtime_filter_uses_same_assist_policy_as_device_discovery(self) -> None:
        policy = {
            "expose_new": False,
            "explicitly_exposed": {"light.kitchen"},
            "explicitly_unexposed": set(),
            "registry_entities": {},
        }
        with (
            patch.object(
                homeassistant,
                "load_homeassistant_config",
                return_value={"base": "http://ha:8123", "token": "secret", "entity_scope": "assist"},
            ),
            patch.object(
                homeassistant,
                "_cached_assist_exposure_policy",
                new=AsyncMock(return_value=policy),
            ),
        ):
            visible = asyncio.run(
                homeassistant.integration_runtime_event_allowed(
                    kind="state_changed",
                    payload={
                        "entity_id": "light.kitchen",
                        "new_state": {"attributes": {}},
                    },
                )
            )
            hidden = asyncio.run(
                homeassistant.integration_runtime_event_allowed(
                    kind="state_changed",
                    payload={
                        "entity_id": "sensor.private",
                        "new_state": {"attributes": {}},
                    },
                )
            )

        self.assertTrue(visible)
        self.assertFalse(hidden)


if __name__ == "__main__":
    unittest.main()
