"""Verify the existing SSH identity guard with AWS-generated hostnames.

No real SSH connections or AAP requests are made.
"""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


MODULE = Path(__file__).resolve().parents[1] / "plugins/modules/aia_aap_student.py"
spec = importlib.util.spec_from_file_location("aia_aws_hostname", MODULE)
aap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aap)


class AwsHostnameIdentityTests(unittest.TestCase):
    def discover(self, hostname, exit_status=0):
        client = Mock()
        output = Mock()
        output.read.return_value = (hostname + "\n").encode()
        output.channel.recv_exit_status.return_value = exit_status
        client.exec_command.return_value = (None, output, None)
        socket = Mock()
        paramiko = SimpleNamespace(SSHClient=Mock(return_value=client), AutoAddPolicy=Mock())
        node = {"host": "ec2.example.test", "port": 22,
                "username": "lab-user", "password": "synthetic-test-password"}
        with patch.dict(sys.modules, {"paramiko": paramiko}), \
                patch.object(aap, "connect_socket", return_value=socket):
            try:
                return aap.discover_fqdn(node, "8hzhc")
            finally:
                client.close.assert_called_once()
                socket.close.assert_called_once()

    def test_guid_scoped_aws_private_hostname_passes(self):
        self.assertEqual(self.discover("rhel.8hzhc.internal"), "rhel.8hzhc.internal")

    def test_inherited_example_domain_is_rejected(self):
        with self.assertRaisesRegex(aap.ConfigurationError, "does not match this student GUID"):
            self.discover("rhel.example.com")

    def test_another_students_hostname_is_rejected(self):
        with self.assertRaisesRegex(aap.ConfigurationError, "does not match this student GUID"):
            self.discover("rhel.otherguid.internal")

    def test_guid_substring_is_not_sufficient(self):
        with self.assertRaisesRegex(aap.ConfigurationError, "does not match this student GUID"):
            self.discover("rhel.8hzhcx.internal")

    def test_passwordless_sudo_requirement_is_preserved(self):
        with self.assertRaisesRegex(aap.ConfigurationError, "passwordless sudo"):
            self.discover("rhel.8hzhc.internal", exit_status=1)


if __name__ == "__main__":
    unittest.main()
