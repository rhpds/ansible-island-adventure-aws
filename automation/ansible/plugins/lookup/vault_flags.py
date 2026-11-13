"""Read the packaged event vault with an explicitly supplied organizer key."""
from pathlib import Path

from ansible.errors import AnsibleLookupError
from ansible.parsing.vault import VaultLib, VaultSecret
from ansible.plugins.lookup import LookupBase
import yaml


def read_bundle(path, password=None, vault_secrets=None):
    try:
        ciphertext = Path(path).read_bytes()
        if not ciphertext.startswith(b'$ANSIBLE_VAULT;'):
            raise ValueError()
        candidates = ([('default', VaultSecret(password.strip().encode()))]
                      if isinstance(password, str) and password.strip() else (vault_secrets or []))
        for identity, secret in candidates:
            try:
                plaintext = VaultLib([(identity, secret)]).decrypt(ciphertext)
                break
            except Exception:
                continue
        else:
            raise ValueError()
        data = yaml.safe_load(plaintext)
        flags = data['ctfd_flags']
        if not isinstance(flags, dict) or not flags or any(
            not isinstance(k, str) or not isinstance(v, str) or not v.strip() or v == 'CHANGEME'
            for k, v in flags.items()
        ):
            raise ValueError()
        return {'ctfd_flags': flags, 'source_password': secret.bytes.decode()}
    except Exception:
        raise AnsibleLookupError('Event flags could not be unlocked or validated; attach the matching organizer Ansible Vault credential to the RHDP job') from None


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        if len(terms) != 1:
            raise AnsibleLookupError('Supply exactly one encrypted event-flags file')
        bundle = read_bundle(terms[0], kwargs.get('password'), self._loader._vault.secrets)
        return [bundle if kwargs.get('include_source_password') else bundle['ctfd_flags']]
