"""Real nested-role execution with job credentials and a mocked Secret API."""
import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

import yaml
from ansible.parsing.vault import VaultLib, VaultSecret

ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = Path(sys.executable).parent / 'ansible-playbook'
GALAXY = Path(sys.executable).parent / 'ansible-galaxy'


@unittest.skipUnless(PLAYBOOK.exists(), 'ansible-playbook required')
class EventKeyHandoff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.password = secrets.token_hex(20)
        self.key = self.path / 'key'
        self.key.write_text(self.password)
        collection = self.path / 'collections/ansible_collections/ansible_island_adventure/automation'
        self.role = collection / 'roles/student_post_provision'
        for directory in ['tasks', 'defaults', 'files']:
            (self.role / directory).mkdir(parents=True, exist_ok=True)
        for filename in ['tasks/unlock_event.yml', 'tasks/main.yml', 'defaults/main.yml']:
            shutil.copy(ROOT / 'roles/student_post_provision' / filename, self.role / filename)
        plugins = collection / 'plugins/lookup'
        plugins.mkdir(parents=True)
        shutil.copy(ROOT / 'plugins/lookup/vault_flags.py', plugins / 'vault_flags.py')
        modules = collection / 'plugins/modules'
        modules.mkdir(parents=True)
        (modules / 'aia_ctfd_flags.py').write_text(
            'from ansible.module_utils.basic import AnsibleModule\n'
            'AnsibleModule(argument_spec={}).fail_json(msg="content changes must not run")\n')
        user_info = self.path / 'collections/ansible_collections/agnosticd/core/plugins/modules/agnosticd_user_info.py'
        user_info.parent.mkdir(parents=True)
        user_info.write_text('from ansible.module_utils.basic import AnsibleModule\n'
                             'AnsibleModule(argument_spec={}).fail_json(msg="publication must not run")\n')
        self.role.joinpath('files/flags.vault').write_bytes(VaultLib([
            ('default', VaultSecret(self.password.encode()))]).encrypt(
                b'ctfd_flags: {answer: synthetic-event-answer}\n'))
        wrapper = collection / 'roles/instructor_config/tasks'
        wrapper.mkdir(parents=True)
        self.wrapper = wrapper / 'main.yml'
        self.wrapper.write_text(yaml.safe_dump([{'ansible.builtin.include_role': {
            'name': 'ansible_island_adventure.automation.student_post_provision',
            'tasks_from': 'unlock_event'}}]))
        fake_module = self.path / 'collections/ansible_collections/kubernetes/core/plugins/modules/k8s_info.py'
        fake_module.parent.mkdir(parents=True)
        fake_module.write_text('''import json, os
from pathlib import Path
from ansible.module_utils.basic import AnsibleModule
m=AnsibleModule(argument_spec={k: {'type':'str'} for k in ['api_version','kind','namespace','name','kubeconfig']})
Path(os.environ['AIA_TEST_TRACE']).write_text('queried')
m.exit_json(changed=False, resources=json.loads(Path(os.environ['AIA_TEST_SECRET']).read_text()))
''')
        self.fixture = self.path / 'secret.json'
        self.fixture.write_text(json.dumps([{'data': {'password': base64.b64encode(self.password.encode()).decode()}}]))
        self.trace = self.path / 'trace'
        self.config = self.path / 'ansible.cfg'
        self.config.write_text('[defaults]\nstdout_callback=default\n')

    def run_play(self, tasks, job_key=False):
        play = self.path / 'test.yml'
        play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                                         'vars': {'ansible_python_interpreter': sys.executable}, 'tasks': tasks}]))
        env = {k: v for k, v in os.environ.items() if not k.startswith('ANSIBLE_VAULT_')}
        env.update(ANSIBLE_CONFIG=str(self.config), ANSIBLE_COLLECTIONS_PATH=str(self.path / 'collections'),
                   AIA_TEST_SECRET=str(self.fixture), AIA_TEST_TRACE=str(self.trace))
        cmd = [str(PLAYBOOK), '-i', 'localhost,', str(play)]
        if job_key:
            cmd += ['--vault-id', 'rhdp-event@' + str(self.key)]
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=40)
        output = result.stdout + result.stderr
        self.assertFalse(self.password in output, 'organizer key appeared in output')
        self.assertFalse(base64.b64encode(self.password.encode()).decode() in output, 'encoded key appeared in output')
        return result.returncode, output

    def unlock(self, job_key=False):
        return self.run_play([
            {'ansible.builtin.include_role': {'name': 'ansible_island_adventure.automation.instructor_config'}},
            {'ansible.builtin.assert': {'that': ["_aia_event_bundle.ctfd_flags.answer == 'synthetic-event-answer'",
                                                "_aia_event_bundle.source_password | length > 0"]}, 'no_log': True},
        ], job_key)

    def test_existing_job_credential_avoids_secret_api(self):
        code, output = self.unlock(job_key=True)
        self.assertEqual(code, 0, output)
        self.assertFalse(self.trace.exists())

    def test_secret_handoff_works_without_a_job_vault_credential(self):
        code, output = self.unlock()
        self.assertEqual(code, 0, output)
        self.assertTrue(self.trace.exists())

    def test_missing_or_wrong_secret_never_provisions_with_unresolved_flags(self):
        for resources in [[], [{'data': {'password': base64.b64encode(b'wrong-key').decode()}}]]:
            with self.subTest(resources_present=bool(resources)):
                self.fixture.write_text(json.dumps(resources))
                code, _ = self.unlock()
                self.assertNotEqual(code, 0)

    def test_main_role_clears_the_key_after_a_later_failure(self):
        self.role.joinpath('tasks/prepare_event.yml').write_text(
            '- ansible.builtin.fail: {msg: synthetic provisioning failure}\n')
        code, output = self.run_play([
            {'block': [{'ansible.builtin.include_role': {
                'name': 'ansible_island_adventure.automation.student_post_provision'}}],
             'rescue': [{'ansible.builtin.set_fact': {'caught_expected_failure': True}}]},
            {'ansible.builtin.assert': {'that': ['caught_expected_failure | default(false)',
                                                '_aia_event_bundle == {}', '_aia_event_vault_secret == {}']},
             'no_log': True},
        ])
        self.assertEqual(code, 0, output)

    def test_built_collection_contains_the_encrypted_event_and_handoff_tasks(self):
        result = subprocess.run([str(GALAXY), 'collection', 'build', str(ROOT), '--output-path', str(self.path)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        archive = next(self.path.glob('*.tar.gz'))
        with tarfile.open(archive) as package:
            name = 'roles/student_post_provision/files/flags.vault'
            self.assertEqual(package.extractfile(name).read(), (ROOT / name).read_bytes())
            self.assertIn('roles/student_post_provision/tasks/unlock_event.yml', package.getnames())
            self.assertFalse(any(x.endswith('.vault-password') for x in package.getnames()))


if __name__ == '__main__':
    unittest.main()
