import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from browser_manager.profile_manager import probe_profile_processes


class ProcessProbeUtf8Tests(unittest.TestCase):
    def test_cim_probe_requests_utf8_and_preserves_unicode_command_line(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "中文" / "Profile-01"
            output = json.dumps([{"ProcessId": 123, "Name": "chrome.exe", "CommandLine": f'chrome.exe --user-data-dir="{profile}"'}], ensure_ascii=False)
            with patch("browser_manager.profile_manager.subprocess.check_output", return_value=output) as check_output:
                with patch("browser_manager.profile_manager.profile_process_matches", return_value=True):
                    result = probe_profile_processes(profile)
            self.assertEqual(result.state, "found")
            self.assertIn("中文", result.processes[0]["CommandLine"])
            command = check_output.call_args.args[0][-1]
            self.assertIn("UTF8Encoding", command)
            self.assertIn("$OutputEncoding", command)
            self.assertEqual(check_output.call_args.kwargs["encoding"], "utf-8")
            self.assertNotIn("errors", check_output.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
