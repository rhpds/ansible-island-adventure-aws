"""Offline Ansible regression tests; no cluster or credentials are used.

Run with: python3 -m unittest discover -s automation/ansible/tests -v
Requires ansible-playbook, GNU coreutils timeout, and PyYAML.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


COLLECTION = Path(__file__).resolve().parents[1]
ROLE = COLLECTION / "roles" / "instructor_config"


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook required")
class ChallengeCheckTests(unittest.TestCase):
    def run_check(self, responses, timeout=10, retries=0, split_home=False):
        tasks = yaml.safe_load((ROLE / "tasks/ctfd_config.yml").read_text())
        check = next(t for t in tasks if t["name"].endswith("Check if challenges already loaded"))
        record = next(t for t in tasks if t["name"].endswith("Record existing challenge count"))
        import_task = next(t for t in tasks if t["name"].endswith("Setup, login, and import CTFd archive"))
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        with tempfile.TemporaryDirectory(prefix="aia-ctfd-test-") as directory:
            root = Path(directory)
            fixture = root / "responses.json"
            fixture.write_text(json.dumps(responses))
            counter = root / "attempts"
            oc = root / "oc"
            oc.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys, time
counter = pathlib.Path(os.environ['AIA_TEST_COUNTER'])
attempt = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(attempt + 1))
responses = json.loads(pathlib.Path(os.environ['AIA_TEST_RESPONSES']).read_text())
response = responses[min(attempt, len(responses) - 1)]
print(response.get('stdout', ''), end='', flush=True)
print(response.get('stderr', ''), end='', file=sys.stderr, flush=True)
time.sleep(response.get('sleep', 0))
sys.exit(response.get('rc', 0))
""")
            oc.chmod(0o755)
            config = root / "ansible.cfg"
            config.write_text("[defaults]\nstdout_callback=default\n")
            variables = {k: v for k, v in defaults.items() if k.startswith("ctfd_challenge_check_")}
            variables.update({
                "instructor": {"name": "test", "ctfd_ns": "test-ctfd"},
                "ansible_env": {"PATH": str(root) + os.pathsep + os.environ["PATH"]},
                "ansible_python_interpreter": "/usr/bin/python3",
                "ctfd_challenge_check_timeout": timeout,
                "ctfd_challenge_check_retries": retries,
                "ctfd_challenge_check_delay": 0,
            })
            # Exercise the real check, parsing, and import condition. Replace
            # the actual archive upload with a harmless visible boundary.
            play = [{"hosts": "localhost", "gather_facts": False, "vars": variables,
                     "tasks": [check, record, {
                         "name": "Import boundary",
                         "ansible.builtin.debug": {"msg": "IMPORT_ALLOWED"},
                         "when": import_task["when"],
                     }]}]
            playbook = root / "check.yml"
            playbook.write_text(yaml.safe_dump(play, sort_keys=False))
            env = dict(os.environ, ANSIBLE_CONFIG=str(config),
                       AIA_TEST_COUNTER=str(counter), AIA_TEST_RESPONSES=str(fixture),
                       ANSIBLE_LOCAL_TEMP=str(root / "local-tmp"),
                       ANSIBLE_REMOTE_TEMP=str(root / "remote-tmp"),
                       ANSIBLE_ASYNC_DIR=str(root / "async"), ANSIBLE_NOCOLOR="1")
            if split_home:
                # The async writer inherits task HOME, while the poller expands
                # ~ using the connection's HOME. Exercise that runner mismatch.
                task_home = root / "task-home"
                task_home.mkdir()
                env["ANSIBLE_ASYNC_DIR"] = "~/.ansible_async"
                play[0]["environment"] = {"HOME": str(task_home)}
                playbook.write_text(yaml.safe_dump(play, sort_keys=False))
            result = subprocess.run(
                ["ansible-playbook", "-i", "localhost,", "-c", "local", str(playbook)],
                env=env, text=True, capture_output=True, timeout=60,
            )
            output = result.stdout + result.stderr
            attempts = int(counter.read_text()) if counter.exists() else 0
            return result.returncode, output, attempts

    def test_zero_allows_import_with_startup_messages(self):
        rc, output, attempts = self.run_check([{"stdout": "Starting CTFd\nCHALLENGE_COUNT:0\n"}])
        self.assertEqual(rc, 0, output)
        self.assertIn("IMPORT_ALLOWED", output)
        self.assertIn("changed=0", output)
        self.assertEqual(attempts, 1)

    def test_existing_challenges_skip_import(self):
        rc, output, _ = self.run_check([{"stdout": "CHALLENGE_COUNT:25\n"}])
        self.assertEqual(rc, 0, output)
        self.assertNotIn("IMPORT_ALLOWED", output)

    def test_task_home_differs_from_controller_home(self):
        rc, output, attempts = self.run_check([
            {"stdout": "CHALLENGE_COUNT:0\n"},
        ], split_home=True)
        self.assertEqual(rc, 0, output)
        self.assertIn("IMPORT_ALLOWED", output)
        self.assertEqual(attempts, 1)

    def test_invalid_counts_stop_before_import(self):
        for stdout in ("", "startup only\n", "CHALLENGE_COUNT:no\n",
                       "CHALLENGE_COUNT:-1\n", "CHALLENGE_COUNT:0\nCHALLENGE_COUNT:25\n"):
            with self.subTest(stdout=stdout):
                rc, output, _ = self.run_check([{"stdout": stdout}])
                self.assertNotEqual(rc, 0, output)
                self.assertNotIn("TASK [Import boundary]", output)
                self.assertNotIn("No last item", output)

    def test_command_failure_preserves_stderr_even_with_count(self):
        rc, output, _ = self.run_check([{
            "rc": 1, "stdout": "CHALLENGE_COUNT:0\n", "stderr": "simulated API failure\n",
        }])
        self.assertNotEqual(rc, 0, output)
        self.assertIn("simulated API failure", output)
        self.assertNotIn("TASK [Import boundary]", output)

    def test_transient_failure_retries_then_allows_import(self):
        rc, output, attempts = self.run_check([
            {"rc": 1, "stderr": "transient API failure\n"},
            {"stdout": "CHALLENGE_COUNT:0\n"},
        ], retries=1)
        self.assertEqual(rc, 0, output)
        self.assertIn("IMPORT_ALLOWED", output)
        self.assertEqual(attempts, 2)

    def test_hung_command_times_out_and_exhausts_retries(self):
        # Even a printed count cannot make an unfinished command successful.
        rc, output, attempts = self.run_check([
            {"stdout": "CHALLENGE_COUNT:0\n", "sleep": 30},
        ], timeout=1, retries=1)
        self.assertNotEqual(rc, 0, output)
        self.assertIn("sending signal term", output.lower())
        self.assertIn('"rc": 124', output)
        self.assertEqual(attempts, 2)
        self.assertNotIn("TASK [Import boundary]", output)


if __name__ == "__main__":
    unittest.main()
