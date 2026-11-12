"""Resumable two-CI development environments for Ansible Island Adventure."""

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

import jsonschema
import requests

from catalog_client import (CatalogClient, CatalogError, DEFAULT_SERVER, browser_login,
                            cookie_map, load_credentials, read_json, secure_write, server_url)
from provision_fields import REQUIRED_FIELDS, extract_provision_data, load_claim, student_fields
from student_source import check_content_source, publish_content, resolve_student_source


ROOT = Path(__file__).resolve().parents[1]
LEADERBOARD = "babylon-catalog-dev/rh1-2027.lb1663-rh1-ans-island-leaderboard-cnv.dev"
STUDENT = "babylon-catalog-dev/rh1-2027.lb1663-rh1-ans-island-student-cnv.dev"
BABYLON = "babylon.gpte.redhat.com/"
DEMO = "demo.redhat.com/"
RUN_LABEL = "aia.rhpds.com/run-id"
AUTO_DETACH_WHEN = "status.resources | json_query(\"[?state.spec.vars.current_state == 'provision-failed']\") | length != 0"


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def duration(value):
    """Catalog duration strings such as 2d, 4h, 30min, and 1d 2h."""
    units = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600,
             "d": 86400, "day": 86400, "w": 604800}
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*(min|sec|day|hr|s|m|h|d|w)", value)
    residue = re.sub(r"(\d+(?:\.\d+)?)\s*(min|sec|day|hr|s|m|h|d|w)", "", value)
    seconds = sum(float(number) * units[unit] for number, unit in parts)
    if not parts or residue.strip() or seconds <= 0:
        raise ValueError("Unsupported catalog lifespan; use a duration such as 2d or 4h")
    return timedelta(seconds=seconds)


def claim_state(claim):
    """Never accept a stale successful summary over a failed resource."""
    status = claim.get("status") or {}
    errors = (status.get("provider") or {}).get("validationErrors")
    if errors:
        # Schema validation errors can contain submitted passwords. Only render
        # the known-safe 'No such parameter' form; keep other details private.
        unknown = []
        for error in errors if isinstance(errors, list) else []:
            match = re.fullmatch(r"No such parameter ([A-Za-z_][A-Za-z0-9_]*)\.", str(error))
            if match:
                unknown.append(match.group(1))
        detail = " Unknown parameters: " + ", ".join(unknown) + "." if unknown else ""
        raise CatalogError("Provider rejected the order parameters." + detail
                           + " Inspect the service page or private claim export for details.")
    summary = status.get("summary") or {}
    state = summary.get("state", "pending")
    states = [state]
    resources_ready = True
    for resource in status.get("resources") or []:
        variables = (((resource.get("state") or {}).get("spec") or {}).get("vars") or {})
        states.append(variables.get("current_state", ""))
        if (resource.get("ready") is False or resource.get("healthy") is False
                or variables.get("healthy") is False
                or variables.get("current_state", "started") != "started"):
            resources_ready = False
    failed = next((s for s in states if "fail" in s.lower() or s.lower() == "error"), None)
    if failed:
        raise CatalogError(f"Deployment entered {failed}; inspect its service page for diagnostics")
    if claim.get("metadata", {}).get("deletionTimestamp") or state in ("destroying", "destroyed", "retired"):
        raise CatalogError("Environment is retiring or has been destroyed")
    for lifespan in [(claim.get("spec") or {}).get("lifespan"), status.get("lifespan")]:
        end = (lifespan or {}).get("end")
        if end and datetime.fromisoformat(end.replace("Z", "+00:00")) <= utcnow():
            raise CatalogError("Environment has expired; order a new environment")
    ready = (state == "started" and resources_ready
             and status.get("ready") is not False and status.get("healthy") is not False)
    return state, ready


def student_parameter_names(item):
    """Discover catalog parameter IDs without needing leaderboard credentials."""
    parameters = item["spec"].get("parameters") or []
    names = {}
    for field in REQUIRED_FIELDS:
        matches = [p for p in parameters if p["name"] == field["target"]]
        if not matches:
            matches = [p for p in parameters if p.get("variable") == field["target"]
                       or p.get("formLabel", "").strip().casefold() == field["label"].casefold()]
        if len(matches) != 1:
            raise ValueError(f"Cannot uniquely match student form field: {field['label']}; run inspect")
        names[field["target"]] = matches[0]["name"]
    if len(set(names.values())) != len(REQUIRED_FIELDS):
        raise ValueError("Student catalog maps multiple provision fields to the same parameter")
    return names


