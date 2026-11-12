"""Offline bootstrap checks: execute the embedded script against SQLite/stub CTFd APIs."""

from contextlib import nullcontext, redirect_stdout
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from jinja2 import Environment, StrictUndefined
import yaml


ROLE = Path(__file__).resolve().parents[1] / "roles/instructor_config"


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT UNIQUE,
                email TEXT UNIQUE, password TEXT, type TEXT, hidden INTEGER,
                banned INTEGER, verified INTEGER);
            CREATE TABLE pages (id INTEGER PRIMARY KEY, title TEXT, route TEXT,
                content TEXT, draft INTEGER, format TEXT);
            CREATE TABLE challenges (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE solves (id INTEGER PRIMARY KEY, user_id INTEGER, challenge_id INTEGER);
        """)
        self.settings = {}
        self.cache_clear = Mock()
        connection = self.db

        class Session:
            def execute(self, sql, parameters=None):
                cursor = connection.execute(sql, parameters or {})
                return SimpleNamespace(scalar=lambda: (cursor.fetchone() or (None,))[0])

            def commit(self):
                connection.commit()

        self.modules = {
            "CTFd": SimpleNamespace(create_app=lambda: SimpleNamespace(app_context=nullcontext)),
            "CTFd.models": SimpleNamespace(db=SimpleNamespace(session=Session())),
            "CTFd.cache": SimpleNamespace(cache=SimpleNamespace(clear=self.cache_clear)),
            "CTFd.utils": SimpleNamespace(get_config=self.settings.get,
                                          set_config=self.settings.__setitem__),
            "CTFd.utils.crypto": SimpleNamespace(
                hash_password=lambda password: "hashed:" + password,
                verify_password=lambda password, hashed: hashed == "hashed:" + password),
            "sqlalchemy": SimpleNamespace(text=lambda sql: sql),
        }
        # Quote/backslash/newline characters must survive template rendering.
        self.password = secrets.token_hex(12) + "'\"\\\n"

    def bootstrap(self):
        task = yaml.safe_load((ROLE / "tasks/ctfd_users.yml").read_text())[0]
        script = task["ansible.builtin.command"]["argv"][-1]
        environment = Environment(undefined=StrictUndefined)
        environment.filters["to_json"] = json.dumps
        script = environment.from_string(script).render(
            ctfd_admin_username="admin", ctfd_admin_password=self.password,
            ctfd_admin_email="admin@example.com", student_name="lab-user",
            common_password=self.password,
        )
        output = io.StringIO()
        with patch.dict(sys.modules, self.modules), redirect_stdout(output):
            exec(compile(script, "ctfd_users.yml", "exec"), {})
        return output.getvalue()

    def test_fresh_instance_has_login_accounts_private_registration_and_no_challenges(self):
        output = self.bootstrap()
        self.assertIn("CONFIGURED:setup", output)
        self.assertEqual(self.db.execute(
            "SELECT name, type, hidden, password FROM users ORDER BY id"
        ).fetchall(), [
            ("admin", "admin", 1, "hashed:" + self.password),
            ("lab-user", "user", 0, "hashed:" + self.password),
        ])
        self.assertTrue(self.settings["setup"])
        self.assertEqual(self.settings["user_mode"], "users")
        self.assertEqual(self.settings["registration_visibility"], "private")
        self.assertIn('/login', self.db.execute(
            "SELECT content FROM pages WHERE route = 'index'"
        ).fetchone()[0])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM challenges").fetchone()[0], 0)

    def test_rerun_preserves_event_settings_pages_and_scores(self):
        self.bootstrap()
        self.settings["ctf_name"] = "Custom event"
        self.db.execute("UPDATE pages SET content = 'Custom landing page'")
        self.db.execute("INSERT INTO challenges VALUES (7, 'Existing challenge')")
        self.db.execute("INSERT INTO solves VALUES (1, 2, 7)")
        self.db.commit()
        before = list(self.db.iterdump())
        self.cache_clear.reset_mock()
        self.assertEqual(self.bootstrap(), "")
        self.assertEqual(list(self.db.iterdump()), before)
        self.assertEqual(self.settings["ctf_name"], "Custom event")
        self.cache_clear.assert_not_called()

    def test_restores_null_passwords_after_archive_import(self):
        self.bootstrap()
        self.db.execute("UPDATE users SET password = NULL")
        self.db.commit()
        output = self.bootstrap()
        self.assertIn("UPDATED:admin", output)
        self.assertIn("UPDATED:lab-user", output)
        self.assertNotIn("CONFIGURED:setup", output)
        self.assertEqual(self.db.execute("SELECT password FROM users").fetchall(),
                         [("hashed:" + self.password,)] * 2)


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook required")
class CatalogPathTests(unittest.TestCase):
    def test_catalog_publishes_access_without_loading_content(self):
        with tempfile.TemporaryDirectory(prefix="aia-bootstrap-test-") as directory:
            root = Path(directory)
            role = root / "roles/instructor_config"
            shutil.copytree(ROLE / "tasks", role / "tasks")
            shutil.copytree(ROLE / "defaults", role / "defaults")
            # Only service interactions are stubbed. Execute the real default
            # task graph, account command, and publication boundary.
            for name in ("oc_login", "namespaces", "ctfd_install", "gitea",
                         "keycloak", "aap", "ctfd_ready"):
                (role / f"tasks/{name}.yml").write_text(yaml.safe_dump([
                    {"ansible.builtin.debug": {"msg": "stub service operation"}},
                ]))
            (role / "tasks/ctfd_config.yml").write_text(yaml.safe_dump([
                {"ansible.builtin.fail": {"msg": "Content loading reached during catalog provisioning"}},
            ]))
            bootstrap = role / "tasks/ctfd_bootstrap.yml"
            tasks = yaml.safe_load(bootstrap.read_text())
            publish = tasks[-1].pop("agnosticd.core.agnosticd_user_info")
            tasks[-1]["ansible.builtin.debug"] = {"msg": "PUBLISHED:" + publish["data"]["ctfd_url"]}
            bootstrap.write_text(yaml.safe_dump(tasks))
            oc = root / "oc"
            oc.write_text("#!/bin/sh\necho CREATED:admin\n")
            oc.chmod(0o755)
            config = root / "ansible.cfg"
            config.write_text("[defaults]\nstdout_callback=default\n")
            playbook = root / "bootstrap.yml"
            playbook.write_text(yaml.safe_dump([{
                "hosts": "localhost", "connection": "local", "gather_facts": False,
                "vars": {
                    "guid": "test", "openshift_cluster_common_password": secrets.token_hex(12),
                    "openshift_cluster_ingress_domain": "apps.example.invalid",
                    "ansible_python_interpreter": "/usr/bin/python3",
                    "ansible_env": {"PATH": str(root) + os.pathsep + os.environ["PATH"]},
                },
                "roles": ["instructor_config"],
            }]))
            result = subprocess.run(
                ["ansible-playbook", "-i", "localhost,", str(playbook)],
                env=dict(os.environ, ANSIBLE_CONFIG=str(config), ANSIBLE_NOCOLOR="1"),
                capture_output=True, text=True, timeout=30,
            )
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("PUBLISHED:https://island-instructor-test-ctfd.apps.example.invalid", output)


if __name__ == "__main__":
    unittest.main()
