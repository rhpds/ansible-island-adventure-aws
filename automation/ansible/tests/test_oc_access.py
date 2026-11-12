"""Exercise exported-token access and legacy login through the real role."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


COLLECTION = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook required")
class OpenShiftAccessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aia-oc-access-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(COLLECTION / "roles/instructor_config", self.root / "roles/instructor_config")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "oc-calls.jsonl"
        oc = self.bin / "oc"
        oc.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['AIA_TEST_OC_LOG'], 'a') as stream:
    stream.write(json.dumps(args) + '\\n')
if 'whoami' in args:
    path = next((a.split('=', 1)[1] for a in args if a.startswith('--kubeconfig=')), '')
    if not path or not Path(path).is_file() or os.environ['AIA_TEST_OC_MODE'] == 'invalid':
        print('Unauthorized exported token', file=sys.stderr)
        sys.exit(1)
    print('system:admin')
elif args and args[0] == 'login' and os.environ['AIA_TEST_OC_MODE'] == 'legacy':
    print('Legacy catalog login succeeded')
else:
    print('Unexpected password login or command', file=sys.stderr)
    sys.exit(9)
''')
        oc.chmod(0o700)
        self.kubeconfig = self.root / "leaderboard.kubeconfig"
        self.kubeconfig.write_text("synthetic exported kubeconfig\n")
        self.playbook = self.root / "access.yml"
        self.inventory = self.root / "inventory.json"
        self.inventory.write_text(json.dumps({"all": {"hosts": {"control": {
            "ansible_connection": "local", "ansible_python_interpreter": sys.executable,
        }}}}))
        self.config = self.root / "ansible.cfg"
        self.config.write_text("[defaults]\nstdout_callback=default\n")

    def run_access(self, mode="valid", exported=True, empty=False):
        variables = {"ocp_api_url": "https://api.example.test:6443", "ocp_admin_user": "admin",
                     "ocp_admin_password": "synthetic-password"}
        if exported:
            variables["aia_kubeconfig"] = "" if empty else str(self.kubeconfig)
        self.playbook.write_text(yaml.safe_dump([{
            "hosts": "all", "gather_facts": True, "vars": variables,
            "tasks": [{"ansible.builtin.include_role": {
                "name": "instructor_config", "tasks_from": "oc_login",
            }}],
        }]))
        result = subprocess.run(
            ["ansible-playbook", "-i", str(self.inventory), str(self.playbook)],
            env=dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                     ANSIBLE_CONFIG=str(self.config), ANSIBLE_NOCOLOR="1",
                     AIA_TEST_OC_LOG=str(self.log), AIA_TEST_OC_MODE=mode),
            capture_output=True, text=True, timeout=90,
        )
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result.returncode, result.stdout + result.stderr, calls

    def test_exported_kubeconfig_is_used_without_password_login(self):
        rc, output, calls = self.run_access()
        self.assertEqual(rc, 0, output)
        self.assertTrue(any("whoami" in args for args in calls), output)
        self.assertTrue(all("--kubeconfig=" + str(self.kubeconfig) in args for args in calls))
        self.assertFalse(any("login" in args for args in calls))
        self.assertFalse(any("synthetic-password" in arg for args in calls for arg in args))

    def test_invalid_token_fails_without_falling_back_to_password(self):
        rc, output, calls = self.run_access(mode="invalid")
        self.assertNotEqual(rc, 0, output)
        self.assertGreater(len(calls), 1)
        self.assertFalse(any("login" in args for args in calls))

    def test_empty_path_cannot_use_the_default_cluster(self):
        rc, output, calls = self.run_access(empty=True)
        self.assertNotEqual(rc, 0, output)
        self.assertEqual(calls, [])
        self.assertIn("empty value must not select", output)

    def test_catalog_without_export_keeps_legacy_login(self):
        rc, output, calls = self.run_access(mode="legacy", exported=False)
        self.assertEqual(rc, 0, output)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "login")
        self.assertIn("--password=synthetic-password", calls[0])


if __name__ == "__main__":
    unittest.main()
