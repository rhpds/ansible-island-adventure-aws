"""Student health checks: isolated logins, sanitized failures and all-student gating."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
COLLECTION = ROOT / 'automation/ansible'
spec = importlib.util.spec_from_file_location('student_health', COLLECTION / 'plugins/modules/aia_student_health.py')
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


def fields(guid='one'):
    domain = 'apps.example.test'
    user = 'instructor-' + guid
    return dict(guid=guid, apps_domain=domain, aap_username=user, gitea_username=user,
                keycloak_username=user, aap_password='synthetic-secret', gitea_password='synthetic-secret',
                aap_url='https://aap.' + domain, gitea_url='https://gitea.' + domain,
                ctfd_url='https://island-' + user + '-ctfd.' + domain,
                devspaces_url='https://devspaces.' + domain, keycloak_url='https://sso.' + domain,
                keycloak_realm='sso', ctfd_student_username='lab-user', ctfd_student_password='synthetic-secret',
                ctfd_admin_username='admin', ctfd_admin_password='synthetic-secret',
                bastion_public_hostname='ec2.example.test', bastion_ssh_port='22',
                bastion_ssh_user_name='lab-user', bastion_ssh_password='synthetic-secret')


class ServiceTests(unittest.TestCase):
    def test_nonce_attribute_order(self):
        parser = health.NonceParser()
        parser.feed("<input value='abc' type='hidden' name='nonce'>")
        self.assertEqual(parser.nonce, 'abc')

    def test_redirect_is_not_proof_of_login(self):
        with patch.object(health, 'request') as request:
            request.side_effect = [MagicMock(text='<input name="nonce" value="abc">'),
                                   MagicMock(), health.HealthError('Unexpected HTTP status: 302')]
            with self.assertRaises(health.HealthError):
                health.ctfd_login('https://ctfd.example.test', 'lab-user', 'bad', False, True)
            self.assertTrue(request.call_args.args[2].endswith('/settings'))

    def test_all_checks_run_and_exception_details_are_not_leaked(self):
        with patch.object(health, 'request', side_effect=RuntimeError('synthetic-secret')), \
             patch.object(health, 'ctfd_login', side_effect=RuntimeError('synthetic-secret')), \
             patch('paramiko.SSHClient') as ssh:
            ssh.return_value.connect.side_effect = RuntimeError('synthetic-secret')
            report = health.measure(fields())
        self.assertEqual(len(report['checks']), 6)
        self.assertEqual(report['status'], 'failed')
        self.assertNotIn('synthetic-secret', json.dumps(report))

    def test_success_and_wrong_aws_hostname(self):
        data = fields()
        def response(session, method, url, **kwargs):
            payload = {}
            if url.endswith('/api/v1/user'):
                payload = {'login': data['gitea_username']}
            elif '/api/v1/repos/' in url:
                payload = {'owner': {'login': data['gitea_username']}}
            elif url.endswith('/.well-known/openid-configuration'):
                payload = {'issuer': data['keycloak_url'] + '/realms/sso'}
            return MagicMock(json=lambda: payload)
        with patch.object(health, 'request', side_effect=response), \
             patch.object(health, 'ctfd_login') as login, patch('paramiko.SSHClient') as ssh:
            output = MagicMock()
            output.read.return_value = b'rhel.one.internal\n'
            output.channel.recv_exit_status.return_value = 0
            ssh.return_value.exec_command.return_value = (None, output, None)
            report = health.measure(data)
            self.assertEqual(report['status'], 'passed')
            self.assertEqual(login.call_count, 2)
            self.assertEqual(login.call_args_list[0].args[1], 'lab-user')
            self.assertEqual(login.call_args_list[1].args[1], 'admin')
            output.read.return_value = b'rhel.other.internal\n'
            report = health.measure(data)
            self.assertEqual(report['checks']['ssh_identity_and_sudo']['status'], 'failed')

    def test_wrong_student_or_leader_is_rejected_before_requests(self):
        for key, value in [('aap_username', 'instructor-other'),
                           ('ctfd_url', 'https://island-instructor-other-ctfd.apps.example.test'),
                           ('gitea_url', 'https://gitea.other.example.test'),
                           ('aap_url', 'https://user:secret@aap.apps.example.test')]:
            with self.subTest(key=key), patch.object(health, 'request') as request:
                data = fields()
                data[key] = value
                with self.assertRaises(health.HealthError):
                    health.measure(data)
                request.assert_not_called()


@unittest.skipUnless(shutil.which('ansible-playbook'), 'Ansible required')
class PlaybookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        shutil.copy(COLLECTION / 'healthcheck.yml', self.root / 'healthcheck.yml')
        self.playbook = self.root / 'healthcheck.yml'
        library = self.root / 'library'
        library.mkdir()
        (library / 'aia_student_health.py').write_text('''from ansible.module_utils.basic import AnsibleModule
m = AnsibleModule(argument_spec=dict(data=dict(type='dict'), validate_certs=dict(type='bool')), supports_check_mode=True)
if m.check_mode: m.exit_json(changed=False, skipped=True)
d = m.params['data']
assert d['aap_username'] == 'instructor-' + d['guid']
assert d['bastion_ssh_password'] == 'synthetic-secret'
m.exit_json(changed=False, report=dict(id='sample', status='failed' if d['guid']=='one' else 'passed'))
''')
        (library / 'aia_connectivity.py').write_text('''from ansible.module_utils.basic import AnsibleModule
m = AnsibleModule(argument_spec=dict(url={}, username={}, password=dict(no_log=True), guid={}, node=dict(type='dict'), endpoints=dict(type='list'), managed_web_url={}, validate_certs=dict(type='bool'), connect_timeout=dict(type='float'), job_timeout=dict(type='int')), supports_check_mode=True)
if m.check_mode: m.exit_json(changed=False, skipped=True)
m.exit_json(changed=False, report=dict(id='runtime', status='passed'))
''')
        self.env = dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(self.root / 'vault'),
                        ANSIBLE_NOCOLOR='1', ANSIBLE_LOCALHOST_WARNING='False')
        (self.root / 'vault').write_text('unused-test-password\n')
        self.reports = self.root / 'reports'

    def run_play(self, *args):
        return subprocess.run(['ansible-playbook', str(self.playbook),
                               '-e', 'aia_healthcheck_report_dir=' + str(self.reports), *args],
                              env=self.env, text=True, capture_output=True, timeout=60)

    def test_flat_single_student_inputs(self):
        data = fields('two')
        path = self.root / 'student.json'
        path.write_text(json.dumps(data))
        result = self.run_play('-i', 'localhost,', '-e', '@' + str(path))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(list(self.reports.glob('*.health.json'))), 1)
        self.assertNotIn('synthetic-secret', result.stdout + result.stderr)

    def test_showroom_loads_only_its_mounted_student_data(self):
        wrapper = (ROOT / 'qa-automation/healthcheck.yml').read_text()
        wrapper = wrapper.replace('../automation/ansible/healthcheck.yml', 'healthcheck.yml')
        self.playbook = self.root / 'showroom.yml'
        self.playbook.write_text(wrapper)
        user_data = self.root / 'user_data.yml'
        user_data.write_text(json.dumps(fields('two')))
        result = self.run_play('-i', 'localhost,', '-e', 'aia_healthcheck_user_data_file=' + str(user_data))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.reports / 'two.sample.health.json').exists())
        self.assertNotIn('synthetic-secret', result.stdout + result.stderr)

    def exported_inventory(self):
        hosts = {}
        for guid in ('one', 'two'):
            d = fields(guid)
            d['openshift_cluster_ingress_domain'] = d.pop('apps_domain')
            d['aia_student'] = dict(aap_username=d.pop('aap_username'))
            d['aia_managed_node'] = dict(host=d.pop('bastion_public_hostname'),
                                        port=d.pop('bastion_ssh_port'), username=d.pop('bastion_ssh_user_name'),
                                        password=d.pop('bastion_ssh_password'))
            d['student_name'] = d.pop('ctfd_student_username')
            d['common_password'] = d.pop('ctfd_student_password')
            # Exporter does not publish Keycloak fields; exercise defaults.
            for key in ('keycloak_url', 'keycloak_username', 'keycloak_realm'):
                d.pop(key)
            hosts['instructor-' + guid] = d
        inventory = self.root / 'inventory.json'
        inventory.write_text(json.dumps(dict(all=dict(children=dict(aia_students=dict(hosts=hosts))))))
        return inventory

    def test_only_selected_exported_student_is_sampled(self):
        inventory = self.exported_inventory()
        result = self.run_play('-i', str(inventory), '-e', 'aia_healthcheck_hosts=instructor-two')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path = self.reports / 'two.sample.health.json'
        self.assertTrue(path.exists())
        self.assertFalse((self.reports / 'one.sample.health.json').exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('synthetic-secret', path.read_text())
        self.assertEqual(self.reports.stat().st_mode & 0o777, 0o700)

    def test_multi_student_target_is_rejected(self):
        inventory = self.exported_inventory()
        result = self.run_play('-i', str(inventory), '-e', 'aia_healthcheck_hosts=aia_students')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.reports.exists())

    def test_missing_student_inputs_are_not_success(self):
        result = self.run_play('-i', 'localhost,')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.reports.exists())

    def test_check_mode_produces_no_health_evidence(self):
        data = fields('two')
        data['aia_healthcheck_hosts'] = 'localhost'
        path = self.root / 'student.json'
        path.write_text(json.dumps(data))
        result = self.run_play('-i', 'localhost,', '-e', '@' + str(path), '--check')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.reports.exists())


if __name__ == '__main__':
    unittest.main()
