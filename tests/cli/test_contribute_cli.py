from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from app import contribute


class ContributeCLITests(unittest.TestCase):
    @staticmethod
    def _norm(value: str) -> str:
        return value.replace("\\", "/")

    def test_setup_routes_to_vps_installer_on_posix(self) -> None:
        with mock.patch("app.contribute.os.name", "posix"), mock.patch(
            "app.contribute.os.execvp"
        ) as mocked_execvp, mock.patch("sys.argv", ["rover", "setup"]):
            contribute.main()

        mocked_execvp.assert_called_once()
        cmd, argv = mocked_execvp.call_args.args
        self.assertEqual(cmd, "bash")
        self.assertEqual(argv[0], "bash")
        self.assertTrue(self._norm(argv[1]).endswith("scripts/install_vps.sh"))

    def test_uninstall_routes_to_vps_uninstaller_on_posix(self) -> None:
        with mock.patch("app.contribute.os.name", "posix"), mock.patch(
            "app.contribute.os.execvp"
        ) as mocked_execvp, mock.patch("sys.argv", ["rover", "uninstall"]):
            contribute.main()

        mocked_execvp.assert_called_once()
        cmd, argv = mocked_execvp.call_args.args
        self.assertEqual(cmd, "bash")
        self.assertEqual(argv[0], "bash")
        self.assertTrue(self._norm(argv[1]).endswith("scripts/uninstall_vps.sh"))

    def test_setup_routes_to_windows_installer_on_windows(self) -> None:
        with mock.patch("app.contribute.os.name", "nt"), mock.patch(
            "app.contribute._wsl_unc_info", return_value=None
        ), mock.patch(
            "app.contribute.subprocess.run"
        ) as mocked_run, mock.patch("sys.argv", ["rover", "setup"]):
            contribute.main()

        mocked_run.assert_called_once()
        argv = mocked_run.call_args.args[0]
        self.assertEqual(argv[:4], ["powershell", "-ExecutionPolicy", "Bypass", "-File"])
        self.assertTrue(self._norm(argv[4]).endswith("scripts/install_windows.ps1"))

    def test_setup_routes_to_wsl_installer_when_windows_python_targets_wsl_repo(self) -> None:
        with mock.patch("app.contribute.os.name", "nt"), mock.patch(
            "app.contribute._wsl_unc_info", return_value=("Ubuntu-20.04", "/home/nadira/project/rover")
        ), mock.patch(
            "app.contribute.subprocess.run"
        ) as mocked_run, mock.patch("sys.argv", ["rover", "setup"]):
            contribute.main()

        mocked_run.assert_called_once_with(
            ["wsl.exe", "-d", "Ubuntu-20.04", "bash", "/home/nadira/project/rover/scripts/install_vps.sh"],
            check=False,
        )

    def test_uninstall_routes_to_windows_uninstaller_on_windows(self) -> None:
        with mock.patch("app.contribute.os.name", "nt"), mock.patch(
            "app.contribute._wsl_unc_info", return_value=None
        ), mock.patch(
            "app.contribute.subprocess.run"
        ) as mocked_run, mock.patch("sys.argv", ["rover", "uninstall"]):
            contribute.main()

        mocked_run.assert_called_once()
        argv = mocked_run.call_args.args[0]
        self.assertEqual(argv[:4], ["powershell", "-ExecutionPolicy", "Bypass", "-File"])
        self.assertTrue(self._norm(argv[4]).endswith("scripts/uninstall_windows.ps1"))

    def test_uninstall_routes_to_wsl_uninstaller_when_windows_python_targets_wsl_repo(self) -> None:
        with mock.patch("app.contribute.os.name", "nt"), mock.patch(
            "app.contribute._wsl_unc_info", return_value=("Ubuntu-20.04", "/home/nadira/project/rover")
        ), mock.patch(
            "app.contribute.subprocess.run"
        ) as mocked_run, mock.patch("sys.argv", ["rover", "uninstall"]):
            contribute.main()

        mocked_run.assert_called_once_with(
            ["wsl.exe", "-d", "Ubuntu-20.04", "bash", "/home/nadira/project/rover/scripts/uninstall_vps.sh"],
            check=False,
        )

    def test_discover_root_prefers_repo_cwd_over_site_packages_location(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        fake_module = repo_root / ".venv" / "lib" / "python3.10" / "site-packages" / "app" / "contribute.py"

        with mock.patch("app.contribute.Path.cwd", return_value=repo_root), mock.patch(
            "app.contribute.__file__", str(fake_module)
        ):
            discovered = contribute._discover_root()

        self.assertEqual(discovered, repo_root)

    def test_repo_url_shorthand_routes_to_targeted_contrib(self) -> None:
        with mock.patch("app.contribute._run_builder") as mocked_builder, mock.patch(
            "sys.argv", ["rover", "https://github.com/example/project"]
        ):
            contribute.main()

        mocked_builder.assert_called_once_with(["--contrib", "example/project"])

    def test_run_accepts_repo_url_and_normalizes_goal_alias(self) -> None:
        with mock.patch("app.contribute._run_builder") as mocked_builder, mock.patch(
            "app.contribute.print_banner"
        ), mock.patch(
            "sys.argv",
            ["rover", "run", "2", "https://github.com/example/project", "--goal", "upgrade"],
        ):
            contribute.main()

        mocked_builder.assert_called_once_with(
            ["--contrib", "example/project", "--2", "--goal", "feature_upgrade"]
        )

    def test_version_passthrough_routes_to_builder(self) -> None:
        with mock.patch("app.contribute._run_builder") as mocked_builder, mock.patch(
            "sys.argv", ["rover", "--version"]
        ):
            contribute.main()

        mocked_builder.assert_called_once_with(["--version"])

    def test_targeted_repo_routes_to_wsl_runtime_when_windows_python_targets_wsl_repo(self) -> None:
        with mock.patch("app.contribute.os.name", "nt"), mock.patch(
            "app.contribute._wsl_unc_info", return_value=("Ubuntu-20.04", "/home/nadira/project/rover")
        ), mock.patch(
            "app.contribute.subprocess.run"
        ) as mocked_run, mock.patch("sys.argv", ["rover", "example/project"]):
            contribute.main()

        mocked_run.assert_called_once_with(
            [
                "wsl.exe",
                "-d",
                "Ubuntu-20.04",
                "bash",
                "-lc",
                "cd /home/nadira/project/rover && python3 -m app.contribute example/project",
            ],
            check=False,
        )

    def test_doctor_verbose_routes_to_styled_doctor_verbose_mode(self) -> None:
        fake_checks = [object()]
        with mock.patch("app.contribute.print_banner"), mock.patch(
            "app.contribute.print_styled_doctor"
        ) as mocked_print, mock.patch(
            "src.core.doctor.collect_doctor_checks", return_value=fake_checks
        ), mock.patch(
            "sys.argv", ["rover", "doctor", "--verbose"]
        ):
            contribute.main()

        mocked_print.assert_called_once_with(fake_checks, verbose=True)

    def test_doctor_rejects_unknown_args(self) -> None:
        with mock.patch("app.contribute.print_banner"), mock.patch(
            "app.contribute.print_err"
        ) as mocked_err, mock.patch(
            "app.contribute.print_styled_doctor"
        ) as mocked_print, mock.patch(
            "sys.argv", ["rover", "doctor", "--wat"]
        ):
            contribute.main()

        mocked_err.assert_called_once()
        mocked_print.assert_not_called()

    def test_doctor_json_routes_directly_to_builder(self) -> None:
        with mock.patch("app.contribute._run_builder") as mocked_builder, mock.patch(
            "sys.argv", ["rover", "doctor", "--json"]
        ):
            contribute.main()

        mocked_builder.assert_called_once_with(["--doctor", "--json"])

    def test_run_json_routes_to_builder_without_banner(self) -> None:
        with mock.patch("app.contribute._run_builder") as mocked_builder, mock.patch(
            "sys.argv", ["rover", "run", "2", "example/project", "--json", "--dry-run"]
        ):
            contribute.main()

        mocked_builder.assert_called_once_with(["--contrib", "example/project", "--2", "--json", "--dry-run"])

    def test_scan_json_routes_to_builder(self) -> None:
        with mock.patch("app.contribute._run_builder") as mocked_builder, mock.patch(
            "sys.argv", ["rover", "scan", "example/project", "--kind", "security", "--json"]
        ):
            contribute.main()

        mocked_builder.assert_called_once_with(["--scan-repo", "example/project", "--scan-kind", "security", "--json"])

    def test_scan_trust_routes_to_builder(self) -> None:
        with mock.patch("app.contribute._run_builder") as mocked_builder, mock.patch(
            "sys.argv", ["rover", "scan", "example/project", "--kind", "trust"]
        ):
            contribute.main()

        mocked_builder.assert_called_once_with(["--scan-repo", "example/project", "--scan-kind", "trust"])

    def test_scan_audit_routes_to_builder(self) -> None:
        with mock.patch("app.contribute._run_builder") as mocked_builder, mock.patch(
            "sys.argv", ["rover", "scan", "example/project", "--kind", "audit"]
        ):
            contribute.main()

        mocked_builder.assert_called_once_with(["--scan-repo", "example/project", "--scan-kind", "audit"])

    def test_profile_routes_to_builder(self) -> None:
        with mock.patch("app.contribute._run_builder") as mocked_builder, mock.patch(
            "sys.argv", ["rover", "profile"]
        ):
            contribute.main()

        mocked_builder.assert_called_once_with(["--profile"])

    def test_windows_setup_keyboard_interrupt_is_handled_without_traceback(self) -> None:
        with mock.patch("app.contribute.os.name", "nt"), mock.patch(
            "app.contribute.subprocess.run", side_effect=KeyboardInterrupt
        ), mock.patch("app.contribute.print_blank") as mocked_blank, mock.patch(
            "app.contribute.print_warn"
        ) as mocked_warn, mock.patch("sys.argv", ["rover", "setup"]):
            contribute.main()

        mocked_blank.assert_called_once()
        mocked_warn.assert_called_once_with("install interrupted by user")


if __name__ == "__main__":
    unittest.main()
