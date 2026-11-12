"""Resolve the student CI's Showroom source and verify it before ordering."""

import hashlib
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

import yaml

from catalog_client import CatalogError


REPO_KEY = "ocp4_workload_showroom_content_git_repo"
REF_KEY = "ocp4_workload_showroom_content_git_repo_ref"


def git(args, cwd=None, check=True, timeout=60):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    try:
        result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True,
                                text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise CatalogError("Git source check failed or timed out; verify Git access and retry") from None
    if check and result.returncode:
        # Git stderr may contain authenticated URLs; keep it out of logs.
        raise CatalogError("Git source operation failed; verify repository access and the selected ref")
    return result


def repository_id(url):
    """Compare HTTPS/SSH URLs without printing credential-bearing values."""
    if re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+", url):
        host, path = url[4:].split(":", 1)
        return host.lower(), path.removesuffix(".git").strip("/")
    parsed = urlsplit(url)
    if parsed.scheme == "https" and parsed.hostname and not (
            parsed.username or parsed.password or parsed.query or parsed.fragment):
        return parsed.hostname.lower(), parsed.path.removesuffix(".git").strip("/")
    # Local repositories are useful for offline verification and tests.
    if not parsed.scheme and not url.startswith("-") and Path(url).is_absolute():
        return "local", str(Path(url).resolve())
    raise CatalogError("Source repositories must use credential-free HTTPS or Git SSH URLs")


def checked_ref(ref):
    if not isinstance(ref, str) or not ref or "{{" in ref or ref.startswith("-"):
        raise CatalogError("Cannot resolve a literal Showroom branch or tag from the student CI")
    if git(["check-ref-format", "refs/heads/" + ref], check=False).returncode:
        raise CatalogError("Invalid Showroom Git ref")
    return ref


def source_from_config(common, stage):
    """Read only the two scalar source values, allowing unrelated !vault tags."""
    values = {}
    for text in (common, stage):
        try:
            root = yaml.compose(text)
        except yaml.YAMLError:
            raise CatalogError("Invalid CI source configuration YAML (contents suppressed)") from None
        if not isinstance(root, yaml.MappingNode):
            raise CatalogError("CI source configuration must be a YAML mapping")
        for key, value in root.value:
            if key.value in (REPO_KEY, REF_KEY):
                if not isinstance(value, yaml.ScalarNode):
                    raise CatalogError("Showroom repository and ref must be literal strings")
                values[key.value] = value.value
    if not values.get(REPO_KEY) or not values.get(REF_KEY) or "{{" in values[REPO_KEY]:
        raise CatalogError("Cannot resolve Showroom repository/ref from common.yaml and the CI stage file")
    repository_id(values[REPO_KEY])
    return {"repo": values[REPO_KEY], "ref": checked_ref(values[REF_KEY])}


def resolve_student_source(item, state_dir):
    """Read the actual AgnosticV common/stage overrides using a private Git cache."""
    upstream = (item["spec"].get("agnosticvRepo") or {}).get("git") or {}
    url, revision = upstream.get("url"), upstream.get("ref")
    if not url or not revision:
        raise CatalogError("Student catalog item does not expose its AgnosticV Git source")
    repository_id(url)
    checked_ref(revision)
    name = item["metadata"]["name"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){2,}", name):
        raise CatalogError("Cannot derive the AgnosticV configuration path from this catalog item")
    *directories, stage = name.split(".")
    config_path = "/".join(directories)
    cache_root = Path(state_dir) / "source-cache"
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256((url + "\n" + revision).encode()).hexdigest()[:20]
    cache = cache_root / (key + ".git")
    if not cache.exists():
        git(["clone", "--bare", "--filter=blob:none", "--depth=1", "--single-branch",
             "--branch", revision, "--", url, str(cache)], timeout=120)
        commit = git(["rev-parse", "HEAD"], cache).stdout.strip()
    else:
        git(["fetch", "--depth=1", "origin", revision], cache, timeout=120)
        commit = git(["rev-parse", "FETCH_HEAD"], cache).stdout.strip()
    contents = []
    for filename in ("common.yaml", stage + ".yaml"):
        result = git(["show", f"{commit}:{config_path}/{filename}"], cache, check=False)
        if result.returncode:
            raise CatalogError(f"Cannot read {config_path}/{filename} from the catalog's AgnosticV source")
        contents.append(result.stdout)
    return {**source_from_config(*contents), "agnosticv_commit": commit,
            "agnosticv_path": config_path, "catalog_resource_version": item["metadata"].get("resourceVersion")}


def check_content_source(source):
    repository_id(source["repo"])
    ref = checked_ref(source["ref"])
    result = git(["ls-remote", "--exit-code", "--heads", "--tags", "--", source["repo"],
                  "refs/heads/" + ref, "refs/tags/" + ref, "refs/tags/" + ref + "^{}"], check=False)
    if result.returncode == 2:
        raise CatalogError(f"Showroom Git ref '{ref}' does not exist. "
                           "Publish the intended commit with publish-content, or update the CI's source ref and wait for catalog sync")
    if result.returncode:
        raise CatalogError("Cannot read the Showroom Git repository; verify access before ordering")
    refs = {fields[1]: fields[0] for line in result.stdout.splitlines()
            if len(fields := line.split()) == 2}
    commit = refs.get("refs/heads/" + ref) or refs.get("refs/tags/" + ref + "^{}") or refs.get("refs/tags/" + ref)
    if not commit or not re.fullmatch(r"[a-f0-9]{40,64}", commit):
        raise CatalogError("Git did not return a verifiable Showroom commit")
    return {**source, "commit": commit}


def publish_content(source, local_repo, commit="HEAD", remote="origin"):
    """Explicitly publish a committed revision, using a normal fast-forward push."""
    branch = checked_ref(source["ref"])
    if not remote or remote.startswith("-"):
        raise CatalogError("Invalid Git remote")
    targets = git(["remote", "get-url", "--push", "--all", remote], local_repo).stdout.splitlines()
    if not targets or any(repository_id(target) != repository_id(source["repo"]) for target in targets):
        raise CatalogError("Git push remote does not match the student CI's Showroom repository")
    resolved = git(["rev-parse", "--verify", "--end-of-options", commit + "^{commit}"], local_repo).stdout.strip()
    for path in ("site.yml", "content/antora.yml", "content/modules/ROOT/pages/index.adoc"):
        if git(["cat-file", "-e", resolved + ":" + path], local_repo, check=False).returncode:
            raise CatalogError(f"Selected content commit does not contain {path}")
    # Do not commit the working tree or force-push a shared content branch.
    result = git(["push", remote, resolved + ":refs/heads/" + branch], local_repo, check=False, timeout=120)
    if result.returncode:
        raise CatalogError("Content push was rejected or failed. No force-push was attempted; reconcile the branch and retry")
    published = check_content_source(source)
    if published["commit"] != resolved:
        raise CatalogError("The published Showroom ref no longer matches the selected commit")
    return published
