#!/usr/bin/env python3
"""Single-student learner journey. Private input on stdin; safe JSON on stdout.

inspect: no board/VM/AAP/workspace mutations. exercise: disposable player/AAP
resources plus real CLI and Apache exercises. full: also resets the personal
board and updates challenge content, requiring a second GUID confirmation.
"""
import base64
from contextlib import ExitStack
import datetime
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit

import requests
import yaml
from ansible.parsing.vault import VaultLib, VaultSecret
from library import aia_student_health as health
from library.aia_connectivity import Controller


class E2EError(Exception):
    pass


class Blocked(E2EError):
    pass


def command(argv, *, env=None, cwd=None, stdin=None, timeout=180):
    try:
        result = subprocess.run(argv, input=stdin, text=True, capture_output=True,
                                env=env, cwd=cwd, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise E2EError('Command unavailable or timed out; private output suppressed') from None
    if result.returncode:
        raise E2EError('Command failed; private output suppressed')
    return result.stdout


def confirmed(mode, guid, confirm_guid='', reset_confirm_guid=''):
    if mode not in ('inspect', 'exercise', 'full'):
        raise E2EError('Unknown E2E mode')
    if mode != 'inspect' and confirm_guid != guid:
        raise E2EError('Exercise mode requires explicit confirmation of this student GUID')
    if mode == 'full' and reset_confirm_guid != guid:
        raise E2EError('Full mode requires separate confirmation to reset this personal board')


class Board:
    def __init__(self, stack, url, username, password, verify):
        self.url = url
        self.http = stack.enter_context(requests.Session())
        self.http.verify = verify
        parser = health.NonceParser()
        parser.feed(health.request(self.http, 'GET', url + '/login').text)
        if not parser.nonce:
            raise E2EError('CTFd login nonce missing')
        health.request(self.http, 'POST', url + '/login', codes=(302, 303),
                       data=dict(name=username, password=password, nonce=parser.nonce))
        page = health.request(self.http, 'GET', url + '/settings').text
        match = re.search(r'csrfNonce[\s\"\x27:]+([a-fA-F0-9]+)', page)
        if not match:
            raise E2EError('Authenticated CTFd session/CSRF token missing')
        self.http.headers['CSRF-Token'] = match.group(1)

    def api(self, method, path, body=None):
        response = health.request(self.http, method, self.url + '/api/v1/' + path,
                                  codes=(200, 201, 204), json=body)
        if response.status_code == 204:
            return None
        result = response.json()
        if result.get('success') is not True:
            raise E2EError('CTFd API operation failed')
        return result.get('data')

    def challenges(self):
        items = self.api('GET', 'challenges?view=admin')
        names = [c['name'] for c in items]
        if len(names) != len(set(names)):
            raise E2EError('Ambiguous duplicate challenge names')
        return {c['name']: c for c in items}

    def submit(self, challenge, answer):
        result = self.api('POST', 'challenges/attempt', dict(challenge_id=challenge['id'], submission=answer))
        if result.get('status') not in ('correct', 'already_solved'):
            raise E2EError('Observed exercise answer was rejected by CTFd')

    def solves(self):
        return {x['challenge_id'] for x in self.api('GET', 'users/me/solves')}


class Journey:
    def __init__(self, data, mode='inspect', confirm_guid='', reset_confirm_guid='',
                 job_timeout=600, workspace_namespace=None):
        self.data, self.mode = data, mode
        self.guid = data.get('guid', '')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', self.guid):
            raise E2EError('Invalid student GUID')
        confirmed(mode, self.guid, confirm_guid, reset_confirm_guid)
        self.user = 'instructor-' + self.guid
        if any(data.get(key) != self.user for key in ('gitea_username', 'aap_username', 'ocp_username')):
            raise E2EError('E2E identities must all match the selected student GUID')
        self.domain = data['openshift_cluster_ingress_domain']
        if not re.fullmatch(r'[A-Za-z0-9.-]+', self.domain):
            raise E2EError('Invalid leader ingress domain')
        self.urls = {key: health.origin(data[key]) for key in ('gitea_url', 'ctfd_url', 'aap_url')}
        if self.urls['ctfd_url'] != 'https://island-' + self.user + '-ctfd.' + self.domain:
            raise E2EError('CTFd board does not belong to the selected student')
        if any(not urlsplit(url).hostname.endswith('.' + self.domain) for url in self.urls.values()):
            raise E2EError('E2E endpoint belongs to a different leader')
        if not 30 <= job_timeout <= 1800:
            raise E2EError('E2E job timeout must be between 30 and 1800 seconds')
        self.timeout, self.workspace_namespace = job_timeout, workspace_namespace
        self.verify = data.get('validate_certs', True)
        self.id = uuid.uuid4().hex
        self.report = dict(id=self.id, guid=self.guid, mode=mode, checks={}, aap_jobs=[], generated_resources=[],
                           started=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                           boundaries=['No browser UI automation or external Ansible Quest gameplay',
                                       'Facilitation, timing and pipeline discussions remain manual'])
        self.workspaces, self.artifacts, self.jobs = {}, [], []
        self.player_id = None
        self.active_job_uncertain = False

    def step(self, key, title, action, needs=(), enabled=True, skip_reason='Not enabled in this mode'):
        result = dict(title=title)
        self.report['checks'][key] = result
        if not enabled:
            result.update(status='skipped', reason=skip_reason)
            return
        if any(self.report['checks'].get(name, {}).get('status') != 'passed' for name in needs):
            result.update(status='blocked', error='Required preceding journey step did not pass')
            return
        try:
            action()
            result['status'] = 'passed'
        except Exception as error:
            result.update(status='blocked' if isinstance(error, Blocked) else 'failed',
                          error=str(error) if isinstance(error, E2EError) else type(error).__name__)

    def repository_file(self, repository, path, branch='main'):
        url = self.urls['gitea_url'] + '/api/v1/repos/' + self.user + '/' + repository + '/contents/' + path
        result = health.request(self.git_http, 'GET', url, params={'ref': branch}).json()
        return base64.b64decode(result['content'], validate=False)

    def repositories(self):
        self.report['repository_revisions'] = {}
        for repo, branches in [('ansible-survival-kit', ('main', 'treasurehunt')), ('aia-instructor', ('main',))]:
            self.report['repository_revisions'][repo] = {}
            for branch in branches:
                url = self.urls['gitea_url'] + '/api/v1/repos/' + self.user + '/' + repo + '/branches/' + branch
                revision = health.request(self.git_http, 'GET', url).json()['commit']['id']
                self.report['repository_revisions'][repo][branch] = revision
        self.definitions = yaml.safe_load(self.repository_file('aia-instructor', 'roles/ctfd/vars/challenges.yml'))['ctfd_challenges']
        if not isinstance(self.definitions, list) or len(self.definitions) < 25:
            raise E2EError('Instructor repository must provide the 25 base challenges')
        for repo, files in {
            'ansible-survival-kit': ['hello-world.yml', 'message-in-a-bottle.txt'],
            'aia-instructor': ['deploy.yml', 'update-challenges.yml', 'generate-handouts.yml',
                               'tasks/discover.yml', 'tasks/content.yml', 'files/ctfd_content.py', 'ISLAND_ACCESS.md'],
        }.items():
            for path in files:
                if not self.repository_file(repo, path):
                    raise E2EError('Required learner repository asset is empty')
            devfile = yaml.safe_load(self.repository_file(repo, '.devfile.yaml'))
            components = [c for c in devfile.get('components', []) if 'container' in c and c['container'].get('mountSources')]
            if not components or not devfile.get('events', {}).get('postStart'):
                raise E2EError('Repository devfile lacks a developer container or startup task')
            self.devfiles[repo] = components[0]['name']
        for path in ('hello-world.yml', 'secret-word.yml', 'apache/apache_role_install.yml'):
            self.repository_file('ansible-survival-kit', path, 'treasurehunt')
        access = self.repository_file('aia-instructor', 'ISLAND_ACCESS.md').decode()
        if self.user not in access or self.urls['ctfd_url'] not in access:
            raise E2EError('Instructor access guide does not identify this student board')

    def vault(self):
        encrypted = self.repository_file('aia-instructor', 'roles/ctfd/vars/flags.yml')
        password = self.data['lab_password']
        plain = VaultLib([('default', VaultSecret(password.encode()))]).decrypt(encrypted)
        self.flags = yaml.safe_load(plain)['ctfd_flags']
        keys = ('write_your_name', 'unlock_the_island', 'hello_world_beach', 'message_in_a_bottle',
                'hello_world_treasure', 'secret_message_treasure', 'treasure_chest')
        if not isinstance(self.flags, dict) or any(not self.flags.get(k) for k in keys):
            raise E2EError('Personal instructor vault is missing required exercise answers')

    def content(self):
        self.admin = Board(self.stack, self.urls['ctfd_url'], self.data['ctfd_admin_username'],
                           self.data['ctfd_admin_password'], self.verify)
        self.challenges = self.admin.challenges()
        categories = set()
        for definition in self.definitions:
            current = self.challenges.get(definition['name'])
            if current is None or current.get('category') != definition['category']:
                raise E2EError('Deployed CTFd board does not match instructor challenge definitions')
            categories.add(current['category'].casefold())
        if not {'warm up', "let's get started", 'at the beach', 'treasure hunt', 'side quest', 'trivia'} <= categories:
            raise E2EError('One or more documented challenge categories are absent')

    def student_openshift(self):
        context = health.openshift_login(self.domain, self.user, self.data['ocp_password'], self.verify,
                                        self.data.get('ocp_api_url') or None, self.data.get('ocp_ca_file') or None)
        cluster = dict(server=context['api_url'])
        if context['verify'] is False:
            cluster['insecure-skip-tls-verify'] = True
        elif isinstance(context['verify'], str):
            cluster['certificate-authority-data'] = base64.b64encode(Path(context['verify']).read_bytes()).decode()
        config = dict(apiVersion='v1', kind='Config', clusters=[dict(name='student', cluster=cluster)],
                      users=[dict(name=self.user, user=dict(token=context['token']))],
                      contexts=[dict(name='student', context=dict(cluster='student', user=self.user))],
                      **{'current-context': 'student'})
        self.kubeconfig = self.root / 'student.kubeconfig'
        self.kubeconfig.write_text(json.dumps(config))
        self.kubeconfig.chmod(0o600)
        self.oc = shutil.which('oc') or shutil.which('kubectl')
        if not self.oc:
            raise Blocked('Runner requires oc or kubectl for student-scoped workspace discovery')
        identity = command([self.oc, '--kubeconfig', str(self.kubeconfig), 'auth', 'whoami', '-o', 'json'])
        if json.loads(identity)['status']['userInfo']['username'] != self.user:
            raise E2EError('CLI OpenShift identity differs from the selected student')
        route = self.oc_json(['get', 'route', 'island', '-n', self.user + '-ctfd', '-o', 'json'])
        if ('https://' + route['spec']['host'] != self.urls['ctfd_url']
                or route['metadata']['namespace'] != self.user + '-ctfd'):
            raise E2EError('Student-discovered CTFd route does not match the published board')

    def oc_json(self, args):
        return json.loads(command([self.oc, '--kubeconfig', str(self.kubeconfig), *args]))

    def workspace_readiness(self):
        projects = self.oc_json(['get', 'projects', '-o', 'json'])['items']
        owned = [p['metadata']['name'] for p in projects if
                 p['metadata'].get('annotations', {}).get('openshift.io/requester') == self.user]
        if self.workspace_namespace:
            if self.workspace_namespace not in owned:
                raise E2EError('Requested workspace namespace is not owned by this student')
            owned = [self.workspace_namespace]
        pvcs = set()
        for namespace in owned:
            try:
                workspaces = self.oc_json(['get', 'devworkspaces.workspace.devfile.io', '-n', namespace, '-o', 'json'])['items']
            except E2EError:
                continue
            for workspace in workspaces:
                if workspace.get('status', {}).get('phase') != 'Running':
                    continue
                if workspace['metadata'].get('annotations', {}).get('controller.devfile.io/storage-type') == 'ephemeral':
                    continue
                for project in workspace.get('spec', {}).get('template', {}).get('projects', []):
                    remote = project.get('git', {}).get('remotes', {}).get('origin', '').removesuffix('.git').rstrip('/')
                    for repo in self.devfiles:
                        if remote != self.urls['gitea_url'] + '/' + self.user + '/' + repo:
                            continue
                        if repo in self.workspaces:
                            raise E2EError('Ambiguous running workspaces for the same learner repository')
                        workspace_id = workspace['status'].get('devworkspaceId')
                        if not workspace_id:
                            raise E2EError('Running workspace has no DevWorkspace ID')
                        pods = self.oc_json(['get', 'pods', '-n', namespace, '-l',
                                             'controller.devfile.io/devworkspace_id=' + workspace_id, '-o', 'json'])['items']
                        pods = [p for p in pods if p.get('status', {}).get('phase') == 'Running'
                                and all(c.get('ready') for c in p.get('status', {}).get('containerStatuses', []))]
                        if len(pods) != 1:
                            raise Blocked('Workspace developer pod is not uniquely running and ready')
                        pod = pods[0]
                        claims = {v['persistentVolumeClaim']['claimName'] for v in pod['spec'].get('volumes', [])
                                  if 'persistentVolumeClaim' in v}
                        if not claims or pvcs.intersection((namespace, c) for c in claims):
                            raise E2EError('Two repository workspaces must have separate persistent PVCs')
                        for claim in claims:
                            pvc = self.oc_json(['get', 'pvc', claim, '-n', namespace, '-o', 'json'])
                            if pvc.get('status', {}).get('phase') != 'Bound':
                                raise Blocked('Workspace persistent volume is not Bound')
                        pvcs.update((namespace, c) for c in claims)
                        container = self.devfiles[repo]
                        if container not in [c['name'] for c in pod['spec']['containers']]:
                            raise E2EError('Workspace pod is not using the repository developer container')
                        self.workspaces[repo] = (namespace, pod['metadata']['name'], container)
        self.report['workspaces'] = {repo: dict(namespace=value[0], pod=value[1], container=value[2])
                                     for repo, value in self.workspaces.items()}
        if set(self.workspaces) != {'ansible-survival-kit', 'aia-instructor'}:
            raise Blocked('Open both repository workspaces in Dev Spaces with persistent storage before E2E')

    def workspace_command(self, repo, script, stdin=None, timeout=300):
        namespace, pod, container = self.workspaces[repo]
        return command([self.oc, '--kubeconfig', str(self.kubeconfig), 'exec', '-i', '-n', namespace,
                        pod, '-c', container, '--', '/bin/sh', '-lc', script], stdin=stdin, timeout=timeout)

    def workspace_tools(self):
        for repo in self.workspaces:
            remote = self.urls['gitea_url'] + '/' + self.user + '/' + repo + '.git'
            prefix = 'cd /projects/' + repo + ' && '
            self.workspace_command(repo, prefix + 'test "$(git branch --show-current)" = main'
                                   + ' && remote=$(git remote get-url origin) && test "${remote%.git}" = ' + shlex.quote(remote.removesuffix('.git'))
                                   + ' && ansible --version && ansible-lint --version && ansible-navigator --version'
                                   + (' && command -v oc' if repo == 'aia-instructor' else ''))
            collections = json.loads(self.workspace_command(repo, prefix + 'ansible-galaxy collection list --format json'))
            installed = {name for path in collections.values() for name in path}
            if not {'kubernetes.core', 'community.general', 'ansible.posix'} <= installed:
                raise Blocked('Wait for the workspace Install lab collections startup task to finish')

    def create_player(self):
        name = 'e2e-' + self.guid + '-' + self.id[:8]
        password = secrets.token_urlsafe(24)
        user = self.admin.api('POST', 'users', dict(name=name, email=name + '@example.invalid', password=password,
                                                   type='user', verified=True, hidden=False))
        self.player_id = user['id']
        self.report['generated_resources'].append(dict(system='ctfd', resource='users', id=self.player_id, name=name))
        self.player = Board(self.stack, self.urls['ctfd_url'], name, password, self.verify)
        self.player.api('PATCH', 'users/me', dict(name=name + '-player', email=name + '-player@example.invalid', confirm=password))
        self.player.submit(self.challenges['Write your name in the sand'], 'DONE')
        self.player.submit(self.challenges['Unlock the island'], str(self.flags['unlock_the_island']))
        if self.challenges['Write your name in the sand']['id'] not in self.player.solves():
            raise E2EError('Warm Up solve was not recorded for the disposable player')

    def answer(self, challenge, key, observed):
        expected = str(self.flags[key])
        if expected.casefold() not in observed.casefold():
            raise E2EError('Exercise output does not contain its instructor-vault answer')
        self.player.submit(self.challenges[challenge], expected)

    def cli_exercises(self):
        output = self.workspace_command('ansible-survival-kit', 'cd /projects/ansible-survival-kit && ansible --version')
        version = re.search(r'ansible \[core ([^\]]+)\]', output)
        if not version:
            raise E2EError('Unable to discover Ansible core version in the participant workspace')
        self.player.submit(self.challenges["The Navigator's Run"], version.group(1))
        self.player.submit(self.challenges['Great Git-Away'], 'ansible-survival-kit')
        output = self.workspace_command('ansible-survival-kit', 'cd /projects/ansible-survival-kit && ansible-playbook hello-world.yml')
        self.answer('Hello World Beach', 'hello_world_beach', output)
        output = self.workspace_command('ansible-survival-kit', 'cd /projects/ansible-survival-kit && '
                                        'ansible-vault view message-in-a-bottle.txt --vault-password-file /dev/stdin', stdin='redhat\n')
        self.answer('Message in a Bottle at the Beach', 'message_in_a_bottle', output)

    def aap_inventory(self):
        self.aap_http = self.stack.enter_context(requests.Session())
        self.aap_http.verify = self.verify
        self.aap_http.auth = (self.user, self.data['aap_password'])
        self.aap = Controller(self.aap_http, self.urls['aap_url'], timeout=self.timeout)
        node = dict(host=self.data['bastion_public_hostname'], port=int(self.data['bastion_ssh_port']))
        self.aap_resources = self.aap.resources(self.user, node)
        self.org = self.aap.one('organizations/', name=self.user)['id']
        group = self.aap.one('groups/', name='web', inventory=self.aap_resources['inventory'])
        host = self.aap.one('hosts/', name='rhel', inventory=self.aap_resources['inventory'])
        members = self.aap.api('GET', 'groups/' + str(group['id']) + '/hosts/')
        if members.get('next') or host['id'] not in [h['id'] for h in members['results']]:
            raise E2EError('AAP web group does not contain the student rhel host')
        self.aap.one('hosts/', name='localhost', inventory=self.aap_resources['inventory'])

    def versions(self):
        gateway = health.request(self.aap_http, 'GET', self.urls['aap_url'] + '/api/gateway/v1/ping/').json()
        controller = self.aap.api('GET', 'ping/')
        for challenge, result in [('The High Tower Signal Strength', gateway),
                                  ('Ansible Automation Platform Controller Version', controller)]:
            version = result.get('version')
            if not isinstance(version, str) or not version:
                raise E2EError('AAP API did not publish its version')
            self.player.submit(self.challenges[challenge], version)

    def documentation_challenges(self):
        for challenge, answer in [('Ansible Docs', 'https://docs.ansible.com/'),
                                  ('Ansible Collections', 'https://galaxy.ansible.com/'),
                                  ('Ansible Certified Collections', 'https://console.redhat.com/ansible/automation-hub/')]:
            self.player.submit(self.challenges[challenge], answer)

    def create_aap(self, resource, body):
        body.update(name='AIA E2E ' + self.guid + ' ' + self.id[:8] + ' ' + body['name'], organization=self.org)
        item = self.aap.api('POST', resource + '/', body)
        self.artifacts.append((resource, item['id']))
        self.report['generated_resources'].append(dict(system='aap', resource=resource, id=item['id'], name=body['name']))
        return item['id']

    def wait_job(self, resource, job_id):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            job = self.aap.api('GET', resource + '/' + str(job_id) + '/')
            if job['status'] in ('successful', 'failed', 'error', 'canceled'):
                if job['status'] != 'successful':
                    raise E2EError('AAP E2E job failed; inspect its recorded job URL')
                return
            time.sleep(2)
        try:
            self.aap.api('POST', resource + '/' + str(job_id) + '/cancel/', {})
            # Never delete resources of a job whose terminal state is unconfirmed.
            for _ in range(15):
                job = self.aap.api('GET', resource + '/' + str(job_id) + '/')
                if job['status'] in ('successful', 'failed', 'error', 'canceled'):
                    break
                time.sleep(2)
            else:
                self.active_job_uncertain = True
        except Exception:
            self.active_job_uncertain = True
        raise E2EError('AAP E2E job exceeded its deadline; cancellation requested')

    def aap_project(self):
        scm_type = self.aap.one('credential_types/', kind='scm')['id']
        scm = self.create_aap('credentials', dict(name='Git', credential_type=scm_type,
                              inputs=dict(username=self.user, password=self.data['gitea_password'])))
        vault_type = self.aap.one('credential_types/', kind='vault')['id']
        self.vault_credential = self.create_aap('credentials', dict(name='Vault', credential_type=vault_type,
                                                                    inputs=dict(vault_password='AAP')))
        self.project = self.create_aap('projects', dict(name='Survival Kit', scm_type='git',
                                        scm_url=self.urls['gitea_url'] + '/' + self.user + '/ansible-survival-kit.git',
                                        scm_branch='treasurehunt', credential=scm))
        # Project creation starts its own initial update. Wait without launching a duplicate.
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            project = self.aap.api('GET', 'projects/' + str(self.project) + '/')
            if project.get('current_update'):
                self.wait_job('project_updates', project['current_update'])
                return
            if project.get('status') == 'successful':
                return
            if project.get('status') in ('failed', 'error'):
                raise E2EError('AAP treasurehunt project sync failed')
            time.sleep(2)
        self.active_job_uncertain = True
        raise E2EError('AAP project sync did not finish; generated resources retained for investigation')

    def aap_job(self, name, playbook, credentials=(), become=False):
        template = self.aap.api('POST', 'job_templates/', dict(
            name='AIA E2E ' + self.guid + ' ' + self.id[:8] + ' ' + name, job_type='run',
            project=self.project, inventory=self.aap_resources['inventory'], playbook=playbook,
            execution_environment=self.aap_resources['execution_environment'], become_enabled=become))
        self.artifacts.append(('job_templates', template['id']))
        self.report['generated_resources'].append(dict(system='aap', resource='job_templates', id=template['id'], name=template['name']))
        for credential in credentials:
            self.aap.api('POST', 'job_templates/' + str(template['id']) + '/credentials/', dict(id=credential))
        job = self.aap.api('POST', 'job_templates/' + str(template['id']) + '/launch/', {})
        self.report['aap_jobs'].append(dict(id=job['id'], playbook=playbook,
                                           url=self.aap.base + 'jobs/' + str(job['id']) + '/'))
        self.wait_job('jobs', job['id'])
        return self.aap.api('GET', 'jobs/' + str(job['id']) + '/stdout/', format='json').get('content', '')

    def treasure_hello(self):
        self.answer('Hello World Treasure Hunt', 'hello_world_treasure', self.aap_job('Hello World', 'hello-world.yml'))

    def treasure_secret(self):
        self.answer('Secret Message Treasure Hunt', 'secret_message_treasure',
                    self.aap_job('Secret Message', 'secret-word.yml', [self.vault_credential]))

    def treasure_web(self):
        url = self.data.get('managed_node_web_url')
        if not url:
            raise Blocked('Student must publish managed_node_web_url to verify the treasure page')
        if health.origin(url) != 'https://treasure-' + self.guid + '.' + self.domain:
            raise E2EError('Treasure web route does not belong to this student')
        self.aap_job('Deploy Web Server', 'apache/apache_role_install.yml',
                     [self.aap_resources['credential'], self.vault_credential], become=True)
        with requests.Session() as session:
            session.verify = self.verify
            page = health.request(session, 'GET', url).text
        self.answer('You found a treasure chest', 'treasure_chest', page)

    def toolkit(self, action):
        # Only a temporary clone is edited; the real workspace and Git remote stay untouched.
        payload = dict(action=action, password=self.data['lab_password'], kubeconfig=json.loads(self.kubeconfig.read_text()),
                       docs_value=15, user=self.user, urls=list(self.urls.values()) + [self.data['devspaces_url']])
        script = '''import json,os,pathlib,subprocess,sys,tempfile,yaml
p=json.load(sys.stdin)
def run(args,env,cwd):
 r=subprocess.run(args,env=env,cwd=cwd,capture_output=True,text=True,timeout=300)
 if r.returncode: raise RuntimeError("toolkit operation failed")
with tempfile.TemporaryDirectory(prefix="aia-e2e-") as d:
 root=pathlib.Path(d); repo=root/"repo"
 subprocess.run(["git","clone","--no-hardlinks","--branch","main","/projects/aia-instructor",str(repo)],check=True,capture_output=True)
 kube=root/"kubeconfig"; kube.write_text(json.dumps(p["kubeconfig"])); kube.chmod(0o600)
 vault=root/"vault"; vault.write_text(p["password"]+"\\n"); vault.chmod(0o600)
 env=dict(os.environ,KUBECONFIG=str(kube),ANSIBLE_VAULT_PASSWORD_FILE=str(vault),ANSIBLE_NOCOLOR="1")
 if p["action"]=="update":
  file=repo/"roles/ctfd/vars/challenges.yml"; data=yaml.safe_load(file.read_text())
  matches=[x for x in data["ctfd_challenges"] if x["name"]=="Ansible Docs"]
  if len(matches)!=1: raise RuntimeError("ambiguous Docs challenge")
  matches[0]["value"]=p["docs_value"]; file.write_text(yaml.safe_dump(data,sort_keys=False))
 commands={"preview":[["deploy.yml","--syntax-check"],["deploy.yml","--check"],["update-challenges.yml","--check"]],"reset":[["deploy.yml"]],"update":[["update-challenges.yml"]],"handouts":[["generate-handouts.yml"]]}
 for args in commands[p["action"]]: run(["ansible-playbook",*args],env,repo)
 if p["action"]=="handouts":
  text=(repo/"handouts/generated/access.md").read_text()
  if p["password"] in text or p["user"] not in text or any(url not in text for url in p["urls"]): raise RuntimeError("unsafe or incomplete handout")
print("AIA_E2E_TOOLKIT_OK")
'''
        namespace, pod, container = self.workspaces['aia-instructor']
        output = command([self.oc, '--kubeconfig', str(self.kubeconfig), 'exec', '-i', '-n', namespace,
                          pod, '-c', container, '--', 'python3', '-c', script],
                         stdin=json.dumps(payload), timeout=1000)
        if output.strip() != 'AIA_E2E_TOOLKIT_OK':
            raise E2EError('Instructor toolkit did not return a success marker')

    def reset_board(self):
        before = {name: c['id'] for name, c in self.admin.challenges().items()}
        if not self.player.solves():
            raise E2EError('Reset verification needs existing disposable-player progress')
        self.toolkit('reset')
        after = self.admin.challenges()
        if self.player.solves() or any(after.get(name, {}).get('id') != ident for name, ident in before.items()):
            raise E2EError('Board reset did not clear progress while preserving challenge IDs')
        self.player.submit(after['Write your name in the sand'], 'DONE')
        self.challenges = after

    def update_board(self):
        before = self.player.solves()
        ids = {name: c['id'] for name, c in self.admin.challenges().items()}
        self.toolkit('update')
        after = self.admin.challenges()
        if (self.player.solves() != before or after['Ansible Docs']['value'] != 15
                or any(after.get(name, {}).get('id') != ident for name, ident in ids.items())):
            raise E2EError('Content update did not preserve solves/IDs and set Ansible Docs to 15')

    def metrics(self):
        if not self.player.solves():
            raise E2EError('Disposable-player solves are absent from engagement metrics')
        # Scoreboard publication can lag the successful attempt response/cache clear.
        for _ in range(15):
            scoreboard = self.admin.api('GET', 'scoreboard')
            if any(entry.get('account_id', entry.get('id')) == self.player_id and entry.get('score', 0) > 0
                   for entry in scoreboard):
                return
            time.sleep(2)
        raise E2EError('Disposable-player score was not published on the personal scoreboard')

    def cleanup(self):
        errors = 0
        if self.active_job_uncertain:
            errors += 1
        else:
            for resource, ident in reversed(self.artifacts):
                try:
                    self.aap.api('DELETE', resource + '/' + str(ident) + '/')
                except Exception:
                    errors += 1
        if self.player_id is not None:
            try:
                self.admin.api('DELETE', 'users/' + str(self.player_id))
            except Exception:
                errors += 1
        if errors:
            raise E2EError('Some generated resources could not be cleaned up; inspect this run ID in AAP/CTFd')

    def run(self):
        with ExitStack() as self.stack:
            self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='aia-student-e2e-')))
            self.git_http = self.stack.enter_context(requests.Session())
            self.git_http.verify = self.verify
            self.git_http.auth = (self.user, self.data['gitea_password'])
            self.devfiles = {}
            exercise = self.mode != 'inspect'
            full = self.mode == 'full'
            try:
                self.step('repositories', 'M3/M4: Own repositories, devfiles and learner assets', self.repositories)
                self.step('vault', 'M4: Personal lab password decrypts instructor answer vault', self.vault, ['repositories'])
                self.step('board', 'M3: All base challenge names and six categories', self.content, ['repositories'])
                self.step('openshift', 'M4: Student CLI identity and personal CTFd route', self.student_openshift)
                self.step('workspaces', 'M3/M4: Two running repository workspaces with separate Bound PVCs',
                          self.workspace_readiness, ['repositories', 'openshift'])
                self.step('tools', 'M3/M4: Main branch and Ansible/Lint/Navigator in both workspaces',
                          self.workspace_tools, ['workspaces'], enabled=exercise)
                self.step('inventory', 'M3: Own AAP inventory, web membership and credentials', self.aap_inventory)
                self.step('player', 'M3: Disposable player profile, DONE solve and island unlock',
                          self.create_player, ['board', 'vault', 'tools'], enabled=exercise)
                self.step('documentation', 'M3: Documentation and collection URL challenge submissions',
                          self.documentation_challenges, ['player'], enabled=exercise)
                self.step('cli', 'M3: Workspace Ansible version, Hello World and redhat Vault exercise',
                          self.cli_exercises, ['tools', 'player'], enabled=exercise)
                self.step('versions', 'M3: Actual Gateway/Controller versions accepted as flags',
                          self.versions, ['inventory', 'cli'], enabled=exercise)
                self.step('project', 'M3: Sync own treasurehunt project with disposable credentials',
                          self.aap_project, ['inventory', 'cli'], enabled=exercise)
                self.step('hello', 'M3: Launch AAP Hello World and submit observed answer',
                          self.treasure_hello, ['project', 'cli'], enabled=exercise)
                self.step('secret', 'M3: Launch AAP Secret Message with AAP Vault credential',
                          self.treasure_secret, ['hello'], enabled=exercise)
                self.step('web', 'M3: Deploy Apache, require HTTP 200 and submit treasure answer',
                          self.treasure_web, ['secret'], enabled=exercise)
                self.step('preview', 'M4/M5: Instructor syntax and reset/update check-mode previews',
                          lambda: self.toolkit('preview'), ['tools', 'vault', 'openshift'], enabled=exercise)
                self.step('reset', 'M4: Reset personal board; clear progress and retain IDs',
                          self.reset_board, ['preview', 'web'], enabled=full,
                          skip_reason='Requires full mode and a separate board-reset GUID confirmation')
                self.step('update', 'M5: Set Ansible Docs to 15 and preserve a new DONE solve',
                          self.update_board, ['reset'], enabled=full,
                          skip_reason='Requires explicitly confirmed full board lifecycle exercise')
                self.step('handouts', 'M5: Generate access handout without passwords',
                          lambda: self.toolkit('handouts'), ['tools', 'openshift'], enabled=exercise)
                self.step('metrics', 'M6: Player solves/score and facilitator scoreboard access',
                          self.metrics, ['player', 'cli'], enabled=exercise)
            finally:
                self.step('cleanup', 'Cleanup: Remove only this run\'s disposable player/AAP objects', self.cleanup)
        checks = self.report['checks'].values()
        self.report['status'] = 'failed' if any(c['status'] in ('failed', 'blocked') for c in checks) else 'passed'
        self.report['coverage'] = 'full-automated-journey' if self.mode == 'full' and self.report['status'] == 'passed' else 'partial'
        self.report['finished'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return self.report


def main():
    # The calling Ansible task has no_log enabled; stdout is sanitized JSON only.
    try:
        payload = json.load(sys.stdin)
        report = Journey(**payload).run()
    except Exception as error:
        report = dict(status='failed', coverage='partial', checks={},
                      error=str(error) if isinstance(error, E2EError) else type(error).__name__)
    print(json.dumps(report))


if __name__ == '__main__':
    main()
