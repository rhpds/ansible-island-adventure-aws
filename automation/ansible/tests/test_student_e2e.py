"""Offline safety, coverage, cleanup and playbook tests for the per-student journey."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
QA = ROOT / 'qa-automation'
sys.path.insert(0, str(QA))
spec = importlib.util.spec_from_file_location('student_e2e', QA / 'student_e2e.py')
e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2e)


def data(guid='test'):
    domain = 'apps.example.test'
    user = 'instructor-' + guid
    return dict(guid=guid, openshift_cluster_ingress_domain=domain,
                gitea_username=user, aap_username=user, ocp_username=user,
                gitea_url='https://gitea.' + domain, aap_url='https://aap.' + domain,
                ctfd_url='https://island-' + user + '-ctfd.' + domain,
                devspaces_url='https://devspaces.' + domain,
                gitea_password='synthetic-secret', aap_password='synthetic-secret',
                ocp_password='synthetic-secret', lab_password='synthetic-secret',
                ctfd_admin_username='admin', ctfd_admin_password='synthetic-secret')


# Stub only the external systems: exercise orchestration and gating remain real.
READ_ACTIONS = ('repositories', 'vault', 'content', 'student_openshift', 'workspace_readiness', 'aap_inventory')
EXERCISE_ACTIONS = ('workspace_tools', 'create_player', 'documentation_challenges', 'cli_exercises',
                    'versions', 'aap_project', 'treasure_hello', 'treasure_secret', 'treasure_web',
                    'toolkit', 'reset_board', 'update_board', 'metrics')


class JourneyTests(unittest.TestCase):
    def stub_actions(self, actions):
        mocks = {}
        for action in actions:
            p = patch.object(e2e.Journey, action)
            mocks[action] = p.start()
            self.addCleanup(p.stop)
        return mocks

    def test_modifying_modes_require_student_specific_confirmations(self):
        for mode, confirm, reset in [('exercise', '', ''), ('exercise', 'other', ''),
                                     ('full', 'test', ''), ('full', 'test', 'other'), ('unknown', '', '')]:
            with self.subTest(mode=mode, confirm=confirm, reset=reset):
                with self.assertRaises(e2e.E2EError):
                    e2e.Journey(data(), mode, confirm, reset)
        e2e.Journey(data(), 'exercise', 'test')
        e2e.Journey(data(), 'full', 'test', 'test')

    def test_admin_or_wrong_student_endpoints_are_rejected(self):
        for key, value in [('ocp_username', 'admin'), ('aap_username', 'instructor-other'),
                           ('ctfd_url', 'https://island-instructor-other-ctfd.apps.example.test'),
                           ('gitea_url', 'https://gitea.other.test')]:
            d = data()
            d[key] = value
            with self.subTest(key=key), self.assertRaises(e2e.E2EError):
                e2e.Journey(d)

    def test_inspect_never_calls_mutating_or_workspace_exec_actions(self):
        self.stub_actions(READ_ACTIONS)
        forbidden = self.stub_actions(EXERCISE_ACTIONS)
        report = e2e.Journey(data()).run()
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['coverage'], 'partial')
        self.assertEqual(report['checks']['reset']['status'], 'skipped')
        self.assertEqual(report['checks']['cli']['status'], 'skipped')
        for mock in forbidden.values():
            mock.assert_not_called()
        self.assertNotIn('synthetic-secret', json.dumps(report))

    def test_missing_workspaces_blocks_exercise_without_creating_player_or_aap_objects(self):
        actions = self.stub_actions(READ_ACTIONS + EXERCISE_ACTIONS)
        actions['workspace_readiness'].side_effect = e2e.Blocked('Open both workspaces first')
        report = e2e.Journey(data(), 'exercise', 'test').run()
        self.assertEqual(report['status'], 'failed')
        for key in ('workspaces', 'tools', 'player', 'cli', 'project', 'web'):
            self.assertEqual(report['checks'][key]['status'], 'blocked')
        actions['create_player'].assert_not_called()
        actions['aap_project'].assert_not_called()
        actions['reset_board'].assert_not_called()

    def test_exercise_runs_real_stages_but_never_board_lifecycle(self):
        actions = self.stub_actions(READ_ACTIONS + EXERCISE_ACTIONS)
        report = e2e.Journey(data(), 'exercise', 'test').run()
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['coverage'], 'partial')
        actions['treasure_web'].assert_called_once()
        actions['reset_board'].assert_not_called()
        actions['update_board'].assert_not_called()
        self.assertEqual([c.args[0] for c in actions['toolkit'].call_args_list], ['preview', 'handouts'])

    def test_full_mode_exercises_reset_and_update_and_reports_coverage(self):
        actions = self.stub_actions(READ_ACTIONS + EXERCISE_ACTIONS)
        report = e2e.Journey(data(), 'full', 'test', 'test').run()
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['coverage'], 'full-automated-journey')
        actions['reset_board'].assert_called_once()
        actions['update_board'].assert_called_once()
        self.assertTrue(all(c['status'] == 'passed' for c in report['checks'].values()))

    def test_errors_do_not_leak_answers_or_prevent_cleanup(self):
        actions = self.stub_actions(READ_ACTIONS + EXERCISE_ACTIONS)
        actions['treasure_secret'].side_effect = RuntimeError('synthetic-secret token password')
        with patch.object(e2e.Journey, 'cleanup') as cleanup:
            report = e2e.Journey(data(), 'exercise', 'test').run()
        cleanup.assert_called_once()
        self.assertEqual(report['checks']['secret']['error'], 'RuntimeError')
        self.assertEqual(report['checks']['web']['status'], 'blocked')
        self.assertNotIn('synthetic-secret', json.dumps(report))

    def test_cleanup_deletes_only_recorded_ids_in_reverse_dependency_order(self):
        journey = e2e.Journey(data())
        journey.aap = MagicMock()
        journey.admin = MagicMock()
        journey.artifacts = [('credentials', 7), ('projects', 8), ('job_templates', 9)]
        journey.player_id = 10
        journey.cleanup()
        self.assertEqual([c.args for c in journey.aap.api.call_args_list],
                         [('DELETE', 'job_templates/9/'), ('DELETE', 'projects/8/'), ('DELETE', 'credentials/7/')])
        journey.admin.api.assert_called_once_with('DELETE', 'users/10')

    def test_unconfirmed_active_job_retains_aap_resources_and_fails_cleanup(self):
        journey = e2e.Journey(data())
        journey.aap = MagicMock()
        journey.artifacts = [('projects', 8)]
        journey.active_job_uncertain = True
        with self.assertRaises(e2e.E2EError):
            journey.cleanup()
        journey.aap.api.assert_not_called()

    def test_cleanup_failure_fails_the_run(self):
        self.stub_actions(READ_ACTIONS)
        with patch.object(e2e.Journey, 'cleanup', side_effect=e2e.E2EError('Cleanup incomplete')):
            report = e2e.Journey(data()).run()
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['checks']['cleanup']['status'], 'failed')

    def test_command_failure_never_includes_private_output(self):
        with patch.object(e2e.subprocess, 'run', return_value=MagicMock(returncode=1, stdout='synthetic-secret', stderr='synthetic-token')):
            with self.assertRaises(e2e.E2EError) as error:
                e2e.command(['oc', 'get', 'pods'])
        self.assertNotIn('synthetic-', str(error.exception))

    def test_vault_decrypts_with_personal_password_without_reporting_answers(self):
        from ansible.parsing.vault import VaultLib, VaultSecret
        flags = {key: 'synthetic-answer' for key in ('write_your_name', 'unlock_the_island', 'hello_world_beach',
                 'message_in_a_bottle', 'hello_world_treasure', 'secret_message_treasure', 'treasure_chest')}
        ciphertext = VaultLib([('default', VaultSecret(b'synthetic-secret'))]).encrypt(
            json.dumps({'ctfd_flags': flags}).encode())
        journey = e2e.Journey(data())
        with patch.object(journey, 'repository_file', return_value=ciphertext):
            journey.step('vault', 'Personal answer Vault', journey.vault)
        self.assertEqual(journey.report['checks']['vault']['status'], 'passed')
        self.assertEqual(journey.flags, flags)
        self.assertNotIn('synthetic-answer', json.dumps(journey.report))

    def test_workspace_discovery_is_student_owned_and_rejects_shared_pvc(self):
        journey = e2e.Journey(data())
        journey.devfiles = {'ansible-survival-kit': 'ansible-tools', 'aia-instructor': 'ansible-tools'}
        calls = []
        def result(args):
            calls.append(args)
            if args[1] == 'projects':
                return {'items': [
                    {'metadata': {'name': 'owned', 'annotations': {'openshift.io/requester': 'instructor-test'}}},
                    {'metadata': {'name': 'other', 'annotations': {'openshift.io/requester': 'instructor-other'}}}]}
            if args[1] == 'devworkspaces.workspace.devfile.io':
                return {'items': [dict(metadata={'annotations': {}}, status={'phase': 'Running', 'devworkspaceId': repo},
                    spec={'template': {'projects': [{'git': {'remotes': {'origin': journey.urls['gitea_url'] + '/instructor-test/' + repo + '.git'}}}]}})
                    for repo in journey.devfiles]}
            if args[1] == 'pods':
                return {'items': [{'metadata': {'name': 'developer'}, 'status': {'phase': 'Running', 'containerStatuses': [{'ready': True}]},
                                  'spec': {'containers': [{'name': 'ansible-tools'}], 'volumes': [{'persistentVolumeClaim': {'claimName': 'shared'}}]}}]}
            if args[1] == 'pvc':
                return {'status': {'phase': 'Bound'}}
            self.fail('Unexpected request')
        with patch.object(journey, 'oc_json', side_effect=result):
            with self.assertRaisesRegex(e2e.E2EError, 'separate persistent PVCs'):
                journey.workspace_readiness()
        self.assertTrue(all('other' not in args for args in calls))

    def test_aap_template_launch_uses_only_personal_inventory_and_records_job_url(self):
        journey = e2e.Journey(data())
        journey.project = 11
        journey.aap_resources = {'inventory': 12, 'execution_environment': 13}
        journey.aap = MagicMock()
        journey.aap.base = 'https://aap.apps.example.test/api/controller/v2/'
        journey.aap.api.side_effect = [dict(id=14, name='generated'), {}, {}, dict(id=15), {'content': 'private-answer'}]
        with patch.object(journey, 'wait_job') as wait:
            stdout = journey.aap_job('Deploy', 'apache/apache_role_install.yml', [16, 17], True)
        self.assertEqual(stdout, 'private-answer')
        body = journey.aap.api.call_args_list[0].args[2]
        self.assertEqual(body['inventory'], 12)
        self.assertTrue(body['become_enabled'])
        self.assertEqual(journey.artifacts, [('job_templates', 14)])
        wait.assert_called_once_with('jobs', 15)
        self.assertNotIn('private-answer', json.dumps(journey.report))

    def test_web_route_must_match_student_and_require_successful_http(self):
        journey = e2e.Journey(data())
        journey.aap_resources = {'credential': 1}
        journey.vault_credential = 2
        journey.data['managed_node_web_url'] = 'https://treasure-other.apps.example.test'
        with patch.object(journey, 'aap_job'), self.assertRaises(e2e.E2EError):
            journey.treasure_web()


@unittest.skipUnless(shutil.which('ansible-playbook'), 'Ansible required')
class E2EPlaybookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        shutil.copy(QA / 'student_e2e.yml', self.root / 'student_e2e.yml')
        (self.root / 'student_e2e.py').write_text('''import json,sys
p=json.load(sys.stdin)
assert p['data']['ocp_username']=='instructor-test'
status='blocked' if p['data'].get('lab_password')=='blocked-test' else 'passed'
print(json.dumps(dict(id='fixture',status='failed' if status=='blocked' else 'passed',coverage='partial',checks={'workspace':dict(title='Two running repository workspaces',status=status)})))
''')
        self.env = dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(self.root / 'vault'), ANSIBLE_NOCOLOR='1')
        (self.root / 'vault').write_text('unused-test-vault\n')
        self.report_dir = self.root / 'reports'
        self.variables = dict(guid='test', gitea_password='synthetic-secret', devspaces_url='https://devspaces.apps.example.test',
                              aia_healthcheck_inputs=data(), aia_e2e_report_dir=str(self.report_dir))

    def run_play(self, **extra):
        variables = dict(self.variables, **extra)
        path = self.root / 'vars.json'
        path.write_text(json.dumps(variables))
        return subprocess.run(['ansible-playbook', '-i', 'localhost,', str(self.root / 'student_e2e.yml'),
                               '-e', '@' + str(path)], env=self.env, text=True, capture_output=True, timeout=60)

    def test_inspect_prints_partial_summary_and_private_report(self):
        result = self.run_play()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('STUDENT E2E SUMMARY', result.stdout)
        self.assertIn('This is NOT a full learner-journey pass', result.stdout)
        self.assertIn('Coverage: partial', result.stdout)
        report = self.report_dir / 'test.fixture.e2e.json'
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('synthetic-secret', report.read_text() + result.stdout + result.stderr)

    def test_blocked_stage_fails_but_still_prints_final_summary(self):
        result = self.run_play(aia_lab_password='blocked-test')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('STUDENT E2E SUMMARY', result.stdout)
        self.assertIn('BLOCKED', result.stdout)
        self.assertTrue((self.report_dir / 'test.fixture.e2e.json').exists())

    def test_unconfirmed_exercise_is_rejected_before_execution(self):
        result = self.run_play(aia_e2e_mode='exercise')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.report_dir.exists())

    def test_full_requires_both_guid_confirmations(self):
        result = self.run_play(aia_e2e_mode='full', aia_e2e_confirm_guid='test')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.report_dir.exists())


if __name__ == '__main__':
    unittest.main()
