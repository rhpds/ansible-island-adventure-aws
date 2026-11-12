"""Offline tests: no SSO, infrastructure orders, or credentials are needed."""

from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_client import (CatalogClient, CatalogError, DEFAULT_SERVER, browser_login,
                            cookie_map, load_credentials, read_json, secure_write)
from dev_environment import (BABYLON, LEADERBOARD, STUDENT, Deployment, build_claim,
                             claim_state, main, smoke_check, state_lock)
from provision_fields import REQUIRED_FIELDS, extract_provision_data, student_fields


SECRET = "synthetic-test-password"
DATA = {f["key"]: SECRET if "password" in f["key"] else f["key"] for f in REQUIRED_FIELDS}
SESSION = {"token": "synthetic-session-token", "user": "developer@example.test", "lifetime": 300,
           "userNamespace": {"name": "user-developer"}, "groups": [],
           "serviceNamespaces": [{"name": "user-developer", "requester": "developer@example.test"}],
           "catalogNamespaces": [{"name": "babylon-catalog-dev", "displayName": "Development"}]}


def catalog(reference, student=False):
    namespace, name = reference.split("/")
    parameters = [{"name": "purpose", "required": True, "openAPIV3Schema": {"type": "string", "x-form-options": [
        {"name": "Developing a community demo", "activity": "Asset Development"}]}}]
    if student:
        parameters += [{"name": f["target"], "formLabel": f["label"], "required": True,
                        "openAPIV3Schema": {"type": "string", "minLength": 1}} for f in REQUIRED_FIELDS]
    return {"metadata": {"namespace": namespace, "name": name, "resourceVersion": "123"},
            "spec": {"displayName": name, "parameters": parameters, "lifespan": {"default": "2d", "maximum": "5d"},
                     "termsOfService": "Synthetic catalog warnings"}}


def ready_claim(body):
    claim = deepcopy(body)
    claim["metadata"]["uid"] = "uid-" + body["metadata"]["name"]
    claim["status"] = {"ready": True, "healthy": True, "summary": {"state": "started", "provision_data": deepcopy(DATA)}}
    if "student" in body["metadata"]["name"]:
        claim["status"]["summary"]["provision_data"].update(
            ctfd_url="https://ctfd.example.test", gitea_url="https://gitea.example.test", aap_url="https://aap.example.test")
    return claim


class FakeCatalog:
    server = DEFAULT_SERVER
    session = deepcopy(SESSION)

    def __init__(self):
        self.claims = {}
        self.posts = []
        self.lose_response = False

    def catalog(self, reference):
        return catalog(reference, reference == STUDENT)

    def claim(self, namespace, name):
        if name not in self.claims:
            raise CatalogError("Not found", 404)
        return deepcopy(self.claims[name])

    claim_path = staticmethod(CatalogClient.claim_path)

    def request(self, method, path, body):
        assert method == "POST"
        self.posts.append(deepcopy(body))
        claim = ready_claim(body)
        self.claims[body["metadata"]["name"]] = claim
        if self.lose_response:
            self.lose_response = False
            raise CatalogError("Synthetic lost response")
        return deepcopy(claim)


def run_state():
    return {"id": "012345abcdef", "orders": {}, "catalogs": {"leaderboard": LEADERBOARD, "student": STUDENT},
            "namespace": "user-developer", "activity": "Asset Development", "purpose": "Developing a community demo",
            "skip_checks": False, "server": DEFAULT_SERVER, "user": SESSION["user"], "phase": "provisioning"}


