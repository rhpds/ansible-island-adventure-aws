#!/usr/bin/python
"""Measure actual control/AAP/VM paths without changing challenge content."""
import datetime
import json
import re
import shlex
import socket
import time
import uuid
from urllib.parse import urlsplit

from ansible.module_utils.basic import AnsibleModule
import requests
import yaml


# The exact same standard-library probe runs on the control host, inside an AAP
# execution environment, and on the managed VM. Its input contains no credentials.
PROBE_SCRIPT = r'''
import concurrent.futures, json, socket, ssl, sys, time

def probe(spec):
 result = {"hostname": socket.gethostname(), "targets": [], "status": "passed"}
 timeout = spec["timeout"]
 for target in spec["targets"]:
  item = dict(target)
  try:
   addresses = sorted({x[4][0] for x in socket.getaddrinfo(target["host"], target["port"], type=socket.SOCK_STREAM)})
   if not addresses or len(addresses) > 64:
    raise ValueError("invalid_address_count")
   def connect(address):
    start = time.monotonic()
    try:
     connection = socket.create_connection((address, target["port"]), timeout=timeout)
     connection.close()
     return {"address": address, "connected": True, "seconds": round(time.monotonic()-start, 3)}
    except OSError as error:
     return {"address": address, "connected": False, "error": type(error).__name__, "seconds": round(time.monotonic()-start, 3)}
   with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    item["addresses"] = list(pool.map(connect, addresses))
   reachable = [a for a in item["addresses"] if a["connected"]]
   item["reachable"] = bool(reachable)
   item["partial"] = bool(reachable) and len(reachable) != len(addresses)
   passed = bool(reachable) and (not spec["require_all_addresses"] or not item["partial"])
   if target["scheme"] == "https" and reachable:
    connection = None
    try:
     context = ssl.create_default_context() if spec["validate_certs"] else ssl._create_unverified_context()
     raw = socket.create_connection((reachable[0]["address"], target["port"]), timeout=timeout)
     try:
      connection = context.wrap_socket(raw, server_hostname=target["host"])
     except BaseException:
      raw.close()
      raise
     item["tls"] = "verified" if spec["validate_certs"] else "unverified"
     # An unconfigured OpenShift route retries its absent backend before its
     # expected 503. Keep the TCP/TLS deadline strict while allowing that HTTP
     # response to arrive; otherwise a healthy route appears unreachable.
     connection.settimeout(max(timeout, 10))
     connection.sendall(("GET / HTTP/1.1\r\nHost: " + target["host"] + "\r\nConnection: close\r\n\r\n").encode())
     with connection.makefile("rb") as response:
      status = response.readline(256).decode("ascii", "replace").split()
     if len(status) < 2 or not status[0].startswith("HTTP/"):
      raise ValueError("invalid_http_response")
     item["http_status"] = int(status[1])
     if not (200 <= item["http_status"] < 500 or (target.get("allow_503") and item["http_status"] == 503)):
      passed = False
    except Exception as error:
     passed = False
     item["tls_http_error"] = type(error).__name__
    finally:
     if connection is not None:
      connection.close()
   item["status"] = "passed" if passed else "failed"
  except Exception as error:
   item.update(status="failed", reachable=False, error=type(error).__name__)
  result["targets"].append(item)
  if item["status"] != "passed": result["status"] = "failed"
 return result

if __name__ == "__main__":
 result = probe(json.loads(sys.argv[1]))
 print(json.dumps(result), flush=True)
 sys.exit(0 if result["status"] == "passed" else 1)
'''


class ReadinessError(Exception):
    pass


def https_target(name, url, allow_503=False):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')
            or not re.fullmatch(r'[A-Za-z0-9.-]+', parsed.hostname)):
        raise ReadinessError('Readiness targets must be HTTPS origins without credentials')
    return dict(name=name, scheme='https', host=parsed.hostname, port=parsed.port or 443, allow_503=allow_503)


def local_probe(spec):
    namespace = {'__name__': 'aia_probe'}
    exec(PROBE_SCRIPT, namespace)
    return namespace['probe'](spec)


