from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "ggsel" / "runtime"


class InstallerTests(unittest.TestCase):
    def test_linux_scripts_are_valid_and_recover_missing_pip(self):
        subprocess.run(
            ["sh", "-n", str(RUNTIME / "install.sh"), str(RUNTIME / "install-service.sh")],
            check=True,
        )
        installer = (RUNTIME / "install.sh").read_text(encoding="utf-8")
        self.assertIn("-m ensurepip --upgrade", installer)
        self.assertIn("python3-venv", installer)
        self.assertIn("--check-buywell", installer)

    def test_linux_service_uses_current_user_and_runtime_folder(self):
        installer = (RUNTIME / "install-service.sh").read_text(encoding="utf-8")
        self.assertIn("runtime_user=$(id -un)", installer)
        self.assertIn("runtime_dir=$(pwd -P)", installer)
        self.assertIn("User=$runtime_user", installer)
        self.assertIn("WorkingDirectory=$runtime_dir", installer)
        self.assertIn('systemctl restart "$service_name"', installer)
        self.assertIn('systemctl is-active --quiet "$service_name"', installer)

    def test_linux_service_can_be_reinstalled_without_duplicate_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime folder"
            binaries = root / "bin"
            runtime.mkdir()
            binaries.mkdir()
            shutil.copy2(RUNTIME / "install-service.sh", runtime / "install-service.sh")
            (runtime / ".venv" / "bin").mkdir(parents=True)
            python = runtime / ".venv" / "bin" / "python"
            python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            python.chmod(0o755)
            (runtime / "config.json").write_text("{}\n", encoding="utf-8")
            log = root / "commands.log"
            for name, body in {
                "install": '#!/bin/sh\nprintf "install %s\\n" "$*" >> "$TEST_COMMAND_LOG"\n',
                "systemctl": '#!/bin/sh\nprintf "systemctl %s\\n" "$*" >> "$TEST_COMMAND_LOG"\n',
                "sudo": '#!/bin/sh\nexec "$@"\n',
            }.items():
                executable = binaries / name
                executable.write_text(body, encoding="utf-8")
                executable.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{binaries}:{os.environ['PATH']}",
                "TEST_COMMAND_LOG": str(log),
            }
            for _ in range(2):
                subprocess.run(
                    ["sh", "install-service.sh"],
                    cwd=runtime,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            commands = log.read_text(encoding="utf-8")
            self.assertEqual(commands.count("systemctl enable buywell-ggsel.service"), 2)
            self.assertEqual(commands.count("systemctl restart buywell-ggsel.service"), 2)
            self.assertEqual(
                commands.count("systemctl is-active --quiet buywell-ggsel.service"), 2
            )

    def test_windows_installer_recovers_pip_and_registers_current_user_task(self):
        installer = (RUNTIME / "install.bat").read_text(encoding="utf-8")
        service = (RUNTIME / "install-service.ps1").read_text(encoding="utf-8")
        self.assertIn("-m ensurepip --upgrade", installer)
        self.assertIn("--check-buywell", installer)
        self.assertIn("GetCurrent().Name", service)
        self.assertIn("-AtLogOn -User $userId", service)
        self.assertIn("-WorkingDirectory $runtimeDirectory", service)
        self.assertIn("Get-ScheduledTask -TaskName $taskName", service)
        self.assertIn("Stop-ScheduledTask -TaskName $taskName", service)
        self.assertIn("Register-ScheduledTask -TaskName $taskName", service)
        self.assertIn("-Force", service)


if __name__ == "__main__":
    unittest.main()
