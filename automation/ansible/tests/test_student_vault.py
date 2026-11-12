"""Re-key real Vault ciphertext without losing student edits or churning Git."""
import importlib.util
from pathlib import Path
import unittest
from ansible.parsing.vault import VaultLib, VaultSecret
from ansible.errors import AnsibleFilterError

spec = importlib.util.spec_from_file_location('aia_vault',Path(__file__).parents[1]/'filter_plugins/aia_vault.py')
vault_filter=importlib.util.module_from_spec(spec);spec.loader.exec_module(vault_filter)

def vault(password):return VaultLib([('default',VaultSecret(password.encode()))])

class StudentVault(unittest.TestCase):
    def test_rekey_preserves_plaintext_and_rerun_ciphertext(self):
        plain=b'ctfd_flags:\n  custom_student_answer: synthetic-custom-answer\n'
        original=vault('upstream-test-key').encrypt(plain).decode()
        result=vault_filter.rekey_vault(original,'upstream-test-key','student-lab-key')
        self.assertNotEqual(original,result)
        self.assertEqual(vault('student-lab-key').decrypt(result),plain)
        self.assertEqual(vault_filter.rekey_vault(result,'upstream-test-key','student-lab-key'),result)
        with self.assertRaises(Exception):vault('upstream-test-key').decrypt(result)

    def test_students_with_distinct_lab_passwords_cannot_cross_decrypt(self):
        original=vault('upstream-test-key').encrypt(b'synthetic-test-data').decode()
        a=vault_filter.rekey_vault(original,'upstream-test-key','first-lab-key')
        b=vault_filter.rekey_vault(original,'upstream-test-key','second-lab-key')
        with self.assertRaises(Exception):vault('first-lab-key').decrypt(b)
        with self.assertRaises(Exception):vault('second-lab-key').decrypt(a)

    def test_wrong_key_or_plaintext_rejected_without_revealing_input(self):
        original=vault('unknown-test-key').encrypt(b'synthetic-answer').decode()
        for content in (original,'ctfd_flags: synthetic-answer'):
            with self.assertRaises(AnsibleFilterError) as failure:
                vault_filter.rekey_vault(content,'wrong-source-key','student-lab-key')
            self.assertNotIn('synthetic-answer',str(failure.exception))
            self.assertNotIn('wrong-source-key',str(failure.exception))
