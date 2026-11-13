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
    def test_showroom_bundle_matches_collection_sources(self):
        self.assertEqual((ROOT / 'qa-automation/student_healthcheck.yml').read_bytes(),
                         (COLLECTION / 'healthcheck.yml').read_bytes())
        for name in ('aia_connectivity.py', 'aia_student_health.py'):
            self.assertEqual((ROOT / 'qa-automation/library' / name).read_bytes(),
                             (COLLECTION / 'plugins/modules' / name).read_bytes())

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

    def test_admin_dashboard_redirect_is_scoped_and_must_finish_successfully(self):
        for location, allowed in [('/admin/statistics', True), ('/login', False),
                                  ('https://other.example.test/admin/statistics', False)]:
            with self.subTest(location=location), patch.object(health, 'request') as request:
                request.side_effect = [
                    MagicMock(text='<input name="nonce" value="abc">'),
                    MagicMock(status_code=302),
                    MagicMock(status_code=302, headers={'Location': location}),
                    MagicMock(status_code=200),
                    MagicMock(json=lambda: {'success': True, 'data': []}),
                ]
                if allowed:
                    health.ctfd_login('https://ctfd.example.test', 'admin', 'synthetic-secret', True, True)
                    self.assertTrue(request.call_args_list[3].args[2].endswith('/admin/statistics'))
                else:
                    with self.assertRaises(health.HealthError):
                        health.ctfd_login('https://ctfd.example.test', 'admin', 'synthetic-secret', True, True)
                    self.assertEqual(request.call_count, 3)

    def test_all_checks_run_and_exception_details_are_not_leaked(self):
        with patch.object(health, 'request', side_effect=RuntimeError('synthetic-secret')), \
             patch.object(health, 'ctfd_login', side_effect=RuntimeError('synthetic-secret')), \
             patch('paramiko.SSHClient') as ssh:
            ssh.return_value.connect.side_effect = RuntimeError('synthetic-secret')
            report = health.measure(fields())
        self.assertEqual(len(report['checks']), 7)
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
             patch.object(health, 'openshift_login'), \
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

    def test_ocp_login_uses_student_password_then_student_bearer_token(self):
        def response(session, method, url, **kwargs):
            if url.endswith('/oauth/authorize'):
                return MagicMock(headers={'Location': 'https://oauth-openshift.apps.example.test/oauth/token/implicit'
                                          + '#access_token=synthetic-token&state=' + kwargs['params']['state']})
            return MagicMock(json=lambda: {'kind': 'User', 'metadata': {'name': 'instructor-one'}})
        with patch.object(health, 'request', side_effect=response) as request:
            health.openshift_login('apps.example.test', 'instructor-one', 'synthetic-password',
                                   api_url='https://api.example.test:6443', ca_file='/private/cluster-ca.crt')
        self.assertEqual(request.call_count, 2)
        auth_call, api_call = request.call_args_list
        self.assertEqual(auth_call.kwargs['auth'], ('instructor-one', 'synthetic-password'))
        self.assertEqual(api_call.kwargs['headers'], {'Authorization': 'Bearer synthetic-token'})
        self.assertNotIn('auth', api_call.kwargs)
        self.assertEqual(api_call.kwargs['verify'], '/private/cluster-ca.crt')
        self.assertTrue(api_call.args[2].endswith('/apis/user.openshift.io/v1/users/~'))

    def test_ocp_login_rejects_bad_oauth_and_wrong_identity(self):
        for failure in ('missing-token', 'wrong-state', 'external-redirect', 'wrong-user'):
            with self.subTest(failure=failure):
                def response(session, method, url, **kwargs):
                    if url.endswith('/oauth/authorize'):
                        host = 'other.example.test' if failure == 'external-redirect' else 'oauth-openshift.apps.example.test'
                        state = 'wrong' if failure == 'wrong-state' else kwargs['params']['state']
                        token = '' if failure == 'missing-token' else 'access_token=synthetic-token&'
                        return MagicMock(headers={'Location': 'https://' + host + '/oauth/token/implicit#' + token + 'state=' + state})
                    return MagicMock(json=lambda: {'kind': 'User', 'metadata': {'name': 'admin'}})
                with patch.object(health, 'request', side_effect=response) as request:
                    with self.assertRaises(health.HealthError) as error:
                        health.openshift_login('apps.example.test', 'instructor-one', 'synthetic-password',
                                               api_url='https://api.example.test:6443')
                    self.assertNotIn('synthetic-token', str(error.exception))
                    self.assertEqual(request.call_count, 2 if failure == 'wrong-user' else 1)

    def test_ocp_login_uses_mounted_ca_but_not_service_account_credentials(self):
        def response(session, method, url, **kwargs):
            if url.endswith('/oauth/authorize'):
                return MagicMock(headers={'Location': 'https://oauth-openshift.apps.example.test/oauth/token/implicit'
                                          + '#access_token=student-token&state=' + kwargs['params']['state']})
            return MagicMock(json=lambda: {'kind': 'User', 'metadata': {'name': 'instructor-one'}})
        with patch.object(health.Path, 'is_file', return_value=True), \
             patch.object(health, 'request', side_effect=response) as request:
            health.openshift_login('apps.example.test', 'instructor-one', 'synthetic-password')
        api_call = request.call_args_list[1]
        self.assertTrue(api_call.args[2].startswith('https://kubernetes.default.svc/'))
        self.assertEqual(api_call.kwargs['verify'], '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt')
        self.assertEqual(api_call.kwargs['headers']['Authorization'], 'Bearer student-token')

    def test_wrong_student_or_leader_is_rejected_before_requests(self):
        for key, value in [('ocp_username', 'admin'), ('aap_username', 'instructor-other'),
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
status = 'failed' if d['guid']=='one' else 'passed'
checks = {key: dict(status='passed') for key in ('ocp_student_login', 'gitea_login_and_repositories', 'ctfd_student_login', 'ctfd_admin_login', 'devspaces_route', 'keycloak_realm', 'ssh_identity_and_sudo')}
checks['ocp_student_login']['status'] = status
m.exit_json(changed=False, report=dict(id='sample', status=status, checks=checks))
''')
        (library / 'aia_connectivity.py').write_text('''from ansible.module_utils.basic import AnsibleModule
m = AnsibleModule(argument_spec=dict(url={}, username={}, password=dict(no_log=True), guid={}, node=dict(type='dict'), endpoints=dict(type='list'), managed_web_url={}, validate_certs=dict(type='bool'), connect_timeout=dict(type='float'), job_timeout=dict(type='int')), supports_check_mode=True)
if m.check_mode: m.exit_json(changed=False, skipped=True)
m.exit_json(changed=False, report=dict(id='runtime', status='passed', sources={key: dict(status='passed') for key in ('control', 'aap', 'aap_ssh', 'managed_node')}))
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
        self.assertIn('Total: 11 | Passed: 11 | Failed: 0 | Blocked: 0', result.stdout)
        self.assertIn('Overall: PASS', result.stdout)
        self.assertIn('OpenShift student login', result.stdout)
        self.assertNotIn('synthetic-secret', result.stdout + result.stderr)

    def test_final_summary_prints_after_failed_health_gate(self):
        data = fields('one')
        path = self.root / 'student.json'
        path.write_text(json.dumps(data))
        result = self.run_play('-i', 'localhost,', '-e', '@' + str(path))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Total: 11 | Passed: 10 | Failed: 1 | Blocked: 0', result.stdout)
        self.assertIn('Overall: FAIL', result.stdout)
        self.assertGreater(result.stdout.index('TASK [Final student health-check summary]'),
                           result.stdout.index('TASK [Require service and fresh runtime checks to pass]'))
        self.assertNotIn('synthetic-secret', result.stdout + result.stderr)

    def test_showroom_loads_only_its_mounted_student_data(self):
        wrapper = (ROOT / 'qa-automation/healthcheck.yml').read_text()
        wrapper = wrapper.replace('student_healthcheck.yml', 'healthcheck.yml')
        self.playbook = self.root / 'showroom.yml'
        self.playbook.write_text(wrapper)
        user_data = self.root / 'user_data.yml'
        user_data.write_text(json.dumps(fields('two')))
        result = self.run_play('-i', 'localhost,', '-e', 'aia_healthcheck_user_data_file=' + str(user_data))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.reports / 'two.sample.health.json').exists())
        self.assertNotIn('synthetic-secret', result.stdout + result.stderr)

    def test_isolated_showroom_bundle_needs_no_sibling_directories(self):
        bundle = self.root / 'isolated-qa'
        shutil.copytree(ROOT / 'qa-automation', bundle)
        self.playbook = bundle / 'healthcheck.yml'
        user_data = self.root / 'user_data.yml'
        user_data.write_text(json.dumps(fields('two')))
        result = self.run_play('-i', 'localhost,', '--check', '-e',
                               'aia_healthcheck_user_data_file=' + str(user_data))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.reports.exists())

    def test_isolated_showroom_e2e_entrypoint_check_mode(self):
        bundle = self.root / 'isolated-qa'
        shutil.copytree(ROOT / 'qa-automation', bundle)
        self.playbook = bundle / 'e2e.yml'
        user_data = self.root / 'user_data.yml'
        user_data.write_text(json.dumps(fields('two')))
        result = self.run_play('-i', 'localhost,', '--check', '-e',
                               'aia_healthcheck_user_data_file=' + str(user_data))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('STUDENT E2E SUMMARY: SKIPPED', result.stdout)
        self.assertFalse(self.reports.exists())

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
        self.assertIn('SKIPPED (check mode); no probes were run.', result.stdout)
        self.assertNotIn('Overall: PASS', result.stdout)


if __name__ == '__main__':
    unittest.main()
