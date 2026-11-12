"""Shared ResourceClaim parsing and the leaderboard-to-student field mapping."""

from collections.abc import Mapping
from pathlib import Path

import yaml


REQUIRED_FIELDS = [
    {"label": "OpenShift API Server URL", "key": "openshift_api_server_url", "target": "openshift_api_server_url"},
    {"label": "OpenShift Cluster Ingress Domain", "key": "openshift_cluster_ingress_domain", "target": "openshift_cluster_ingress_domain"},
    {"label": "OpenShift Cluster Common Password", "key": "openshift_cluster_admin_password", "target": "openshift_cluster_common_password"},
    {"label": "Keycloak Admin Username", "key": "keycloak_admin_user", "target": "keycloak_admin_user"},
    {"label": "Keycloak Admin Password", "key": "keycloak_admin_password", "target": "keycloak_admin_password"},
]


def load_claim(path):
    try:
        claim = yaml.safe_load(Path(path).read_text())
    except yaml.YAMLError:
        raise ValueError("Invalid ResourceClaim YAML (contents suppressed)") from None
    if not isinstance(claim, Mapping):
        raise ValueError("ResourceClaim YAML must contain a mapping")
    return claim


def extract_provision_data(claim):
    """Resource values are merged first; status.summary is authoritative."""
    if not isinstance(claim, Mapping):
        raise ValueError("ResourceClaim must be a mapping")
    def mapping(value):
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError("ResourceClaim provision-data containers must be mappings")
        return value

    status = mapping(claim.get("status"))
    merged = {}
    blocks = [mapping(claim.get("summary")).get("provision_data")]
    resources = status.get("resources") or []
    if not isinstance(resources, list):
        raise ValueError("ResourceClaim status.resources must be a list")
    for resource in resources:
        state = mapping(mapping(resource).get("state"))
        blocks.append(mapping(mapping(state.get("spec")).get("vars")).get("provision_data"))
    blocks.append(mapping(status.get("summary")).get("provision_data"))
    for block in blocks:
        if block is not None:
            if not isinstance(block, Mapping):
                raise ValueError("provision_data must be a mapping")
            merged.update(block)
    return merged


def student_fields(claim):
    data = extract_provision_data(claim)
    missing = [f["key"] for f in REQUIRED_FIELDS
               if not isinstance(data.get(f["key"]), str) or not data[f["key"]].strip()]
    if missing:
        raise ValueError("Missing or invalid required fields: " + ", ".join(missing))
    return {f["target"]: data[f["key"]] for f in REQUIRED_FIELDS}
