"""Exercise vault-credential selection and leaderboard readiness failure modes."""
import copy
import importlib.util
import json
import secrets
import os
import shutil
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

import yaml
from ansible.errors import AnsibleLookupError
from ansible.parsing.vault import VaultLib, VaultSecret


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


vault = load('catalog_vault', ROOT / 'plugins/lookup/vault_flags.py')
health = load('leaderboard_health', ROOT / 'plugins/modules/aia_leaderboard_health.py')


class CatalogVaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'flags.vault'
        self.password = secrets.token_hex(18)
        self.flags = {'answer': 'synthetic-event-answer'}
        self.encrypt({'ctfd_flags': self.flags})

    def encrypt(self, data):
        self.path.write_bytes(VaultLib([('default', VaultSecret(self.password.encode()))]).encrypt(yaml.safe_dump(data).encode()))

    def test_uses_matching_existing_named_rhdp_credential(self):
        bundle = vault.read_bundle(self.path, vault_secrets=[('other', VaultSecret(b'wrong-key')),
                                                           ('gpte_vault_0', VaultSecret(self.password.encode()))])
        self.assertEqual(bundle['ctfd_flags'], self.flags)
        self.assertEqual(bundle['source_password'], self.password)

    def test_explicit_override_must_match_instead_of_falling_back(self):
        with self.assertRaises(AnsibleLookupError):
            vault.read_bundle(self.path, 'wrong-override', [('gpte_vault_0', VaultSecret(self.password.encode()))])

    def test_errors_do_not_echo_answers_keys_or_ciphertext(self):
        for value in [b'ctfd_flags: {answer: unencrypted}', b'$ANSIBLE_VAULT;malformed']:
            self.path.write_bytes(value)
            with self.assertRaises(AnsibleLookupError) as error:
                vault.read_bundle(self.path, self.password)
            self.assertNotIn(self.password, str(error.exception)); self.assertNotIn('synthetic-event-answer', str(error.exception))

    def test_empty_and_placeholder_answers_are_rejected(self):
        for flags in [{}, {'answer': ''}, {'answer': 'CHANGEME'}, {'answer': None}]:
            self.encrypt({'ctfd_flags': flags})
            with self.assertRaises(AnsibleLookupError): vault.read_bundle(self.path, self.password)


def snapshots():
    objects = {
        'nodes': [{'metadata': {'name': str(i), 'labels': {'node-role.kubernetes.io/worker': ''}},
                   'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}} for i in range(3)],
        'storageclasses': [{'metadata': {'name': 'ocs-storagecluster-cephfs'}, 'provisioner': 'openshift-storage.cephfs.csi.ceph.com'}],
        'storageclusters': [{'status': {'conditions': [{'type': 'Available', 'status': 'True'}]}}],
        'cephclusters': [{'status': {'ceph': {'health': 'HEALTH_OK'}}}],
        'aap_pvcs': [{'spec': {'storageClassName': 'ocs-storagecluster-cephfs', 'accessModes': ['ReadWriteMany']}, 'status': {'phase': 'Bound'}}],
        'aap': [{'status': {'conditions': [{'type': 'Successful', 'status': 'True'}]}}],
        'devspaces': [{'status': {'chePhase': 'Active'}, 'spec': {'devEnvironments': {
            'maxNumberOfWorkspacesPerUser': 2, 'maxNumberOfRunningWorkspacesPerUser': 2, 'storage': {'pvcStrategy': 'per-workspace'}}}}],
    }
    for namespace in ['aap', 'gitea', 'keycloak', 'openshift-devspaces', 'openshift-storage']:
        objects[namespace + '_deployments'] = [{'metadata': {'name': 'application', 'generation': 2},
                                               'spec': {'replicas': 1}, 'status': {'readyReplicas': 1, 'observedGeneration': 2}}]
    return [{'item': {'key': key}, 'resources': value} for key, value in objects.items()]


class LeaderboardHealthTests(unittest.TestCase):
    def setUp(self):
        self.data = snapshots(); self.http = MagicMock(); self.http.get.return_value.status_code = 200

    def evaluate(self): return health.evaluate(self.data, 'apps.example.test', http=self.http)

    def get(self, key): return next(r['resources'] for r in self.data if r['item']['key'] == key)

    def test_ready_cluster_requires_all_fourteen_checks(self):
        result = self.evaluate(); self.assertEqual(result['status'], 'passed'); self.assertEqual(len(result['checks']), 14)
        self.assertEqual(self.http.get.call_count, 4)

    def test_ceph_warning_fails_storage_but_other_checks_continue(self):
        self.get('cephclusters')[0]['status']['ceph']['health'] = 'HEALTH_WARN'
        result = self.evaluate(); self.assertEqual(result['checks']['odf_cephfs']['status'], 'failed')
        self.assertEqual(result['checks']['aap_reconciliation']['status'], 'passed'); self.assertEqual(self.http.get.call_count, 4)

    def test_no_rwx_claim_is_not_established_by_a_bound_rwo_claim(self):
        self.get('aap_pvcs')[0]['spec']['accessModes'] = ['ReadWriteOnce']
        self.assertEqual(self.evaluate()['checks']['aap_rwx_claim']['status'], 'failed')

    def test_stale_deployment_generation_fails_even_with_ready_replicas(self):
        self.get('gitea_deployments')[0]['status']['observedGeneration'] = 1
        self.assertEqual(self.evaluate()['checks']['gitea_workloads']['status'], 'failed')

    def test_missing_custom_resources_cannot_pass(self):
        for key, check in [('aap', 'aap_reconciliation'), ('devspaces', 'devspaces_policy'), ('storageclusters', 'odf_cephfs')]:
            with self.subTest(key=key):
                self.data = snapshots(); self.get(key).clear()
                self.assertEqual(self.evaluate()['checks'][check]['status'], 'failed')

    def test_shared_503_is_not_treated_as_an_unconfigured_treasure_route(self):
        self.http.get.return_value.status_code = 503
        result = self.evaluate(); self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['checks']['aap_https']['status'], 'failed')

    def test_raw_workload_values_and_http_bodies_are_not_in_reports(self):
        self.get('aap_deployments')[0]['spec']['template'] = {'secret': 'synthetic-credential'}
        self.http.get.return_value.text = 'synthetic-http-body'
        result = json.dumps(self.evaluate())
        self.assertNotIn('synthetic-credential', result); self.assertNotIn('synthetic-http-body', result)

    @unittest.skipUnless(shutil.which('ansible-playbook'), 'ansible-playbook required')
    def test_destroy_skips_health_without_cluster_credentials_or_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'ansible.cfg'
            config.write_text('[defaults]\nstdout_callback=default\n')
            play = root / 'destroy.yml'
            play.write_text(yaml.safe_dump([{
                'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                'vars': {'ACTION': 'destroy', 'ansible_python_interpreter': sys.executable},
                'roles': ['leaderboard_health'],
            }]))
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(play)],
                                    env=dict(os.environ, ANSIBLE_CONFIG=str(config),
                                             ANSIBLE_ROLES_PATH=str(ROOT / 'roles')),
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('skipping: [localhost]', result.stdout)
            self.assertNotIn('Read core leaderboard readiness objects', result.stdout)
