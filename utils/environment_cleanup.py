"""UID-checked catalog cleanup: workshops/students first, shared leaderboard last."""

import time
from urllib.parse import quote

from catalog_client import CatalogError, secure_write
from provision_fields import extract_provision_data, load_claim


def resource_path(namespace, plural, name=None):
    group = 'poolboy.gpte.redhat.com' if plural == 'resourceclaims' else 'babylon.gpte.redhat.com'
    path = f"/apis/{group}/v1/namespaces/{quote(namespace, safe='')}/{plural}"
    return path + '/' + quote(name, safe='') if name else path


def get_optional(client, path):
    try:
        return client.request('GET', path)
    except CatalogError as error:
        if error.status == 404:
            return None
        raise


def identity(resource, plural):
    meta = resource.get('metadata') or {}
    if not all(isinstance(meta.get(key), str) and meta[key] for key in ('namespace', 'name', 'uid')):
        raise ValueError('Cleanup snapshots must include namespace, name, and UID')
    return {'namespace': meta['namespace'], 'name': meta['name'], 'uid': meta['uid'], 'plural': plural}


def same_identity(target, resource):
    if identity(resource, target['plural']) != target:
        raise CatalogError('Cleanup target was replaced; refusing to delete a different resource')


def catalog_matches(resource, reference):
    namespace, name = reference.split('/', 1)
    labels = resource.get('metadata', {}).get('labels', {})
    return (labels.get('babylon.gpte.redhat.com/catalogItemNamespace') == namespace
            and labels.get('babylon.gpte.redhat.com/catalogItemName') == name)


def cluster_api(resource):
    data = extract_provision_data(resource)
    params = resource.get('spec', {}).get('provider', {}).get('parameterValues', {})
    value = (params.get('openshift_api_server_url') or data.get('openshift_api_server_url')
             or data.get('openshift_api_url') or '')
    return value.rstrip('/') if isinstance(value, str) else ''


def plan_cleanup(client, leaderboard_snapshot, workshop_snapshots, student_snapshots,
                 leaderboard_catalog, student_catalog, include_linked=False):
    leaderboard = identity(leaderboard_snapshot, 'resourceclaims')
    if not catalog_matches(leaderboard_snapshot, leaderboard_catalog):
        raise ValueError('Leaderboard snapshot belongs to a different catalog item')
    api = cluster_api(leaderboard_snapshot)
    if not api.startswith('https://'):
        raise ValueError('Leaderboard snapshot lacks a cluster API identity')
    namespace = leaderboard['namespace']
    live_lb = get_optional(client, resource_path(namespace, 'resourceclaims', leaderboard['name']))
    if live_lb:
        same_identity(leaderboard, live_lb)
        if not catalog_matches(live_lb, leaderboard_catalog) or cluster_api(live_lb) != api:
            raise CatalogError('Live leaderboard differs from the supplied deployment')
    claims = client.request('GET', resource_path(namespace, 'resourceclaims')).get('items', [])
    workshops = {}
    students = {}
    for snapshot in workshop_snapshots:
        target = identity(snapshot, 'workshops')
        if target['namespace'] != namespace or snapshot.get('kind') != 'Workshop':
            raise ValueError('Workshop snapshot is not a Workshop in the leaderboard namespace')
        live = get_optional(client, resource_path(namespace, 'workshops', target['name']))
        if live:
            same_identity(target, live)
        workshops[target['name']] = target
    for snapshot in student_snapshots:
        target = identity(snapshot, 'resourceclaims')
        if target['namespace'] != namespace or not catalog_matches(snapshot, student_catalog) or cluster_api(snapshot) != api:
            raise ValueError('Student snapshot does not belong to the selected leaderboard')
        live = get_optional(client, resource_path(namespace, 'resourceclaims', target['name']))
        if live:
            same_identity(target, live)
            if not catalog_matches(live, student_catalog) or cluster_api(live) != api:
                raise CatalogError('Live student no longer belongs to the selected leaderboard')
        students[target['name']] = target
    linked = [claim for claim in claims if catalog_matches(claim, student_catalog) and cluster_api(claim) == api]
    for claim in linked:
        labels = claim['metadata'].get('labels', {})
        workshop_name = labels.get('babylon.gpte.redhat.com/workshop')
        if include_linked and workshop_name and workshop_name not in workshops:
            live = get_optional(client, resource_path(namespace, 'workshops', workshop_name))
            if live:
                if live['metadata']['uid'] != labels.get('babylon.gpte.redhat.com/workshop-uid'):
                    raise CatalogError('Linked student has an unexpected workshop identity')
                workshops[workshop_name] = identity(live, 'workshops')
        belongs = workshop_name in workshops and labels.get('babylon.gpte.redhat.com/workshop-uid') == workshops[workshop_name]['uid']
        if claim['metadata']['name'] not in students and not (include_linked or belongs):
            raise CatalogError('Other student CIs still depend on this leaderboard; supply their snapshots or --include-linked-students')
        students[claim['metadata']['name']] = identity(claim, 'resourceclaims')
    # Removing a Workshop must not cascade to students on another leaderboard.
    for claim in claims:
        labels = claim['metadata'].get('labels', {})
        workshop = workshops.get(labels.get('babylon.gpte.redhat.com/workshop'))
        if workshop and labels.get('babylon.gpte.redhat.com/workshop-uid') == workshop['uid']:
            if not catalog_matches(claim, student_catalog) or cluster_api(claim) != api:
                raise CatalogError('Workshop contains resources outside this leaderboard; refusing broad cleanup')
    return {'server': client.server, 'cluster_api': api, 'student_catalog': student_catalog,
            'workshops': list(workshops.values()), 'students': list(students.values()), 'leaderboard': leaderboard}