def resolve_parameters(item, fields, activity, purpose, groups):
    parameters = item["spec"].get("parameters") or []
    values = {}
    for parameter in parameters:
        schema = parameter.get("openAPIV3Schema") or {}
        if "default" in schema:
            values[parameter["name"]] = deepcopy(schema["default"])
        elif "value" in parameter:
            values[parameter["name"]] = deepcopy(parameter["value"])
    if fields is not None:
        for target, name in student_parameter_names(item).items():
            values[name] = fields[target]
    values.update(purpose=purpose, purpose_activity=activity, purpose_explanation="")
    for parameter in parameters:
        name = parameter["name"]
        schema = parameter.get("openAPIV3Schema") or {}
        value = values.get(name)
        if parameter.get("required") and (value is None or value == ""):
            raise ValueError(f"Required catalog parameter has no value: {name}")
        if value is not None:
            try:
                jsonschema.validate(value, schema)
            except jsonschema.ValidationError:
                raise ValueError(f"Catalog schema rejected parameter: {name} (value suppressed)") from None
        if name == "purpose":
            options = schema.get("x-form-options") or []
            if options:
                matching = [p for p in options if p["name"] == purpose and p["activity"] == activity
                            and (not p.get("requiredRoles") or set(p["requiredRoles"]) & set(groups))]
                if not matching:
                    raise ValueError("Activity/purpose is unavailable for this catalog item or account")
    # Activity/explanation belong in annotations. Poolboy rejects undeclared
    # provider parameters even when the Kubernetes API accepted the claim.
    return {p["name"]: values[p["name"]] for p in parameters if p["name"] in values}


def build_claim(item, session, namespace, name, run_id, server, fields=None,
                activity="Asset Development", purpose="Developing a community demo", lifespan=None,
                keep_on_failure=False):
    spec, metadata = item["spec"], item["metadata"]
    if spec.get("externalUrl"):
        raise ValueError("External catalog items cannot be ordered as ResourceClaims")
    parameters = resolve_parameters(item, fields, activity, purpose, session.get("groups", []))
    lifetime = spec.get("lifespan") or {}
    now = utcnow()
    supported_actions = spec.get("supportedActions")
    runtime = (spec.get("runtime") or {}).get("default") or "4h"
    auto_stop_disabled = ((supported_actions is not None and "stop" not in supported_actions)
                          or "999h" in runtime)
    if not auto_stop_disabled:
        parameters["stop_timestamp"] = timestamp(now + duration(runtime))
    end = now + duration(lifespan or lifetime.get("default", "2d"))
    for key in ("maximum", "relativeMaximum"):
        if lifetime.get(key) and end > now + duration(lifetime[key]):
            raise ValueError(f"Requested lifespan exceeds catalog {key}")
    if lifetime.get("end") and end > datetime.fromisoformat(lifetime["end"].replace("Z", "+00:00")):
        raise ValueError("Requested lifespan exceeds catalog end date")
    service_ns = next((n for n in session.get("serviceNamespaces", []) if n["name"] == namespace), None)
    if not service_ns and (session.get("userNamespace") or {}).get("name") == namespace:
        service_ns = session["userNamespace"]
    if not service_ns:
        raise ValueError("Selected namespace is not an accessible service namespace")
    annotations = {
        BABYLON + "catalogDisplayName": metadata["namespace"],
        BABYLON + "catalogItemDisplayName": spec.get("displayName") or metadata["name"],
        BABYLON + "category": spec.get("category", ""),
        BABYLON + "url": f"{server}/services/{namespace}/{name}",
        DEMO + "requester": service_ns.get("requester") or session["user"],
        DEMO + "orderedBy": session["user"],
        DEMO + "scheduled": "false",
        DEMO + "provide_salesforce-id_later": "false",
        DEMO + "purpose": purpose,
        DEMO + "purpose-activity": activity,
    }
    for key, value in (metadata.get("annotations") or {}).items():
        if key.startswith(BABYLON + "displayNameComponent"):
            annotations[key] = value
    if spec.get("userData") is not None:
        annotations[BABYLON + "userData"] = json.dumps(spec["userData"])
    for template in ("info", "user"):
        value = (spec.get("messageTemplates") or {}).get(template)
        if value is not None and (template != "user" or spec.get("workshopUserMode") != "none"):
            annotations[DEMO + template + "-message-template"] = json.dumps(value)
    if spec.get("supportLink"):
        annotations[BABYLON + "support-link"] = spec["supportLink"]
    if spec.get("workshopUiDisabled"):
        annotations[DEMO + "workshopUiDisabled"] = "true"
    labels = {BABYLON + "catalogItemName": metadata["name"],
              BABYLON + "catalogItemNamespace": metadata["namespace"],
              DEMO + "white-glove": "false", RUN_LABEL: run_id}
    if (metadata.get("labels") or {}).get("gpte.redhat.com/asset-uuid"):
        labels["gpte.redhat.com/asset-uuid"] = metadata["labels"]["gpte.redhat.com/asset-uuid"]
    if spec.get("bookbag"):
        labels[BABYLON + "labUserInterface"] = "bookbag"
    return {"apiVersion": "poolboy.gpte.redhat.com/v1", "kind": "ResourceClaim",
            "metadata": {"name": name, "namespace": namespace, "labels": labels, "annotations": annotations},
            "spec": {"provider": {"name": metadata["name"], "parameterValues": parameters},
                     "lifespan": {"end": timestamp(end)},
                     **({} if keep_on_failure else {"autoDetach": {"when": AUTO_DETACH_WHEN}})}}


