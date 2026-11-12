"""Post-deployment inventory identity checks and secret handling."""

from copy import deepcopy
from pathlib import Path
import secrets
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_client import CatalogError
from dev_environment import STUDENT, LEADERBOARD, build_claim
from student_inventory import inventory_from_claims, kubeconfig_from_claim
from test_dev_environment import DATA, SESSION, ready_claim, run_state


class InventoryTests(unittest.TestCase):
    def setUp(self):
        from test_dev_environment import catalog
        self.run = run_state()
        self.run["workshop"] = {"user_count": 2}
        self.claims = {}
        for role, guid, reference in (("leaderboard", "leader", LEADERBOARD),
                                      ("student-1", "first", STUDENT), ("student-2", "second", STUDENT)):
            body = build_claim(catalog(reference), SESSION, "user-developer", role, self.run["id"], "https://example.test")
            claim = ready_claim(body)
            self.run["orders"][role] = {"namespace": "user-developer", "name": role,
                                        "uid": claim["metadata"]["uid"], "catalog": reference, "external": True}
            if role != "leaderboard":
                d = claim["status"]["summary"]["provision_data"]
                d.update(guid=guid, ctfd_url=f"https://island-instructor-{guid}-ctfd.{DATA['openshift_cluster_ingress_domain']}",
                         gitea_url="https://gitea.example.test", aap_url="https://aap.example.test",
                         devspaces_url="https://devspaces.example.test", gitea_username="instructor-" + guid,
                         gitea_password=secrets.token_hex(12), ctfd_admin_username="admin",
                         ctfd_admin_password=secrets.token_hex(12), ctfd_student_username="lab-user",
                         ctfd_student_password=secrets.token_hex(12), aap_username="instructor-" + guid,
                         managed_node="rhel", password=secrets.token_hex(12), aap_password=secrets.token_hex(12),
                         bastion_public_hostname=f'ssh-{guid}.example.test', bastion_ssh_port='30222',
                         bastion_ssh_user_name='lab-user', bastion_ssh_password=secrets.token_hex(12))
                claim["status"]["resourceHandle"] = {"name": "guid-" + guid}
            self.claims[role] = claim

    def inventory(self):
        return inventory_from_claims(self.run, self.claims, "/private/run.kubeconfig")

    def test_distinct_student_credentials_and_explicit_account_mapping(self):
        inventory = self.inventory()["all"]["children"]["aia_students"]
        self.assertEqual(len(inventory["hosts"]), 2)
        for guid in ("first", "second"):
            host = inventory["hosts"]["instructor-" + guid]
            self.assertEqual(host["student_name"], "lab-user")
            self.assertEqual(host["instructor"]["ctfd_ns"], "instructor-" + guid + "-ctfd")
            self.assertNotEqual(host["common_password"], host["gitea_password"])
            self.assertNotIn("password", host["aia_student"])
        self.assertEqual(inventory["vars"]["aia_kubeconfig"], "/private/run.kubeconfig")

    def test_mixed_up_guid_ctfd_url_and_gitea_owner_are_rejected(self):
        original = deepcopy(self.claims)
        for key, value in (("guid", "wrong"), ("ctfd_url", "https://another.example.test"),
                           ("gitea_username", "instructor-second")):
            with self.subTest(key=key):
                self.claims = deepcopy(original)
                self.claims["student-1"]["status"]["summary"]["provision_data"][key] = value
                with self.assertRaises(ValueError):
                    self.inventory()

    def test_changed_claim_uid_and_pending_deployments_are_rejected(self):
        claim = self.claims["student-1"]
        claim["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(CatalogError, "replaced"):
            self.inventory()
        claim["metadata"]["uid"] = self.run["orders"]["student-1"]["uid"]
        claim["status"]["summary"]["state"] = "provisioning"
        with self.assertRaisesRegex(CatalogError, "started and healthy"):
            self.inventory()

    def test_incomplete_roster_and_missing_secret_fail_without_exposing_values(self):
        self.run["workshop"]["user_count"] = 3
        with self.assertRaisesRegex(ValueError, "student count"):
            self.inventory()
        self.run["workshop"]["user_count"] = 2
        del self.claims["student-1"]["status"]["summary"]["provision_data"]["ctfd_admin_password"]
        with self.assertRaisesRegex(ValueError, "ctfd_admin_password"):
            self.inventory()

    def test_private_managed_node_access_uses_published_endpoint_and_port(self):
        hosts=self.inventory()['all']['children']['aia_students']['hosts']
        host=hosts['instructor-first']
        self.assertEqual(host['aia_managed_node']['host'],'ssh-first.example.test')
        self.assertEqual(host['aia_managed_node']['port'],30222)
        data=self.claims['student-1']['status']['summary']['provision_data']
        self.assertEqual(host['aia_lab_password'],data['password'])
        self.assertEqual(host['aap_password'],data['aap_password'])
        self.assertNotIn('aia_managed_node',host['aia_student'])
        data['bastion_ssh_port']='70000'
        with self.assertRaisesRegex(ValueError,'SSH port'):
            self.inventory()

    def test_optional_web_route_is_exported_only_for_its_student(self):
        first=self.claims['student-1']['status']['summary']['provision_data']
        first['managed_node_web_url']='https://treasure-first.apps.hosting.example.test/'
        hosts=self.inventory()['all']['children']['aia_students']['hosts']
        self.assertEqual(hosts['instructor-first']['aia_student']['managed_node_web_url'],
                         'https://treasure-first.apps.hosting.example.test')
        self.assertNotIn('managed_node_web_url',hosts['instructor-second']['aia_student'])
        for value in ['https://treasure-second.apps.hosting.example.test',
                      'http://treasure-first.apps.hosting.example.test',
                      'https://treasure-first.apps.hosting.example.test/?password=unsafe']:
            first['managed_node_web_url']=value
            with self.assertRaises(ValueError):self.inventory()

    def test_control_host_kubeconfig_uses_leaderboard_token_and_ca(self):
        import base64
        data = self.claims['leaderboard']['status']['summary']['provision_data']
        data.update(openshift_api_server_url='https://api.example.test:6443',
                    openshift_cluster_admin_token='synthetic-api-token',
                    openshift_api_ca_cert='-----BEGIN CERTIFICATE-----\nsynthetic\n-----END CERTIFICATE-----')
        config = kubeconfig_from_claim(self.claims['leaderboard'])
        self.assertEqual(config['current-context'], 'leaderboard')
        self.assertEqual(config['users'][0]['user']['token'], 'synthetic-api-token')
        cluster = config['clusters'][0]['cluster']
        self.assertEqual(cluster['server'], data['openshift_api_server_url'])
        self.assertEqual(base64.b64decode(cluster['certificate-authority-data']).decode(), data['openshift_api_ca_cert'])
        self.assertNotIn('insecure-skip-tls-verify', cluster)


if __name__ == "__main__":
    unittest.main()
