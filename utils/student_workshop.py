"""Resumable student workshops using Babylon's Workshop/WorkshopProvision API.

Payloads follow catalog/ui/src/app/api.ts createWorkshop/createWorkshopProvision
in redhat-cop/babylon. Passwords and exact requests stay in private run files.
"""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import secrets
import string
import time
from urllib.parse import quote, urlencode

from catalog_client import CatalogError, read_json, secure_write
from dev_environment import AUTO_DETACH_WHEN, BABYLON, RUN_LABEL, build_claim, claim_state, smoke_check, utcnow


def validate_workshop_catalog(item, count):
    spec = item["spec"]
    if spec.get("workshopUiDisabled"):
        raise ValueError("The student catalog disables its workshop interface")
    if spec.get("workshopUserMode", "none") != "none":
        raise ValueError("This utility supports independent student CI workshops (workshopUserMode: none)")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= spec.get("workshopUiMaxInstances", 30):
        raise ValueError("Workshop user count must be positive and within the catalog workshop limit")


def workshop_bodies(item, session, run, fields, server):
    """Build the same defaults as the catalog form, without undeclared inputs."""
    count = run["workshop"]["user_count"]
    validate_workshop_catalog(item, count)
    name = "aia-workshop-" + run["id"]
    base = build_claim(item, session, run["namespace"], name, run["id"], server, fields,
                       run["activity"], run["purpose"], run.get("lifespan"),
                       keep_on_failure=run.get("keep_on_failure", False))
    metadata = deepcopy(base["metadata"])
    metadata["annotations"][BABYLON + "url"] = f"{server}/workshops/{run['namespace']}/{name}"
    parameters = deepcopy(base["spec"]["provider"]["parameterValues"])
    stop = parameters.pop("stop_timestamp", None)
    lifespan = deepcopy(base["spec"]["lifespan"])
    for key in ("maximum", "relativeMaximum"):
        if item["spec"].get("lifespan", {}).get(key):
            lifespan[key] = item["spec"]["lifespan"][key]
    workshop = {
        "apiVersion": BABYLON.rstrip("/") + "/v1", "kind": "Workshop", "metadata": metadata,
        "spec": {"displayName": item["spec"].get("displayName") or item["metadata"]["name"],
                 "accessPassword": "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8)),
                 "description": "<p></p>", "openRegistration": True, "multiuserServices": False,
                 "lifespan": lifespan, "actionSchedule": {"stop": stop} if stop else {}},
    }
    if item["spec"].get("workshopLabUiRedirect"):
        workshop["spec"]["labUserInterface"] = {"redirect": True}
    provision = {
        "apiVersion": workshop["apiVersion"], "kind": "WorkshopProvision",
        "metadata": {"name": name, "namespace": run["namespace"],
                     "labels": {**metadata["labels"], BABYLON + "workshop": name}},
        "spec": {"catalogItem": {"name": item["metadata"]["name"], "namespace": item["metadata"]["namespace"]},
                 "count": count, "concurrency": 10, "startDelay": 30, "workshopName": name,
                 "parameters": {**parameters, "purpose_activity": run["activity"], "salesforce_items": "[]"}},
    }
    if not run.get("keep_on_failure"):
        provision["spec"]["autoDetach"] = {"when": AUTO_DETACH_WHEN}
    return workshop, provision


def resource_path(namespace, plural, name=None):
    path = f"/apis/{BABYLON}v1/namespaces/{quote(namespace, safe='')}/{plural}"
    return path + "/" + quote(name, safe="") if name else path


def ensure_resource(deployment, plural, body):
    """Persist exact names and intent before POST; reconcile lost responses."""
    settings = deployment.run["workshop"]
    orders = settings.setdefault("orders", {})
    if plural not in orders:
        filename = deployment.run["id"] + f"-{plural}.request.json"
        secure_write(deployment.path.parent / filename, body)
        orders[plural] = {"name": body["metadata"]["name"], "namespace": body["metadata"]["namespace"],
                          "request_file": filename, "attempted": False}
        deployment.save()
    order = orders[plural]
    body = read_json(deployment.path.parent / order["request_file"])
    client = deployment.client
    path = resource_path(order["namespace"], plural, order["name"])
    try:
        resource = client.request("GET", path)
    except CatalogError as error:
        if error.status != 404:
            raise
        if order.get("uid"):
            raise CatalogError("Saved workshop resource no longer exists; refusing to recreate it") from None
        if order["attempted"] and not deployment.retry_submit:
            raise CatalogError("Workshop submission is unresolved; inspect the catalog, then resume --retry-submit") from None
        if plural == "workshops":
            if datetime.fromisoformat(body["spec"]["lifespan"]["end"].replace("Z", "+00:00")) <= utcnow():
                raise CatalogError("Prepared workshop has expired; start a new run")
        else:
            # The provision, rather than the empty Workshop, starts student CIs.
            deployment.verify_student_source()
        order["attempted"] = True
        deployment.save()
        try:
            resource = client.request("POST", resource_path(order["namespace"], plural), body)
        except CatalogError as submit_error:
            if submit_error.status != 409:
                raise
            resource = client.request("GET", path)
    metadata = resource["metadata"]
    if (metadata["name"] != order["name"] or metadata["namespace"] != order["namespace"]
            or resource.get("kind") != body["kind"]
            or metadata.get("labels", {}).get(RUN_LABEL) != deployment.run["id"]
            or (order.get("uid") and order["uid"] != metadata.get("uid"))):
        raise CatalogError("Workshop identity changed; refusing to reuse it")
    order["uid"] = metadata["uid"]
    order["submitted"] = True
    secure_write(deployment.path.parent / (deployment.run["id"] + f"-{plural}.json"), resource)
    deployment.save()
    return resource