def validate_claim_identity(order, claim, run_id=None):
    metadata = claim["metadata"]
    if metadata["name"] != order["name"] or metadata["namespace"] != order["namespace"]:
        raise CatalogError("Catalog returned a different ResourceClaim identity")
    if order.get("uid") and metadata.get("uid") != order["uid"]:
        raise CatalogError("ResourceClaim was replaced; refusing to reuse a different environment")
    if not order.get("external") and (metadata.get("labels") or {}).get(RUN_LABEL) != run_id:
        raise CatalogError("Existing ResourceClaim does not belong to this run")
    expected_ns, expected_name = order["catalog"].split("/", 1)
    labels = metadata.get("labels") or {}
    if (labels.get(BABYLON + "catalogItemName") != expected_name
            or labels.get(BABYLON + "catalogItemNamespace") != expected_ns):
        raise CatalogError("ResourceClaim belongs to a different catalog item")


def remember_leaderboard(client, directory, claim, catalog):
    metadata = claim["metadata"]
    selected = {"server": client.server, "namespace": metadata["namespace"], "name": metadata["name"],
                "uid": metadata["uid"], "catalog": catalog, "external": True}
    validate_claim_identity(selected, claim)
    if not claim_state(claim)[1]:
        raise CatalogError("Selected leaderboard must be started and healthy")
    student_fields(claim)
    secure_write(Path(directory) / "leaderboard.json", selected)
    return selected


@contextmanager
def state_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = directory / ".lock"
    if lock_path.is_symlink():
        raise ValueError("State lock must not be a symbolic link")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CatalogError("Another environment command is running for this state directory") from None
        yield


