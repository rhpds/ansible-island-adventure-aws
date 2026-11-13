#!/usr/bin/python
"""Read-only service/login checks for one deployed AWS student CI."""
import datetime
import base64
import json
from html.parser import HTMLParser
from pathlib import Path
import re
import subprocess
import sys
import uuid
from urllib.parse import parse_qs, urljoin, urlsplit

from ansible.module_utils.basic import AnsibleModule
import requests
import yaml


class HealthError(Exception):
    pass


HEALTH_DATA_FIELDS = (
    'guid', 'apps_domain', 'openshift_cluster_ingress_domain',
    'aap_url', 'aap_username', 'aap_password', 'gitea_url', 'gitea_username', 'gitea_password',
    'ctfd_url', 'ctfd_student_username', 'ctfd_student_password', 'ctfd_admin_username', 'ctfd_admin_password',
    'devspaces_url', 'keycloak_url', 'keycloak_username', 'keycloak_password', 'keycloak_realm',
    'ocp_username', 'ocp_password', 'ocp_api_url', 'ocp_ca_file', 'lab_password',
    'bastion_public_hostname', 'bastion_ssh_port', 'bastion_ssh_user_name', 'bastion_ssh_password',
    'expected_challenge_count', 'require_post_provision',
)


def verify_lab_vault(ciphertext, password):
    # AnsiballZ supplies a restricted ansible package in its ZIP. Start the same
    # runner interpreter afresh to use its installed Vault library. Ciphertext
    # and key travel through stdin, never command arguments or temporary files;
    # the child emits no plaintext, and its stderr is not returned to the caller.
    validator = '''import base64,json,sys,yaml
from ansible.parsing.vault import VaultLib,VaultSecret
d=json.load(sys.stdin)
p=VaultLib([('default',VaultSecret(d['password'].encode()))]).decrypt(base64.b64decode(d['ciphertext']))
flags=yaml.safe_load(p).get('ctfd_flags')
sys.exit(0 if isinstance(flags,dict) and flags else 1)
'''
    result = subprocess.run([sys.executable, '-c', validator],
                            input=json.dumps({'ciphertext': ciphertext, 'password': password}),
                            capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise HealthError('Instructor vault does not unlock with the provisioned lab password')


class NonceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nonce = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name') == 'nonce':
            self.nonce = attrs.get('value')


def origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise HealthError('Expected a credential-free HTTPS origin')
    return value.rstrip('/')


def request(session, method, url, codes=(200,), **kwargs):
    response = session.request(method, url, timeout=30, allow_redirects=False, **kwargs)
    if response.status_code not in codes:
        raise HealthError('Unexpected HTTP status: ' + str(response.status_code))
    return response


def ctfd_login(url, username, password, admin, verify, minimum_challenges=0):
    # Independent cookie jars: an admin session must never mask a bad student login.
    with requests.Session() as session:
        session.verify = verify
        page = request(session, 'GET', url + '/login')
        parser = NonceParser()
        parser.feed(page.text)
        if not parser.nonce:
            raise HealthError('CTFd login nonce missing (setup may be incomplete)')
        request(session, 'POST', url + '/login', codes=(302, 303),
                data=dict(name=username, password=password, nonce=parser.nonce))
        # A failed login can redirect too; require a protected page, not just 302.
        if admin:
            # CTFd /admin redirects to its statistics dashboard. Accept only bounded,
            # same-origin redirects within /admin/, never a redirect back to login.
            protected_url = url + '/admin'
            for _ in range(4):
                protected = request(session, 'GET', protected_url, codes=(200, 302, 303))
                if protected.status_code == 200:
                    break
                destination = urljoin(protected_url, protected.headers.get('Location', ''))
                parsed = urlsplit(destination)
                if (parsed.scheme != urlsplit(url).scheme or parsed.netloc != urlsplit(url).netloc
                        or not parsed.path.startswith('/admin/') or parsed.username or parsed.password):
                    raise HealthError('CTFd admin did not reach an authenticated dashboard')
                protected_url = destination
            else:
                raise HealthError('CTFd admin dashboard redirect limit exceeded')
        else:
            request(session, 'GET', url + '/settings')
        result = request(session, 'GET', url + '/api/v1/challenges' +
                         ('?view=admin' if admin and minimum_challenges else '')).json()
        if result.get('success') is not True or not isinstance(result.get('data'), list):
            raise HealthError('CTFd authenticated challenge API failed')
        if admin and minimum_challenges:
            if len(result['data']) < minimum_challenges:
                raise HealthError('Event challenge content is incomplete')
            for challenge in result['data']:
                flags = request(session, 'GET', url + '/api/v1/challenges/' + str(challenge['id']) + '/flags').json()
                if (flags.get('success') is not True or not isinstance(flags.get('data'), list)
                        or not flags['data'] or any(not isinstance(f.get('content'), str)
                        or not f['content'].strip() or f['content'] == 'CHANGEME' for f in flags['data'])):
                    raise HealthError('Challenge answers are missing or contain placeholders')


def openshift_login(domain, username, password, verify=True, api_url=None, ca_file=None):
    oauth = 'https://oauth-openshift.' + domain
    # Inside Showroom, use only the mounted CA certificate, NEVER its SA token.
    # The student OAuth token authenticates the API request in both execution modes.
    mounted_ca = Path('/var/run/secrets/kubernetes.io/serviceaccount/ca.crt')
    if not api_url:
        if mounted_ca.is_file():
            api_url = 'https://kubernetes.default.svc'
            ca_file = ca_file or str(mounted_ca)
        elif domain.startswith('apps.'):
            api_url = 'https://api.' + domain.removeprefix('apps.') + ':6443'
        else:
            raise HealthError('Supply the OpenShift API URL for this ingress domain')
    api_url = origin(api_url)
    state = uuid.uuid4().hex
    with requests.Session() as session:
        session.verify = verify
        response = request(session, 'GET', oauth + '/oauth/authorize', codes=(302,),
                           params=dict(client_id='openshift-challenging-client', response_type='token', state=state),
                           auth=(username, password), headers={'X-CSRF-Token': '1'})
        redirect = urlsplit(response.headers.get('Location', ''))
        fragment = parse_qs(redirect.fragment)
        if (redirect.scheme != 'https' or redirect.netloc != urlsplit(oauth).netloc
                or redirect.path != '/oauth/token/implicit' or fragment.get('state') != [state]
                or len(fragment.get('access_token', [])) != 1):
            raise HealthError('OpenShift student OAuth login did not return a valid token')
        token = fragment['access_token'][0]
        # Never forward the password, follow redirects or expose token-bearing URLs.
        user = request(session, 'GET', api_url + '/apis/user.openshift.io/v1/users/~',
                       headers={'Authorization': 'Bearer ' + token},
                       verify=(ca_file or True) if verify else False).json()
        if user.get('kind') != 'User' or user.get('metadata', {}).get('name') != username:
            raise HealthError('OpenShift authenticated username does not match the student')
        # Internal callers may reuse this student-only login; health reports ignore
        # return values and never include this token-bearing connection context.
        return dict(api_url=api_url, token=token, verify=(ca_file or True) if verify else False)


def measure(data, verify=True):
    guid = data.get('guid', '')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', guid):
        raise HealthError('Invalid student GUID')
    expected = 'instructor-' + guid
    domain = data.get('openshift_cluster_ingress_domain') or data.get('apps_domain')
    if not isinstance(domain, str) or not re.fullmatch(r'[A-Za-z0-9.-]+', domain):
        raise HealthError('Missing or invalid leaderboard ingress domain')
    if (data.get('ocp_username') or data.get('keycloak_username')) != expected:
        raise HealthError('OpenShift login must use this student identity')
    for key in ('aap_username', 'gitea_username', 'keycloak_username'):
        if data.get(key) != expected:
            raise HealthError(key + ' does not match the student GUID')
    urls = {key: origin(data[key]) for key in
            ('aap_url', 'gitea_url', 'ctfd_url', 'devspaces_url', 'keycloak_url')}
    for url in urls.values():
        if not urlsplit(url).hostname.endswith('.' + domain):
            raise HealthError('Student endpoint belongs to a different leaderboard')
    if urls['ctfd_url'] != 'https://island-' + expected + '-ctfd.' + domain:
        raise HealthError('CTFd endpoint does not match this student GUID')
    report = dict(id=uuid.uuid4().hex, guid=guid, instructor=expected,
                  started=datetime.datetime.now(datetime.timezone.utc).isoformat(), checks={},
                  requirements={'minimum_challenges': int(data.get('expected_challenge_count', 0)),
                                'post_provision': bool(data.get('require_post_provision', False))})

    def check(name, action):
        try:
            action()
            report['checks'][name] = dict(status='passed')
        except Exception as error:
            # Never include response bodies, credential values or raw exception URLs.
            report['checks'][name] = dict(status='failed', error=(str(error) if isinstance(error, HealthError)
                                                               else type(error).__name__))

    def gitea():
        with requests.Session() as session:
            session.verify = verify
            session.auth = (data['gitea_username'], data['gitea_password'])
            user = request(session, 'GET', urls['gitea_url'] + '/api/v1/user').json()
            if user.get('login') != expected:
                raise HealthError('Authenticated Gitea identity mismatch')
            for repository in ('ansible-survival-kit', 'aia-instructor'):
                repo = request(session, 'GET', urls['gitea_url'] + '/api/v1/repos/' + expected + '/' + repository).json()
                if repo.get('owner', {}).get('login') != expected:
                    raise HealthError('Repository owner mismatch')
            if data.get('require_post_provision', False):
                base = urls['gitea_url'] + '/api/v1/repos/' + expected + '/aia-instructor/contents/'
                guide = request(session, 'GET', base + 'ISLAND_ACCESS.md?ref=main').json()
                guide_text = base64.b64decode(guide['content'], validate=False).decode()
                if (urls['ctfd_url'] not in guide_text or
                        urls['gitea_url'] + '/' + expected + '/ansible-survival-kit' not in guide_text):
                    raise HealthError('Personal access guide is missing or belongs to another student')
                encrypted = request(session, 'GET', base + 'roles/ctfd/vars/flags.yml?ref=main').json()
                verify_lab_vault(encrypted['content'], data.get('lab_password') or data['gitea_password'])

    def public(url):
        with requests.Session() as session:
            session.verify = verify
            request(session, 'GET', url, codes=(200, 302, 303))

    def keycloak():
        realm = data['keycloak_realm']
        if not re.fullmatch(r'[A-Za-z0-9_-]+', realm):
            raise HealthError('Invalid Keycloak realm')
        with requests.Session() as session:
            session.verify = verify
            discovery = request(session, 'GET', urls['keycloak_url'] + '/realms/' + realm
                                + '/.well-known/openid-configuration').json()
            if discovery.get('issuer') != urls['keycloak_url'] + '/realms/' + realm:
                raise HealthError('Keycloak issuer mismatch')
            # Do not require direct-password grants: browser-only SSO is supported.

    def ssh_identity():
        import paramiko
        client = paramiko.SSHClient()
        # Fresh catalog endpoints have no pre-distributed host key (as in provisioning).
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(data['bastion_public_hostname'], port=int(data['bastion_ssh_port']),
                           username=data['bastion_ssh_user_name'], password=data['bastion_ssh_password'],
                           look_for_keys=False, allow_agent=False, timeout=10, auth_timeout=20, banner_timeout=20)
            _, output, _ = client.exec_command('hostname -f && sudo -n true', timeout=30)
            hostname = output.read(4096).decode().strip()
            if output.channel.recv_exit_status() != 0 or hostname != 'rhel.' + guid + '.internal':
                raise HealthError('AWS managed-node hostname or passwordless sudo mismatch')
        finally:
            client.close()

    check('ocp_student_login', lambda: openshift_login(
        domain, expected, data.get('ocp_password') or data.get('keycloak_password') or data.get('gitea_password'),
        verify, data.get('ocp_api_url') or None, data.get('ocp_ca_file') or None))
    check('ssh_identity_and_sudo', ssh_identity)
    check('gitea_login_and_repositories', gitea)
    check('ctfd_student_login', lambda: ctfd_login(urls['ctfd_url'], data['ctfd_student_username'],
                                                 data['ctfd_student_password'], False, verify))
    check('ctfd_admin_login', lambda: ctfd_login(urls['ctfd_url'], data['ctfd_admin_username'],
                                               data['ctfd_admin_password'], True, verify,
                                               int(data.get('expected_challenge_count', 0))))
    check('devspaces_route', lambda: public(urls['devspaces_url']))
    check('keycloak_realm', keycloak)
    report['status'] = 'passed' if all(c['status'] == 'passed' for c in report['checks'].values()) else 'failed'
    report['finished'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return report


def main():
    options = {key: dict(type='str', no_log='password' in key) for key in HEALTH_DATA_FIELDS}
    options['bastion_ssh_port'] = dict(type='int')
    options['expected_challenge_count'] = dict(type='int', default=0)
    options['require_post_provision'] = dict(type='bool', default=False)
    module = AnsibleModule(argument_spec=dict(
        data=dict(type='dict', required=True, options=options),
        validate_certs=dict(type='bool', default=True)), supports_check_mode=True)
    if module.check_mode:
        module.exit_json(changed=False, skipped=True)
    try:
        report = measure(module.params['data'], module.params['validate_certs'])
        module.exit_json(changed=False, report=report)
    except Exception:
        module.fail_json(msg='Invalid student health-check inputs; details suppressed')


if __name__ == '__main__':
    main()
