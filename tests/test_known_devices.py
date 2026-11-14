import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import madhat


class KnownDevicesTests(unittest.IsolatedAsyncioTestCase):
    async def test_connected_node_name_is_saved(self):
        address = "AA:BB:CC:DD:EE:FF"
        config = json.loads(json.dumps(madhat.app_config))
        config["known_devices"] = []
        config["known_device_names"] = {}
        state = dict(madhat.app_state, known_devices=[], connection_type="bluetooth",
                     ble_mac=address, ble_pin="", is_connected=False)
        instance = MagicMock()
        instance.self_info = {"name": "Kitchen Node"}
        instance.commands.get_msg = AsyncMock()
        instance.start_auto_message_fetching = AsyncMock()

        @asynccontextmanager
        async def pairing_agent(pin):
            yield

        with (
            patch.object(madhat, "app_config", config),
            patch.object(madhat, "app_state", state),
            patch.object(madhat, "meshcore_instance", None),
            patch.object(madhat, "_disconnect_hardware_locked", new_callable=AsyncMock),
            patch.object(madhat.MeshCore, "create_ble", new=AsyncMock(return_value=instance)),
            patch.object(madhat, "ble_pairing_agent", pairing_agent),
            patch.object(madhat, "refresh_device_limits", new_callable=AsyncMock),
            patch.object(madhat, "refresh_contacts", new_callable=AsyncMock),
            patch.object(madhat, "refresh_channels", new_callable=AsyncMock),
            patch.object(madhat, "write_app_config") as write_config,
            patch.object(madhat, "log_to_dash"),
        ):
            await madhat.connect_hardware()
            self.assertEqual(config["known_devices"], [address])
            self.assertEqual(config["known_device_names"], {address: "Kitchen Node"})
            write_config.assert_called()

    async def test_scan_remembers_name_and_existing_config_survives_reload(self):
        config = json.loads(json.dumps(madhat.app_config))
        config["known_devices"] = ["AA:BB:CC:DD:EE:FF"]
        config["known_device_names"] = {}
        state = dict(madhat.app_state, known_devices=list(config["known_devices"]))
        device = SimpleNamespace(address="aa:bb:cc:dd:ee:ff", name=None)
        advertisement = SimpleNamespace(local_name="MeshCore Kitchen")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with (
                patch.object(madhat, "CONFIG_FILE_PATH", path),
                patch.object(madhat, "app_config", config),
                patch.object(madhat, "app_state", state),
                patch.object(
                    madhat.BleakScanner, "discover",
                    new=AsyncMock(return_value={"device": (device, advertisement)}),
                ),
            ):
                response = await madhat.bluetooth_scan_handler(None)
                self.assertEqual(json.loads(response.text)["devices"][0]["name"], "MeshCore Kitchen")
                loaded = madhat.load_app_config()
                self.assertEqual(loaded["known_devices"], config["known_devices"])
                self.assertEqual(loaded["known_device_names"], {
                    "AA:BB:CC:DD:EE:FF": "MeshCore Kitchen",
                })
                validated = madhat.validate_app_config({
                    **config, "known_devices": [], "known_device_names": {},
                })
                self.assertEqual(validated["known_devices"], config["known_devices"])
                self.assertEqual(validated["known_device_names"], config["known_device_names"])

    async def test_node_name_replaces_scan_name_and_forget_removes_it(self):
        address = "AA:BB:CC:DD:EE:FF"
        config = json.loads(json.dumps(madhat.app_config))
        config["known_devices"] = [address]
        config["known_device_names"] = {address: "MeshCore"}
        state = dict(madhat.app_state, known_devices=[address], ble_mac=address, ble_pin="1234")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with (
                patch.object(madhat, "CONFIG_FILE_PATH", path),
                patch.object(madhat, "app_config", config),
                patch.object(madhat, "app_state", state),
                patch.object(madhat, "log_to_dash"),
            ):
                madhat.remember_known_device_name(address, "Kitchen Node")
                self.assertEqual(json.loads(path.read_text())["known_device_names"][address], "Kitchen Node")
                request = SimpleNamespace(json=AsyncMock(return_value={"device_mac": address}))
                response = await madhat.forget_device_handler(request)
                self.assertEqual(response.status, 200)
                saved = json.loads(path.read_text())
                self.assertEqual(saved["known_devices"], [])
                self.assertEqual(saved["known_device_names"], {})
                self.assertEqual(saved["connection"]["ble_mac"], "")
