"""Exercise the real credential roles and create-only API race handling."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

import yaml
from ansible.parsing.vault import VaultLib, VaultSecret

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('event_secret', ROOT / 'plugins/modules/aia_event_secret.py')
secret_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(secret_module)
PLAYBOOK = Path(sys.executable).parent / 'ansible-playbook'


class ApiError(Exception):
    def __init__(self, status): self.status = status


class CreateOnlySecretTests(unittest.TestCase):
    def setUp(self):
        self.password = secrets.token_hex(20)
        self.stored = {'metadata': {'uid': 'stable-uid'}, 'data': {
            'password': base64.b64encode(self.password.encode()).decode()}}
        self.api = Mock(); self.api.get.return_value = self.stored
        self.api.create.return_value = self.stored

    def ensure(self, **kwargs):
        return secret_module.ensure_secret(self.api, 'aia-automation', 'aia-event-vault',
                                           'password', self.password, **kwargs)

    def test_create_and_idempotent_rerun_return_no_secret_data(self):
        self.api.get.side_effect = ApiError(404)
        self.assertEqual(self.ensure(), {'changed': True, 'uid': 'stable-uid'})
        self.api.get.side_effect = None
        self.assertEqual(self.ensure(), {'changed': False, 'uid': 'stable-uid'})
        self.assertEqual(self.api.create.call_count, 1)
        self.api.patch.assert_not_called(); self.api.replace.assert_not_called()

    def test_concurrent_create_accepts_same_key_and_refuses_different_key(self):
        self.api.create.side_effect = ApiError(409)
        self.api.get.side_effect = [ApiError(404), self.stored]
        self.assertFalse(self.ensure()['changed'])
        self.stored['data']['password'] = base64.b64encode(b'different-test-key').decode()
        self.api.get.side_effect = [ApiError(404), self.stored]
        with self.assertRaises(secret_module.CredentialError): self.ensure()
        self.api.patch.assert_not_called(); self.api.replace.assert_not_called()

    def test_invalid_existing_key_is_never_repaired_or_rotated(self):
        self.stored['data'] = {}
        with self.assertRaises(secret_module.CredentialError): self.ensure()
        self.api.create.assert_not_called()

    def test_api_failure_never_echoes_the_response(self):
        self.api.get.side_effect = RuntimeError(self.password)
        with self.assertRaises(secret_module.CredentialError) as error: self.ensure()
        self.assertNotIn(self.password, str(error.exception))

    def test_check_mode_never_posts_a_secret(self):
        self.api.get.side_effect = ApiError(404)
        self.assertEqual(self.ensure(check_mode=True), {'changed': True})
        self.api.create.assert_not_called()


@unittest.skipUnless(PLAYBOOK.exists(), 'ansible-playbook required')
class LeaderboardCredentialRoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.password = secrets.token_hex(20)
        self.key = self.path / 'key'; self.key.write_text(self.password)
        collection = self.path / 'collections/ansible_collections/ansible_island_adventure/automation'
        shutil.copytree(ROOT / 'roles/leaderboard_event_credentials', collection / 'roles/leaderboard_event_credentials')
        shutil.copytree(ROOT / 'roles/leaderboard_health', collection / 'roles/leaderboard_health')
        source = collection / 'roles/student_post_provision/files'; source.mkdir(parents=True)
        source.joinpath('flags.vault').write_bytes(VaultLib([('default', VaultSecret(self.password.encode()))]).encrypt(
            b'ctfd_flags: {answer: synthetic-event-answer}\n'))
        lookup = collection / 'plugins/lookup'; lookup.mkdir(parents=True)
        shutil.copy(ROOT / 'plugins/lookup/vault_flags.py', lookup / 'vault_flags.py')
        user_info = self.path / 'collections/ansible_collections/agnosticd/core/plugins/modules/agnosticd_user_info.py'
        user_info.parent.mkdir(parents=True)
        user_info.write_text('import os,json\nfrom pathlib import Path\n'
            'from ansible.module_utils.basic import AnsibleModule\n'
            'm=AnsibleModule(argument_spec={"msg":{"type":"str"},"data":{"type":"dict"}})\n'
            'Path(os.environ["AIA_PUBLISHED_DATA"]).write_text(json.dumps(m.params["data"]))\n'
            'm.exit_json(changed=False)\n')
        modules = collection / 'plugins/modules'; modules.mkdir()
        modules.joinpath('aia_leaderboard_health.py').write_text('''from ansible.module_utils.basic import AnsibleModule
m=AnsibleModule(argument_spec={'snapshots':{'type':'list','elements':'dict'},'domain':{'type':'str'},'minimum_workers':{'type':'int'},'rwx_class':{'type':'str'},'event_credentials_status':{'type':'str'}})
s=m.params['event_credentials_status']
m.exit_json(changed=False,report={'id':'fixture','status':s,'checks':{'event_credentials':{'status':s}}})
''')
        modules.joinpath('aia_event_secret.py').write_text('''import os, json, base64
from pathlib import Path
from ansible.module_utils.basic import AnsibleModule
m=AnsibleModule(argument_spec={k:{'type':'str','no_log':k=='event_password'} for k in ['kubeconfig','name','namespace','key','event_password']})
p=Path(os.environ['AIA_SECRET_STATE']); exists=p.exists()
if not exists:
 p.write_text(json.dumps({'metadata':{'uid':'stable-uid'},'data':{m.params['key']:base64.b64encode(m.params['event_password'].encode()).decode()}}))
 with open(os.environ['AIA_API_TRACE'],'a') as f: f.write('Secret created\\n')
m.exit_json(changed=not exists,uid='stable-uid')
''')
        k8s = self.path / 'collections/ansible_collections/kubernetes/core/plugins/modules'; k8s.mkdir(parents=True)
        k8s.joinpath('k8s_info.py').write_text('''import os,json
from pathlib import Path
from ansible.module_utils.basic import AnsibleModule
m=AnsibleModule(argument_spec={k:{'type':'str'} for k in ['kubeconfig','api_version','kind','namespace','name']})
p=Path(os.environ['AIA_SECRET_STATE'])
with open(os.environ['AIA_API_TRACE'],'a') as f: f.write('Secret read\\n')
m.exit_json(changed=False,resources=[json.loads(p.read_text())] if p.exists() and m.params['kind']=='Secret' else [])
''')
        k8s.joinpath('k8s.py').write_text('''import os
from ansible.module_utils.basic import AnsibleModule
m=AnsibleModule(argument_spec={'kubeconfig':{'type':'str'},'state':{'type':'str'},'definition':{'type':'dict'}})
d=m.params['definition']; kind=d['kind']
assert kind in ['Namespace','SubjectAccessReview']
with open(os.environ['AIA_API_TRACE'],'a') as f: f.write(kind+'\\n')
if kind=='SubjectAccessReview':
 assert d['spec']['resourceAttributes']['namespace']=='aia-automation'
 allowed=os.environ.get('AIA_ALLOW_INSTRUCTOR')=='true' or d['spec']['resourceAttributes']['verb']==os.environ.get('AIA_ALLOW_VERB')
 m.exit_json(changed=False,result={'status':{'allowed':allowed}})
m.exit_json(changed=False)
''')
        self.secret = self.path / 'secret.json'; self.trace = self.path / 'api.log'
        self.config = self.path / 'ansible.cfg'; self.config.write_text('[defaults]\nstdout_callback=default\n')

    def run_role(self, job_key=True, verify=False, allow=False, action='provision', check=False, catch=False, health=False, allow_verb='', organizer=..., encrypted_include=False):
        task = {'ansible.builtin.include_role': {'name': 'ansible_island_adventure.automation.leaderboard_event_credentials'}}
        if verify: task['ansible.builtin.include_role']['tasks_from'] = 'verify'
        if health: task['ansible.builtin.include_role']['name'] = 'ansible_island_adventure.automation.leaderboard_health'
        tasks = [task]
        if catch:
            tasks = [{'block': [task], 'rescue': [{'ansible.builtin.set_fact': {'expected_failure': True}}]},
                     {'ansible.builtin.assert': {'that': ['expected_failure | default(false)',
                       "aia_event_credentials_status == 'failed'",
                       '_aia_leaderboard_event_bundle | default({}) == {}',
                       '_aia_leaderboard_verified_bundle | default({}) == {}',
                       '_aia_leaderboard_verify_secret | default({}) == {}']}}]
        elif action == 'provision' and not check:
            tasks.append({'ansible.builtin.assert': {'that': ["aia_event_credentials_status == 'passed'",
                          '_aia_leaderboard_event_bundle | default({}) == {}',
                          '_aia_leaderboard_verified_bundle == {}', '_aia_leaderboard_verify_secret == {}']}})
        play = self.path / 'play.yml'
        definition={'hosts':'localhost','connection':'local','gather_facts':False,
            'vars':{'ACTION':action, 'ansible_python_interpreter':sys.executable,
                    'aia_healthcheck_publish_user_info':health,'aia_kubeconfig':'selected-leaderboard',
                    'aia_leaderboard_require_event_credentials':True,
                    'openshift_cluster_ingress_domain':'apps.example.test','output_dir':str(self.path)},'tasks':tasks}
        if organizer is not ...:
            definition['vars']['aia_organizer_vault_password']=organizer
        if encrypted_include:
            platform_password=secrets.token_hex(20)
            platform_key=self.path/'platform-key'; platform_key.write_text(platform_password)
            ciphertext=VaultLib([('default',VaultSecret(platform_password.encode()))]).encrypt(self.password.encode()).decode()
            include=self.path/'approved-include.yml'
            include.write_text('aia_organizer_vault_password: !vault |\n'+''.join('  '+line+'\n' for line in ciphertext.splitlines()))
            definition['vars_files']=[str(include)]
        play.write_text(yaml.safe_dump([definition]))
        env = {k:v for k,v in os.environ.items() if not k.startswith('ANSIBLE_VAULT_')}
        env.update(ANSIBLE_CONFIG=str(self.config), ANSIBLE_COLLECTIONS_PATH=str(self.path/'collections'),
                   AIA_SECRET_STATE=str(self.secret), AIA_API_TRACE=str(self.trace), AIA_ALLOW_INSTRUCTOR=str(allow).lower(),
                   AIA_PUBLISHED_DATA=str(self.path/'published.json'), AIA_ALLOW_VERB=allow_verb)
        cmd = [str(PLAYBOOK), '-i', 'localhost,', str(play)]
        if job_key: cmd += ['--vault-id','rhdp-event@'+str(self.key)]
        if encrypted_include: cmd += ['--vault-password-file',str(platform_key)]
        if check: cmd += ['--check','--diff']
        result = subprocess.run(cmd,env=env,capture_output=True,text=True,timeout=45)
        output = result.stdout+result.stderr
        self.assertNotIn(self.password,output)
        self.assertNotIn(base64.b64encode(self.password.encode()).decode(),output)
        self.assertNotIn('synthetic-event-answer',output)
        return result.returncode,output

    def test_named_job_credential_bootstraps_and_reruns_without_rotating(self):
        for _ in range(2):
            code,out=self.run_role(); self.assertEqual(code,0,out)
        self.assertEqual(self.trace.read_text().count('Secret created'),1)
        code,out=self.run_role(job_key=False,verify=True); self.assertEqual(code,0,out)

    def test_absent_job_credential_fails_before_any_api_call(self):
        code,out=self.run_role(job_key=False,catch=True); self.assertEqual(code,0,out)
        self.assertFalse(self.trace.exists()); self.assertFalse(self.secret.exists())

    def test_organizer_variable_bootstraps_without_an_event_vault_credential(self):
        code,out=self.run_role(job_key=False,organizer=self.password)
        self.assertEqual(code,0,out); self.assertTrue(self.secret.exists())

    def test_encrypted_include_uses_platform_key_to_deliver_a_different_event_key(self):
        code,out=self.run_role(job_key=False,encrypted_include=True)
        self.assertEqual(code,0,out); self.assertTrue(self.secret.exists())

    def test_invalid_explicit_variable_cannot_fall_back_to_a_valid_job_credential(self):
        for value in [None,'','incorrect-event-key']:
            code,out=self.run_role(organizer=value,catch=True)
            self.assertEqual(code,0,out)
            self.assertFalse(self.trace.exists()); self.assertFalse(self.secret.exists())

    def test_wrong_existing_secret_fails_without_overwriting(self):
        self.secret.write_text(json.dumps({'data':{'password':base64.b64encode(b'other-test-key').decode()}}))
        original=self.secret.read_bytes(); code,out=self.run_role(catch=True)
        self.assertEqual(code,0,out); self.assertEqual(self.secret.read_bytes(),original)
        self.assertNotIn('Namespace',self.trace.read_text())

    def test_secret_that_an_instructor_can_read_fails_verification(self):
        code,out=self.run_role(allow=True,catch=True); self.assertEqual(code,0,out)
        self.assertIn('SubjectAccessReview',self.trace.read_text())
        self.assertFalse(self.secret.exists())
        self.assertNotIn('Namespace',self.trace.read_text())

    def test_read_only_verification_rejects_missing_and_wrong_keys(self):
        code,out=self.run_role(job_key=False,verify=True,catch=True); self.assertEqual(code,0,out)
        self.secret.write_text(json.dumps({'data':{'password':base64.b64encode(b'other-test-key').decode()}}))
        original=self.secret.read_bytes()
        code,out=self.run_role(job_key=False,verify=True,catch=True); self.assertEqual(code,0,out)
        self.assertEqual(self.secret.read_bytes(),original)
        self.assertNotIn('Namespace',self.trace.read_text())

    def test_list_or_watch_permission_blocks_bootstrap_before_key_creation(self):
        for verb in ['list','watch']:
            code,out=self.run_role(allow_verb=verb,catch=True)
            self.assertEqual(code,0,out)
            self.assertFalse(self.secret.exists())
            self.assertNotIn('Namespace',self.trace.read_text())

    def test_destroy_and_check_mode_need_no_vault_or_api(self):
        for args in [{'action':'destroy'}, {'check':True}]:
            code,out=self.run_role(job_key=False,**args); self.assertEqual(code,0,out)
        self.assertFalse(self.trace.exists())

    def test_health_retains_and_publishes_credential_failure_without_stale_pass(self):
        code,out=self.run_role(job_key=False,health=True,catch=True)
        self.assertEqual(code,0,out)
        published=json.loads((self.path/'published.json').read_text())
        self.assertEqual(published['aia_event_credentials_status'],'failed')
        self.assertEqual(published['aia_leaderboard_health_status'],'failed')
        self.assertEqual(set(published),{'aia_event_credentials_status','aia_leaderboard_health_status','aia_leaderboard_health_id'})


if __name__ == '__main__': unittest.main()