class Controller:
    def __init__(self, http, url, timeout=300, poll=2):
        self.http = http
        self.origin = url.rstrip('/')
        self.base = self.origin + '/api/controller/v2/'
        self.timeout, self.poll = timeout, poll
        self.jobs = []

    def api(self, method, path, body=None, **params):
        response = self.http.request(method, self.base + path, json=body, params=params,
                                     timeout=30, allow_redirects=False)
        if response.status_code not in (200, 201, 202, 204):
            raise ReadinessError('AAP ' + method + ' ' + path + ' returned HTTP ' + str(response.status_code))
        return response.json() if response.content else {}

    def one(self, path, **params):
        result = self.api('GET', path, **params)
        if result.get('next') or len(result.get('results', [])) != 1:
            raise ReadinessError('AAP resource selection is missing or ambiguous: ' + path)
        return result['results'][0]

    def resources(self, username, node):
        me = self.one('me/')
        if me['username'] != username:
            raise ReadinessError('AAP authentication does not match the requested instructor')
        org = self.one('organizations/', name=username)
        inventory = self.one('inventories/', name='Island Adventure', organization=org['id'])
        credential = self.one('credentials/', name='Managed Node SSH', organization=org['id'])
        if inventory['organization'] != org['id'] or credential['organization'] != org['id']:
            raise ReadinessError('AAP resources do not belong to this instructor organization')
        host = self.one('hosts/', name='rhel', inventory=inventory['id'])
        variables = yaml.safe_load(host.get('variables') or '{}')
        if (not isinstance(variables, dict) or variables.get('ansible_host') != node['host']
                or int(variables.get('ansible_port', 0)) != node['port']):
            raise ReadinessError('AAP rhel host does not match this student CI SSH endpoint')
        ee = self.one('execution_environments/', name='Default execution environment')
        return dict(inventory=inventory['id'], credential=credential['id'], execution_environment=ee['id'])

    def run(self, resources, module_name, limit, module_args=''):
        body = dict(resources, module_name=module_name, module_args=module_args,
                    limit=limit, become_enabled=False)
        job = self.api('POST', 'ad_hoc_commands/', body)
        path = 'ad_hoc_commands/' + str(job['id']) + '/'
        summary = dict(id=job['id'], url=self.base + path, module=module_name, limit=limit, status=job['status'])
        self.jobs.append(summary)
        deadline = time.monotonic() + self.timeout
        while job['status'] not in ('successful', 'failed', 'error', 'canceled'):
            if time.monotonic() >= deadline:
                summary.update(status='failed', error='aap_probe_timeout')
                try:
                    self.api('POST', path + 'cancel/', {})
                    summary['cancellation'] = 'requested'
                except (ReadinessError, requests.RequestException):
                    summary['cancellation'] = 'unconfirmed'
                return dict(summary)
            time.sleep(self.poll)
            job = self.api('GET', path)
            summary['status'] = job['status']
        summary.update(status=job['status'], started=job.get('started'), finished=job.get('finished'))
        # Controller can publish the terminal status before its result events.
        # Retry evidence retrieval, never the command itself, for up to 9 waits.
        for event_attempt in range(10):
            events = self.api('GET', path + 'events/', event__in='runner_on_ok,runner_on_failed,runner_on_unreachable', page_size=50)
            if events.get('next'):
                return dict(summary, status='failed', error='ambiguous_aap_events')
            results = [e.get('event_data', {}).get('res', {}) for e in events.get('results', [])]
            if module_name == 'command':
                for result in results:
                    try:
                        output = json.loads(result.get('stdout', ''))
                        if output.get('status') in ('passed', 'failed') and isinstance(output.get('targets'), list):
                            if output['status'] == 'passed' and job['status'] != 'successful':
                                return dict(summary, status='failed', error='aap_probe_job_not_successful', probe=output)
                            return dict(summary, status=output['status'], probe=output)
                    except (ValueError, TypeError, AttributeError):
                        pass
            elif results:
                break
            if event_attempt < 9:
                time.sleep(1)
        if module_name == 'command':
            return dict(summary, status='failed', error='aap_probe_output_missing')
        if job['status'] == 'successful' and any(r.get('ping') == 'pong' for r in results):
            return dict(summary, status='passed')
        message = ' '.join(str(r.get('msg', '')).lower() for r in results)
        reason = next((label for phrase, label in [('timed out', 'ssh_timeout'), ('permission denied', 'ssh_authentication'),
                                                  ('resolve hostname', 'ssh_dns'), ('no route', 'ssh_route')]
                       if phrase in message), 'see_aap_job')
        return dict(summary, status='failed', error=reason)


def reverse_probe(node, address_results, spec):
    import paramiko
    addresses = [a['address'] for a in address_results if a['connected']]
    if not addresses:
        return dict(status='blocked', error='control_to_vm_ssh_unreachable')
    connection = None
    for address in addresses:
        try:
            connection = socket.create_connection((address, node['port']), timeout=spec['timeout'])
            break
        except OSError:
            pass
    if connection is None:
        return dict(status='failed', error='control_to_vm_ssh_unreachable')
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(node['host'], port=node['port'], sock=connection, username=node['username'], password=node['password'],
                    look_for_keys=False, allow_agent=False, timeout=15, auth_timeout=20, banner_timeout=20)
        _, output, _ = ssh.exec_command(shlex.join(['python3', '-c', PROBE_SCRIPT, json.dumps(spec)]), timeout=90)
        text = output.read(262144).decode()
        status = output.channel.recv_exit_status()
        if status not in (0, 1):
            return dict(status='failed', error='vm_probe_command_failed')
        result = json.loads(text)
        if result.get('status') not in ('passed', 'failed') or not isinstance(result.get('targets'), list):
            raise ValueError('invalid_vm_probe_output')
        return result
    except Exception as error:
        return dict(status='failed', error=type(error).__name__)
    finally:
        ssh.close()
        connection.close()


