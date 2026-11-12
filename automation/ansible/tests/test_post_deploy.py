"""Execute the real student/Gitea orchestration against a local fixture API."""

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import yaml
from ansible.parsing.vault import VaultLib, VaultSecret


COLLECTION = Path(__file__).resolve().parents[1]
REPO = COLLECTION.parents[1]


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook required")
class PostDeployTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aia-post-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.collection = self.root / "automation/ansible"
        self.collection.mkdir(parents=True)
        shutil.copy(COLLECTION / "post_deploy.yml", self.collection)
        shutil.copy(COLLECTION / "configure_devspaces.yml", self.collection)
        shutil.copy(COLLECTION / "validate_devspaces.yml", self.collection)
        shutil.copy(COLLECTION / "validate_connectivity.yml", self.collection)
        shutil.copytree(COLLECTION / "roles/validate_connectivity", self.collection / "roles/validate_connectivity")
        library = self.collection / "library"
        library.mkdir()
        (library / "aia_ctfd_flags.py").write_text('''from ansible.module_utils.basic import AnsibleModule
module = AnsibleModule(argument_spec={
    'url': {'type': 'str'}, 'username': {'type': 'str'},
    'password': {'type': 'str', 'no_log': True},
    'challenges': {'type': 'list', 'elements': 'dict', 'no_log': True},
    'validate_certs': {'type': 'bool'},
})
module.exit_json(changed=False, updated=0)
''')
        shutil.copytree(COLLECTION / "filter_plugins", self.collection / "filter_plugins")
        shutil.copytree(COLLECTION / "roles/configure_student_aap", self.collection / "roles/configure_student_aap")
        (library / "aia_aap_student.py").write_text('''from ansible.module_utils.basic import AnsibleModule
m=AnsibleModule(argument_spec={
 'url':{'type':'str'},'username':{'type':'str'},'password':{'type':'str','no_log':True},
 'guid':{'type':'str'},'node':{'type':'dict','no_log':True},'validate_certs':{'type':'bool'},'rotate_credential':{'type':'bool'}})
m.exit_json(changed=False,fqdn='rhel.sandbox-'+m.params['guid']+'.test.invalid')
''')
        (library / "aia_connectivity.py").write_text('''import os
from ansible.module_utils.basic import AnsibleModule
m=AnsibleModule(argument_spec={
 'url':{'type':'str'},'username':{'type':'str'},'password':{'type':'str','no_log':True},
 'guid':{'type':'str'},'node':{'type':'dict','no_log':True},'endpoints':{'type':'list'},
 'managed_web_url':{'type':'str'},'connect_timeout':{'type':'float'},
 'require_all_addresses':{'type':'bool'},'validate_certs':{'type':'bool'},'job_timeout':{'type':'int'}})
status='failed' if os.environ.get('AIA_TEST_READINESS_FAILED_USER')==m.params['username'] else 'passed'
m.exit_json(changed=False,report={'id':'fixture','guid':m.params['guid'],'instructor':m.params['username'],
 'status':status,'sources':{s:{'status':status} for s in ['control','aap','aap_ssh','managed_node']}})
''')
        self.source_password='synthetic-source-key'
        (self.root / '.vault-password').write_text(self.source_password)
        self.student_vaults={}
        self.original_vault_plaintext=b'ctfd_flags: {custom: synthetic-student-edit}\n'
        shutil.copytree(COLLECTION / "roles/configure_student", self.collection / "roles/configure_student")
        shutil.copytree(COLLECTION / "roles/configure_devspaces", self.collection / "roles/configure_devspaces")
        modules = self.root / "collections/ansible_collections/kubernetes/core/plugins/modules"
        modules.mkdir(parents=True)
        (modules / "k8s_info.py").write_text('''import json, os
from ansible.module_utils.basic import AnsibleModule
module = AnsibleModule(argument_spec={key: {'type': 'str'} for key in
    ('kubeconfig', 'api_version', 'kind', 'namespace', 'name')})
with open(os.environ['AIA_TEST_K8S_READS'], 'a') as stream:
    stream.write('read\\n')
module.exit_json(changed=False, resources=[{'spec': {'devEnvironments':
    json.loads(os.environ['AIA_TEST_DEVSPACES_POLICY'])}}])
''')
        (modules / "k8s.py").write_text('''import json
from ansible.module_utils.basic import AnsibleModule
module = AnsibleModule(argument_spec={'kubeconfig':{'type':'str'},'state':{'type':'str'},'definition':{'type':'dict'}})
definition=module.params['definition']
if definition.get('kind')!='ConfigMap' or definition.get('metadata',{}).get('name')!='aia-readiness':
 module.fail_json(msg='Unexpected Kubernetes mutation outside connectivity evidence')
report=json.loads(definition['data']['report.json'])
if definition['metadata']['namespace']!=report['instructor']+'-lab':
 module.fail_json(msg='Readiness evidence crossed instructor namespaces')
module.exit_json(changed=False)
''')
        self.policy = {"maxNumberOfWorkspacesPerUser": 2,
                       "maxNumberOfRunningWorkspacesPerUser": 2,
                       "storage": {"pvcStrategy": "per-workspace"}}
        self.k8s_reads = self.root / "k8s-reads"
        tasks = self.collection / "roles/instructor_config/tasks"
        tasks.mkdir(parents=True)
        (tasks / "oc_login.yml").write_text(yaml.safe_dump([
            {"ansible.builtin.debug": {"msg": "OpenShift interaction stub"}},
        ]))
        (tasks / "ctfd_config.yml").write_text(yaml.safe_dump([{
            "name": "Verify the CTFd handoff contains resolved flags for this student",
            "ansible.builtin.assert": {"that": [
                "ctfd_challenges | length == 25",
                "ctfd_challenges_advanced | length == 0",
                "ctfd_challenges[0].flags[0].content == 'synthetic-event-answer'",
                "gitea_username in ctfd_challenges[8].description",
            ]},
        }]))
        definitions = self.root / "ansible-testing/vars"
        definitions.mkdir(parents=True)
        source = (REPO / "ansible-testing/vars/challenges.yml").read_text()
        (definitions / "challenges.yml").write_text(source)
        keys = set(re.findall(r"ctfd_flags\.(\w+)", source))
        self.flags = definitions / "flags.yml"
        self.flags.write_text(yaml.safe_dump({"ctfd_flags": {key: "synthetic-event-answer" for key in keys}}))
        self.users = {"instructor-one": secrets.token_hex(12), "instructor-two": secrets.token_hex(12)}
        self.guides = {}
        self.forks = {(user, repo) for user in self.users for repo in ("ansible-survival-kit", "aia-instructor")}
        self.forks.remove(("instructor-two", "ansible-survival-kit"))
        self.writes = []
        self.requests = []
        for user in self.users:
            self.student_vaults[user]=VaultLib([('default',VaultSecret(self.source_password.encode()))]).encrypt(self.original_vault_plaintext).decode()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                auth = self.headers.get("Authorization", "").removeprefix("Basic ")
                user, password = base64.b64decode(auth).decode().split(":", 1)
                fixture.requests.append((self.command, self.path, user))
                code, data = 200, {}
                path = urlsplit(self.path).path.split("/")
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if fixture.users.get(user) != password:
                    code = 401
                elif self.path == "/api/v1/user":
                    data = {"login": user}
                elif self.command == "POST" and path[-1] == "forks":
                    fixture.forks.add((user, path[-2]))
                    fixture.writes.append((user, "fork"))
                    code = 202
                elif "contents" in path and self.path.split('?')[0].endswith('/roles/ctfd/vars/flags.yml'):
                    if self.command == 'GET':
                        data={'sha':'vault-sha','content':base64.b64encode(fixture.student_vaults[user].encode()).decode()}
                    elif self.command == 'PUT' and body.get('sha')=='vault-sha':
                        fixture.student_vaults[user]=base64.b64decode(body['content']).decode()
                        fixture.writes.append((user,'vault'))
                    else:code=409
                elif "contents" in path:
                    if self.command == "GET":
                        if user in fixture.guides:
                            data = {"sha": "file-sha", "content": base64.b64encode(fixture.guides[user].encode()).decode()}
                        else:
                            code = 404
                    else:
                        if self.command == "PUT" and body.get("sha") != "file-sha":
                            code = 409
                        else:
                            fixture.guides[user] = base64.b64decode(body["content"]).decode()
                            fixture.writes.append((user, "guide"))
                            code = 201 if self.command == "POST" else 200
                elif "branches" in path:
                    data = {"name": path[-1]}
                elif len(path) == 6 and path[3] == "repos":
                    if (user, path[-1]) not in fixture.forks or path[-2] != user:
                        code = 404
                    else:
                        data = {"name": path[-1], "owner": {"login": user}}
                else:
                    code = 404
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            do_GET = do_POST = do_PUT = handle_request

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_port}"
        hosts = {}
        for user, password in self.users.items():
            guid = user.removeprefix("instructor-")
            hosts[user] = {
                "guid": guid, "gitea_url": self.url, "gitea_username": user, "gitea_password": password,
                "ctfd_admin_username": "admin", "ctfd_admin_password": password,
                "student_name": "lab-user", "common_password": password, "aia_lab_password": password,
                "aap_password": password, "aia_managed_node": {"host":"ssh.example.test","port":30222,"username":"lab-user","password":password},
                "instructor": {"name": user},
                "aia_student": {"guid": guid, "ctfd_username": "lab-user", "ctfd_url": f"https://ctfd-{guid}.example.test",
                                "gitea_url": self.url, "aap_url": "https://aap.example.test", "aap_username": user,
                                "devspaces_url": "https://devspaces.example.test", "managed_node": "rhel"},
            }
        self.inventory = self.root / "students.json"
        self.inventory.write_text(json.dumps({"all": {"children": {"aia_students": {
            "hosts": hosts, "vars": {"ansible_connection": "local", "ansible_python_interpreter": "/usr/bin/python3",
                                      "aia_kubeconfig": str(self.root / "kubeconfig")},
        }}}}))
        self.config = self.root / "ansible.cfg"
        self.config.write_text("[defaults]\nstdout_callback=default\n")

    def run_playbook(self, *extra_args, playbook="post_deploy.yml"):
        result = subprocess.run(["ansible-playbook", "-i", str(self.inventory), str(self.collection / playbook), *extra_args],
                                env=dict(os.environ, ANSIBLE_CONFIG=str(self.config), ANSIBLE_NOCOLOR="1",
                                         ANSIBLE_COLLECTIONS_PATH=str(self.root / "collections"),
                                         AIA_TEST_K8S_READS=str(self.k8s_reads),
                                         AIA_TEST_DEVSPACES_POLICY=json.dumps(self.policy)),
                                capture_output=True, text=True, timeout=90)
        return result.returncode, result.stdout + result.stderr

    def test_two_students_link_to_their_own_repos_and_rerun_does_not_commit(self):
        rc, output = self.run_playbook()
        self.assertEqual(rc, 0, output)
        self.assertEqual(self.k8s_reads.read_text(), "read\n", output)
        self.assertEqual(len(self.guides), 2)
        for user, content in self.guides.items():
            self.assertIn(self.url + "/" + user + "/ansible-survival-kit", content)
            other = next(name for name in self.users if name != user)
            self.assertNotIn(other, content)
            self.assertNotIn(self.users[user], content)
        for user,password in self.users.items():
            plain=VaultLib([('default',VaultSecret(password.encode()))]).decrypt(self.student_vaults[user])
            self.assertEqual(plain,self.original_vault_plaintext)
        vaults=dict(self.student_vaults)
        writes = list(self.writes)
        rc, output = self.run_playbook()
        self.assertEqual(rc, 0, output)
        self.assertEqual(self.writes, writes)
        self.assertEqual(self.student_vaults,vaults)

    def test_failed_readiness_is_retained_for_both_students_before_nonzero_exit(self):
        with patch.dict(os.environ,{'AIA_TEST_READINESS_FAILED_USER':'instructor-one'}):
            rc,output=self.run_playbook()
        self.assertNotEqual(rc,0,output)
        for user in self.users:
            report=json.loads((self.root/(user+'.fixture.readiness.json')).read_text())
            self.assertEqual(report['status'],'failed' if user=='instructor-one' else 'passed')
            for password in self.users.values():self.assertNotIn(password,json.dumps(report))

    def test_standalone_connectivity_checks_do_not_need_the_event_vault(self):
        self.flags.unlink()
        rc,output=self.run_playbook(playbook='validate_connectivity.yml')
        self.assertEqual(rc,0,output);self.assertEqual(self.writes,[])

    def test_web_url_is_only_in_its_students_access_guide(self):
        data=json.loads(self.inventory.read_text())
        first,second=list(self.users)
        url='https://treasure-'+first.removeprefix('instructor-')+'.apps.hosting.example.test'
        data['all']['children']['aia_students']['hosts'][first]['aia_student']['managed_node_web_url']=url
        self.inventory.write_text(json.dumps(data))
        rc,output=self.run_playbook('--tags','student_access')
        self.assertEqual(rc,0,output)
        self.assertIn(url,self.guides[first])
        self.assertNotIn(url,self.guides[second])
        self.assertIn('does not publish a web URL',self.guides[second])

    def test_missing_event_flag_stops_before_any_service_request(self):
        self.flags.write_text("ctfd_flags: {}\n")
        rc, output = self.run_playbook()
        self.assertNotEqual(rc, 0, output)
        self.assertEqual(self.requests, [])

    def test_legacy_skip_tag_does_not_bypass_ci_policy_validation(self):
        rc, output = self.run_playbook("--skip-tags", "devspaces_ci_workaround")
        self.assertEqual(rc, 0, output)
        self.assertEqual(self.k8s_reads.read_text(), "read\n", output)
        self.assertEqual(len(self.guides), 2)

    def test_stale_ci_policy_stops_before_student_service_changes(self):
        for policy in [
            {**self.policy, "maxNumberOfRunningWorkspacesPerUser": 1},
            {**self.policy, "maxNumberOfWorkspacesPerUser": 1},
            {**self.policy, "storage": {"pvcStrategy": "per-user"}},
            {},
        ]:
            with self.subTest(policy=policy):
                self.policy = policy
                rc, output = self.run_playbook()
                self.assertNotEqual(rc, 0, output)
                self.assertIn("post-deploy does not patch Dev Spaces", output)
                self.assertEqual(self.requests, [])

    def test_access_tag_cannot_touch_ctfd_or_workspace_policy(self):
        tasks=self.collection/'roles/instructor_config/tasks'
        for filename in ('oc_login.yml','ctfd_config.yml'):
            (tasks/filename).write_text('- ansible.builtin.fail: {msg: "CTFd must not run during access-only updates"}\n')
        (self.collection/'library/aia_ctfd_flags.py').write_text('from ansible.module_utils.basic import AnsibleModule\nAnsibleModule(argument_spec={}, bypass_checks=True).fail_json(msg="CTFd flags must not run")\n')
        rc,output=self.run_playbook('--tags','student_access')
        self.assertEqual(rc,0,output)
        self.assertEqual(len(self.guides),2)
        self.assertFalse(self.k8s_reads.exists())

    def test_devspaces_validation_needs_no_flags_or_student_service_calls(self):
        self.flags.unlink()
        rc, output = self.run_playbook(playbook="validate_devspaces.yml")
        self.assertEqual(rc, 0, output)
        self.assertEqual(self.k8s_reads.read_text(), "read\n", output)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
