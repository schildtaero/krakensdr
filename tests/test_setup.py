"""First-use dependency/bootstrap and native build recipe tests."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_helper(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load_helper("krakendf_build_recipe", "scripts/build-runtime.py")
rules = load_helper("krakendf_device_rules", "scripts/device-rules.py")


class BootstrapTests(unittest.TestCase):
    def test_shipped_sources_match_pinned_hashes_and_licenses(self):
        manifest = build.verify_sources(ROOT)
        self.assertEqual({s["name"] for s in manifest["sources"]}, {"heimdall", "librtlsdr"})
        self.assertTrue((ROOT / "vendor/heimdall/LICENSE").is_file())
        self.assertTrue((ROOT / "vendor/librtlsdr/COPYING").is_file())

    def test_tampered_source_is_rejected_before_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "vendor/example").mkdir(parents=True)
            (root / "vendor/example/file.c").write_text("changed")
            (root / "vendor/SOURCE_MANIFEST.json").write_text(json.dumps({"sources": [{
                "name": "example", "directory": "example", "sha256_files": {"file.c": "0"*64}}]}))
            with self.assertRaisesRegex(RuntimeError, "differs"):
                build.verify_sources(root)

    def test_dry_run_and_missing_dependency_check_do_not_mutate_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            shutil.copy2(ROOT / "setup.sh", root / "setup.sh")
            shutil.copy2(ROOT / "scripts/build-runtime.py", root / "scripts/build-runtime.py")
            before = {p.relative_to(root) for p in root.rglob("*")}
            done = subprocess.run(["bash", str(root / "setup.sh"), "--dry-run"], text=True, capture_output=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("No files", done.stdout)
            checked = subprocess.run(["bash", str(root / "setup.sh"), "--check", "--app-only"], text=True, capture_output=True)
            self.assertNotEqual(checked.returncode, 0)
            self.assertIn("virtual environment", checked.stderr)
            self.assertEqual(before, {p.relative_to(root) for p in root.rglob("*")})

    def test_udev_is_factory_serial_specific_not_global_world_writable(self):
        text = rules.rules_for("demo-user")
        self.assertIn('ATTR{serial}=="100[0-4]"', text)
        self.assertIn('OWNER="demo-user"', text)
        self.assertIn('ATTRS{idVendor}=="1546"', text)
        self.assertNotIn("0666", text)
        self.assertNotIn("blacklist", text)
        with self.assertRaises(ValueError):
            rules.rules_for('bad" RUN+="touch /tmp/injected')

    def test_dependency_check_imports_modules_not_just_their_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / ".venv/bin/python"
            executable.parent.mkdir(parents=True)
            executable.write_text("")
            (root / "pyproject.toml").write_text('[project]\nversion="0.2.0"\n')
            with patch.object(build.subprocess, "run") as mocked:
                mocked.return_value = subprocess.CompletedProcess([], 1, "", "binary module ABI incompatible")
                errors = build.python_errors(root, False)
                self.assertIn("ABI incompatible", errors[0])
                self.assertIn("importlib.import_module", mocked.call_args.args[0][-1])

    def test_runtime_marker_requires_real_binaries_and_matching_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".runtime"
            runtime.mkdir()
            (runtime / "manifest.json").write_text(json.dumps({
                "schema_version": 1, "ready": True, "architecture": build.platform.machine(),
                "build_fingerprint": "fingerprint", "python": str(root / "wrong/bin/python"),
                "firmware_dir": "firmware", "native_prefix": "native"}))
            with patch.dict(os.environ, {"KRAKENDF_RUNTIME": str(runtime)}), \
                 patch.object(build, "python_errors", return_value=[]), \
                 patch.object(build, "build_fingerprint", return_value="fingerprint"):
                errors = build.runtime_errors(root)
            self.assertTrue(any("different virtual" in message for message in errors))
            self.assertEqual(sum("Missing executable" in message for message in errors), 4)
            self.assertTrue(any("librtlsdr.so" in message for message in errors))

    def test_unsupported_architecture_rejected_before_mutation(self):
        with patch.object(build.platform, "machine", return_value="armv7l"):
            with self.assertRaisesRegex(RuntimeError, "64-bit"):
                build.build(ROOT)


class UnityConverterTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("gcc"), "Optional native equation test needs gcc")
    def test_upstream_data_and_cal_conversion_equation_is_preserved(self):
        original = (ROOT / "vendor/heimdall/Firmware/_daq_core/fir_decimate.c").read_text()
        # Compile the unchanged conversion loop extracted from the upstream
        # DATA-at-unity/CAL path, rather than an independently copied formula.
        body = re.search(r"for\(int sample_index=0; sample_index<iq_header->cpi_length\*iq_header->active_ant_chs; sample_index\+\+\)\s*\{([^}]+)\}", original).group(1)
        self.assertIn("FRAME_TYPE_CAL || dec == 1", original)
        patch_text = (ROOT / "patches/heimdall-unity.patch").read_text()
        self.assertIn("config.decimation_ratio != 1 || config.tap_size != 1", patch_text)
        self.assertNotIn("output_data_buffer", patch_text)
        source = '#include <stdio.h>\n#include <stdint.h>\n#define DC 127.5\nint main(void){\n'
        source += 'uint8_t input_data_buffer[]={0,127,128,255,255,0}; float output_data_buffer[6];\n'
        source += 'for(int frame_type=0;frame_type<=3;frame_type+=3){ for(int sample_index=0;sample_index<3;sample_index++){\n'+body+'\n}\n'
        source += 'for(int i=0;i<6;i++)printf("%.9g ",output_data_buffer[i]); puts("");} return 0;}\n'
        with tempfile.TemporaryDirectory() as directory:
            cfile, executable = Path(directory)/"conversion.c", Path(directory)/"conversion"
            cfile.write_text(source)
            subprocess.run(["gcc", "-std=c99", str(cfile), "-o", str(executable)], check=True, capture_output=True)
            lines = subprocess.check_output([str(executable)], text=True).splitlines()
        expected = [(v-127.5)/127.5 for v in (0,127,128,255,255,0)]
        self.assertEqual(len(lines), 2)
        for line in lines:
            for actual, target in zip(map(float, line.split()), expected):
                self.assertAlmostEqual(actual, target, places=7)


if __name__ == "__main__":
    unittest.main()
