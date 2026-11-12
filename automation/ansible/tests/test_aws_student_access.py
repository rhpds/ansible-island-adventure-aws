"""Render the AWS catalog adapters without accessing cloud infrastructure."""
from pathlib import Path
import unittest

from jinja2 import Environment, StrictUndefined
import yaml


COLLECTION = Path(__file__).resolve().parents[1]
ROLES = COLLECTION / "roles"


class AwsStudentAccessTests(unittest.TestCase):
    def setUp(self):
        self.tasks = yaml.safe_load((ROLES / "aws_student_access/tasks/main.yml").read_text())
        self.variables = {
            "cloud_provider": "aws", "guid": "unit123",
            "aia_aws_managed_node_host": "ec2.example.test",
            "aia_aws_managed_node_ip": "203.0.113.10",
            "aia_aws_managed_node_port": 22,
            "aia_aws_managed_node_web_url": "https://treasure-unit123.apps.example.test",
            "openshift_cluster_ingress_domain": "apps.example.test",
            "instructor": {"name": "instructor-unit123", "lab_ns": "instructor-unit123-lab"},
        }
        self.env = Environment(undefined=StrictUndefined)

    def test_route_targets_the_ec2_public_ip_over_http_and_terminates_tls(self):
        task = next(t for t in self.tasks if "kubernetes.core.k8s" in t)
        rendered = self.env.from_string(yaml.safe_dump(task["kubernetes.core.k8s"]["definition"])).render(self.variables)
        resources = {resource["kind"]: resource for resource in yaml.safe_load(rendered)}
        self.assertNotIn("selector", resources["Service"]["spec"])
        endpoints = resources["Endpoints"]["subsets"][0]
        self.assertEqual(endpoints["addresses"][0]["ip"], "203.0.113.10")
        self.assertEqual(endpoints["ports"][0]["port"], 80)
        route = resources["Route"]["spec"]
        self.assertEqual(route["host"], "treasure-unit123.apps.example.test")
        self.assertEqual(route["to"]["name"], "treasure")
        self.assertEqual(route["tls"]["termination"], "edge")
        self.assertEqual(route["tls"]["insecureEdgeTerminationPolicy"], "Redirect")
        for resource in resources.values():
            self.assertEqual(resource["metadata"]["namespace"], "instructor-unit123-lab")

    def test_token_read_and_publication_are_masked_and_not_marked_changed(self):
        command = next(t for t in self.tasks if "ansible.builtin.command" in t)
        self.assertTrue(command["no_log"])
        self.assertFalse(command["changed_when"])
        fact = next(t for t in self.tasks if "ansible.builtin.set_fact" in t)
        self.assertTrue(fact["no_log"])
        self.assertIn("ocp4_workload_showroom_openshift_api_token", fact["ansible.builtin.set_fact"])
        expression = command["ansible.builtin.command"]["argv"]
        render = self.env.compile_expression(expression[2:-2].strip())
        self.assertEqual(render(**self.variables), ["oc", "whoami", "-t"])
        self.assertEqual(render(**dict(self.variables, aia_kubeconfig="/tmp/leaderboard.config")),
                         ["oc", "--kubeconfig=/tmp/leaderboard.config", "whoami", "-t"])

    def test_endpoint_is_required_before_any_api_write(self):
        assertions = self.tasks[0]["ansible.builtin.assert"]["that"]
        for assertion in assertions:
            if " is match(" not in assertion:  # Ansible-only test; checked by syntax validation.
                self.assertTrue(self.env.compile_expression(assertion)(**self.variables), assertion)
        self.assertIn("cloud_provider == 'aws'", assertions)

    def test_readiness_adapter_uses_aws_host_and_port(self):
        path = ROLES / "student_readiness/tasks/main.yml"
        tasks = yaml.safe_load(path.read_text())
        node = tasks[0]["vars"]["aia_managed_node"]
        self.assertEqual(self.env.from_string(node["host"]).render(self.variables), "ec2.example.test")
        self.assertEqual(self.env.from_string(node["port"]).render(self.variables), "22")
        self.assertNotIn("openshift_cnv_ssh_address", path.read_text())
        self.assertNotIn("sandbox_openshift_apps_domain", path.read_text())


if __name__ == "__main__":
    unittest.main()
