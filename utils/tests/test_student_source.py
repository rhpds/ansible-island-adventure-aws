"""Exercise source verification and publication against disposable local Git repos."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_client import CatalogError
from student_source import (check_content_source, publish_content, repository_id,
                            resolve_student_source, source_from_config)


def run_git(cwd, *args):
    return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.DEVNULL, text=True).strip()


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.work = self.directory / "work"
        self.work.mkdir()
        run_git(self.work, "init", "-b", "main")
        run_git(self.work, "config", "user.name", "Offline Test")
        run_git(self.work, "config", "user.email", "test@example.invalid")
        for path in ("site.yml", "content/antora.yml", "content/modules/ROOT/pages/index.adoc"):
            target = self.work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("committed content\n")
        run_git(self.work, "add", ".")
        run_git(self.work, "commit", "-m", "Initial synthetic content")
        self.commit = run_git(self.work, "rev-parse", "HEAD")
        self.remote = self.directory / "content.git"
        run_git(self.directory, "clone", "--bare", str(self.work), str(self.remote))
        run_git(self.work, "remote", "add", "origin", str(self.remote))
        self.source = {"repo": str(self.remote), "ref": "dev"}

    def test_stage_override_selects_dev_not_common_main(self):
        source = source_from_config(
            f"ocp4_workload_showroom_content_git_repo: {self.remote}\n"
            "ocp4_workload_showroom_content_git_repo_ref: main\nsecret: !vault |\n  encrypted\n",
            "ocp4_workload_showroom_content_git_repo_ref: dev\n")
        self.assertEqual(source["ref"], "dev")
        self.assertEqual(source["repo"], str(self.remote))

    def test_missing_dev_reproduces_log_failure_before_ordering(self):
        with self.assertRaisesRegex(CatalogError, "ref 'dev' does not exist"):
            check_content_source(self.source)
        self.assertEqual(check_content_source({**self.source, "ref": "main"})["commit"], self.commit)

    def test_explicit_publish_creates_dev_from_chosen_committed_revision(self):
        (self.work / "site.yml").write_text("uncommitted changes must stay local\n")
        result = publish_content(self.source, self.work, self.commit)
        self.assertEqual(result["commit"], self.commit)
        self.assertEqual(run_git(self.remote, "show", "refs/heads/dev:site.yml"), "committed content")
        self.assertIn("uncommitted", (self.work / "site.yml").read_text())

    def test_non_fast_forward_publish_is_rejected(self):
        publish_content(self.source, self.work, self.commit)
        (self.work / "site.yml").write_text("newer committed content\n")
        run_git(self.work, "add", "site.yml")
        run_git(self.work, "commit", "-m", "Newer synthetic content")
        newer = run_git(self.work, "rev-parse", "HEAD")
        publish_content(self.source, self.work, newer)
        with self.assertRaisesRegex(CatalogError, "No force-push"):
            publish_content(self.source, self.work, self.commit)
        self.assertEqual(check_content_source(self.source)["commit"], newer)

    def test_wrong_push_destination_is_rejected(self):
        wrong = {**self.source, "repo": str(self.directory / "unrelated.git")}
        with self.assertRaisesRegex(CatalogError, "does not match"):
            publish_content(wrong, self.work, self.commit)
        with self.assertRaises(CatalogError):
            check_content_source(self.source)

    def test_selected_commit_requires_showroom_entrypoints(self):
        run_git(self.work, "rm", "site.yml")
        run_git(self.work, "commit", "-m", "Remove required entrypoint")
        with self.assertRaisesRegex(CatalogError, "site.yml"):
            publish_content(self.source, self.work, "HEAD")

    def test_https_and_ssh_repositories_match_without_credentials(self):
        self.assertEqual(repository_id("https://github.com/example/content.git"),
                         repository_id("git@github.com:example/content.git"))
        with self.assertRaises(CatalogError) as error:
            repository_id("https://username:synthetic-secret@github.com/example/content.git")
        self.assertNotIn("synthetic-secret", str(error.exception))

    def test_effective_ci_source_is_read_from_common_and_stage_and_refreshed(self):
        agv = self.directory / "agnosticv"
        agv.mkdir()
        run_git(agv, "init", "-b", "master")
        run_git(agv, "config", "user.name", "Offline Test")
        run_git(agv, "config", "user.email", "test@example.invalid")
        config = agv / "rh1-2027/lb1663-rh1-ans-island-student-cnv"
        config.mkdir(parents=True)
        (config / "common.yaml").write_text(
            f"ocp4_workload_showroom_content_git_repo: {self.remote}\n"
            "ocp4_workload_showroom_content_git_repo_ref: main\n")
        (config / "dev.yaml").write_text("ocp4_workload_showroom_content_git_repo_ref: dev\n")
        run_git(agv, "add", ".")
        run_git(agv, "commit", "-m", "Synthetic CI source configuration")
        item = {"metadata": {"name": "rh1-2027.lb1663-rh1-ans-island-student-cnv.dev", "resourceVersion": "123"},
                "spec": {"agnosticvRepo": {"git": {"url": str(agv), "ref": "master"}}}}
        source = resolve_student_source(item, self.directory)
        self.assertEqual(source["ref"], "dev")
        (config / "dev.yaml").write_text("ocp4_workload_showroom_content_git_repo_ref: main\n")
        run_git(agv, "add", ".")
        run_git(agv, "commit", "-m", "Change configured source ref")
        updated = resolve_student_source(item, self.directory)
        self.assertEqual(updated["ref"], "main")
        self.assertNotEqual(source["agnosticv_commit"], updated["agnosticv_commit"])


if __name__ == "__main__":
    unittest.main()