def deploy_workshop(deployment, fields):
    client, run = deployment.client, deployment.run
    settings = run["workshop"]
    item = client.catalog(run["catalogs"]["student"])
    expected = run.get("expected_student_version")
    if expected and item["metadata"].get("resourceVersion") != expected:
        raise ValueError("Student catalog resourceVersion differs from --expected-student-version")
    workshop_body, provision_body = workshop_bodies(item, client.session, run, fields, client.server)
    settings.setdefault("catalog_resource_version", item["metadata"].get("resourceVersion"))
    settings.setdefault("catalog_spec_sha256", hashlib.sha256(json.dumps(item["spec"], sort_keys=True).encode()).hexdigest())
    workshop = ensure_resource(deployment, "workshops", workshop_body)
    metadata = workshop["metadata"]
    settings["url"] = f"{client.server}/workshops/{metadata['namespace']}/{metadata['name']}"
    provision_body["metadata"]["ownerReferences"] = [{
        "apiVersion": workshop["apiVersion"], "kind": "Workshop", "controller": True,
        "name": metadata["name"], "uid": metadata["uid"],
    }]
    provision = ensure_resource(deployment, "workshopprovisions", provision_body)
    print("Workshop: " + settings["url"], flush=True)
    print(f"Student CIs requested: {settings['user_count']}; keep on failure: {run.get('keep_on_failure', False)}", flush=True)
    selector = urlencode({"labelSelector": f"{BABYLON}workshop={metadata['name']},{BABYLON}workshop-provision={provision['metadata']['name']}"})
    deadline, last = time.monotonic() + deployment.timeout, None
    while True:
        result = client.request("GET", client.claim_path(metadata["namespace"]) + "?" + selector)
        claims = result.get("items", [])
        ready, failed = [], []
        # Save all available diagnostics before reporting any failing child.
        for claim in sorted(claims, key=lambda claim: claim["metadata"]["name"]):
            cm = claim["metadata"]
            labels = cm.get("labels", {})
            if (labels.get(BABYLON + "workshop-uid") != metadata["uid"]
                    or labels.get(BABYLON + "workshop-provision") != provision["metadata"]["name"]
                    or not any(owner.get("uid") == provision["metadata"]["uid"] for owner in cm.get("ownerReferences", []))):
                raise CatalogError("Student claim does not belong to the saved workshop provision")
            role = next((key for key, order in run["orders"].items() if order["name"] == cm["name"]), None)
            if role is None:
                role = "student-" + str(1 + sum(key.startswith("student-") for key in run["orders"]))
                run["orders"][role] = {"namespace": cm["namespace"], "name": cm["name"], "uid": cm["uid"],
                                       "catalog": run["catalogs"]["student"], "external": True, "submitted": True}
            order = run["orders"][role]
            deployment.validate_identity(order, claim)
            secure_write(deployment.path.with_name(run["id"] + f"-{role}.claim.json"), claim)
            try:
                state, is_ready = claim_state(claim)
                order["state"] = state
                if is_ready:
                    ready.append(claim)
            except CatalogError as error:
                order["state"], order["error"] = "failed", str(error)
                failed.append(cm["name"])
        deployment.save()
        if failed:
            raise CatalogError("Student workshop provisioning failed: " + ", ".join(failed)
                               + ". Inspect the workshop and private claim exports; no cleanup was requested.")
        progress = f"Student CIs: {len(ready)}/{settings['user_count']} ready ({len(claims)} claims created)"
        if progress != last:
            print(progress, flush=True)
            last = progress
        if len(ready) >= settings["user_count"]:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CatalogError("Timed out waiting for student workshop; resume this run")
        time.sleep(min(deployment.poll, remaining))
    run["phase"] = "deployed"
    deployment.save()
    if not run.get("skip_checks"):
        for claim in ready:
            print("Checking student: " + claim["metadata"]["name"], flush=True)
            smoke_check(claim, deployment.poll, min(deployment.timeout, 300))
    run["phase"] = "complete"
    run["checks"] = "skipped" if run.get("skip_checks") else "passed"
    deployment.save()
    print("Student workshop deployed: " + settings["url"], flush=True)
    print("Checks: " + run["checks"], flush=True)
