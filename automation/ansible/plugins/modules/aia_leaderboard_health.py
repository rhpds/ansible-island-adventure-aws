#!/usr/bin/python
"""Evaluate selected, read-only AWS leaderboard resource snapshots."""
import datetime
import uuid
from urllib.parse import urlsplit

from ansible.module_utils.basic import AnsibleModule
import requests


def evaluate(snapshots, domain, minimum_workers=3, rwx_class='ocs-storagecluster-cephfs', http=None):
    objects = {r['item']['key']: r.get('resources', []) for r in snapshots}
    report = {'id': uuid.uuid4().hex, 'started': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'checks': {}}

    def require(value, message):
        if not value:
            raise ValueError(message)

    def check(name, action):
        try:
            action()
            report['checks'][name] = {'status': 'passed'}
        except Exception as error:
            report['checks'][name] = {'status': 'failed', 'error': str(error) if isinstance(error, ValueError) else type(error).__name__}

    def workers():
        nodes = [n for n in objects.get('nodes', []) if 'node-role.kubernetes.io/worker' in n['metadata'].get('labels', {})]
        require(len(nodes) >= minimum_workers, 'Too few worker nodes')
        require(all(any(c['type'] == 'Ready' and c['status'] == 'True' for c in n.get('status', {}).get('conditions', [])) for n in nodes), 'Worker nodes are not all Ready')

    def storage():
        sc = [s for s in objects.get('storageclasses', []) if s['metadata']['name'] == rwx_class]
        require(len(sc) == 1 and 'cephfs' in sc[0].get('provisioner', ''), 'Expected CephFS storage class is missing')
        clusters = objects.get('storageclusters', [])
        require(bool(clusters) and all(any(c['type'] == 'Available' and c['status'] == 'True' for c in s.get('status', {}).get('conditions', [])) for s in clusters), 'ODF StorageCluster is not Available')
        ceph = objects.get('cephclusters', [])
        require(bool(ceph) and all(c.get('status', {}).get('ceph', {}).get('health') == 'HEALTH_OK' for c in ceph), 'Ceph is not HEALTH_OK')

    def hub_storage():
        claims = objects.get('aap_pvcs', [])
        require(bool(claims) and all(c.get('status', {}).get('phase') == 'Bound' for c in claims), 'AAP persistent claims are not all Bound')
        require(any('ReadWriteMany' in c['spec'].get('accessModes', []) and c['spec'].get('storageClassName') == rwx_class for c in claims), 'AAP has no bound CephFS ReadWriteMany claim')

    def aap():
        instances = objects.get('aap', [])
        require(len(instances) == 1 and any(c['type'] == 'Successful' and c['status'] == 'True' for c in instances[0].get('status', {}).get('conditions', [])), 'AAP reconciliation is not Successful')

    def devspaces():
        instances = objects.get('devspaces', [])
        require(len(instances) == 1, 'Dev Spaces CheCluster is missing or ambiguous')
        instance = instances[0];env = instance['spec']['devEnvironments']
        require(instance.get('status', {}).get('chePhase') == 'Active', 'Dev Spaces is not Active')
        require(int(env.get('maxNumberOfWorkspacesPerUser', 0)) >= 2 and int(env.get('maxNumberOfRunningWorkspacesPerUser', 0)) >= 2 and env.get('storage', {}).get('pvcStrategy') == 'per-workspace', 'Dev Spaces persistent concurrent workspace policy is incorrect')

    def workloads(namespace):
        deployments = objects.get(namespace + '_deployments', [])
        require(bool(deployments), 'Required namespace has no deployments: ' + namespace)
        for resource in deployments + objects.get(namespace + '_statefulsets', []):
            wanted = resource['spec'].get('replicas', 1)
            if wanted == 0:
                continue
            status = resource.get('status', {})
            require(status.get('readyReplicas', 0) >= wanted and status.get('observedGeneration') == resource['metadata']['generation'], 'Workload is not ready: ' + namespace + '/' + resource['metadata']['name'])

    check('worker_nodes', workers)
    check('odf_cephfs', storage)
    check('aap_rwx_claim', hub_storage)
    check('aap_reconciliation', aap)
    check('devspaces_policy', devspaces)
    for namespace in ['aap', 'gitea', 'keycloak', 'openshift-devspaces', 'openshift-storage']:
        check(namespace + '_workloads', lambda namespace=namespace: workloads(namespace))
    parsed = urlsplit('https://' + domain)
    if parsed.hostname != domain or any(x in domain for x in '/:@?#'):
        raise ValueError('Invalid leaderboard ingress domain')
    for name, host in [('gitea', 'gitea'), ('aap', 'aap-aap'), ('devspaces', 'devspaces'), ('sso', 'sso')]:
        def route(host=host):
            response = (http or requests).get('https://' + host + '.' + domain, timeout=15, allow_redirects=False)
            require(response.status_code in (200, 302, 303), 'Shared HTTPS endpoint returned HTTP ' + str(response.status_code))
        check(name + '_https', route)
    report['status'] = 'passed' if all(c['status'] == 'passed' for c in report['checks'].values()) else 'failed'
    report['finished'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return report


def main():
    # Task-level no_log hides raw snapshots without treating every nested string
    # (including single-character labels) as a secret to replace in the report.
    module = AnsibleModule(argument_spec={'snapshots': {'type': 'list', 'elements': 'dict', 'required': True},
                                         'domain': {'type': 'str', 'required': True},
                                         'minimum_workers': {'type': 'int', 'default': 3},
                                         'rwx_class': {'type': 'str', 'default': 'ocs-storagecluster-cephfs'}}, supports_check_mode=True)
    if module.check_mode:
        module.exit_json(changed=False, skipped=True, msg='Check mode does not establish leaderboard health')
    try:
        p = module.params
        module.exit_json(changed=False, report=evaluate(p['snapshots'], p['domain'], p['minimum_workers'], p['rwx_class']))
    except Exception:
        module.fail_json(msg='Leaderboard health evaluation failed; response contents suppressed')


if __name__ == '__main__':
    main()