class Deployment:
    def __init__(self, client, directory, run, poll=30, timeout=7200, retry_submit=False):
        self.client, self.directory, self.run = client, Path(directory), run
        self.poll, self.timeout, self.retry_submit = poll, timeout, retry_submit
        self.path = self.directory / "runs" / (run["id"] + ".json")

    def save(self):
        secure_write(self.path, self.run)

    def url(self, order):
        return f"{self.client.server}/services/{order['namespace']}/{order['name']}"

    def prepare(self, role, fields=None):
        if role in self.run["orders"]:
            return
        reference = self.run["catalogs"][role]
        item = self.client.catalog(reference)
        if role == "student" and self.run.get("expected_student_version"):
            if item["metadata"].get("resourceVersion") != self.run["expected_student_version"]:
                raise ValueError("Student catalog resourceVersion differs from --expected-student-version")
        name = f"aia-{role}-{self.run['id']}"
        namespace = self.run["namespace"]
        body = build_claim(item, self.client.session, namespace, name, self.run["id"], self.client.server,
                           fields, self.run["activity"], self.run["purpose"], self.run.get("lifespan"),
                           keep_on_failure=self.run.get("keep_on_failure", False))
        request_path = self.path.with_name(self.run["id"] + f"-{role}.request.json")
        secure_write(request_path, body)
        self.run["orders"][role] = {
            "namespace": namespace, "name": name, "catalog": reference,
            "request_file": request_path.name, "attempted": False,
            "catalog_resource_version": item["metadata"].get("resourceVersion"),
            "catalog_spec_sha256": hashlib.sha256(json.dumps(item["spec"], sort_keys=True).encode()).hexdigest(),
            "warnings_sha256": hashlib.sha256(str(item["spec"].get("termsOfService", "")).encode()).hexdigest(),
        }
        self.save()

    def validate_identity(self, order, claim):
        validate_claim_identity(order, claim, self.run["id"])

    def ensure_submitted(self, role):
        order = self.run["orders"][role]
        try:
            claim = self.client.claim(order["namespace"], order["name"])
        except CatalogError as error:
            if error.status != 404:
                raise
            if order.get("external") or order.get("uid"):
                raise CatalogError("Saved environment no longer exists; select or order a new one") from None
            if order["attempted"] and not self.retry_submit:
                raise CatalogError("Previous submission is unresolved. Inspect the catalog, then resume --retry-submit to retry the SAME claim name") from None
            request_path = self.path.parent / order["request_file"]
            body = read_json(request_path)
            self.validate_identity(order, body)
            if role == "student":
                self.verify_student_source()
            end = body["spec"]["lifespan"]["end"]
            if datetime.fromisoformat(end.replace("Z", "+00:00")) <= utcnow():
                raise CatalogError("Prepared order has expired; start a new run")
            order["attempted"] = True
            self.save()  # Intent and exact name are durable BEFORE the POST.
            try:
                claim = self.client.request("POST", self.client.claim_path(order["namespace"]), body)
            except CatalogError as submit_error:
                if submit_error.status != 409:
                    raise
                claim = self.client.claim(order["namespace"], order["name"])
        self.validate_identity(order, claim)
        order["uid"] = claim["metadata"]["uid"]
        order["submitted"] = True
        self.save()
        return claim

    def verify_student_source(self):
        source = self.run.get("student_source")
        if source is None:
            source = resolve_student_source(self.client.catalog(self.run["catalogs"]["student"]), self.directory)
        verified = check_content_source(source)
        if source.get("commit") and verified["commit"] != source["commit"]:
            raise CatalogError("Showroom ref moved since preflight. Start a new run to select the new content commit")
        self.run["student_source"] = verified
        self.save()

    def wait(self, role):
        order = self.run["orders"][role]
        claim = self.ensure_submitted(role)
        print(f"{role}: {self.url(order)}", flush=True)
        deadline = time.monotonic() + self.timeout
        last = None
        while True:
            self.validate_identity(order, claim)
            secure_write(self.path.with_name(self.run["id"] + f"-{role}.claim.json"), claim)
            state, ready = claim_state(claim)
            if role == "leaderboard" and order.get("external") and state == "stopped":
                raise CatalogError("Saved leaderboard is stopped. Start it in the catalog, then resume")
            if state != last:
                print(f"{role}: {state}", flush=True)
                last = state
            order["state"] = state
            self.save()
            if ready:
                return claim
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CatalogError(f"Timed out waiting for {role} (last state: {state}); resume this run")
            time.sleep(min(self.poll, remaining))
            claim = self.client.claim(order["namespace"], order["name"])

    def execute(self):
        if "student" not in self.run["orders"]:
            item = self.client.catalog(self.run["catalogs"]["student"])
            student_parameter_names(item)
            expected = self.run.get("expected_student_version")
            if expected and item["metadata"].get("resourceVersion") != expected:
                raise ValueError("Student catalog resourceVersion differs from --expected-student-version")
            if not self.run.get("student_source"):
                self.run["student_source"] = check_content_source(resolve_student_source(item, self.directory))
                self.save()
        self.prepare("leaderboard")
        leaderboard = self.wait("leaderboard")
        fields = student_fields(leaderboard)
        # Save only the identity for reuse; credentials are refreshed from the API.
        remember_leaderboard(self.client, self.directory, leaderboard, self.run["catalogs"]["leaderboard"])
        if self.run.get("workshop"):
            from student_workshop import deploy_workshop
            deploy_workshop(self, fields)
            return
        self.prepare("student", fields)
        # A stopped/retired leaderboard must not be reused on a resumed student order.
        if not claim_state(self.client.claim(self.run["orders"]["leaderboard"]["namespace"],
                                             self.run["orders"]["leaderboard"]["name"]))[1]:
            raise CatalogError("Leaderboard is no longer started; restore it before resuming")
        student = self.wait("student")
        self.run["phase"] = "deployed"
        self.save()
        if not self.run.get("skip_checks"):
            smoke_check(student, self.poll, min(self.timeout, 300))
        self.run["phase"] = "complete"
        self.run["checks"] = "skipped" if self.run.get("skip_checks") else "passed"
        self.save()
        print(f"Environment deployed. Service: {self.url(self.run['orders']['student'])}")
        print(f"Checks: {self.run['checks']}. Private exports: {self.path.parent}")