def measure(http, url, username, guid, node, endpoints, managed_web_url=None,
            timeout=3, require_all=True, validate_certs=True, job_timeout=300, poll=2):
    if username != 'instructor-' + guid or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', guid):
        raise ReadinessError('Readiness instructor identity must match the student GUID')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', node['host']) or not 1 <= node['port'] <= 65535:
        raise ReadinessError('Invalid published SSH endpoint')
    https_target('aap', url)
    shared = [https_target(item['name'], item['url']) for item in endpoints]
    targets = shared + [dict(name='managed_ssh', scheme='tcp', host=node['host'], port=node['port'])]
    if managed_web_url:
        targets.append(https_target('managed_web', managed_web_url, allow_503=True))
    spec = dict(targets=targets, timeout=timeout, require_all_addresses=require_all, validate_certs=validate_certs)
    report = dict(id=uuid.uuid4().hex, guid=guid, instructor=username,
                  started=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  require_all_addresses=require_all, sources={})
    report['sources']['control'] = local_probe(spec)
    api = Controller(http, url, job_timeout, poll)
    try:
        resources = api.resources(username, node)
        # Some supported EEs expose only a versioned Python executable in PATH.
        launcher = ('for interpreter in python3 python3.13 python3.12 python3.11 python3.9; do '
                    'if command -v "$interpreter" >/dev/null 2>&1; then exec "$interpreter" "$@"; fi; '
                    'done; exit 127')
        args = shlex.join(['/bin/sh', '-c', launcher, 'aia-probe', '-c', PROBE_SCRIPT, json.dumps(spec)])
        runtime = api.run(resources, 'command', 'localhost', args)
        report['sources']['aap'] = runtime
        ssh_target = next((t for t in runtime.get('probe', {}).get('targets', []) if t['name'] == 'managed_ssh'), {})
        if ssh_target.get('reachable'):
            report['sources']['aap_ssh'] = api.run(resources, 'ping', 'rhel')
        else:
            report['sources']['aap_ssh'] = dict(status='blocked', error='aap_to_vm_tcp_unreachable_or_probe_failed')
    except Exception as error:
        failure = dict(status='failed', error=str(error) if isinstance(error, ReadinessError) else type(error).__name__)
        if 'aap' not in report['sources']:
            report['sources']['aap'] = failure
            report['sources']['aap_ssh'] = dict(status='blocked', error='aap_probe_failed')
        else:
            report['sources']['aap_ssh'] = failure
    control_ssh = next(t for t in report['sources']['control']['targets'] if t['name'] == 'managed_ssh')
    report['sources']['managed_node'] = reverse_probe(node, control_ssh.get('addresses', []), dict(spec, targets=shared))
    report['aap_commands'] = api.jobs
    report['status'] = 'passed' if all(s['status'] == 'passed' for s in report['sources'].values()) else 'failed'
    report['finished'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return report


def main():
    module = AnsibleModule(argument_spec={
        'url': dict(type='str', required=True), 'username': dict(type='str', required=True),
        'password': dict(type='str', required=True, no_log=True), 'guid': dict(type='str', required=True),
        'node': dict(type='dict', required=True, options={
            'host': dict(type='str', required=True), 'port': dict(type='int', required=True),
            'username': dict(type='str', required=True), 'password': dict(type='str', required=True, no_log=True)}),
        'endpoints': dict(type='list', elements='dict', required=True, options={
            'name': dict(type='str', required=True), 'url': dict(type='str', required=True)}),
        'managed_web_url': dict(type='str'), 'connect_timeout': dict(type='float', default=3),
        'require_all_addresses': dict(type='bool', default=True), 'validate_certs': dict(type='bool', default=True),
        'job_timeout': dict(type='int', default=300),
    }, supports_check_mode=True)
    if module.check_mode:
        module.exit_json(changed=False, skipped=True, msg='Readiness probes launch AAP ad hoc commands; skipped in check mode')
    p = module.params
    if not 0.5 <= p['connect_timeout'] <= 15 or not 30 <= p['job_timeout'] <= 900:
        module.fail_json(msg='Readiness connect/job timeouts are outside the supported bounds')
    try:
        with requests.Session() as http:
            http.auth = (p['username'], p['password'])
            http.verify = p['validate_certs']
            report = measure(http, p['url'], p['username'], p['guid'], p['node'], p['endpoints'],
                             p['managed_web_url'], p['connect_timeout'], p['require_all_addresses'],
                             p['validate_certs'], p['job_timeout'])
        # The role persists the evidence before asserting status, including failures.
        module.exit_json(changed=False, report=report)
    except Exception as error:
        module.fail_json(msg=str(error) if isinstance(error, ReadinessError) else
                         'Readiness configuration failed; response contents suppressed')


if __name__ == '__main__':
    main()
