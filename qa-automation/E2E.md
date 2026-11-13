# Per-student Showroom E2E

The entry point is **`/stream/qa/e2e` on the selected student's Showroom URL**.
For this deployment:

```text
https://showroom-showroom-ldzc6.apps.ocp.pzwql.sandbox3945.opentlc.com/stream/qa/e2e
```

This replaces the legacy CNV/first-instructor test. It imports the single-student
health check as preflight, uses the same published student fields, and runs the
journey in `student_e2e.py`. The whole QA bundle is self-contained for the
Showroom runner. No leader admin credentials, service-account token, default
kubeconfig or shared-namespace admin access are used for E2E.

## Modes and consent

| Mode | Purpose | Changes | Required confirmation |
| --- | --- | --- | --- |
| `inspect` (default URL run) | Read-only prerequisite/content acceptance | Student OAuth login and health-probe audit records; no learner board/AAP/VM/workspace edits | None |
| `exercise` | Real Module 3 exercises plus safe instructor previews and handouts | Temporary CTFd player/score, temporary AAP project/credentials/templates; runs participant playbooks; installs Apache on this student's node | `aia_e2e_confirm_guid=<guid>` |
| `full` | Exercise plus Module 4 reset and Module 5 update/preservation drill | **Clears ALL players' progress on this personal board**; changes Ansible Docs to 15; all exercise-mode changes | Both `aia_e2e_confirm_guid=<guid>` and `aia_e2e_reset_confirm_guid=<guid>` |

**Do not run modifying modes on an active competition or a learner environment
with progress that must be retained.** Full mode clears solves, submissions,
awards and purchased hints exactly as the Showroom warns. It is not a backup or
restore workflow. It does not reset accounts, the VM or Dev Spaces. Apache and
the full-mode challenge content changes remain afterward; they are intentional
learner-journey effects, not disposable infrastructure.

The URL defaults to inspection; merely opening it never authorizes a board reset
or web deployment. To run another mode through the same URL, configure the
runner-supplied Ansible variables with the mode and confirmations. Do not assume
arbitrary URL query parameters become Ansible variables. CLI runs can pass the
same nonsecret controls explicitly.

## Journey coverage

The tests follow `content/modules/ROOT/pages/module-03-play-the-game.adoc`,
`module-04-explore-the-environment.adoc`, `module-05-customize-and-extend.adoc`
and `module-06-from-event-to-pipeline.adoc`.

| Module | Automated test | Inspect | Exercise | Full |
| --- | --- | --- | --- | --- |
| 3–4 | Student-owned repositories, main/treasurehunt revisions, assets, devfiles and access guide | Run | Run | Run |
| 4 | Personal lab password decrypts instructor answer Vault; answers never logged | Run | Run | Run |
| 3 | Deployed base challenge names/categories match instructor definitions (at least 25; six categories) | Run | Run | Run |
| 4 | Student OpenShift CLI identity and discovery of their personal CTFd route | Run | Run | Run |
| 3–4 | Two already-running repository workspaces, developer pods, separate Bound PVCs | Run | Run | Run |
| 3–4 | Main branch and Ansible, Lint, Navigator inside both real Dev Spaces containers | Skip | Run | Run |
| 3 | AAP organization/inventory/Machine credential, `localhost`, `web` → `rhel` membership | Run | Run | Run |
| 3 | Disposable player profile change, `DONE` solve, island unlock | Skip | Run | Run |
| 3 | Documentation, Galaxy and certified collection URL submissions | Skip | Run | Run |
| 3 | Participant-workspace Ansible version, Hello World and bottle Vault (`redhat`) exercises; observed answers accepted by CTFd | Skip | Run | Run |
| 3 | Live Gateway/Controller version answers accepted by CTFd | Skip | Run | Run |
| 3 | Sync student's treasurehunt project using generated SCM/Vault credentials | Skip | Run | Run |
| 3 | Launch AAP Hello World and verify output before submitting its answer | Skip | Run | Run |
| 3 | Launch AAP Secret Message with Vault passphrase `AAP`; verify output and submission | Skip | Run | Run |
| 3 | Launch Apache job with Machine + Vault credentials and become; require treasure HTTP 200 and observed answer | Skip | Run | Run |
| 4–5 | Instructor syntax check and reset/update `--check` previews | Skip | Run | Run |
| 4 | Reset personal board; verify player progress cleared and challenge IDs retained; solve `DONE` again | Skip | Skip | Run |
| 5 | Change Ansible Docs to 15, apply update, verify new `DONE` solve and challenge IDs retained | Skip | Skip | Run |
| 5 | Generate handout; verify this student's links/username and no password | Skip | Run | Run |
| 6 | Disposable-player solves and positive scoreboard result | Skip | Run | Run |
| Cleanup | Delete only the run's generated CTFd player and AAP objects | Run | Run | Run |

