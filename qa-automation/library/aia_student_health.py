#!/usr/bin/python
"""Read-only service/login checks for one deployed AWS student CI."""
import datetime
from html.parser import HTMLParser
import re
import uuid
from urllib.parse import urljoin, urlsplit

from ansible.module_utils.basic import AnsibleModule
import requests


class HealthError(Exception):
    pass


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


def ctfd_login(url, username, password, admin, verify):
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
        result = request(session, 'GET', url + '/api/v1/challenges').json()
        if result.get('success') is not True or not isinstance(result.get('data'), list):
            raise HealthError('CTFd authenticated challenge API failed')
        # Zero challenges is valid before the separate event-content load.


def measure(data, verify=True):
    guid = data.get('guid', '')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', guid):
        raise HealthError('Invalid student GUID')
    expected = 'instructor-' + guid
    domain = data.get('openshift_cluster_ingress_domain') or data.get('apps_domain')
    if not isinstance(domain, str) or not re.fullmatch(r'[A-Za-z0-9.-]+', domain):
        raise HealthError('Missing or invalid leaderboard ingress domain')
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
                  started=datetime.datetime.now(datetime.timezone.utc).isoformat(), checks={})

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

    check('ssh_identity_and_sudo', ssh_identity)
    check('gitea_login_and_repositories', gitea)
    check('ctfd_student_login', lambda: ctfd_login(urls['ctfd_url'], data['ctfd_student_username'],
                                                 data['ctfd_student_password'], False, verify))
    check('ctfd_admin_login', lambda: ctfd_login(urls['ctfd_url'], data['ctfd_admin_username'],
                                               data['ctfd_admin_password'], True, verify))
    check('devspaces_route', lambda: public(urls['devspaces_url']))
    check('keycloak_realm', keycloak)
    report['status'] = 'passed' if all(c['status'] == 'passed' for c in report['checks'].values()) else 'failed'
    report['finished'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return report


def main():
    module = AnsibleModule(argument_spec=dict(
        data=dict(type='dict', required=True, no_log=True),
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