class ProvisionTests(unittest.TestCase):
    def test_precedence_and_password_mapping(self):
        claim = {"summary": {"provision_data": {"keycloak_admin_user": "summary-user"}}, "status": {
            "resources": [{"state": {"spec": {"vars": {"provision_data": DATA}}}}],
            "summary": {"provision_data": {"keycloak_admin_user": "authoritative-user"}}}}
        fields = student_fields(claim)
        self.assertEqual(fields["keycloak_admin_user"], "authoritative-user")
        self.assertEqual(fields["openshift_cluster_common_password"], SECRET)
        self.assertNotIn("openshift_cluster_admin_password", fields)

    def test_missing_empty_wrong_type_are_rejected_without_values(self):
        for value in (None, "", "  ", {}, 42):
            with self.subTest(value=value):
                data = {**DATA, "keycloak_admin_password": value}
                with self.assertRaises(ValueError) as error:
                    student_fields({"summary": {"provision_data": data}})
                self.assertNotIn(SECRET, str(error.exception))

    def test_invalid_provision_container_has_no_traceback_or_values(self):
        for claim in ({"status": SECRET}, {"status": {"resources": SECRET}},
                      {"summary": {"provision_data": [SECRET]}}):
            with self.subTest(claim=claim), self.assertRaises(ValueError) as error:
                extract_provision_data(claim)
            self.assertNotIn(SECRET, str(error.exception))

    def test_extractor_json_and_private_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "claim.yaml", Path(directory) / "fields.json"
            source.write_text(json.dumps({"summary": {"provision_data": DATA}}))
            result = subprocess.run([sys.executable, str(Path(__file__).parents[1] / "extract-provision-fields.py"),
                                     str(source), str(target), "--format", "json"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["openshift_cluster_common_password"], SECRET)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            source.write_text("summary: {provision_data: {}}")
            before = target.read_text()
            result = subprocess.run([sys.executable, str(Path(__file__).parents[1] / "extract-provision-fields.py"),
                                     str(source), str(target)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertEqual(before, target.read_text())


class AuthTests(unittest.TestCase):
    def response(self, status=200, data=None, content_type="application/json"):
        response = Mock(status_code=status, headers={"Content-Type": content_type})
        response.json.return_value = data if data is not None else deepcopy(SESSION)
        return response

    def client(self, responses):
        http = requests.Session()
        http.request = Mock(side_effect=responses)
        return CatalogClient(DEFAULT_SERVER, {"__oauth_proxy": "synthetic-cookie"}, http=http)

    def test_session_refresh_and_custom_authentication_header(self):
        client = self.client([self.response(), self.response(401), self.response(), self.response(data={"kind": "CatalogItem"})])
        client.authenticate()
        self.assertEqual(client.catalog(STUDENT)["kind"], "CatalogItem")
        calls = client.http.request.call_args_list
        self.assertEqual(calls[1].kwargs["headers"]["Authentication"], "Bearer " + SESSION["token"])
        self.assertNotIn("Authorization", calls[1].kwargs["headers"])
        self.assertTrue(all(c.kwargs["allow_redirects"] is False for c in calls))
        self.assertEqual(calls[2].args[1], DEFAULT_SERVER + "/auth/session")

    def test_redirect_and_html_stop_for_sso_without_following(self):
        for response in (self.response(302), self.response(content_type="text/html")):
            client = self.client([response])
            with self.assertRaisesRegex(CatalogError, "SSO"):
                client.authenticate()
            self.assertEqual(client.http.request.call_count, 1)

    def test_error_body_cannot_leak_password(self):
        response = self.response(400, {"message": SECRET})
        client = self.client([self.response(), response])
        with self.assertRaises(CatalogError) as error:
            client.catalog(STUDENT)
        self.assertNotIn(SECRET, str(error.exception))

    def test_no_retry_of_ambiguous_post(self):
        client = self.client([self.response(), requests.Timeout()])
        with self.assertRaises(CatalogError):
            client.request("POST", "/apis/example", {"secret": SECRET})
        self.assertEqual(client.http.request.call_count, 2)

    def test_transient_reads_retry_without_replaying_mutations(self):
        client = self.client([requests.Timeout(), self.response(), self.response(503), self.response(data={"kind": "CatalogItem"})])
        with patch("catalog_client.time.sleep"):
            self.assertEqual(client.catalog(STUDENT)["kind"], "CatalogItem")
        self.assertEqual(client.http.request.call_count, 4)

    def test_read_retries_are_bounded(self):
        client = self.client([requests.Timeout(), requests.Timeout(), requests.Timeout()])
        with patch("catalog_client.time.sleep"), self.assertRaisesRegex(CatalogError, "read request"):
            client.authenticate()
        self.assertEqual(client.http.request.call_count, 3)

    def test_browser_callback_handoff(self):
        callbacks = []
        threads = []

        def open_browser(url):
            callback = parse_qs(urlsplit(url).query)["callback"][0]

            def deliver():
                # Random callback path rejects unrelated cross-site posts.
                wrong = requests.post(callback + "-wrong", data={"token": "wrong"}, timeout=5)
                self.assertEqual(wrong.status_code, 404)
                result = requests.post(callback, data={"token": "synthetic", "cookie___oauth_proxy": "synthetic-cookie"}, timeout=5)
                callbacks.append(result.status_code)

            thread = threading.Thread(target=deliver)
            threads.append(thread)
            thread.start()
            return True

        with patch("catalog_client.webbrowser.open", side_effect=open_browser), redirect_stdout(io.StringIO()):
            cookies = browser_login(DEFAULT_SERVER, timeout=5)
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(cookies["__oauth_proxy"], "synthetic-cookie")
        self.assertEqual(callbacks, [200])

    def test_cookie_import_filters_host_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.json"
            path.write_text(json.dumps({"cookies": [
                {"name": "valid", "value": "yes", "domain": ".demo.redhat.com", "expires": -1},
                {"name": "other", "value": "no", "domain": "other.example.test"},
                {"name": "expired", "value": "no", "domain": "catalog.demo.redhat.com", "expires": 1}]}))
            self.assertEqual(cookie_map(path, DEFAULT_SERVER), {"valid": "yes"})
            path.write_text('# Netscape HTTP Cookie File\n#HttpOnly_catalog.demo.redhat.com\tFALSE\t/\tTRUE\t0\t__oauth_proxy\tsynthetic\n')
            self.assertEqual(cookie_map(path, DEFAULT_SERVER), {"__oauth_proxy": "synthetic"})

    def test_reuse_babylon_config_and_reject_server_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "babylon.yaml"
            config.write_text(json.dumps({"server": DEFAULT_SERVER, "auth": {"cookies": {"__oauth_proxy": "synthetic"}}}))
            self.assertEqual(load_credentials(directory, DEFAULT_SERVER, config), {"__oauth_proxy": "synthetic"})
            with self.assertRaisesRegex(CatalogError, "server differs"):
                load_credentials(directory, "https://other.example.test", config)


class OrderTests(unittest.TestCase):
    def build(self, item=None):
        return build_claim(item or catalog(STUDENT, True), SESSION, "user-developer", "aia-student-test",
                           "012345abcdef", DEFAULT_SERVER, student_fields({"summary": {"provision_data": DATA}}))

    def test_dynamic_field_names_and_default_types(self):
        item = catalog(STUDENT, True)
        item["spec"]["parameters"][3]["name"] = "catalog_password_parameter"
        item["spec"]["parameters"].append({"name": "enabled", "openAPIV3Schema": {"type": "boolean", "default": False}})
        claim = self.build(item)
        values = claim["spec"]["provider"]["parameterValues"]
        self.assertEqual(values["catalog_password_parameter"], SECRET)
        self.assertIs(values["enabled"], False)
        self.assertNotIn("purpose_activity", values)
        self.assertNotIn("purpose_explanation", values)
        self.assertEqual(claim["metadata"]["annotations"]["demo.redhat.com/purpose"], "Developing a community demo")
        self.assertEqual(claim["metadata"]["annotations"]["demo.redhat.com/purpose-activity"], "Asset Development")

    def test_leaderboard_matches_successful_browser_order_contract(self):
        # Shape captured from a successful browser order; dates are synthetic.
        expected = read_json(Path(__file__).parent / "fixtures/leaderboard-browser-spec.json")
        item = catalog(LEADERBOARD)
        item["spec"].update(lifespan={"default": "3d"}, runtime={"default": "4h", "maximum": "8h"})
        with patch("dev_environment.utcnow", return_value=datetime(2030, 1, 1, tzinfo=timezone.utc)):
            body = build_claim(item, SESSION, "user-developer", "aia-leaderboard-test", "012345abcdef", DEFAULT_SERVER)
        self.assertEqual(body["spec"], expected)

    def test_activity_is_provider_input_only_if_declared(self):
        item = catalog(STUDENT, True)
        item["spec"]["parameters"].append({"name": "purpose_activity", "openAPIV3Schema": {"type": "string"}})
        self.assertEqual(self.build(item)["spec"]["provider"]["parameterValues"]["purpose_activity"], "Asset Development")

    def test_auto_stop_follows_catalog_supported_actions(self):
        for config in ({"supportedActions": {"start": {}}}, {"runtime": {"default": "999h"}}):
            item = catalog(STUDENT, True)
            item["spec"].update(config)
            self.assertNotIn("stop_timestamp", self.build(item)["spec"]["provider"]["parameterValues"])

    def test_provider_validation_failure_is_visible_without_exposing_values(self):
        claim = {"status": {"provider": {"validationErrors": [
            "No such parameter purpose_activity.",
            "Parameter keycloak_admin_password schema validation failed: " + SECRET]}}}
        with self.assertRaisesRegex(CatalogError, "purpose_activity") as error:
            claim_state(claim)
        self.assertNotIn(SECRET, str(error.exception))

    def test_unknown_required_field_fails_before_submission(self):
        item = catalog(STUDENT, True)
        item["spec"]["parameters"].append({"name": "new_required_field", "required": True})
        with self.assertRaisesRegex(ValueError, "new_required_field"):
            self.build(item)

    def test_catalog_resource_templates_still_order_via_provider(self):
        item = catalog(STUDENT, True)
        item["spec"]["resources"] = [{"name": "catalog-workload-template"}]
        body = self.build(item)
        self.assertEqual(body["spec"]["provider"]["name"], STUDENT.split("/")[1])
        self.assertNotIn("resources", body["spec"])

    def test_schema_error_does_not_print_secret(self):
        item = catalog(STUDENT, True)
        item["spec"]["parameters"][3]["openAPIV3Schema"]["enum"] = ["different"]
        with self.assertRaises(ValueError) as error:
            self.build(item)
        self.assertNotIn(SECRET, str(error.exception))

    def test_ineligible_purpose_fails_before_submission(self):
        item = catalog(STUDENT, True)
        item["spec"]["parameters"][0]["openAPIV3Schema"]["x-form-options"][0]["requiredRoles"] = ["admin"]
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.build(item)

    def test_summary_failure_and_resource_failure(self):
        claim = ready_claim(self.build())
        claim["status"]["resources"] = [{"state": {"spec": {"vars": {"current_state": "provision-failed"}}}}]
        with self.assertRaisesRegex(CatalogError, "provision-failed"):
            claim_state(claim)
        claim["status"]["resources"] = []
        claim["status"]["summary"]["state"] = "provision-failed"
        with self.assertRaises(CatalogError):
            claim_state(claim)

    def test_stopped_unhealthy_deleted_and_expired_are_not_ready(self):
        claim = ready_claim(self.build())
        claim["status"]["summary"]["state"] = "stopped"
        self.assertFalse(claim_state(claim)[1])
        claim["status"]["summary"]["state"] = "started"
        claim["status"]["healthy"] = False
        self.assertFalse(claim_state(claim)[1])
        claim["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
        with self.assertRaises(CatalogError):
            claim_state(claim)
        del claim["metadata"]["deletionTimestamp"]
        claim["spec"]["lifespan"]["end"] = "2020-01-01T00:00:00Z"
        with self.assertRaisesRegex(CatalogError, "expired"):
            claim_state(claim)

    def test_stale_started_summary_cannot_hide_pending_resource(self):
        claim = ready_claim(self.build())
        claim["status"]["resources"] = [{"ready": False, "state": {"spec": {"vars": {"current_state": "provisioning"}}}}]
        self.assertFalse(claim_state(claim)[1])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.client = FakeCatalog()
        self.source = {"repo": "https://github.com/example/content.git", "ref": "dev"}
        resolver = patch("dev_environment.resolve_student_source", return_value=self.source)
        self.resolve_source = resolver.start()
        self.addCleanup(resolver.stop)
        checker = patch("dev_environment.check_content_source", side_effect=lambda source: {**source, "commit": "a" * 40})
        self.check_source = checker.start()
        self.addCleanup(checker.stop)
        self.deployment = Deployment(self.client, self.directory, run_state(), poll=0.001, timeout=1)
        self.output = io.StringIO()
        self.capture = redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def test_bootstrap_then_repeat_student_reuses_leaderboard(self):
        with patch("dev_environment.smoke_check") as check:
            self.deployment.execute()
            first = self.deployment.run
            self.assertEqual(len(self.client.posts), 2)
            check.assert_called_once()
            secure_write(self.directory / "latest.json", {"id": first["id"]})
            args = __import__("dev_environment").parser().parse_args([
                "--state-dir", str(self.directory), "new-student", "--accept-warnings"])
            second = __import__("dev_environment").new_run(args, self.client)
            second.execute()
            self.assertEqual(len(self.client.posts), 3)
            self.assertEqual(second.run["orders"]["leaderboard"]["uid"], first["orders"]["leaderboard"]["uid"])
            self.assertNotEqual(second.run["orders"]["student"]["name"], first["orders"]["student"]["name"])
            self.assertEqual(self.client.posts[-1]["spec"]["provider"]["parameterValues"]["openshift_cluster_common_password"], SECRET)
            self.assertNotIn(SECRET, self.output.getvalue())

    def test_lost_post_response_is_reconciled_on_resume(self):
        self.deployment.prepare("leaderboard")
        self.client.lose_response = True
        with self.assertRaises(CatalogError):
            self.deployment.ensure_submitted("leaderboard")
        saved = read_json(self.deployment.path)
        self.assertTrue(saved["orders"]["leaderboard"]["attempted"])
        resumed = Deployment(self.client, self.directory, saved)
        resumed.ensure_submitted("leaderboard")
        self.assertEqual(len(self.client.posts), 1)

    def test_ambiguous_absent_order_requires_explicit_same_name_retry(self):
        self.deployment.prepare("leaderboard")
        self.deployment.run["orders"]["leaderboard"]["attempted"] = True
        with self.assertRaisesRegex(CatalogError, "retry-submit"):
            self.deployment.ensure_submitted("leaderboard")
        self.assertEqual(len(self.client.posts), 0)
        name = self.deployment.run["orders"]["leaderboard"]["name"]
        self.deployment.retry_submit = True
        self.deployment.ensure_submitted("leaderboard")
        self.assertEqual(self.client.posts[0]["metadata"]["name"], name)

    def test_replaced_claim_uid_cannot_be_adopted(self):
        self.deployment.prepare("leaderboard")
        self.deployment.ensure_submitted("leaderboard")
        order = self.deployment.run["orders"]["leaderboard"]
        self.client.claims[order["name"]]["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(CatalogError, "replaced"):
            self.deployment.ensure_submitted("leaderboard")

    def test_leaderboard_failure_never_orders_student(self):
        self.deployment.prepare("leaderboard")
        self.deployment.ensure_submitted("leaderboard")
        order = self.deployment.run["orders"]["leaderboard"]
        self.client.claims[order["name"]]["status"]["summary"]["state"] = "provision-failed"
        with self.assertRaises(CatalogError):
            self.deployment.execute()
        self.assertEqual(len(self.client.posts), 1)

    def test_provider_rejection_stops_wait_immediately_and_exports_diagnostics(self):
        self.deployment.prepare("leaderboard")
        self.deployment.ensure_submitted("leaderboard")
        order = self.deployment.run["orders"]["leaderboard"]
        self.client.claims[order["name"]]["status"] = {"provider": {"validationErrors": [
            "No such parameter purpose_activity.", "No such parameter purpose_explanation."]}}
        with patch("dev_environment.time.sleep") as sleep:
            with self.assertRaisesRegex(CatalogError, "Provider rejected"):
                self.deployment.wait("leaderboard")
            sleep.assert_not_called()
        snapshot = self.deployment.path.with_name(self.deployment.run["id"] + "-leaderboard.claim.json")
        self.assertTrue(read_json(snapshot)["status"]["provider"]["validationErrors"])
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)

    def test_select_existing_leaderboard_then_new_student_never_orders_leaderboard(self):
        from dev_environment import new_run, parser
        body = build_claim(catalog(LEADERBOARD), SESSION, "user-developer", "existing-leaderboard",
                           "originalrun", DEFAULT_SERVER)
        self.client.claims["existing-leaderboard"] = ready_claim(body)
        self.client.authenticate = Mock(return_value=SESSION)
        with patch("dev_environment.CatalogClient", return_value=self.client), patch("dev_environment.load_credentials", return_value={}):
            self.assertEqual(main(["--state-dir", str(self.directory), "use-leaderboard", "existing-leaderboard"]), 0)
        self.assertEqual(self.client.posts, [])
        saved = read_json(self.directory / "leaderboard.json")
        self.assertEqual(saved["uid"], "uid-existing-leaderboard")
        args = parser().parse_args(["--state-dir", str(self.directory), "new-student", "--accept-warnings"])
        with patch("dev_environment.smoke_check"):
            new_run(args, self.client).execute()
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.client.posts[0]["spec"]["provider"]["name"], STUDENT.split("/")[1])

    def test_wrong_student_version_stops_before_any_order(self):
        self.deployment.run["expected_student_version"] = "out-of-date"
        with self.assertRaisesRegex(ValueError, "resourceVersion"):
            self.deployment.execute()
        self.assertEqual(self.client.posts, [])

    def test_missing_showroom_ref_stops_before_any_order(self):
        self.check_source.side_effect = CatalogError("Showroom Git ref 'dev' does not exist")
        with self.assertRaisesRegex(CatalogError, "does not exist"):
            self.deployment.execute()
        self.assertEqual(self.client.posts, [])

    def test_source_preflight_failure_does_not_create_run_state(self):
        from dev_environment import new_run, parser
        self.check_source.side_effect = CatalogError("Showroom Git ref 'dev' does not exist")
        args = parser().parse_args(["--state-dir", str(self.directory), "bootstrap", "--accept-warnings"])
        with self.assertRaises(CatalogError):
            new_run(args, self.client)
        self.assertFalse((self.directory / "latest.json").exists())
        self.assertFalse((self.directory / "runs").exists())
        self.assertEqual(self.client.posts, [])

    def test_changed_content_ref_stops_before_student_post(self):
        self.deployment.run["student_source"] = {**self.source, "commit": "b" * 40}
        self.deployment.prepare("student", student_fields({"summary": {"provision_data": DATA}}))
        with self.assertRaisesRegex(CatalogError, "ref moved"):
            self.deployment.ensure_submitted("student")
        self.assertEqual(self.client.posts, [])

    def test_timeout_keeps_order_identity_for_resume(self):
        self.deployment.prepare("leaderboard")
        self.deployment.ensure_submitted("leaderboard")
        order = self.deployment.run["orders"]["leaderboard"]
        self.client.claims[order["name"]]["status"]["summary"]["state"] = "provisioning"
        with patch("dev_environment.time.monotonic", side_effect=[100, 102]):
            with self.assertRaisesRegex(CatalogError, "Timed out"):
                self.deployment.wait("leaderboard")
        self.assertEqual(read_json(self.deployment.path)["orders"]["leaderboard"]["name"], order["name"])
        self.assertEqual(len(self.client.posts), 1)

    def test_abandon_is_local_and_unblocks_new_run(self):
        self.deployment.save()
        secure_write(self.directory / "latest.json", {"id": self.deployment.run["id"]})
        args = __import__("dev_environment").parser().parse_args([
            "--state-dir", str(self.directory), "bootstrap", "--accept-warnings"])
        with self.assertRaisesRegex(CatalogError, "incomplete"):
            __import__("dev_environment").new_run(args, self.client)
        self.assertEqual(main(["--state-dir", str(self.directory), "abandon"]), 0)
        self.assertEqual(read_json(self.deployment.path)["phase"], "abandoned")
        self.assertEqual(self.client.posts, [])
        self.assertNotEqual(__import__("dev_environment").new_run(args, self.client).run["id"], self.deployment.run["id"])

    def test_wait_through_provisioning_then_ready(self):
        self.deployment.prepare("leaderboard")
        self.deployment.ensure_submitted("leaderboard")
        order = self.deployment.run["orders"]["leaderboard"]
        ready = deepcopy(self.client.claims[order["name"]])
        pending = deepcopy(ready)
        pending["status"]["summary"]["state"] = "provisioning"
        with patch.object(self.client, "claim", side_effect=[pending, ready]), patch("dev_environment.time.sleep"):
            self.assertEqual(self.deployment.wait("leaderboard")["status"]["summary"]["state"], "started")

    def test_private_files_and_state_lock(self):
        self.deployment.prepare("leaderboard")
        self.assertEqual(stat.S_IMODE(self.deployment.path.stat().st_mode), 0o600)
        request = self.deployment.path.parent / self.deployment.run["orders"]["leaderboard"]["request_file"]
        self.assertEqual(stat.S_IMODE(request.stat().st_mode), 0o600)
        with state_lock(self.directory):
            with self.assertRaises(CatalogError):
                with state_lock(self.directory):
                    pass

    def test_service_check_uses_separate_session_without_catalog_credentials(self):
        claim = ready_claim(build_claim(catalog(STUDENT, True), SESSION, "user-developer", "aia-student-test",
                                        "012345abcdef", DEFAULT_SERVER, student_fields({"summary": {"provision_data": DATA}})))
        http = Mock()
        http.__enter__ = Mock(return_value=http)
        http.__exit__ = Mock(return_value=False)
        http.get.return_value = Mock(status_code=200)
        with patch("dev_environment.requests.Session", return_value=http):
            smoke_check(claim)
        self.assertEqual(http.get.call_count, 3)
        for call in http.get.call_args_list:
            self.assertNotIn("headers", call.kwargs)
            self.assertNotIn("auth", call.kwargs)
            self.assertFalse(call.kwargs["allow_redirects"])


if __name__ == "__main__":
    unittest.main()
