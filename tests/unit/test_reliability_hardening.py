import multiprocessing
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from browser_manager.instance_lock import DataDirInstanceLock, DataDirLockError
from browser_manager.profile_manager import profile_process_matches, query_profile_processes


def _hold_data_dir_lock(data_dir: str, ready, release) -> None:
    lock = DataDirInstanceLock(Path(data_dir))
    try:
        lock.acquire()
        ready.set()
        release.wait(10)
    finally:
        lock.release()


class ReliabilityHardeningTests(unittest.TestCase):
    def test_same_data_dir_is_exclusive_across_processes_and_releases(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            ready = context.Event()
            release = context.Event()
            process = context.Process(
                target=_hold_data_dir_lock,
                args=(directory, ready, release),
            )
            process.start()
            self.assertTrue(ready.wait(10), "lock holder did not start")
            try:
                with self.assertRaises(DataDirLockError):
                    DataDirInstanceLock(Path(str(directory).upper()) / "").acquire()
            finally:
                release.set()
                process.join(10)
            self.assertEqual(process.exitcode, 0)

            lock = DataDirInstanceLock(Path(directory))
            lock.acquire()
            lock.release()

    def test_user_data_dir_matching_handles_quotes_case_and_trailing_separator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Program Files" / "Profiles"
            target = root / "Profile-10"
            target.mkdir(parents=True)
            command_line = subprocess.list2cmdline(
                ["chrome.exe", f"--user-data-dir={str(target).upper()}\\"]
            )
            self.assertTrue(profile_process_matches(command_line, target))

    def test_profile_10_does_not_match_profile_100(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Profiles"
            target = root / "Profile-10"
            other = root / "Profile-100"
            self.assertFalse(
                profile_process_matches(
                    f'chrome.exe --user-data-dir="{other}"',
                    target,
                )
            )

    def test_user_data_dir_matching_accepts_separate_argument_form(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Profile 10"
            command_line = f'chrome.exe --user-data-dir "{target}"'
            self.assertTrue(profile_process_matches(command_line, target))

    def test_query_profile_processes_filters_by_parsed_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Profile-10"
            raw = json.dumps([
                {"ProcessId": 10, "Name": "chrome.exe", "CommandLine": f'chrome.exe --user-data-dir="{target}"'},
                {"ProcessId": 100, "Name": "chrome.exe", "CommandLine": f'chrome.exe --user-data-dir="{target}0"'},
            ])
            with patch("browser_manager.profile_manager.subprocess.check_output", return_value=raw) as check_output:
                result = query_profile_processes(target)
            self.assertEqual([item["ProcessId"] for item in result], [10])
            command = check_output.call_args.args[0][-1]
            self.assertNotIn("Contains($root)", command)


if __name__ == "__main__":
    unittest.main()
