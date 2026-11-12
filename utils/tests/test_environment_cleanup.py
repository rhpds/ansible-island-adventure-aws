"""Cleanup must be dependency-ordered, scoped, resumable, and UID-checked."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_client import CatalogError
from dev_environment import BABYLON, LEADERBOARD, STUDENT
from environment_cleanup import plan_cleanup, execute_cleanup, resource_path


API = 'https://api.example.test:6443'
NS = 'user-developer'


def claim(name, reference, api=API):
    namespace, item = reference.split('/')
    return {'kind': 'ResourceClaim', 'metadata': {
        'namespace': NS, 'name': name, 'uid': 'uid-' + name,
        'labels': {BABYLON + 'catalogItemNamespace': namespace, BABYLON + 'catalogItemName': item}},
        'spec': {'provider': {'parameterValues': {'openshift_api_server_url': api}}},
        'status': {'summary': {'provision_data': {'openshift_api_server_url': api}}}}


def workshop(name):
    return {'kind': 'Workshop', 'metadata': {'namespace': NS, 'name': name, 'uid': 'uid-' + name}}


class CleanupCatalog:
    server = 'https://catalog.example.test'

    def __init__(self):
        self.resources = {}
        self.deletes = []
        self.fail_delete = None
        self.new_student_after_delete = False

    def add(self, obj):
        plural = 'workshops' if obj['kind'] == 'Workshop' else 'resourceclaims'
        self.resources[resource_path(NS, plural, obj['metadata']['name'])] = deepcopy(obj)

    def request(self, method, path, body=None):
        if method == 'GET' and path == resource_path(NS, 'resourceclaims'):
            return {'items': [deepcopy(r) for r in self.resources.values() if r['kind'] == 'ResourceClaim']}
        if path not in self.resources:
            raise CatalogError('Not found', 404)
        if method == 'GET':
            return deepcopy(self.resources[path])
        if method != 'DELETE':
            raise AssertionError('Unexpected mutation: ' + method)
        if path.endswith('/' + str(self.fail_delete)):
            raise CatalogError('Synthetic deletion failure')
        self.assert_uid(path, body)
        obj = self.resources.pop(path)
        self.deletes.append((obj['metadata']['name'], deepcopy(body)))
        if self.new_student_after_delete and obj['metadata']['name'] == 'student':
            self.add(claim('new-dependent', STUDENT))
        return {'kind': 'Status', 'status': 'Success'}

    def assert_uid(self, path, body):
        if body['preconditions']['uid'] != self.resources[path]['metadata']['uid']:
            raise CatalogError('Conflict', 409)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.client = CleanupCatalog()
        self.lb = claim('leaderboard', LEADERBOARD)
        self.student = claim('student', STUDENT)
        self.ws = workshop('workshop')
        self.student['metadata']['labels'].update({BABYLON + 'workshop': 'workshop', BABYLON + 'workshop-uid': 'uid-workshop'})
        for obj in (self.lb, self.student, self.ws):
            self.client.add(obj)

    def plan(self, include=False, workshops=None):
        return plan_cleanup(self.client, self.lb, [self.ws] if workshops is None else workshops, [],
                            LEADERBOARD, STUDENT, include)

    def test_workshop_students_deleted_before_leaderboard_with_uid_preconditions(self):
        plan = self.plan()
        execute_cleanup(self.client, plan, poll=0.001, timeout=0.01)
        self.assertEqual([name for name, _ in self.client.deletes], ['workshop', 'student', 'leaderboard'])
        for name, body in self.client.deletes:
            self.assertEqual(body['preconditions']['uid'], 'uid-' + name)
            self.assertEqual(body['propagationPolicy'], 'Foreground')
        execute_cleanup(self.client, plan, poll=0.001, timeout=0.01)
        self.assertEqual(len(self.client.deletes), 3)

    def test_unselected_dependent_blocks_cleanup_before_any_deletion(self):
        self.client.add(claim('other-student', STUDENT))
        with self.assertRaisesRegex(CatalogError, 'Other student CIs'):
            self.plan()
        self.assertEqual(self.client.deletes, [])

    def test_include_linked_students_excludes_other_clusters_and_finds_workshops(self):
        self.client.add(claim('other-student', STUDENT))
        self.client.add(claim('unrelated-cluster', STUDENT, 'https://other.example.test'))
        plan = self.plan(include=True, workshops=[])
        self.assertEqual({s['name'] for s in plan['students']}, {'student', 'other-student'})
        self.assertEqual([w['name'] for w in plan['workshops']], ['workshop'])

    def test_mixed_cluster_workshop_cannot_cascade_delete_unrelated_students(self):
        other = claim('other-student', STUDENT, 'https://other.example.test')
        other['metadata']['labels'].update(self.student['metadata']['labels'])
        self.client.add(other)
        with self.assertRaisesRegex(CatalogError, 'outside this leaderboard'):
            self.plan()
        self.assertEqual(self.client.deletes, [])

    def test_replaced_snapshot_uid_is_rejected(self):
        self.lb['metadata']['uid'] = 'replacement'
        with self.assertRaisesRegex(CatalogError, 'replaced'):
            self.plan()

    def test_replaced_uid_after_plan_is_rejected(self):
        plan = self.plan()
        self.client.resources[resource_path(NS, 'workshops', 'workshop')]['metadata']['uid'] = 'replacement'
        with self.assertRaisesRegex(CatalogError, 'replaced'):
            execute_cleanup(self.client, plan)
        self.assertEqual(self.client.deletes, [])

    def test_failed_student_deletion_preserves_leaderboard(self):
        plan = self.plan()
        self.client.fail_delete = 'student'
        with self.assertRaisesRegex(CatalogError, 'deletion failure'):
            execute_cleanup(self.client, plan)
        self.assertIn(resource_path(NS, 'resourceclaims', 'leaderboard'), self.client.resources)

    def test_new_dependency_before_leaderboard_delete_preserves_cluster(self):
        plan = self.plan()
        self.client.new_student_after_delete = True
        with self.assertRaisesRegex(CatalogError, 'preserving the leaderboard'):
            execute_cleanup(self.client, plan)
        self.assertIn(resource_path(NS, 'resourceclaims', 'leaderboard'), self.client.resources)


if __name__ == '__main__':
    unittest.main()