def smoke_check(claim, poll=15, timeout=300):
    """Check public service endpoints, using NO catalog cookies or passwords."""
    data = extract_provision_data(claim)
    endpoints = {"CTFd": ("ctfd_url", ""), "Gitea": ("gitea_url", "/api/v1/version"),
                 "AAP": ("aap_url", "")}
    urls = {}
    for label, (key, suffix) in endpoints.items():
        url = data.get(key, "")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise CatalogError(f"Student provision data lacks a valid HTTPS {key}; inspect its YAML or use --skip-checks")
        urls[label] = url.rstrip("/") + suffix
    deadline = time.monotonic() + timeout
    pending = dict(urls)
    with requests.Session() as http:
        while pending:
            for label, url in list(pending.items()):
                try:
                    response = http.get(url, timeout=15, allow_redirects=False)
                    response.close()
                    if response.status_code == 200 or (label == "AAP" and response.status_code in (302, 303)):
                        print(f"Check {label}: passed", flush=True)
                        pending.pop(label)
                except requests.RequestException:
                    pass
            if pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CatalogError("Service reachability checks failed: " + ", ".join(pending)
                                       + "; environment is deployed. Resume to retry")
                time.sleep(min(poll, remaining))


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--server", default=DEFAULT_SERVER)
    cli.add_argument("--state-dir", type=Path, default=ROOT / ".aia-dev")
    cli.add_argument("--babylon-config", type=Path, help="Reuse cookies from a Babylon CLI config")
    commands = cli.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Complete browser SSO or import catalog cookies")
    login.add_argument("--cookies-file", type=Path)
    login.add_argument("--no-browser", action="store_true", help="Print the SSO URL without opening it")
    login.add_argument("--timeout", type=float, default=300)
    commands.add_parser("whoami", help="Verify SSO and API access without ordering")
    destroy = commands.add_parser("destroy", help="Plan UID-checked cleanup; students before leaderboard")
    destroy.add_argument("--leaderboard-file", type=Path, required=True)
    destroy.add_argument("--workshop-file", type=Path, action="append", default=[])
    destroy.add_argument("--student-file", type=Path, action="append", default=[])
    destroy.add_argument("--include-linked-students", action="store_true", help="Include all student CIs in this namespace using the selected leaderboard")
    destroy.add_argument("--leaderboard-catalog", default=LEADERBOARD)
    destroy.add_argument("--student-catalog", default=STUDENT)
    destroy.add_argument("--execute", action="store_true", help="Execute the cleanup; default is plan only")
    destroy.add_argument("--poll", type=float, default=15)
    destroy.add_argument("--timeout", type=float, default=3600)
    export = commands.add_parser("export-students", help="Export private post-deployment Ansible inventory; places no orders")
    export.add_argument("--run", help="Run ID; defaults to the latest run")
    select = commands.add_parser("use-leaderboard", help="Save a healthy existing leaderboard for reuse; places no orders")
    select.add_argument("leaderboard", help="Existing claim name or namespace/name")
    select.add_argument("--leaderboard-catalog", default=LEADERBOARD)
    inspect = commands.add_parser("inspect", help="Show live form fields, versions, and warnings")
    inspect.add_argument("--leaderboard-catalog", default=LEADERBOARD)
    inspect.add_argument("--student-catalog", default=STUDENT)
    check = commands.add_parser("check-student", help="Verify the CI's effective Showroom Git source; places no orders")
    check.add_argument("--student-catalog", default=STUDENT)
    publish = commands.add_parser("publish-content", help="Explicitly push a committed snapshot to the CI's Showroom branch")
    publish.add_argument("--commit", required=True, help="Committed revision to publish, e.g. HEAD or a commit SHA")
    publish.add_argument("--remote", default="origin", help="Local Git remote for the content repository")
    publish.add_argument("--student-catalog", default=STUDENT)
    for command in ("bootstrap", "new-student"):
        sub = commands.add_parser(command, help="Create/select a leaderboard, then order a new student" if command == "bootstrap" else "Reuse a leaderboard and order a new student")
        source = sub.add_mutually_exclusive_group()
        source.add_argument("--leaderboard", help="Existing leaderboard namespace/name (or name in your namespace)")
        source.add_argument("--leaderboard-file", type=Path, help="ResourceClaim YAML; identity is refreshed from the API")
        if command == "bootstrap":
            source.add_argument("--new-leaderboard", action="store_true", help="Order a new leaderboard even if one is saved")
        sub.add_argument("--namespace", help="Service namespace; defaults to the authenticated user's namespace")
        sub.add_argument("--leaderboard-catalog", default=LEADERBOARD)
        sub.add_argument("--student-catalog", default=STUDENT)
        sub.add_argument("--activity", default="Asset Development")
        sub.add_argument("--purpose", default="Developing a community demo")
        sub.add_argument("--lifespan", help="Catalog lifespan override, e.g. 2d")
        sub.add_argument("--expected-student-version", help="Require this live CatalogItem resourceVersion")
        sub.add_argument("--workshop", action="store_true", help="Order student CIs through the workshop interface")
        sub.add_argument("--workshop-user-count", type=int, help="Independent student CIs in a workshop (default: 2)")
        sub.add_argument("--keep-on-failure", action="store_true", help="Keep infrastructure attached if provisioning fails")
        sub.add_argument("--accept-warnings", action="store_true", required=True,
                         help="Acknowledge the catalog warnings shown by inspect, equivalent to its checkbox")
        sub.add_argument("--skip-checks", action="store_true", help="Skip service reachability checks after deployment")
        sub.add_argument("--poll", type=float, default=30)
        sub.add_argument("--timeout", type=float, default=7200, help="Seconds per deployment stage")
    resume = commands.add_parser("resume", help="Resume a saved run, reusing its exact order names")
    resume.add_argument("--run", help="Run ID; defaults to the latest run")
    resume.add_argument("--retry-submit", action="store_true", help="Retry an unresolved submission with the SAME claim name")
    resume.add_argument("--poll", type=float, default=30)
    resume.add_argument("--timeout", type=float, default=7200)
    resume.add_argument("--skip-checks", action="store_true")
    status = commands.add_parser("status", help="Read live status and service URLs for a run")
    status.add_argument("--run")
    abandon = commands.add_parser("abandon", help="Close a local run without deleting any catalog resources")
    abandon.add_argument("--run")
    return cli


