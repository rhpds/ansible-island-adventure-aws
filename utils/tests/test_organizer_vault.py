"""Organizer keys stay in the selected cluster's Secret, not deployment state."""
import base64
from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import secrets
import sys
import tempfile
import unittest

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_client import CatalogError
from organizer_vault import NAMESPACE, SECRET_NAME, SECRET_KEY, read_organizer_password, seed_organizer_vault


class Response:
    def __init__(self, status, data):
        self.status_code, self.data = status, data

    def json(self):
        return deepcopy(self.data)


class Cluster:
    def __init__(self):
        self.headers, self.calls = {}, []
        self.namespace = False
        self.secret = None
        self.race = False
        self.failure = None

    def request(self, method, url, json=None, **kwargs):
        self.calls.append((method, url, deepcopy(json)))
        assert url.startswith('https://api.example.test:6443/api/v1/')
        assert kwargs['allow_redirects'] is False
        assert Path(self.verify).exists()
        if self.failure:
            raise self.failure
        if url.endswith('/namespaces'):
            self.namespace = True
            return Response(201, json)
        if '/secrets' not in url:
            return Response(200 if self.namespace else 404, {})
        if method == 'GET':
            return Response(200 if self.secret else 404, self.secret or {})
        assert method == 'POST' and self.secret is None
        self.secret = dict(deepcopy(json), metadata={'uid': 'organizer-secret-uid'})
        return Response(409 if self.race else 201, {} if self.race else self.secret)


class OrganizerVaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.password = secrets.token_hex(20)
        self.keyfile = self.directory / 'key'
        self.keyfile.write_text(self.password + '\n')
        self.http = Cluster()
        self.claim = {'summary': {'provision_data': {
            'openshift_api_server_url': 'https://api.example.test:6443',
            'openshift_cluster_admin_token': 'synthetic-cluster-token',
            'openshift_api_ca_cert': '-----BEGIN CERTIFICATE-----\nsynthetic-public-ca\n-----END CERTIFICATE-----',
        }}}

    def seed(self):
        return seed_organizer_vault(self.claim, self.keyfile, self.directory, http=self.http)

    def test_seed_is_verified_idempotent_and_returns_metadata_only(self):
        output = io.StringIO()
        with redirect_stdout(output):
            first, second = self.seed(), self.seed()
        self.assertEqual(first, {'namespace': NAMESPACE, 'name': SECRET_NAME, 'uid': 'organizer-secret-uid'})
        self.assertEqual(first, second)
        self.assertEqual(sum(c[0] == 'POST' and c[1].endswith('/secrets') for c in self.http.calls), 1)
        self.assertEqual(base64.b64decode(self.http.secret['data'][SECRET_KEY]).decode(), self.password)
        self.assertNotIn(self.password, str(first) + output.getvalue())
        self.assertFalse(Path(self.http.verify).exists())

    def test_existing_different_key_is_retained(self):
        self.seed()
        self.keyfile.write_text('different-organizer-key')
        before = deepcopy(self.http.secret)
        with self.assertRaisesRegex(CatalogError, 'refusing to overwrite') as error:
            self.seed()
        self.assertEqual(self.http.secret, before)
        self.assertNotIn(self.password, str(error.exception))

    def test_concurrent_creation_is_confirmed_without_overwriting(self):
        self.http.race = True
        self.assertEqual(self.seed()['uid'], 'organizer-secret-uid')
        self.assertTrue(all(c[0] in ['GET', 'POST'] for c in self.http.calls))

    def test_transport_exception_does_not_echo_request_secrets(self):
        self.http.failure = requests.ConnectionError(self.password)
        with self.assertRaises(CatalogError) as error:
            self.seed()
        self.assertNotIn(self.password, str(error.exception))

    def test_invalid_input_fails_before_cluster_requests(self):
        for value in ['', ' \n', 'first\nsecond', 'x' * 16385]:
            self.keyfile.write_text(value)
            with self.assertRaises(CatalogError):
                self.seed()
        self.assertEqual(self.http.calls, [])

    def test_role_and_bootstrap_use_the_same_secret_contract(self):
        root = Path(__file__).resolve().parents[2]
        defaults = yaml.safe_load((root / 'automation/ansible/roles/student_post_provision/defaults/main.yml').read_text())
        self.assertEqual(defaults['aia_event_vault_secret_namespace'], NAMESPACE)
        self.assertEqual(defaults['aia_event_vault_secret_name'], SECRET_NAME)
        self.assertEqual(defaults['aia_event_vault_secret_key'], SECRET_KEY)


if __name__ == '__main__':
    unittest.main()
