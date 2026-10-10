from __future__ import annotations

import json
import socket
import sys
import threading
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

from integrations import dyson


class FakeRedis:
    def __init__(self) -> None:
        self.hashes = {}

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)


def configured_settings(**overrides):
    settings = {
        "DYSON_ENABLED": True,
        "DYSON_SERIAL_NUMBER": "VS6-US-TEST1234",
        "DYSON_HOST": "192.0.2.42",
        "DYSON_CREDENTIAL": "local-device-password",
        "DYSON_MQTT_PREFIX": "475",
        "DYSON_NAME": "Office Dyson",
        "DYSON_ROOM": "Office",
        "DYSON_TIMEOUT_SECONDS": "8",
    }
    settings.update(overrides)
    return settings


class FakeDysonClient:
    def __init__(self, product_state=None):
        self.product_state = dict(product_state or {"fpwr": "OFF"})
        self.commands = []

    def snapshot(self):
        return {
            "online": True,
            "product_state": dict(self.product_state),
            "environment": {},
            "faults": {},
            "last_seen": 1.0,
            "host": "192.0.2.42",
            "port": 1883,
        }

    def request_snapshot(self, **_kwargs):
        return self.snapshot()

    def send_state(self, command):
        self.commands.append(dict(command))
        self.product_state.update(command)


class DysonSettingsTests(unittest.TestCase):
    def tearDown(self) -> None:
        dyson._shutdown_clients()

    def test_settings_round_trip(self) -> None:
        store = FakeRedis()

        result = dyson.save_dyson_settings(
            enabled=True,
            serial_number="VS6-US-TEST1234",
            host="192.0.2.42",
            credential="secret",
            mqtt_prefix="475",
            name="Office Dyson",
            room="Office",
            additional_devices=(
                "HP07-US-SECOND01 | 192.0.2.43 | 527 | "
                "Bedroom Dyson | Bedroom"
            ),
            additional_credentials="HP07-US-SECOND01=second-secret",
            timeout_seconds=99,
            client=store,
        )

        self.assertEqual(result["DYSON_SERIAL_NUMBER"], "VS6-US-TEST1234")
        self.assertEqual(result["DYSON_CREDENTIAL"], "secret")
        self.assertIn("HP07-US-SECOND01", result["DYSON_ADDITIONAL_DEVICES"])
        self.assertIn("second-secret", result["DYSON_ADDITIONAL_CREDENTIALS"])
        self.assertEqual(result["DYSON_TIMEOUT_SECONDS"], "30")

    def test_configuration_requires_local_device_credentials(self) -> None:
        error = dyson._configuration_error(configured_settings(DYSON_CREDENTIAL=""))

        self.assertIn("device password", error)