def load_run(directory, run_id=None):
    run_id = run_id or read_json(directory / "latest.json")["id"]
    if not re.fullmatch(r"[a-f0-9]{12}", run_id):
        raise ValueError("Invalid run ID")
    return read_json(directory / "runs" / (run_id + ".json"))


def code_revision():
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL, text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True))
        return {"commit": commit, "dirty": dirty, "note": "Local revision only; not proof of deployed code"}
    except (OSError, subprocess.CalledProcessError):
        return {"note": "Local revision unavailable"}


def new_run(args, client):
    directory = args.state_dir
    if (directory / "latest.json").exists():
        previous = load_run(directory)
        if previous["phase"] not in ("complete", "abandoned"):
            raise CatalogError(f"Run {previous['id']} is incomplete. Resume it or use abandon before starting another run")
    namespace = args.namespace or client.session["userNamespace"]["name"]
    catalogs = {"leaderboard": args.leaderboard_catalog, "student": args.student_catalog}
    for reference in catalogs.values():
        if len(reference.split("/")) != 2 or not all(reference.split("/")):
            raise ValueError("Catalog references must be namespace/name")
    run = {"id": uuid.uuid4().hex[:12], "server": client.server, "user": client.session["user"],
           "namespace": namespace, "catalogs": catalogs, "activity": args.activity, "purpose": args.purpose,
           "lifespan": args.lifespan, "expected_student_version": args.expected_student_version,
           "warnings_acknowledged": True, "skip_checks": args.skip_checks, "created": timestamp(utcnow()),
           "keep_on_failure": args.keep_on_failure,
           "local_code": code_revision(), "orders": {}, "phase": "provisioning"}
    selected = None
    if args.leaderboard_file:
        claim = load_claim(args.leaderboard_file)
        metadata = claim.get("metadata") or {}
        if not metadata.get("name") or not metadata.get("namespace"):
            raise ValueError("Leaderboard YAML must include metadata.name and metadata.namespace")
        selected = {"namespace": metadata["namespace"], "name": metadata["name"], "uid": metadata.get("uid")}
    elif args.leaderboard:
        pieces = args.leaderboard.split("/")
        if len(pieces) > 2 or not all(pieces):
            raise ValueError("Leaderboard must be name or namespace/name")
        selected = {"namespace": pieces[0] if len(pieces) == 2 else namespace, "name": pieces[-1]}
    elif not getattr(args, "new_leaderboard", False) and (directory / "leaderboard.json").exists():
        selected = read_json(directory / "leaderboard.json")
        if selected["server"] != client.server:
            raise CatalogError("Saved leaderboard belongs to a different catalog server")
    if selected:
        run["orders"]["leaderboard"] = {**selected, "catalog": catalogs["leaderboard"], "external": True}
    elif args.command == "new-student":
        raise CatalogError("No leaderboard selected. Use bootstrap, --leaderboard, or --leaderboard-file")
    item = client.catalog(catalogs["student"])
    student_parameter_names(item)
    if args.workshop_user_count is not None and not args.workshop:
        raise ValueError("--workshop-user-count requires --workshop")
    if args.workshop:
        from student_workshop import validate_workshop_catalog
        count = args.workshop_user_count if args.workshop_user_count is not None else 2
        validate_workshop_catalog(item, count)
        run["workshop"] = {"user_count": count}
    run["student_source"] = check_content_source(resolve_student_source(item, directory))
    deployment = Deployment(client, directory, run, args.poll, args.timeout)
    deployment.save()
    secure_write(directory / "latest.json", {"id": run["id"]})
    print(f"Run: {run['id']}. To continue: python utils/dev-environment.py resume --run {run['id']}", flush=True)
    return deployment


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        args.server = server_url(args.server)
        args.state_dir = args.state_dir.resolve()
        for option in ("poll", "timeout"):
            if hasattr(args, option) and getattr(args, option) <= 0:
                raise ValueError(f"--{option} must be positive")
        if args.command == "login":
            cookies = cookie_map(args.cookies_file, args.server) if args.cookies_file else browser_login(
                args.server, args.timeout, not args.no_browser)
            client = CatalogClient(args.server, cookies)
            session = client.authenticate()
            secure_write(args.state_dir / "auth.json", {"server": args.server, "auth": {
                "cookies": client.http.cookies.get_dict()}})
            print(f"Logged in as {session['user']}. Credentials stored privately in {args.state_dir / 'auth.json'}")
            return 0
        if args.command == "abandon":
            with state_lock(args.state_dir):
                run = load_run(args.state_dir, args.run)
                run["phase"] = "abandoned"
                secure_write(args.state_dir / "runs" / (run["id"] + ".json"), run)
                print(f"Run {run['id']} abandoned locally. Catalog resources are retained; manage cleanup in the catalog.")
            return 0
        cookies = load_credentials(args.state_dir, args.server, args.babylon_config)
        client = CatalogClient(args.server, cookies, args.state_dir / "auth.json")
        client.authenticate()
        if args.command == "whoami":
            print(f"User: {client.session['user']}\nNamespace: {client.session['userNamespace']['name']}")
            return 0
        if args.command == "destroy":
            from environment_cleanup import destroy_environment
            with state_lock(args.state_dir):
                destroy_environment(client, args)
            return 0
        if args.command == "inspect":
            for reference in (args.leaderboard_catalog, args.student_catalog):
                item = client.catalog(reference)
                print(f"\n{reference}\nresourceVersion: {item['metadata'].get('resourceVersion', 'unknown')}")
                for parameter in item["spec"].get("parameters") or []:
                    print(f"  {parameter['name']}: {parameter.get('formLabel', '')} (required={parameter.get('required', False)})")
                print("Catalog warnings:\n" + str(item["spec"].get("termsOfService") or "None published"))
            return 0
        with state_lock(args.state_dir):
            if args.command == "export-students":
                from student_inventory import inventory_from_claims, kubeconfig_from_claim
                run = load_run(args.state_dir, args.run)
                if run["server"] != client.server or run["user"] != client.session["user"]:
                    raise CatalogError("Run belongs to a different server or account")
                if run["phase"] not in ("deployed", "complete"):
                    raise CatalogError("Run must be deployed before exporting student inventory")
                claims = {role: client.claim(order["namespace"], order["name"])
                          for role, order in run["orders"].items()}
                path = args.state_dir / "runs" / (run["id"] + ".students.inventory.json")
                inventory = inventory_from_claims(run, claims, path.with_suffix(".kubeconfig"))
                kubeconfig = kubeconfig_from_claim(claims['leaderboard'])
                secure_write(path.with_suffix('.kubeconfig'), kubeconfig)
                secure_write(path, inventory)
                print(f"Exported private student inventory: {path}")
                print("No catalog orders or service changes made.")
                return 0
            if args.command in ("check-student", "publish-content"):
                source = resolve_student_source(client.catalog(args.student_catalog), args.state_dir)
                if args.command == "publish-content":
                    source = publish_content(source, ROOT, args.commit, args.remote)
                    print(f"Published committed content to {source['ref']}: {source['commit']}")
                else:
                    source = check_content_source(source)
                    print(f"Showroom source verified: {source['repo']} ({source['ref']}) at {source['commit']}")
                print("No catalog orders placed.")
                return 0
            if args.command == "use-leaderboard":
                pieces = args.leaderboard.split("/")
                if len(pieces) > 2 or not all(pieces):
                    raise ValueError("Leaderboard must be name or namespace/name")
                namespace = pieces[0] if len(pieces) == 2 else client.session["userNamespace"]["name"]
                claim = client.claim(namespace, pieces[-1])
                remember_leaderboard(client, args.state_dir, claim, args.leaderboard_catalog)
                print(f"Saved leaderboard: {namespace}/{pieces[-1]}. No orders placed.")
                return 0
            if args.command in ("bootstrap", "new-student"):
                deployment = new_run(args, client)
            else:
                run = load_run(args.state_dir, args.run)
                if run["server"] != client.server or run["user"] != client.session["user"]:
                    raise CatalogError("Run belongs to a different server or account")
                if args.command == "resume" and run["phase"] == "abandoned":
                    raise CatalogError("This run was abandoned; start a new run instead")
                deployment = Deployment(client, args.state_dir, run,
                                        getattr(args, "poll", 30), getattr(args, "timeout", 7200),
                                        getattr(args, "retry_submit", False))
                if args.command == "status":
                    print(f"Run: {run['id']} ({run['phase']}); checks: {run.get('checks', 'pending')}")
                    if run.get("workshop", {}).get("url"):
                        print("Workshop: " + run["workshop"]["url"])
                    for role, order in run["orders"].items():
                        claim = client.claim(order["namespace"], order["name"])
                        deployment.validate_identity(order, claim)
                        print(f"{role}: {deployment.url(order)}")
                        print(f"State: {claim_state(claim)[0]}")
                    return 0
                if args.skip_checks:
                    deployment.run["skip_checks"] = True
                    deployment.save()
            deployment.execute()
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted. Saved orders are retained; use resume to continue.", file=sys.stderr)
        return 130
    except (CatalogError, ValueError, OSError, KeyError, TypeError, AttributeError, jsonschema.SchemaError) as error:
        # Parser/schema errors must not render YAML/JSON contents or API bodies.
        message = str(error) if isinstance(error, (CatalogError, ValueError)) else type(error).__name__
        print("ERROR: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
