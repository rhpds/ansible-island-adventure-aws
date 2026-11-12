"""Private Ansible inventory for already deployed student CIs."""

import base64
import re
import sys
from urllib.parse import urlsplit

from catalog_client import CatalogError
from dev_environment import claim_state, validate_claim_identity
from provision_fields import extract_provision_data, student_fields


def required(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Student provision data is missing {key}")
    return value


def endpoint(data, key):
    value = required(data, key).rstrip("/")
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path):
        raise ValueError(f"Student provision data has an invalid {key}")
    return value


def managed_node_access(data):
    host = required(data, 'bastion_public_hostname')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host):
        raise ValueError('Student provision data has an invalid SSH hostname')
    try:
        port = int(data['bastion_ssh_port'])
    except (KeyError, TypeError, ValueError):
        raise ValueError('Student provision data is missing a valid bastion_ssh_port') from None
    if not 1 <= port <= 65535:
        raise ValueError('Student SSH port must be between 1 and 65535')
    return {'host': host, 'port': port, 'username': required(data, 'bastion_ssh_user_name'),
            'password': required(data, 'bastion_ssh_password')}


def kubeconfig_from_claim(claim):
    """Build the private control-host kubeconfig from the refreshed leaderboard."""
    data = extract_provision_data(claim)
    server = endpoint(data, 'openshift_api_server_url')
    token = required(data, 'openshift_cluster_admin_token')
    cluster = {'server': server}
    ca = data.get('openshift_api_ca_cert')
    if ca:
        if not isinstance(ca, str) or 'BEGIN CERTIFICATE' not in ca:
            raise ValueError('Leaderboard provision data has an invalid API CA certificate')
        cluster['certificate-authority-data'] = base64.b64encode(ca.encode()).decode()
    return {'apiVersion': 'v1', 'kind': 'Config',
            'clusters': [{'name': 'leaderboard', 'cluster': cluster}],
            'users': [{'name': 'leaderboard-admin', 'user': {'token': token}}],
            'contexts': [{'name': 'leaderboard', 'context': {'cluster': 'leaderboard', 'user': 'leaderboard-admin'}}],
            'current-context': 'leaderboard'}


def inventory_from_claims(run, claims, kubeconfig):
    """Validate every saved identity before exporting credentials or host mappings."""
    if "leaderboard" not in claims:
        raise ValueError("A deployed leaderboard is required")
    for role, claim in claims.items():
        validate_claim_identity(run["orders"][role], claim, run["id"])
        if not claim_state(claim)[1]:
            raise CatalogError(f"{role} must be started and healthy before post-deployment configuration")
    fields = student_fields(claims["leaderboard"])
    hosts = {}
    for role, claim in claims.items():
        if role != "student" and not role.startswith("student-"):
            continue
        data = extract_provision_data(claim)
        guid = required(data, "guid")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", guid):
            raise ValueError("Student provision data has an invalid guid")
        handle = claim.get("status", {}).get("resourceHandle", {}).get("name")
        if handle and handle != "guid-" + guid:
            raise ValueError("Student provision data GUID differs from its ResourceHandle")
        username = required(data, "gitea_username")
        if username != "instructor-" + guid or username in hosts:
            raise ValueError("Student Gitea identity does not uniquely match its deployed GUID")
        ctfd_url = endpoint(data, "ctfd_url")
        if ctfd_url != f"https://island-{username}-ctfd.{fields['openshift_cluster_ingress_domain']}":
            raise ValueError("Student CTFd URL does not match its GUID and leaderboard cluster")
        resources = {key: endpoint(data, key) for key in ("gitea_url", "aap_url", "devspaces_url")}
        # Older CIs expose SSH only; new CIs publish a native HTTPS Route.
        if "managed_node_web_url" in data:
            web_url = endpoint(data, "managed_node_web_url")
            if not urlsplit(web_url).hostname.startswith(f"treasure-{guid}."):
                raise ValueError("Student managed-node web URL does not match its GUID")
            resources["managed_node_web_url"] = web_url
        hosts[username] = {
            "guid": guid,
            "instructor": {"name": username, "ctfd_ns": username + "-ctfd", "lab_ns": username + "-lab",
                           "password": required(data, "gitea_password")},
            "common_password": required(data, "ctfd_student_password"),
            "aia_lab_password": required(data, "password"),
            "aap_password": required(data, "aap_password"),
            "aia_managed_node": managed_node_access(data),
            "student_name": required(data, "ctfd_student_username"),
            "ctfd_admin_username": required(data, "ctfd_admin_username"),
            "ctfd_admin_password": required(data, "ctfd_admin_password"),
            "gitea_username": username, "gitea_password": required(data, "gitea_password"),
            **resources,
            "aia_student": {"guid": guid, "claim_name": claim["metadata"]["name"],
                            "ctfd_url": ctfd_url, "ctfd_username": data["ctfd_student_username"],
                            "gitea_username": username, "aap_username": required(data, "aap_username"),
                            "managed_node": required(data, "managed_node"), **resources},
        }
    count = run.get("workshop", {}).get("user_count", 1)
    if len(hosts) != count:
        raise ValueError("Deployed student count differs from the requested run; inspect the workshop first")
    return {"all": {"children": {"aia_students": {
        "hosts": hosts,
        "vars": {**fields, "ansible_connection": "local", "ansible_python_interpreter": sys.executable,
                 "aia_kubeconfig": str(kubeconfig), "aia_run_id": run["id"]},
    }}}}