Browser UI clicks/SSO consent, external Ansible Quest gameplay, optional advanced
verifier challenges (`not.available.com` is not a verifier), event pacing,
facilitation, screenshots, and pipeline/follow-up discussions remain manual.
The test does not claim those experiences are automated.

## Prerequisites and execution

Complete organizer post-deploy first: the health check deliberately accepts an
empty fresh CTFd board, but E2E requires the documented challenge content,
repository assets and personal re-keyed answer Vault.

Open both repository workspaces as the Showroom instructs, using the repository
devfiles and **persistent storage**. Keep both running. The test does not create,
restart or delete Dev Spaces workspaces; missing, ambiguous or unready workspaces
are **BLOCKED**, not passed. Only namespaces whose project requester is the
student are inspected. If needed, select one owned workspace namespace with
`aia_e2e_workspace_namespace`.

The runner needs `oc` or `kubectl`. Developer containers need the tools and
collections supplied by the repository devfiles; instructor toolkit execution
also requires `oc` in the instructor workspace. Health preflight handles Python
requests/PyYAML/paramiko dependencies. The student login's private kubeconfig is
created in a temporary `0700` directory, with mode `0600`, and deleted afterward.
Only student credentials authenticate API operations.

To read the URL's streamed result:

```bash
curl -N --fail --max-time 1800 \
  'https://showroom-showroom-ldzc6.apps.ocp.pzwql.sandbox3945.opentlc.com/stream/qa/e2e'
```

Equivalent CLI inspection (no credentials on the command line):

```bash
ansible-playbook -i localhost, qa-automation/e2e.yml \
  -e @/private/student.yml
```

Explicitly authorized exercise on a disposable student environment:

```bash
ansible-playbook -i localhost, qa-automation/e2e.yml \
  -e @/private/student.yml \
  -e aia_e2e_mode=exercise -e aia_e2e_confirm_guid=<guid>
```

For the **destructive full lifecycle**, also set `aia_e2e_mode=full` and
`aia_e2e_reset_confirm_guid=<guid>`. Never pass real passwords as CLI extra-vars.
Select one exported inventory hostname with
`-e aia_healthcheck_hosts=instructor-<guid>` if using the existing exporter.

## Isolation, cleanup and evidence

- The real CTFd player's name/profile is not changed. Modifying tests create a
  random `e2e-<guid>-<run>` user in that student's board, use its profile/solves,
  and delete it afterward. It temporarily appears on this personal scoreboard.
  Full mode still resets **all** board progress and requires separate consent.
- AAP objects have a unique `AIA E2E <guid> <run>` prefix. The existing student
  inventory, Machine credential and any learner-created resources are reused
  read-only, never overwritten or deleted.
- Cleanup uses only resource IDs returned by this run's create calls and runs
  even on a failed exercise. AAP timeouts cancel only the test's own job. If its
  terminal state is unconfirmed, generated AAP resources are retained and cleanup
  fails rather than deleting credentials/resources under a running job.
- Instructor operations run inside the real instructor developer container,
  from a private temporary clone of its committed `main` branch. Workspace files,
  uncommitted edits and Git remotes are not modified or pushed. The temporary
  clone and student kubeconfig/Vault-password files are removed afterward.
- Reports contain check status/reasons, source revisions, workspace identities,
  generated object IDs and AAP job URLs, not answers, passwords, tokens, cookies,
  job stdout, decrypted Vault data or raw exception messages.

Every completed E2E run ends with a table, counts, overall status, coverage and
evidence location. Failure or a BLOCKED required stage returns nonzero. Disabled
steps are SKIPPED and inspection/exercise report **partial** coverage even when
enabled tests pass. Only a successful `full` run reports
`full-automated-journey`, still subject to the manual boundaries above.

Reports use `/tmp/aia-e2e/<guid>.<run-id>.e2e.json` (or `$TMPDIR`) with mode
`0600` in a `0700` directory. Override `aia_e2e_report_dir` for persistent local
evidence. `aia_e2e_job_timeout` defaults to 600 seconds per AAP job (30–1800
supported). `--check` does not execute E2E and reports SKIPPED.
