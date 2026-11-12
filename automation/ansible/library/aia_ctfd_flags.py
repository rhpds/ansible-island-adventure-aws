#!/usr/bin/python
"""Update existing CTFd flags in place, preserving challenge/flag IDs and solves."""

from html.parser import HTMLParser
import re

from ansible.module_utils.basic import AnsibleModule
import requests


class SyncError(Exception):
    pass


class NonceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nonce = None

    def handle_starttag(self, tag, attributes):
        fields = dict(attributes)
        if tag == "input" and fields.get("name") == "nonce":
            self.nonce = fields.get("value")


def authenticate(http, url, username, password):
    response = http.get(url + "/login", timeout=30, allow_redirects=False)
    if response.status_code != 200:
        raise SyncError("CTFd login page is unavailable")
    parser = NonceParser()
    parser.feed(response.text)
    if not parser.nonce:
        raise SyncError("CTFd login nonce is missing")
    response = http.post(url + "/login", data={"name": username, "password": password, "nonce": parser.nonce},
                         timeout=30, allow_redirects=False)
    if response.status_code not in (200, 302, 303):
        raise SyncError("CTFd login failed")
    response = http.get(url + "/admin/challenges", timeout=30, allow_redirects=False)
    token = re.search(r"csrfNonce.*?([0-9a-f]{64})", response.text, re.S)
    if response.status_code != 200 or not token:
        raise SyncError("CTFd admin authentication failed")
    http.headers.update({"CSRF-Token": token[1]})


def api(http, url, method, path, body=None):
    response = http.request(method, url + "/api/v1/" + path, json=body, timeout=30, allow_redirects=False)
    if response.status_code not in (200, 201):
        raise SyncError(f"CTFd {method} {path} returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise SyncError("CTFd returned invalid API JSON") from None
    if not payload.get("success"):
        raise SyncError("CTFd rejected a flag API operation")
    return payload["data"]


def canonical(flag):
    return {"type": flag.get("type", "static"), "content": flag["content"], "data": flag.get("data") or ""}


def sync_flags(http, url, challenges, check_mode=False):
    existing = api(http, url, "GET", "challenges?view=admin")
    names = [challenge["name"] for challenge in existing]
    if len(names) != len(set(names)):
        raise SyncError("CTFd contains duplicate challenge names; flag mapping is ambiguous")
    by_name = {challenge["name"]: challenge["id"] for challenge in existing}
    planned = []
    # Plan the entire operation before changing a flag. Never delete flags or
    # challenges to reconcile unexpected state.
    for challenge in challenges:
        if challenge["name"] not in by_name:
            raise SyncError("A configured challenge is missing from CTFd; load initial content first")
        current = sorted(api(http, url, "GET", f"challenges/{by_name[challenge['name']]}/flags"), key=lambda flag: flag["id"])
        desired = challenge.get("flags", [])
        if len(current) != len(desired):
            raise SyncError("A challenge has an unexpected flag count; no flag changes were applied")
        for before, after in zip(current, desired, strict=True):
            if canonical(before) != canonical(after):
                planned.append((before["id"], canonical(after)))
    if not check_mode:
        for flag_id, body in planned:
            api(http, url, "PATCH", f"flags/{flag_id}", body)
    return {"changed": bool(planned), "planned_updates": len(planned), "updated": 0 if check_mode else len(planned)}


def main():
    module = AnsibleModule(argument_spec={
        "url": {"type": "str", "required": True},
        "username": {"type": "str", "required": True},
        "password": {"type": "str", "required": True, "no_log": True},
        "challenges": {"type": "list", "elements": "dict", "required": True, "no_log": True},
        "validate_certs": {"type": "bool", "default": True},
    }, supports_check_mode=True)
    try:
        with requests.Session() as http:
            http.verify = module.params["validate_certs"]
            url = module.params["url"].rstrip("/")
            authenticate(http, url, module.params["username"], module.params["password"])
            result = sync_flags(http, url, module.params["challenges"], module.check_mode)
        module.exit_json(**result)
    except (SyncError, requests.RequestException, KeyError, TypeError, ValueError) as error:
        message = str(error) if isinstance(error, SyncError) else "CTFd flag synchronization failed; response contents suppressed"
        module.fail_json(msg=message)


if __name__ == "__main__":
    main()
