"""Flag updates plan first and preserve existing CTFd object identities."""

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock


SOURCE = Path(__file__).resolve().parents[1] / "library/aia_ctfd_flags.py"
spec = importlib.util.spec_from_file_location("aia_ctfd_flags", SOURCE)
flags = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flags)


class FlagSyncTests(unittest.TestCase):
    def client(self, current):
        http = Mock()
        def request(method, url, **kwargs):
            response = Mock(status_code=200)
            data = [{"id": 7, "name": "Example"}] if url.endswith("challenges?view=admin") else current
            response.json.return_value = {"success": True, "data": data}
            return response
        http.request.side_effect = request
        return http

    def test_unchanged_flags_have_no_mutations(self):
        http = self.client([{"id": 11, "type": "static", "content": "synthetic", "data": None}])
        result = flags.sync_flags(http, "https://ctfd.example.test", [{"name": "Example", "flags": [{"content": "synthetic"}]}])
        self.assertFalse(result["changed"])
        self.assertEqual([call.args[0] for call in http.request.call_args_list], ["GET", "GET"])

    def test_changed_flag_patches_its_existing_id(self):
        http = self.client([{"id": 11, "type": "static", "content": "old", "data": None}])
        result = flags.sync_flags(http, "https://ctfd.example.test", [{"name": "Example", "flags": [{"content": "new"}]}])
        self.assertEqual(result["updated"], 1)
        call = http.request.call_args_list[-1]
        self.assertEqual(call.args, ("PATCH", "https://ctfd.example.test/api/v1/flags/11"))
        self.assertEqual(call.kwargs["json"]["content"], "new")

    def test_count_mismatch_and_missing_challenge_stop_before_mutation(self):
        for name in ("Example", "Missing"):
            http = self.client([])
            with self.assertRaises(flags.SyncError):
                flags.sync_flags(http, "https://ctfd.example.test", [{"name": name, "flags": [{"content": "new"}]}])
            self.assertTrue(all(call.args[0] == "GET" for call in http.request.call_args_list))

    def test_check_mode_reports_plan_without_writing(self):
        http = self.client([{"id": 11, "content": "old"}])
        result = flags.sync_flags(http, "https://ctfd.example.test", [{"name": "Example", "flags": [{"content": "new"}]}], check_mode=True)
        self.assertEqual(result, {"changed": True, "planned_updates": 1, "updated": 0})
        self.assertTrue(all(call.args[0] == "GET" for call in http.request.call_args_list))


if __name__ == "__main__":
    unittest.main()
