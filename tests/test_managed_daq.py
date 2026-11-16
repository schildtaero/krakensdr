from __future__ import annotations

import asyncio
from configparser import ConfigParser
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
import venv
from unittest.mock import patch

from krakendf.hardware import HardwareError
from krakendf.managed_daq import ManagedDAQ, SHM_NAMES, FIFO_NAMES


CONFIG = {"center_frequency_hz": 434000000, "sample_rate_hz": 2400000, "gain_db": 20.7}
DEVICES = [{"accessible": True, "serial": str(i)} for i in range(1000, 1005)]
TEMPLATE = """[meta]
ini_version=7
[hw]
num_ch=5
[daq]
center_freq=700000000
[pre_processing]
cpi_size=1048576
[calibration]
en_iq_cal=1
phase_tolerance=1
cal_track_mode=2
iq_adjust_source=explicit-time-delay
[adpis]
en_adpis=0
[data_interface]
out_data_iface_type=shmem
"""


def fixture(root):
    runtime = root / "runtime"
    firmware = runtime / "firmware"
    core = firmware / "_daq_core"
    core.mkdir(parents=True)
    (runtime / "native" / "lib").mkdir(parents=True)
    (firmware / "_calibration").mkdir()
    (firmware / "daq_chain_config.ini").write_text(TEMPLATE)
    # Native programs are harmless executable test helpers. They do not open
    # USB devices or sockets, and manager-owned subprocesses are still real.
    for name in ("rtl_daq.out", "rebuffer.out", "decimate.out", "iq_server.out"):
        path = core / name
        path.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        path.chmod(0o700)
    for name in ("delay_sync.py", "hw_controller.py"):
        (core / name).write_text("import time\ntime.sleep(60)\n")
    for name in ("shmemIface.py", "iq_header.py"):
        (core / name).write_text("")
    manifest = {"schema_version": 1, "ready": True, "firmware_dir": "firmware",
                "native_prefix": "native", "python": sys.executable}
    (runtime / "manifest.json").write_text(json.dumps(manifest))
    manager = ManagedDAQ(runtime, root / "logs")
    manager.shm_dir = root / "shm"
    manager.shm_dir.mkdir()
    return manager


class RuntimePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = fixture(self.root)
        self.patchers = [patch("krakendf.managed_daq.listening_ports", return_value=set()),
                         patch("krakendf.managed_daq.discover_rtl_devices", return_value=DEVICES),
                         patch("krakendf.managed_daq.platform.system", return_value="Linux"),
                         patch("krakendf.managed_daq.platform.machine", return_value="x86_64")]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_absent_runtime_status_is_safe_and_helpful(self):
        manager = ManagedDAQ(self.root / "not-installed", self.root / "logs")
        self.assertFalse(manager.status()["installed"])
        self.assertFalse(manager.status()["running"])
        with self.assertRaisesRegex(HardwareError, "setup.sh"):
            manager._preflight(CONFIG)

    def test_manifest_interpreter_keeps_venv_prefix_and_private_modules(self):
        environment = self.root / ".venv"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
        interpreter = environment / "bin" / "python"
        site = subprocess.check_output([str(interpreter), "-I", "-c",
                                        "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip()
        (Path(site) / "krakendf_private_venv_probe.py").write_text("VALUE = 'venv-only'\n")
        path = self.manager.runtime_dir / "manifest.json"
        manifest = json.loads(path.read_text())
        for configured in (str(interpreter), "../.venv/bin/python"):
            with self.subTest(configured=configured):
                manifest["python"] = configured
                path.write_text(json.dumps(manifest))
                selected = self.manager._read_manifest()["python_path"]
                result = subprocess.run([str(selected), "-I", "-c",
                    "import sys,json,krakendf_private_venv_probe as probe; "
                    "print(json.dumps([sys.prefix,sys.base_prefix,probe.VALUE]))"],
                    text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                prefix, base, value = json.loads(result.stdout)
                self.assertEqual(Path(prefix).resolve(), environment.resolve())
                self.assertNotEqual(prefix, base)
                self.assertEqual(value, "venv-only")

    def test_incomplete_manifest_and_missing_binary_are_rejected(self):
        path = self.manager.runtime_dir / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["ready"] = False
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(HardwareError, "fertig installiert"):
            self.manager._preflight(CONFIG)
        manifest["ready"] = True
        path.write_text(json.dumps(manifest))
        (self.manager.runtime_dir / "firmware/_daq_core/rebuffer.out").unlink()
        with self.assertRaisesRegex(HardwareError, "unvollständig"):
            self.manager._preflight(CONFIG)

    def test_port_conflicts_never_take_over_external_daq(self):
        with patch("krakendf.managed_daq.listening_ports", return_value={5001, 1130}):
            with self.assertRaisesRegex(HardwareError, "bereits belegt"):
                self.manager._preflight(CONFIG)

    def test_existing_unknown_shared_memory_is_preserved(self):
        stale = self.manager.shm_dir / SHM_NAMES[0]
        stale.write_bytes(b"external owner")
        with self.assertRaisesRegex(HardwareError, "nicht automatisch gelöscht"):
            self.manager._preflight(CONFIG)
        self.assertEqual(stale.read_bytes(), b"external owner")

    def test_no_usb_and_inaccessible_usb_are_distinct(self):
        with patch("krakendf.managed_daq.discover_rtl_devices", return_value=[]):
            with self.assertRaisesRegex(HardwareError, "Kein KrakenSDR"):
                self.manager._preflight(CONFIG)
        with patch("krakendf.managed_daq.discover_rtl_devices", return_value=[{"accessible": False, "serial": ""}]):
            with self.assertRaisesRegex(HardwareError, "USB-Zugriff fehlt"):
                self.manager._preflight(CONFIG)

    def test_unique_serials_required_without_eeprom_writes(self):
        bad = [{"accessible": True, "serial": "00000001"}] * 5
        with patch("krakendf.managed_daq.discover_rtl_devices", return_value=bad):
            with self.assertRaisesRegex(HardwareError, "kein EEPROM"):
                self.manager._preflight(CONFIG)

    def test_supported_rate_gain_and_finite_values(self):
        for overrides in ({"sample_rate_hz": 500000}, {"sample_rate_hz": 2400000.5},
                          {"gain_db": 20}, {"center_frequency_hz": float("nan")}):
            with self.subTest(overrides=overrides), self.assertRaises(HardwareError):
                self.manager._preflight({**CONFIG, **overrides})
        self.assertIsNotNone(self.manager._preflight(CONFIG))

    def test_low_shared_memory_has_actionable_error(self):
        class Usage:
            free = 64 * 1024 * 1024
        with patch("krakendf.managed_daq.shutil.disk_usage", return_value=Usage()):
            with self.assertRaisesRegex(HardwareError, "/dev/shm"):
                self.manager._preflight(CONFIG)

    def test_generated_configuration_preserves_calibration_and_uses_private_fifos(self):
        manifest = self.manager._preflight(CONFIG)
        directory = self.manager._prepare_run(manifest, CONFIG)
        parser = ConfigParser()
        parser.read(directory / "daq_chain_config.ini")
        self.assertEqual(parser.get("data_interface", "out_data_iface_type"), "eth")
        self.assertEqual(parser.getint("daq", "center_freq"), 434000000)
        self.assertEqual(parser.getint("daq", "gain"), 207)
        self.assertEqual(parser.getint("calibration", "en_iq_cal"), 1)
        self.assertEqual(parser.getint("calibration", "phase_tolerance"), 1)
        self.assertEqual(parser.getint("calibration", "cal_track_mode"), 2)
        self.assertEqual(parser.getint("adpis", "en_adpis"), 0)
        self.assertEqual(parser.get("adpis", "adpis_gains_init"), "207,207,207,207,207")
        self.assertTrue(all((directory / "_data_control" / name).is_fifo() for name in FIFO_NAMES))
        self.assertEqual((directory / "_data_control/fir_coeffs.txt").read_text(), "1.0\n")
        # The shipped template remains untouched.
        self.assertIn("center_freq=700000000", (manifest["firmware"] / "daq_chain_config.ini").read_text())


@unittest.skipUnless(sys.platform.startswith("linux"), "Managed USB DAQ is Linux-only")
class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = fixture(self.root)
        self.manifest = self.manager._read_manifest()
        self.manager.stop_timeout = .5
        self.preflight = patch.object(self.manager, "_preflight", return_value=self.manifest)
        self.preflight.start()

    async def asyncTearDown(self):
        await self.manager.stop()
        self.preflight.stop()
        self.temp.cleanup()

    async def test_stop_before_start_is_idempotent(self):
        await self.manager.stop()
        await self.manager.stop()
        self.assertFalse(self.manager.status()["running"])

    async def test_own_subprocesses_stop_and_unrelated_process_survives(self):
        unrelated = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time;time.sleep(60)", start_new_session=True)
        try:
            with patch("krakendf.managed_daq.listening_ports", return_value={5000, 5001}):
                status = await self.manager.start(CONFIG)
            self.assertTrue(status["running"])
            self.assertEqual(len(status["processes"]), 6)
            owned = list(self.manager._children.values())
            first_pids = [p.pid for p in owned]
            same = await self.manager.start(CONFIG)
            self.assertEqual([p["pid"] for p in same["processes"].values()], first_pids)
            await self.manager.stop()
            self.assertTrue(all(p.returncode is not None for p in owned))
            self.assertIsNone(unrelated.returncode)
        finally:
            if unrelated.returncode is None:
                os.killpg(unrelated.pid, signal.SIGTERM)
            await unrelated.wait()

    async def test_startup_timeout_cleans_every_spawned_child(self):
        self.manager.startup_timeout = .1
        with patch("krakendf.managed_daq.listening_ports", return_value=set()):
            with self.assertRaisesRegex(HardwareError, "nicht bereit"):
                await self.manager.start(CONFIG)
        self.assertEqual(self.manager.status()["processes"], {})
        self.assertFalse(self.manager.status()["running"])
        self.assertIsNone(self.manager._lock_stream)

    async def test_startup_child_failure_cleans_and_reports_role(self):
        (self.manifest["firmware"] / "_daq_core/delay_sync.py").write_text("raise SystemExit(17)\n")
        self.manager.startup_timeout = 1
        with patch("krakendf.managed_daq.listening_ports", return_value=set()):
            with self.assertRaisesRegex(HardwareError, "delay_sync"):
                await self.manager.start(CONFIG)
        self.assertFalse(self.manager.status()["processes"])

    async def test_cleanup_only_removes_exact_recorded_inode(self):
        owned = self.manager.shm_dir / SHM_NAMES[0]
        owned.write_bytes(b"created in this launch")
        info = owned.stat()
        self.manager._owned_shm[owned] = (info.st_dev, info.st_ino)
        replaced = self.manager.shm_dir / SHM_NAMES[1]
        replaced.write_bytes(b"old")
        old_info = replaced.stat()
        self.manager._owned_shm[replaced] = (old_info.st_dev, old_info.st_ino)
        # Atomic replacement guarantees a different inode even on a filesystem
        # that would reuse a freshly unlinked file's inode.
        temporary = self.manager.shm_dir / "replacement"
        temporary.write_bytes(b"new unrelated owner")
        temporary.replace(replaced)
        await self.manager.stop()
        self.assertFalse(owned.exists())
        self.assertEqual(replaced.read_bytes(), b"new unrelated owner")

    async def test_native_windows_status_stop_and_start_are_safe(self):
        with patch("krakendf.managed_daq.platform.system", return_value="Windows"):
            self.assertFalse(self.manager.status()["running"])
            await self.manager.stop()
            with self.assertRaisesRegex(HardwareError, "WSL2"):
                await self.manager.start(CONFIG)


if __name__ == "__main__":
    unittest.main()
