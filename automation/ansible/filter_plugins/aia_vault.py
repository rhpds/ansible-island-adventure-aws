"""Re-key a student's existing vault in memory, preserving edits and rerun bytes."""
from ansible.errors import AnsibleFilterError
from ansible.parsing.vault import VaultLib, VaultSecret


def rekey_vault(ciphertext, source_password, lab_password):
    if not isinstance(ciphertext, str) or not ciphertext.startswith('$ANSIBLE_VAULT;'):
        raise AnsibleFilterError('The instructor flags file must be an encrypted Ansible Vault')
    if not source_password or not lab_password:
        raise AnsibleFilterError('Source and provisioned lab vault passwords are required')
    def vault(password):
        return VaultLib([('default', VaultSecret(password.encode()))])
    try:
        vault(lab_password).decrypt(ciphertext)
        return ciphertext
    except Exception:
        pass
    try:
        plaintext = vault(source_password).decrypt(ciphertext)
        return vault(lab_password).encrypt(plaintext, vault_id='default').decode()
    except Exception:
        raise AnsibleFilterError('The instructor vault could not be unlocked; its existing contents were preserved') from None


class FilterModule:
    def filters(self):
        return {'aia_rekey_vault': rekey_vault}
