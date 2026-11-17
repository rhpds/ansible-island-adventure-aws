#!/usr/bin/python
"""Create the organizer Secret once; never patch or replace an existing key."""
import base64
import hmac

class CredentialError(Exception):
    pass


def ensure_secret(api, namespace, name, key, password, check_mode=False):
    resource = api.resource('Secret', 'v1')

    def read():
        try:
            return api.get(resource, name=name, namespace=namespace)
        except Exception as error:
            if getattr(error, 'status', None) == 404:
                return None
            raise CredentialError('Organizer Secret read failed; response suppressed') from None

    secret = read()
    changed = False
    if secret is None:
        if check_mode:
            return {'changed': True}
        definition = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
                      'metadata': {'name': name, 'namespace': namespace,
                                   'labels': {'app.kubernetes.io/part-of': 'ansible-island-adventure'}},
                      'data': {key: base64.b64encode(password.encode()).decode()}}
        try:
            secret = api.create(resource, definition, namespace=namespace)
            changed = True
        except Exception as error:
            if getattr(error, 'status', None) != 409:
                raise CredentialError('Organizer Secret create failed; response suppressed') from None
            secret = read()
    try:
        obj = secret.to_dict() if hasattr(secret, 'to_dict') else secret
        stored = base64.b64decode(obj['data'][key], validate=True).decode().strip()
        if not hmac.compare_digest(stored.encode(), password.encode()):
            raise ValueError()
        uid = obj['metadata']['uid']
    except (KeyError, ValueError, TypeError, UnicodeError):
        raise CredentialError('Existing organizer key is missing, invalid or different; refusing rotation') from None
    return {'changed': changed, 'uid': uid}


def main():
    from ansible_collections.kubernetes.core.plugins.module_utils.args_common import AUTH_ARG_SPEC
    from ansible_collections.kubernetes.core.plugins.module_utils.k8s.client import get_api_client
    from ansible_collections.kubernetes.core.plugins.module_utils.k8s.core import AnsibleK8SModule
    spec = dict(AUTH_ARG_SPEC)
    spec.update(namespace={'type': 'str', 'required': True}, name={'type': 'str', 'required': True},
                key={'type': 'str', 'required': True}, event_password={'type': 'str', 'required': True, 'no_log': True})
    module = AnsibleK8SModule(argument_spec=spec, supports_check_mode=True)
    try:
        p = module.params
        api = get_api_client(module=module)
        module.exit_json(**ensure_secret(api, p['namespace'], p['name'], p['key'], p['event_password'], module.check_mode))
    except Exception as error:
        module.fail_json(msg=str(error) if isinstance(error, CredentialError) else
                         f'Organizer Secret provisioning failed ({type(error).__name__}); response contents suppressed')


if __name__ == '__main__':
    main()