def delete_exact(client, target):
    path = resource_path(target['namespace'], target['plural'], target['name'])
    live = get_optional(client, path)
    if live is None:
        return
    same_identity(target, live)
    if live['metadata'].get('deletionTimestamp'):
        return
    try:
        client.request('DELETE', path, {'apiVersion': 'v1', 'kind': 'DeleteOptions',
                       'preconditions': {'uid': target['uid']}, 'propagationPolicy': 'Foreground'})
    except CatalogError as error:
        if error.status != 404:
            raise


def wait_deleted(client, targets, poll, timeout):
    deadline = time.monotonic() + timeout
    last = None
    while True:
        pending = []
        for target in targets:
            live = get_optional(client, resource_path(target['namespace'], target['plural'], target['name']))
            if live:
                same_identity(target, live)
                pending.append(target['name'])
        if not pending:
            return
        if pending != last:
            print('Waiting for catalog cleanup: ' + ', '.join(pending), flush=True)
            last = pending
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CatalogError('Cleanup timed out; rerun destroy with the same snapshots to continue')
        time.sleep(min(poll, remaining))


def execute_cleanup(client, plan, poll=15, timeout=3600):
    for target in plan['workshops']:
        delete_exact(client, target)
    for target in plan['students']:
        delete_exact(client, target)
    wait_deleted(client, plan['workshops'] + plan['students'], poll, timeout)
    lb = plan['leaderboard']
    remaining = client.request('GET', resource_path(lb['namespace'], 'resourceclaims')).get('items', [])
    if any(catalog_matches(c, plan['student_catalog']) and cluster_api(c) == plan['cluster_api'] for c in remaining):
        raise CatalogError('A student CI still depends on this leaderboard; preserving the leaderboard')
    delete_exact(client, lb)
    wait_deleted(client, [lb], poll, timeout)


def destroy_environment(client, args):
    plan = plan_cleanup(client, load_claim(args.leaderboard_file),
                        [load_claim(p) for p in args.workshop_file],
                        [load_claim(p) for p in args.student_file],
                        args.leaderboard_catalog, args.student_catalog, args.include_linked_students)
    secure_write(args.state_dir / 'destroy-plan.json', plan)
    for group in ('workshops', 'students', 'leaderboard'):
        targets = [plan[group]] if group == 'leaderboard' else plan[group]
        for target in targets:
            print(f"{target['plural']}: {target['namespace']}/{target['name']} (UID {target['uid']})")
    if not args.execute:
        print('Plan only. Use --execute to destroy exactly these resources; progress and data will be lost.')
        return
    execute_cleanup(client, plan, args.poll, args.timeout)
    print('Catalog cleanup complete: student resources removed before the shared leaderboard.')