class DysonMqttProtocolTests(unittest.TestCase):
    def tearDown(self) -> None:
        dyson._shutdown_clients()

    def test_remaining_length_uses_mqtt_variable_encoding(self) -> None:
        self.assertEqual(dyson._mqtt_remaining_length(0), b"\x00")
        self.assertEqual(dyson._mqtt_remaining_length(127), b"\x7f")
        self.assertEqual(dyson._mqtt_remaining_length(128), b"\x80\x01")
        self.assertEqual(dyson._mqtt_remaining_length(16384), b"\x80\x80\x01")

    def test_state_change_and_environment_messages_are_normalized(self) -> None:
        client = dyson._DysonMQTTClient(configured_settings())

        client._apply_message(
            {
                "msg": "STATE-CHANGE",
                "product-state": {
                    "fpwr": ["OFF", "ON"],
                    "fnsp": ["0001", "0007"],
                },
            }
        )
        client._apply_message(
            {
                "msg": "ENVIRONMENTAL-CURRENT-SENSOR-DATA",
                "data": {"tact": "2942", "hact": "0044", "p25r": "0008"},
            }
        )

        snapshot = client.snapshot()
        self.assertEqual(snapshot["product_state"]["fpwr"], "ON")
        self.assertEqual(snapshot["product_state"]["fnsp"], "0007")
        self.assertEqual(snapshot["environment"]["p25r"], "0008")

    def test_client_connects_and_reads_state_from_a_local_broker(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        port = listener.getsockname()[1]
        broker_errors = []

        def broker() -> None:
            connection = None
            try:
                connection, _address = listener.accept()
                connection.settimeout(3)
                first_byte, _body = dyson._recv_mqtt_packet(connection)
                self.assertEqual(first_byte >> 4, 1)
                connection.sendall(dyson._mqtt_packet(0x20, b"\x00\x00"))

                first_byte, body = dyson._recv_mqtt_packet(connection)
                self.assertEqual(first_byte, 0x82)
                packet_id = body[:2]
                connection.sendall(dyson._mqtt_packet(0x90, packet_id + b"\x00"))

                sent_state = False
                while True:
                    first_byte, body = dyson._recv_mqtt_packet(connection)
                    if first_byte >> 4 == 14:
                        return
                    if first_byte >> 4 != 3:
                        continue
                    topic_length = int.from_bytes(body[:2], "big")
                    payload = json.loads(body[2 + topic_length :].decode("utf-8"))
                    if sent_state or payload.get("msg") != "REQUEST-CURRENT-STATE":
                        continue
                    response = json.dumps(
                        {
                            "msg": "CURRENT-STATE",
                            "product-state": {"fpwr": "ON", "fnsp": "0005"},
                        }
                    ).encode("utf-8")
                    connection.sendall(
                        dyson._mqtt_packet(
                            0x30,
                            dyson._mqtt_utf8("475/VS6-US-TEST1234/status/current") + response,
                        )
                    )
                    sent_state = True
            except Exception as exc:  # pragma: no cover - surfaced by the assertion below
                broker_errors.append(exc)
            finally:
                if connection is not None:
                    connection.close()
                listener.close()

        thread = threading.Thread(target=broker, daemon=True)
        thread.start()
        client = dyson._DysonMQTTClient(
            configured_settings(DYSON_HOST=f"127.0.0.1:{port}", DYSON_TIMEOUT_SECONDS="2")
        )
        try:
            snapshot = client.request_snapshot(timeout=2, refresh=True)
        finally:
            client.close()
            thread.join(timeout=3)

        self.assertEqual(broker_errors, [])
        self.assertEqual(snapshot["product_state"]["fpwr"], "ON")
        self.assertEqual(snapshot["product_state"]["fnsp"], "0005")


class DysonDeviceMappingTests(unittest.TestCase):
    def tearDown(self) -> None:
        dyson._shutdown_clients()

    def test_inventory_exposes_fan_and_environmental_sensors(self) -> None:
        snapshot = {
            "online": True,
            "host": "192.0.2.42",
            "port": 1883,
            "last_seen": 100.0,
            "product_state": {
                "fpwr": "ON",
                "fnsp": "0007",
                "auto": "OFF",
                "nmod": "ON",
                "oson": "ON",
                "hflr": "0087",
                "cflr": "0065",
            },
            "environment": {
                "tact": "2942",
                "hact": "0044",
                "p25r": "0008",
                "p10r": "0011",
                "va10": "0123",
                "noxl": "0004",
                "co2r": "0560",
                "hchr": "0025",
            },
            "faults": {},
            "last_error": "",
        }

        devices = dyson._build_devices(configured_settings(), snapshot)
        by_id = {item["id"].rsplit(":", 1)[-1]: item for item in devices}

        self.assertEqual(by_id["fan"]["state"], "on")
        self.assertEqual(by_id["fan"]["details"]["percentage"], 70)
        self.assertTrue(by_id["fan"]["details"]["night_mode"])
        self.assertAlmostEqual(by_id["temperature"]["state"], 21.05)
        self.assertEqual(by_id["humidity"]["state"], 44)
        self.assertEqual(by_id["pm25"]["state"], 8)
        self.assertEqual(by_id["hepa_filter_life"]["state"], 87)
        self.assertEqual(by_id["carbon_filter_life"]["state"], 65)

    def test_percentage_action_maps_to_dyson_speed_levels(self) -> None:
        fake_client = FakeDysonClient({"fpwr": "OFF", "fnsp": "0001"})
        with (
            patch.object(dyson, "read_dyson_settings", return_value=configured_settings()),
            patch.object(dyson, "_client_for", return_value=fake_client),
        ):
            result = dyson.run_integration_device_action(
                "set_percentage",
                "dyson:vs6_us_test1234:fan",
                {"percentage": 61},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(fake_client.commands, [{"fpwr": "ON", "fnsp": "0007"}])

    def test_inventory_supports_multiple_configured_devices(self) -> None:
        settings = configured_settings(
            DYSON_ADDITIONAL_DEVICES=(
                "HP07-US-SECOND01 | 192.0.2.43 | 527 | "
                "Bedroom Dyson | Bedroom"
            ),
            DYSON_ADDITIONAL_CREDENTIALS="HP07-US-SECOND01=second-secret",
        )
        clients = {
            "VS6-US-TEST1234": FakeDysonClient({"fpwr": "ON", "fnsp": "0004"}),
            "HP07-US-SECOND01": FakeDysonClient({"fpwr": "OFF", "fnsp": "0002"}),
        }

        with (
            patch.object(dyson, "read_dyson_settings", return_value=settings),
            patch.object(
                dyson,
                "_client_for",
                side_effect=lambda item: clients[item["DYSON_SERIAL_NUMBER"]],
            ),
        ):
            result = dyson.integration_devices()

        fans = [item for item in result["devices"] if item["type"] == "fan"]
        self.assertEqual(len(fans), 2)
        self.assertEqual(
            {item["name"] for item in fans},
            {"Office Dyson", "Bedroom Dyson"},
        )
        self.assertEqual(result.get("warnings"), None)

    def test_action_routes_to_the_selected_additional_device(self) -> None:
        settings = configured_settings(
            DYSON_ADDITIONAL_DEVICES=(
                "HP07-US-SECOND01 | 192.0.2.43 | 527 | "
                "Bedroom Dyson | Bedroom"
            ),
            DYSON_ADDITIONAL_CREDENTIALS="HP07-US-SECOND01=second-secret",
        )
        primary = FakeDysonClient({"fpwr": "OFF"})
        secondary = FakeDysonClient({"fpwr": "OFF"})
        clients = {
            "VS6-US-TEST1234": primary,
            "HP07-US-SECOND01": secondary,
        }
        with (
            patch.object(dyson, "read_dyson_settings", return_value=settings),
            patch.object(
                dyson,
                "_client_for",
                side_effect=lambda item: clients[item["DYSON_SERIAL_NUMBER"]],
            ),
        ):
            result = dyson.run_integration_device_action(
                "turn_on", "dyson:hp07_us_second01:fan", {}
            )

        self.assertTrue(result["ok"])
        self.assertEqual(primary.commands, [])
        self.assertEqual(secondary.commands, [{"fpwr": "ON"}])

    def test_legacy_link_models_use_fmod_for_power_and_auto(self) -> None:
        fake_client = FakeDysonClient({"fmod": "OFF", "fnsp": "0001"})
        with (
            patch.object(dyson, "read_dyson_settings", return_value=configured_settings()),
            patch.object(dyson, "_client_for", return_value=fake_client),
        ):
            dyson.run_integration_device_action(
                "turn_on", "dyson:vs6_us_test1234:fan", {}
            )
            dyson.run_integration_device_action(
                "set_auto_mode", "dyson:vs6_us_test1234:fan", {"enabled": True}
            )

        self.assertEqual(fake_client.commands, [{"fmod": "FAN"}, {"fmod": "AUTO"}])

    def test_runtime_poll_emits_only_changed_state_events(self) -> None:
        devices = [
            {
                "id": "dyson:test:fan",
                "ref": "dyson:test:fan",
                "name": "Test Fan",
                "state": "on",
                "room": "Office",
                "details": {"percentage": 50},
            }
        ]
        cursor = {"states": {"dyson:test:fan": "off"}}
        with (
            patch.object(dyson, "read_dyson_settings", return_value=configured_settings()),
            patch.object(dyson, "_inventory", return_value=(devices, [])),
        ):
            result = dyson.integration_poll_events(cursor=cursor)

        self.assertEqual(len(result["states"]), 1)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["previous_state"], "off")
        self.assertEqual(result["events"][0]["state"], "on")


if __name__ == "__main__":
    unittest.main()
