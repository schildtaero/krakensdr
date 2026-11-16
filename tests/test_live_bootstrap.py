"""Turnkey live startup tests without USB devices, setup or external sockets.

ManagedDAQ and the IQ client are mocked for lifecycle tests. A separate API test
uses a genuinely absent runtime to prove that the UI remains available and never
falls back to generated emitters when local acquisition cannot start.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch, sentinel

from fastapi.testclient import TestClient

from krakendf.app import create_app
from krakendf.hardware import HardwareError
from krakendf.service import DemoService


@contextmanager
def isolated_environment(runtime: Path, **extra):
    values = {"KRAKENDF_RUNTIME": str(runtime), "KRAKENDF_DAQ_MODE": "managed", **extra}
    with patch.dict(os.environ, values):
        for key in ("KRAKENDF_SOURCE", "KRAKENDF_DAQ_HOST", "KRAKENDF_DAQ_PORT"):
            if key not in extra:
                os.environ.pop(key, None)
        yield


class FakeDAQ:
    def __init__(self, order):
        self.order = order
        self.running = False
        self.start_error = None
        self.start_gate = None
        self.start_entered = asyncio.Event()
        self.start = AsyncMock(side_effect=self._start)
        self.stop = AsyncMock(side_effect=self._stop)

    async def _start(self, config):
        self.order.append("daq.start")
        self.start_entered.set()
        if self.start_gate is not None:
            await self.start_gate.wait()
        if self.start_error:
            raise self.start_error
        self.running = True
        return self.status()

    async def _stop(self):
        self.order.append("daq.stop")
        self.running = False

    def status(self):
        return {"managed": True, "installed": True, "running": self.running,
                "state": "running" if self.running else "stopped", "message": "Mock runtime",
                "processes": {}, "hardware_validated": False}


class FakeClient:
    def __init__(self, order, host, port, connect_error=None):
        self.order, self.host, self.port = order, host, port
        self.connected = False
        self.connect_error = connect_error
        self.read_started = asyncio.Event()
        self.connect = AsyncMock(side_effect=self._connect)
        self.configure = AsyncMock(side_effect=self._configure)
        self.read_frame = AsyncMock(side_effect=self._read)
        self.close = AsyncMock(side_effect=self._close)

    async def _connect(self):
        self.order.append("client.connect")
        if self.connect_error:
            raise self.connect_error
        self.connected = True

    async def _configure(self, **configuration):
        self.order.append("client.configure")
        return {"acknowledged": True, "requires_frame_verification": True}

    async def _read(self):
        self.read_started.set()
        await asyncio.Event().wait()

    async def _close(self):
        self.order.append("client.close")
        self.connected = False


class LiveLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.environment = isolated_environment(self.directory / "runtime")
        self.environment.__enter__()
        self.order, self.clients = [], []
        self.daq = FakeDAQ(self.order)
        self.connect_error = None

        def client_factory(host, port, **kwargs):
            result = FakeClient(self.order, host, port, self.connect_error)
            result.timeout = kwargs.get("timeout")
            self.clients.append(result)
            return result

        self.manager_patch = patch("krakendf.service.ManagedDAQ", return_value=self.daq)
        self.manager_factory = self.manager_patch.start()
        self.client_patch = patch("krakendf.service.HeimdallClient", side_effect=client_factory)
        self.client_patch.start()
        self.service = DemoService(self.directory / "data")

    async def asyncTearDown(self):
        await self.service.close()
        self.client_patch.stop()
        self.manager_patch.stop()
        self.environment.__exit__(None, None, None)
        self.temp.cleanup()

    async def wait_for(self, condition, timeout=2):
        async def poll():
            while not condition():
                await asyncio.sleep(.002)
        await asyncio.wait_for(poll(), timeout)

    async def live_ready(self, *, managed=True, host=None):
        await self.service.set_source("live", host=host, managed=managed)
        await self.wait_for(lambda: self.clients and self.clients[-1].read_started.is_set())
        return self.clients[-1]

    async def test_managed_start_precedes_connection_and_mode_switch_stops_owned_stack(self):
        client = await self.live_ready()
        self.assertLess(self.order.index("daq.start"), self.order.index("client.connect"))
        self.daq.start.assert_awaited_once()
        self.assertEqual(self.daq.start.await_args.args[0]["sample_rate_hz"], 2400000)
        self.assertTrue(self.service.source["managed"])
        self.assertFalse(self.service.source["simulated"])
        self.assertEqual(self.service.source["mode"], "live")
        self.assertEqual(self.manager_factory.call_args.args[0], self.directory / "runtime")
        stops = self.daq.stop.await_count
        await self.service.set_source("idle")
        self.assertGreater(self.daq.stop.await_count, stops)
        self.assertFalse(self.daq.running)
        client.close.assert_awaited()
        self.assertIsNone(self.service._task)

    async def test_missing_runtime_keeps_live_error_and_never_generates_emitters(self):
        self.daq.start_error = HardwareError("Kraken-Stack fehlt; setup.sh ausführen.")
        await self.service.set_source("live")
        await self.wait_for(lambda: self.service.source["status"] == "error")
        state = self.service.snapshot()
        self.assertEqual(state["source"]["mode"], "live")
        self.assertFalse(state["source"]["simulated"])
        self.assertIn("setup.sh", state["source"]["message"])
        self.assertEqual(state["emitters"], [])
        self.assertEqual(state["stats"]["count"], 0)
        self.assertEqual(state["spectrum"]["power_db"], [])
        self.clients[0].connect.assert_not_awaited()
        self.daq.stop.assert_awaited()

    async def test_connect_failure_after_managed_start_cleans_up(self):
        self.connect_error = HardwareError("IQ-Verbindung fehlgeschlagen")
        await self.service.set_source("live")
        await self.wait_for(lambda: self.service.source["status"] == "error")
        self.assertIn("IQ-Verbindung", self.service.source["message"])
        self.assertFalse(self.daq.running)
        self.clients[0].close.assert_awaited()
        self.assertLess(self.order.index("daq.start"), self.order.index("client.connect"))
        self.assertIn("daq.stop", self.order[self.order.index("client.connect"):])

    async def test_cancel_during_managed_start_stops_without_connecting(self):
        self.daq.start_gate = asyncio.Event()
        await self.service.set_source("live")
        await asyncio.wait_for(self.daq.start_entered.wait(), 1)
        await self.service.set_source("idle")
        self.assertEqual(self.service.source["mode"], "idle")
        self.assertFalse(self.daq.running)
        self.clients[0].connect.assert_not_awaited()
        self.clients[0].close.assert_awaited()
        self.daq.stop.assert_awaited()

    async def test_managed_profile_change_restarts_with_new_rate_and_preserves_mode(self):
        first = await self.live_ready()
        await self.service.update_config({"sample_rate_hz": 2000000, "fft_size": 4096})
        await self.wait_for(lambda: len(self.clients) == 2 and self.clients[-1].read_started.is_set())
        self.assertEqual(self.daq.start.await_count, 2)
        self.assertEqual(self.daq.start.await_args.args[0]["sample_rate_hz"], 2000000)
        self.assertEqual(self.service.config.fft_size, 4096)
        first.close.assert_awaited()
        self.assertTrue(self.service.source["managed"])
        self.assertTrue(self.daq.running)

    async def test_external_stack_is_never_launched_or_owned_and_rate_change_is_rejected(self):
        client = await self.live_ready(managed=False, host="192.0.2.25")
        self.daq.start.assert_not_awaited()
        self.assertEqual((client.host, client.port), ("192.0.2.25", 5000))
        task = self.service._task
        with self.assertRaisesRegex(ValueError, "Abtastrate"):
            await self.service.update_config({"sample_rate_hz": 2000000})
        self.assertIs(self.service._task, task)
        self.assertEqual(self.service.config.sample_rate_hz, 2400000)
        self.assertTrue(client.connected)
        await self.service.update_config({"fft_size": 4096})
        await self.wait_for(lambda: len(self.clients) == 2 and self.clients[-1].read_started.is_set())
        self.daq.start.assert_not_awaited()
        self.assertFalse(self.service.source["managed"])
        self.assertEqual(self.clients[-1].host, "192.0.2.25")
        # Manager.stop only cleans this application's own supervisor; no
        # supervisor was ever started for the external receiver.
        self.assertFalse(self.daq.running)

    async def test_remote_host_disables_managed_unless_explicitly_requested(self):
        await self.service.set_source("live", host="192.0.2.26")
        await self.wait_for(lambda: self.clients and self.clients[-1].read_started.is_set())
        self.assertFalse(self.service.source["managed"])
        self.daq.start.assert_not_awaited()
        task = self.service._task
        with self.assertRaisesRegex(ValueError, "Lokaler USB"):
            await self.service.set_source("live", host="192.0.2.27", managed=True)
        self.assertIs(self.service._task, task)

    @contextmanager
    def serial_ports(self, ports):
        serial = ModuleType("serial")
        serial.__path__ = []
        tools = ModuleType("serial.tools")
        tools.__path__ = []
        listing = ModuleType("serial.tools.list_ports")
        listing.comports = Mock(return_value=ports)
        tools.list_ports = listing
        serial.tools = tools
        with patch.dict(sys.modules, {"serial": serial, "serial.tools": tools, "serial.tools.list_ports": listing}):
            yield listing.comports

    async def test_unique_usb_gps_is_selected_persisted_and_reader_started(self):
        gps = SimpleNamespace(device="/dev/ttyACM-test", vid=0x1546, description="u-blox GPS",
                              manufacturer="u-blox", product="VK-162")
        unrelated = SimpleNamespace(device="/dev/ttyUSB-other", vid=0x1234, description="Console",
                                    manufacturer="Example", product="Serial")
        with self.serial_ports([unrelated, gps]), patch.object(self.service, "_gps_loop", new_callable=AsyncMock) as loop:
            await self.service._start_gps()
            await asyncio.sleep(0)
            self.assertEqual(self.service.config.gps_port, gps.device)
            loop.assert_awaited_once()
        persisted = json.loads(self.service.config_path.read_text())
        self.assertEqual(persisted["gps_port"], gps.device)
        self.assertTrue(any("automatisch erkannt" in event["message"] for event in self.service.events))

    async def test_ambiguous_usb_gps_does_not_choose_arbitrary_device(self):
        ports = [SimpleNamespace(device=f"/dev/ttyACM{i}", vid=0x1546, description="GPS",
                                 manufacturer="u-blox", product="Receiver") for i in range(2)]
        with self.serial_ports(ports), patch.object(self.service, "_gps_loop", new_callable=AsyncMock) as loop:
            await self.service._start_gps()
            loop.assert_not_awaited()
        self.assertEqual(self.service.gps["status"], "ambiguous")
        self.assertEqual(self.service.config.gps_port, "")
        self.assertIsNone(self.service._gps_task)

    async def test_missing_serial_dependency_is_visible_without_reader_task(self):
        with patch.dict(sys.modules, {"serial": None, "serial.tools": None, "serial.tools.list_ports": None}):
            await self.service._start_gps()
        self.assertEqual(self.service.gps["status"], "missing_dependency")
        self.assertFalse(self.service.gps["valid"])
        self.assertIsNone(self.service._gps_task)


class DefaultLiveApplicationTests(unittest.TestCase):
    def test_default_missing_runtime_keeps_http_available_without_simulation_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with isolated_environment(directory / "absent-runtime"), \
                 patch.object(DemoService, "_start_gps", new_callable=AsyncMock), \
                 patch("krakendf.managed_daq.discover_rtl_devices", side_effect=AssertionError("USB enumeration forbidden")) as enumerate_usb, \
                 patch("krakendf.service.HeimdallClient") as client_factory:
                client = client_factory.return_value
                client.connect = AsyncMock()
                client.close = AsyncMock()
                with TestClient(create_app(directory / "data")) as api:
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        state = api.get("/api/state").json()
                        if state["source"]["status"] == "error":
                            break
                        time.sleep(.01)
                    self.assertEqual(state["source"]["mode"], "live")
                    self.assertEqual(state["source"]["status"], "error")
                    self.assertFalse(state["source"]["simulated"])
                    self.assertTrue(state["source"]["managed"])
                    self.assertFalse(state["daq"]["installed"])
                    self.assertIn("setup.sh", state["source"]["message"])
                    self.assertEqual(state["emitters"], [])
                    self.assertEqual(state["stats"]["count"], 0)
                    self.assertEqual(api.get("/api/health").status_code, 200)
                    self.assertEqual(api.get("/api/health").json()["source_mode"], "live")
                    self.assertEqual(api.get("/").status_code, 200)
                    self.assertEqual(api.get("/static/app.js").status_code, 200)
                    self.assertEqual(api.post("/api/record/start", json={"name": "No IQ"}).status_code, 400)
                    client.connect.assert_not_awaited()
                    enumerate_usb.assert_not_called()

    def test_source_endpoint_passes_explicit_external_ownership(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with isolated_environment(directory / "runtime"), patch.object(DemoService, "_start_gps", new_callable=AsyncMock):
                with TestClient(create_app(directory / "data", initial_mode="idle")) as api:
                    service = api.app.state.service
                    with patch.object(service, "set_source", new_callable=AsyncMock) as set_source:
                        response = api.post("/api/source", json={"mode": "live", "host": "192.0.2.8", "port": 5000, "managed": False})
                        self.assertEqual(response.status_code, 200, response.text)
                        set_source.assert_awaited_once_with("live", "192.0.2.8", 5000, False)


class CLIBootstrapTests(unittest.TestCase):
    def test_parser_defaults_to_local_managed_live_and_accepts_explicit_simulation(self):
        from krakendf.__main__ import build_parser
        with isolated_environment(Path("/tmp/krakendf-test-runtime")):
            defaults = build_parser().parse_args([])
            self.assertEqual(defaults.source, "live")
            self.assertEqual(defaults.daq_mode, "managed")
            self.assertEqual((defaults.daq_host, defaults.daq_port), ("127.0.0.1", 5000))
            self.assertEqual(build_parser().parse_args(["--source", "simulation"]).source, "simulation")

    def test_main_maps_cli_settings_to_environment_without_starting_server(self):
        from krakendf.__main__ import main
        argv = ["krakendf", "--source", "live", "--daq-mode", "external", "--daq-host", "192.0.2.50",
                "--daq-port", "15000", "--runtime-dir", "/tmp/example-runtime", "--data-dir", "/tmp/example-data",
                "--host", "127.0.0.1", "--port", "8088"]
        with isolated_environment(Path("/tmp/old-runtime")), patch.object(sys, "argv", argv), \
             patch("krakendf.app.create_app", return_value=sentinel.application) as factory, \
             patch("uvicorn.run") as run:
            main()
            self.assertEqual(os.environ["KRAKENDF_SOURCE"], "live")
            self.assertEqual(os.environ["KRAKENDF_DAQ_MODE"], "external")
            self.assertEqual(os.environ["KRAKENDF_DAQ_HOST"], "192.0.2.50")
            self.assertEqual(os.environ["KRAKENDF_DAQ_PORT"], "15000")
            self.assertEqual(os.environ["KRAKENDF_RUNTIME"], "/tmp/example-runtime")
            self.assertEqual(os.environ["KRAKENDF_DATA"], "/tmp/example-data")
            factory.assert_called_once_with()
            run.assert_called_once_with(sentinel.application, host="127.0.0.1", port=8088, log_level="info")


if __name__ == "__main__":
    unittest.main()
