#!/usr/bin/python
"""Populate an instructor's AAP inventory using their CI's published SSH access."""
import json
import re
import socket
import time

from ansible.module_utils.basic import AnsibleModule
import requests
import yaml


class ConfigurationError(Exception):
    pass


def connect_socket(host, port, budget=30, attempt_timeout=3):
    """Try advertised addresses without letting one black hole exhaust every attempt."""
    deadline = time.monotonic() + budget
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen = set()
    for family, kind, protocol, _, address in addresses:
        if (family, address) in seen:
            continue
        seen.add((family, address))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(min(attempt_timeout, remaining))
            connection.connect(address)
            return connection
        except OSError:
            connection.close()
    raise ConfigurationError('No published SSH address was reachable within the connection budget')


def discover_fqdn(node, guid):
    import paramiko
    ssh = paramiko.SSHClient()
    # First connection to a freshly provisioned RHDP endpoint; no global key files.
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connection = None
    try:
        connection = connect_socket(node['host'], node['port'])
        ssh.connect(node['host'], port=node['port'], username=node['username'], password=node['password'],
                    sock=connection, look_for_keys=False, allow_agent=False,
                    timeout=30, auth_timeout=30, banner_timeout=30)
        _, output, _ = ssh.exec_command('hostname -f && sudo -n true', timeout=30)
        fqdn = output.read().decode().strip()
        if output.channel.recv_exit_status() != 0:
            raise ConfigurationError('The managed node must provide its FQDN and passwordless sudo')
        if (not re.fullmatch(r'[a-zA-Z0-9.-]+', fqdn) or '.' not in fqdn
                or not re.search(r'(^|[.-])' + re.escape(guid) + r'([.-]|$)', fqdn)):
            raise ConfigurationError('The discovered VM FQDN does not match this student GUID')
        return fqdn
    finally:
        ssh.close()
        if connection is not None:
            connection.close()


class Controller:
    def __init__(self, http, url, check=False):
        self.http, self.base, self.check = http, url.rstrip('/') + '/api/controller/v2/', check
        self.changes = []

    def api(self, method, path, body=None, params=None):
        response = self.http.request(method, self.base + path, json=body, params=params,
                                     timeout=40, allow_redirects=False)
        if response.status_code not in (200, 201, 204):
            raise ConfigurationError(f'AAP {method} {path.split("?")[0]} returned HTTP {response.status_code}')
        return response.json() if response.content else {}

    def listing(self, path, **params):
        data = self.api('GET', path, params=params)
        if data.get('next'):
            raise ConfigurationError('AAP returned an ambiguous paginated resource selection')
        return data['results']

    def one(self, path, **params):
        items = self.listing(path, **params)
        if len(items) > 1:
            raise ConfigurationError('AAP resource names are ambiguous within the selected organization')
        return items[0] if items else None

    def ensure(self, path, selector, body):
        current = self.one(path, **selector)
        if current is None:
            self.changes.append('create ' + path)
            return dict(body, id=None) if self.check else self.api('POST', path, body)
        changes = {k: v for k, v in body.items() if current.get(k) != v}
        if changes:
            self.changes.append('update ' + path)
            if not self.check:
                return self.api('PATCH', f'{path}{current["id"]}/', changes)
        return current


