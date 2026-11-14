"""Workshop API contracts and recovery; no live orders are placed."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_client import CatalogError, read_json
from dev_environment import BABYLON, STUDENT, Deployment, build_claim, new_run, parser
from student_workshop import deploy_workshop, validate_workshop_catalog, workshop_bodies
from test_dev_environment import DATA, SESSION, FakeCatalog, catalog, ready_claim, run_state
from provision_fields import student_fields


class WorkshopCatalog(FakeCatalog):
    def __init__(self):
        super().__init__()
        self.resources = {}
        self.lose_kind = None
        self.child_state = "started"
        self.patches = []
        self.replace_on_pause = False
        self.lose_pause_response = False
        self.reject_pause = False

    def request(self, method, path, body=None):
        if method == "GET":
            if "?labelSelector=" in path:
                return {"items": deepcopy(list(self.claims.values()))}
            if path not in self.resources:
                raise CatalogError("Not found", 404)
            return deepcopy(self.resources[path])
        if method == "PATCH":
            self.patches.append(deepcopy(body))
            resource = self.resources[path]
            if self.replace_on_pause:
                resource["metadata"]["uid"] = "replacement"
            if self.reject_pause or resource["metadata"]["uid"] != body[0]["value"]:
                raise CatalogError("Pause rejected", 409)
            resource["spec"]["concurrency"] = body[1]["value"]
            if self.lose_pause_response:
                raise CatalogError("Synthetic lost pause response")
            return deepcopy(resource)
        if method != "POST":
            raise AssertionError("Unexpected mutation: " + method)
        self.posts.append(deepcopy(body))
        resource = deepcopy(body)
        resource["metadata"]["uid"] = "uid-" + body["kind"]
        self.resources[path + "/" + body["metadata"]["name"]] = resource
        if body["kind"] == "WorkshopProvision":
            for index in range(body["spec"]["count"]):
                claim_body = build_claim(catalog(STUDENT, True), SESSION, "user-developer",
                                         "student-" + str(index), "unused", self.server,
                                         student_fields({"summary": {"provision_data": DATA}}),
                                         keep_on_failure="autoDetach" not in body["spec"])
                claim_body["metadata"]["labels"].update({
                    BABYLON + "workshop-uid": "uid-Workshop",
                    BABYLON + "workshop-provision": body["metadata"]["name"],
                })
                claim_body["metadata"]["ownerReferences"] = [{"uid": "uid-WorkshopProvision"}]
                claim = ready_claim(claim_body)
                claim["status"]["summary"]["state"] = self.child_state
                self.claims[claim["metadata"]["name"]] = claim
        if body["kind"] == self.lose_kind:
            self.lose_kind = None
            raise CatalogError("Synthetic lost response")
        return deepcopy(resource)


class WorkshopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.client = WorkshopCatalog()
        run = run_state()
        run.update(workshop={"user_count": 2}, keep_on_failure=True,
                   student_source={"repo": "https://github.com/example/content.git", "ref": "dev", "commit": "a" * 40})
        self.deployment = Deployment(self.client, self.directory, run, poll=0.001, timeout=0.01)
        self.fields = student_fields({"summary": {"provision_data": DATA}})
        self.output = io.StringIO()
        capture = redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        checker = patch("dev_environment.check_content_source", side_effect=lambda source: source)
        checker.start()
        self.addCleanup(checker.stop)
        smoke = patch("student_workshop.smoke_check")
        self.smoke = smoke.start()
        self.addCleanup(smoke.stop)

    def deploy(self):
        deploy_workshop(self.deployment, self.fields)

    def test_form_defaults_and_retention_match_workshop_contract(self):
        item = catalog(STUDENT, True)
        item["spec"]["displayName"] = "LB1663: Ansible Island Adventure Student"
        workshop, provision = workshop_bodies(item, SESSION, self.deployment.run, self.fields, self.client.server)
        self.assertEqual(workshop["spec"]["displayName"], item["spec"]["displayName"])
        self.assertRegex(workshop["spec"]["accessPassword"], r"^[a-z0-9]{8}$")
        self.assertTrue(workshop["spec"]["openRegistration"])
        self.assertFalse(workshop["spec"]["multiuserServices"])
        self.assertEqual(workshop["spec"]["description"], "<p></p>")
        self.assertEqual(provision["spec"]["count"], 2)
        self.assertEqual(provision["spec"]["concurrency"], 10)
        self.assertEqual(provision["spec"]["startDelay"], 30)
        self.assertNotIn("autoDetach", provision["spec"])
        self.assertNotIn("workshop_user_count", provision["spec"]["parameters"])
        self.assertNotIn("stop_timestamp", provision["spec"]["parameters"])
        self.assertIn("stop", workshop["spec"]["actionSchedule"])
        self.assertIn("end", workshop["spec"]["lifespan"])

    def test_canary_concurrency_preserves_requested_student_count(self):
        self.deployment.run['workshop']['concurrency'] = 1
        _, provision = workshop_bodies(catalog(STUDENT, True), SESSION, self.deployment.run,
                                        self.fields, self.client.server)
        self.assertEqual(provision['spec']['count'], 2)
        self.assertEqual(provision['spec']['concurrency'], 1)
        for value in [0, -1, True]:
            with self.assertRaises(ValueError):
                validate_workshop_catalog(catalog(STUDENT, True), 2, value)

    def test_two_children_checked_and_resuming_complete_does_not_reorder(self):
        self.deploy()
        self.assertEqual([body["kind"] for body in self.client.posts], ["Workshop", "WorkshopProvision"])
        self.assertEqual(self.deployment.run["phase"], "complete")
        self.assertEqual(self.smoke.call_count, 2)
        self.assertEqual(len(self.deployment.run["orders"]), 2)
        self.deploy()
        self.assertEqual(len(self.client.posts), 2)
        for path in (self.directory / "runs").glob("*.json"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        password = self.client.posts[0]["spec"]["accessPassword"]
        self.assertNotIn(password, self.output.getvalue())

    def test_lost_workshop_or_provision_response_reconciles_same_identity(self):
        for kind in ("Workshop", "WorkshopProvision"):
            with self.subTest(kind=kind):
                self.setUp()
                self.client.lose_kind = kind
                with self.assertRaisesRegex(CatalogError, "lost response"):
                    self.deploy()
                self.deployment.run = read_json(self.deployment.path)
                self.deploy()
                self.assertEqual(len(self.client.posts), 2)

    def test_failed_children_export_diagnostics_without_cleanup(self):
        self.client.child_state = "provision-failed"
        with self.assertRaisesRegex(CatalogError, "workshop provisioning failed"):
            self.deploy()
        self.assertEqual(len(list((self.directory / "runs").glob("*-student-*.claim.json"))), 2)
        self.assertEqual(len(self.client.posts), 2)
        self.smoke.assert_not_called()
        self.assertNotEqual(self.deployment.run["phase"], "complete")
        self.assertTrue(self.deployment.run["workshop"]["paused_on_failure"])
        provision = next(v for v in self.client.resources.values() if v["kind"] == "WorkshopProvision")
        self.assertEqual(provision["spec"]["concurrency"], 0)
        self.assertEqual(provision["spec"]["count"], 2)
        self.assertEqual(len(self.client.claims), 2)
        self.assertTrue(all(c["status"]["summary"]["state"] == "provision-failed"
                            for c in self.client.claims.values()))

    def test_lost_pause_response_is_confirmed_without_replaying_patch(self):
        self.client.child_state = "provision-failed"
        self.client.lose_pause_response = True
        with self.assertRaisesRegex(CatalogError, "workshop provisioning failed"):
            self.deploy()
        self.assertTrue(self.deployment.run["workshop"]["paused_on_failure"])
        self.assertEqual(len(self.client.patches), 1)
        self.assertEqual(len(self.client.posts), 2)

    def test_replaced_provision_is_not_paused(self):
        self.client.child_state = "provision-failed"
        self.client.replace_on_pause = True
        with self.assertRaisesRegex(CatalogError, "workshop provisioning failed"):
            self.deploy()
        provision = next(v for v in self.client.resources.values() if v["kind"] == "WorkshopProvision")
        self.assertEqual(provision["spec"]["concurrency"], 10)
        self.assertNotIn("paused_on_failure", self.deployment.run["workshop"])
        self.assertIn("Could not confirm", self.output.getvalue())

    def test_rejected_pause_preserves_failure_and_does_not_claim_success(self):
        self.client.child_state = "provision-failed"
        self.client.reject_pause = True
        with self.assertRaisesRegex(CatalogError, "workshop provisioning failed"):
            self.deploy()
        self.assertNotIn("paused_on_failure", self.deployment.run["workshop"])
        self.assertIn("WARNING", self.output.getvalue())

    def test_pending_children_timeout_without_duplicate_orders(self):
        self.client.child_state = "provisioning"
        with self.assertRaisesRegex(CatalogError, "Timed out"):
            self.deploy()
        with self.assertRaisesRegex(CatalogError, "Timed out"):
            self.deploy()
        self.assertEqual(len(self.client.posts), 2)

    def test_changed_showroom_ref_prevents_provision_submission(self):
        with patch("dev_environment.check_content_source", return_value={"commit": "b" * 40}):
            with self.assertRaisesRegex(CatalogError, "ref moved"):
                self.deploy()
        self.assertEqual([body["kind"] for body in self.client.posts], ["Workshop"])

    def test_replaced_workshop_is_rejected(self):
        self.deploy()
        workshop = next(value for value in self.client.resources.values() if value["kind"] == "Workshop")
        workshop["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(CatalogError, "identity changed"):
            self.deploy()
        self.assertEqual(len(self.client.posts), 2)

    def test_limits_and_disabled_workshops_fail_before_new_run(self):
        for changes, count in (({}, 0), ({"workshopUiMaxInstances": 1}, 2),
                               ({"workshopUiDisabled": True}, 2), ({"workshopUserMode": "multi"}, 2)):
            with self.subTest(changes=changes):
                item = catalog(STUDENT, True)
                item["spec"].update(changes)
                with self.assertRaises(ValueError):
                    validate_workshop_catalog(item, count)
        args = parser().parse_args(["--state-dir", str(self.directory), "new-student", "--workshop",
                                   "--workshop-user-count", "0", "--accept-warnings", "--leaderboard", "existing"])
        with self.assertRaises(ValueError):
            new_run(args, self.client)
        self.assertFalse((self.directory / "latest.json").exists())

    def test_single_claim_keep_on_failure_omits_auto_detach(self):
        body = build_claim(catalog(STUDENT, True), SESSION, "user-developer", "student", "run",
                           self.client.server, self.fields, keep_on_failure=True)
        self.assertNotIn("autoDetach", body["spec"])


if __name__ == "__main__":
    unittest.main()
