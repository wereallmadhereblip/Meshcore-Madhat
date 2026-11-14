import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import madhat


class ConnectionInitializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_contacts_do_not_delay_channels_or_start_duplicate_refresh(self):
        contact_started = asyncio.Event()
        release_contacts = asyncio.Event()
        channel_refreshes = []

        async def stalled_contacts():
            contact_started.set()
            await release_contacts.wait()

        async def refresh_channels(saved_only=False):
            channel_refreshes.append(saved_only)

        instance = MagicMock()
        instance.commands.get_msg = AsyncMock()
        instance.start_auto_message_fetching = AsyncMock()
        instance.is_connected = True
        state = dict(madhat.app_state, connection_type="serial", serial_port="/dev/test", is_connected=False)

        with (
            patch.object(madhat, "app_state", state),
            patch.object(madhat, "meshcore_instance", None),
            patch.object(madhat, "_disconnect_hardware_locked", new_callable=AsyncMock),
            patch.object(madhat.MeshCore, "create_serial", new=AsyncMock(return_value=instance)),
            patch.object(madhat, "refresh_device_limits", new_callable=AsyncMock),
            patch.object(madhat, "refresh_contacts", side_effect=stalled_contacts) as contacts,
            patch.object(madhat, "refresh_channels", side_effect=refresh_channels),
            patch.object(madhat, "log_to_dash"),
        ):
            connection = asyncio.create_task(madhat.connect_hardware())
            telemetry = None
            try:
                await asyncio.wait_for(contact_started.wait(), 1)
                self.assertEqual(channel_refreshes, [False])
                telemetry = asyncio.create_task(madhat.telemetry_loop())
                await asyncio.sleep(0.05)
                self.assertEqual(contacts.call_count, 1)
            finally:
                release_contacts.set()
                await asyncio.wait_for(connection, 1)
                if telemetry:
                    telemetry.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await telemetry

    async def test_unavailable_channel_scan_is_reported(self):
        instance = MagicMock()
        instance.commands.get_channel = AsyncMock(side_effect=asyncio.TimeoutError)
        state = dict(madhat.app_state, is_connected=True, channels={}, limits={"max_channels": 2})

        with (
            patch.object(madhat, "app_state", state),
            patch.object(madhat, "meshcore_instance", instance),
            patch.object(madhat, "HARDWARE_COMMAND_MIN_INTERVAL", 0),
            patch.object(madhat, "log_to_dash") as log,
        ):
            await madhat.refresh_channels()
            log.assert_called_once_with(
                "Could not read any channels: all channel requests timed out or failed."
            )
            instance.commands.get_channel = AsyncMock(
                return_value=SimpleNamespace(type=madhat.EventType.CHANNEL_INFO, payload={})
            )
            log.reset_mock()
            await madhat.refresh_channels()
            log.assert_not_called()