def configure(http, url, username, guid, node, fqdn, check=False, rotate=False):
    if username != 'instructor-' + guid:
        raise ConfigurationError('AAP username does not match the student GUID')
    api = Controller(http, url, check)
    me = api.listing('me/')
    if len(me) != 1 or me[0]['username'] != username:
        raise ConfigurationError('AAP authentication does not match the instructor')
    org = api.one('organizations/', name=username)
    if org is None:
        raise ConfigurationError('The student CI has not created the instructor organization')
    org_id = org['id']
    types = api.listing('credential_types/', kind='ssh')
    if len(types) != 1:
        raise ConfigurationError('AAP must expose exactly one Machine credential type')
    marker = 'Managed by Ansible Island Adventure post-deploy (' + guid + ')'
    inventory = api.ensure('inventories/', dict(name='Island Adventure', organization=org_id),
                           dict(name='Island Adventure', organization=org_id, description=marker))
    cred = api.one('credentials/', name='Managed Node SSH', organization=org_id)
    inputs = dict(username=node['username'], password=node['password'], become_method='sudo', become_username='root')
    if cred is None:
        api.changes.append('create Machine credential')
        cred = dict(id=None) if check else api.api('POST', 'credentials/',
            dict(name='Managed Node SSH', organization=org_id, credential_type=types[0]['id'], description=marker, inputs=inputs))
    else:
        if cred.get('description') != marker or cred['credential_type'] != types[0]['id']:
            raise ConfigurationError('A different credential already uses the managed Machine credential name')
        nonsecret = {k: v for k, v in inputs.items() if k != 'password'}
        if rotate or any(cred.get('inputs', {}).get(k) != v for k, v in nonsecret.items()):
            api.changes.append('update Machine credential')
            if not check:
                api.api('PATCH', f'credentials/{cred["id"]}/', dict(inputs=inputs))
    inventory_id = inventory['id']
    if inventory_id is None:
        return dict(changed=True, fqdn=fqdn, inventory_id=None, credential_id=cred['id'])
    host = api.one('hosts/', name='rhel', inventory=inventory_id)
    variables = yaml.safe_load(host.get('variables') or '{}') if host else {}
    if not isinstance(variables, dict):
        raise ConfigurationError('Existing managed-host variables are not a mapping')
    variables.update(ansible_host=node['host'], ansible_port=node['port'],
                     ansible_python_interpreter='/usr/bin/python3', aia_managed_node_fqdn=fqdn)
    host = api.ensure('hosts/', dict(name='rhel', inventory=inventory_id), dict(name='rhel', inventory=inventory_id,
        description=marker + ' — ' + fqdn, enabled=True, variables=json.dumps(variables, sort_keys=True)))
    api.ensure('hosts/', dict(name='localhost', inventory=inventory_id), dict(name='localhost', inventory=inventory_id,
        description=marker + ' — controller-local exercises', enabled=True,
        variables=json.dumps(dict(ansible_connection='local', ansible_python_interpreter='{{ ansible_playbook_python }}'), sort_keys=True)))
    group = api.ensure('groups/', dict(name='web', inventory=inventory_id),
                       dict(name='web', inventory=inventory_id, description=marker))
    if group['id'] is not None and host['id'] is not None:
        members = api.listing(f'groups/{group["id"]}/hosts/')
        if host['id'] not in [h['id'] for h in members]:
            api.changes.append('associate rhel with web')
            if not check:
                api.api('POST', f'groups/{group["id"]}/hosts/', dict(id=host['id']))
    return dict(changed=bool(api.changes), fqdn=fqdn, inventory_id=inventory_id,
                credential_id=cred['id'], inventory_name='Island Adventure', credential_name='Managed Node SSH')


def main():
    module = AnsibleModule(argument_spec={
        'url': dict(type='str', required=True), 'username': dict(type='str', required=True),
        'password': dict(type='str', required=True, no_log=True), 'guid': dict(type='str', required=True),
        'node': dict(type='dict', required=True, no_log=True, options={
            'host': dict(type='str', required=True), 'port': dict(type='int', required=True),
            'username': dict(type='str', required=True), 'password': dict(type='str', required=True, no_log=True)}),
        'validate_certs': dict(type='bool', default=True), 'rotate_credential': dict(type='bool', default=False),
    }, supports_check_mode=True)
    p = module.params
    try:
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', p['guid']) or not 1 <= p['node']['port'] <= 65535:
            raise ConfigurationError('Invalid student GUID or SSH port')
        fqdn = discover_fqdn(p['node'], p['guid'])
        with requests.Session() as http:
            http.auth = (p['username'], p['password'])
            http.verify = p['validate_certs']
            result = configure(http, p['url'], p['username'], p['guid'], p['node'], fqdn,
                               module.check_mode, p['rotate_credential'])
        module.exit_json(**result)
    except Exception as error:
        module.fail_json(msg=str(error) if isinstance(error, ConfigurationError) else
                         'Managed-node/AAP configuration failed; response contents suppressed')


if __name__ == '__main__':
    main()
