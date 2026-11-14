"""Deliver the organizer key to the selected leaderboard, outside catalog data."""

import base64
import hmac
from pathlib import Path
import tempfile

import requests

from catalog_client import CatalogError
from student_inventory import kubeconfig_from_claim


NAMESPACE = "aia-automation"
SECRET_NAME = "aia-event-vault"
SECRET_KEY = "password"


def read_organizer_password(path):
    try:
        with Path(path).open("rb") as source:
            value = source.read(16385).decode("utf-8").strip()
        if not value or len(value.encode()) > 16384 or any(c in value for c in "\r\n\0"):
            raise ValueError()
        return value
    except (OSError, UnicodeError, ValueError):
        raise CatalogError("Organizer vault password file must be readable and contain one nonempty password line") from None


def seed_organizer_vault(leaderboard, password_file, state_dir, http=None):
    """Idempotently create the protected Secret; refuse to overwrite another key."""
    password = read_organizer_password(password_file)
    kube = kubeconfig_from_claim(leaderboard)
    cluster = kube["clusters"][0]["cluster"]
    server = cluster["server"].rstrip("/")
    token = kube["users"][0]["user"]["token"]
    session = http if http is not None else requests.Session()
    session.headers.update({"Accept": "application/json", "Authorization": "Bearer " + token})

    def api(method, path, body=None, allowed=(200,)):
        try:
            response = session.request(method, server + path, json=body,
                                       timeout=20, allow_redirects=False)
        except requests.RequestException:
            raise CatalogError("Organizer key handoff could not reach the leaderboard API; no catalog order was submitted") from None
        if response.status_code not in allowed:
            # Kubernetes errors may echo the Secret request. Never render bodies.
            raise CatalogError(f"Organizer key handoff returned HTTP {response.status_code}; response suppressed")
        try:
            return response.status_code, response.json()
        except ValueError:
            raise CatalogError("Organizer key handoff returned invalid JSON; response suppressed") from None

    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with tempfile.NamedTemporaryFile(dir=state_dir, prefix=".aia-api-ca-", suffix=".crt") as ca:
            if cluster.get("certificate-authority-data"):
                ca.write(base64.b64decode(cluster["certificate-authority-data"], validate=True))
                ca.flush()
                session.verify = ca.name
            else:
                session.verify = True
            namespace_path = "/api/v1/namespaces/" + NAMESPACE
            status, _ = api("GET", namespace_path, allowed=(200, 404))
            if status == 404:
                api("POST", "/api/v1/namespaces", {
                    "apiVersion": "v1", "kind": "Namespace",
                    "metadata": {"name": NAMESPACE, "labels": {"app.kubernetes.io/part-of": "ansible-island-adventure"}},
                }, allowed=(201, 409))
            secret_path = namespace_path + "/secrets/" + SECRET_NAME
            status, secret = api("GET", secret_path, allowed=(200, 404))
            if status == 404:
                status, secret = api("POST", namespace_path + "/secrets", {
                    "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                    "metadata": {"name": SECRET_NAME, "namespace": NAMESPACE,
                                 "labels": {"app.kubernetes.io/part-of": "ansible-island-adventure"}},
                    "data": {SECRET_KEY: base64.b64encode(password.encode()).decode()},
                }, allowed=(201, 409))
                if status == 409:
                    _, secret = api("GET", secret_path)
            try:
                stored = base64.b64decode(secret["data"][SECRET_KEY], validate=True).decode().strip()
            except (KeyError, TypeError, ValueError, UnicodeError):
                raise CatalogError("Existing organizer Secret has no valid password; refusing to replace it") from None
            if not hmac.compare_digest(stored.encode(), password.encode()):
                raise CatalogError("Existing organizer Secret contains a different key; refusing to overwrite it")
            return {"namespace": NAMESPACE, "name": SECRET_NAME, "uid": secret["metadata"]["uid"]}
    finally:
        if http is None:
            session.close()
