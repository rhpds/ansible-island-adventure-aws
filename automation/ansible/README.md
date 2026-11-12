# ansible_island_adventure.automation — Ansible Island Adventure Collection

This collection contains automation for deploying and configuring the Ansible Island Adventure CTF platform on OpenShift CNV.

## Roles

Catalog workload roles live in this directory:

- `roles/instructor_config/` — Instructor services and access, including CTFd bootstrap, Gitea, AAP, and Keycloak integration
- `roles/keycloak_ocp_logout_fix/` — Keycloak/OpenShift logout configuration
- `roles/configure_devspaces/` — Read-only validation of the leaderboard CI's workspace policy, plus standalone repair for older deployments
- `roles/validate_connectivity/` — Runtime connectivity gates and persistent evidence for each instructor
- `roles/student_readiness/` — Catalog adapter for managed-node AAP access and connectivity validation

The project root also retains `ansible-testing/` playbooks and the
`collections/ansible_collections/rhpds/aia/` collection for local role development.
The collection's `deploy_vms` role is currently a stub.

## Usage

Order the leaderboard CI first, wait for deployment, then order the student CI
using the leaderboard's five provision fields. Workload deployment runs through
RHDP provisioning. See the [root README](../../README.md#quick-start) for manual
ordering and the SSO-enabled `utils/dev-environment.py` CLI, which reuses the
leaderboard for subsequent student deployments.

Catalog provisioning deploys CTFd, waits for health, initializes the admin and
student accounts, completes setup, and publishes its URL and credentials. New
instances have an empty challenge board and a landing page linking to login.
Challenge definitions, archive generation/import, and challenge-count checks
are excluded from the default role path. Reprovisioning preserves existing
challenge data and event settings. The bootstrap command has its own timeout
and retry settings: `ctfd_bootstrap_timeout` (120 seconds),
`ctfd_bootstrap_retries` (2), and `ctfd_bootstrap_delay` (10 seconds).

Catalog maintainers can enable `instructor_config_validate_connectivity: true`
after the instructor services bootstrap. This configures the personal AAP
inventory and Machine credential, then runs the same connectivity gate as
post-deploy. The adapter uses the cloud provider's published SSH hostname and
actual bastion NodePort. It publishes the readiness status and evidence location
through AgnosticD user data before failing an unhealthy deployment. The option
defaults to false for existing catalog consumers; post-deploy enables its own
readiness gate by default.

Modules used by catalog roles live in `plugins/modules/`. The `library/` links
retain local playbook compatibility without maintaining separate implementations.

Load content after the student CI succeeds, either from your event playbook or
with the standalone entry point (starting at the repository root):

```bash
cd automation/ansible
ansible-playbook configure_ctfd.yml \
  -e @/private/student-vars.yml -e @/private/event-challenges.yml \
  -e ansible_python_interpreter=/usr/bin/python3
```

Install `ansible-core`, the `kubernetes.core` and `community.general`
collections, Python `kubernetes`, `requests`, `paramiko`, and `PyYAML`, and GNU
coreutils `timeout` on the control host. The playbook prepares `oc` using the role's existing CLI
installation/login tasks; the automatic installation requires sudo if `oc`
is missing. The interpreter override selects your local Python instead of
the role's provisioning-image default `/usr/bin/python3.11`.
Running from this directory also avoids the root `ansible.cfg`, which expects
the legacy `.vault-password` file. If your event vars are encrypted, pass your
vault password file explicitly with `--vault-password-file`.

The private student vars file supplies `guid` (the student's deployed GUID),
`openshift_api_server_url`, `openshift_cluster_ingress_domain`, and
`openshift_cluster_common_password`, matching the catalog workload inputs.
Use the GUID from `status.resourceHandle.name` (`guid-<guid>`) in a ResourceClaim.
The role derives the namespace as `instructor-<guid>-ctfd` unless you override
`instructor`. The event file supplies `ctfd_challenges` and optionally
`ctfd_challenges_advanced`, with real flags. Without an override, the loader uses
the bundled definitions, whose flags are `CHANGEME` placeholders.

To integrate with an existing post-deployment playbook, include
`instructor_config` with `tasks_from: ctfd_config` after authenticating `oc`;
use `ansible_island_adventure.automation.instructor_config` when installed as
a collection. This task entry point does not rerun Gitea, Keycloak, AAP, or
infrastructure deployment and does not require AgnosticD publication plugins.
The retained archive loader only imports into an empty challenge database; it
does not update challenges or reset scores on an existing populated instance.

The post-deployment challenge-count check allows 120 seconds per attempt and
two retries, with a 10-second delay between attempts. Override
`ctfd_challenge_check_timeout`, `ctfd_challenge_check_retries`, and
`ctfd_challenge_check_delay` when needed. Failed commands and missing or malformed
counts stop content loading before archive import; their command diagnostics are
retained in the Ansible output.

The check uses GNU coreutils `timeout` on the control host
instead of Ansible async polling. This avoids missing async result files when
the launch and polling processes resolve their job directories differently.
On timeout, it preserves partial output and reports the termination on stderr;
commands that ignore termination are killed after another five seconds.

## Configure deployed students

The deployment sequence has three responsibilities: the leaderboard CI creates
the shared cluster and Dev Spaces service; the student CI integrates with that
leaderboard and creates the requested student accounts and resources; this
post-deploy playbook applies iterated configuration to those existing resources.
The leaderboard CI owns the shared Dev Spaces workspace and storage policy.

`post_deploy.yml` connects each already deployed student CI's CTFd instance and
Gitea account. It uses the real definitions in
[`ansible-testing/vars/challenges.yml`](../../ansible-testing/vars/challenges.yml)
and vault-encrypted answers in
[`ansible-testing/vars/flags.yml`](../../ansible-testing/vars/flags.yml).
The flags were aligned with the instructor toolkit at
[`d05745d`](https://github.com/l3acon/aia-instructor/tree/d05745d1c726f12bf460aaebb5126b7245957016),
including corrected YAML quoting of regex answers and keys matched by challenge
name. The repository-name and main-branch Hello World answers were also corrected
to match the current student Gitea content. The platform and controller answers
were checked against the deployed gateway and controller ping APIs. Recheck
version-based answers when changing AAP or the workspace image.
The vault password is supplied
separately; `.vault-password` is gitignored.

After validating all student mappings and flags, post-deploy checks Dev
Spaces once for the shared leaderboard cluster, before its serial student
configuration. The leaderboard CI must supply at least two saved and running
workspaces per instructor, with
`per-workspace` persistent storage. Separate PVCs prevent the cross-node
`Multi-Attach` failures caused by sharing a ReadWriteOnce per-user volume.
The exported inventory represents one leaderboard; do not combine students
from different leaderboard clusters into it.

The provisioning feature merged in [core_workloads#197](https://github.com/rhpds/core_workloads/pull/197)
and [agnosticv#28869](https://github.com/rhpds/agnosticv/pull/28869).
`post_deploy.yml` imports the read-only `validate_devspaces.yml`; it never patches
the CheCluster. A stale catalog or collection version fails validation before
student service changes. The former `devspaces_ci_workaround` skip tag is no
longer needed. Keep this cluster-wide policy in the leaderboard CI when moving
student configuration into provisioning.

For a fresh-CI acceptance check, export the inventory and run
`validate_devspaces.yml` before the full post-deploy playbook. Then confirm that
one instructor can run two simultaneous workspaces with separate bound PVCs.

From the repository root, using the development virtual environment:

```bash
.venv/bin/python -m pip install kubernetes
ansible-galaxy collection install -p ~/.ansible/collections kubernetes.core community.general
.venv/bin/python utils/dev-environment.py export-students --run <run-id>

cd automation/ansible
ansible-playbook -i ../../.aia-dev/runs/<run-id>.students.inventory.json \
  post_deploy.yml --vault-password-file ../../.vault-password
```

The exporter refreshes saved claim identities from the catalog, requires the
leaderboard and every student to be started and healthy, and validates the GUID,
CTFd URL, and Gitea owner before writing an inventory with mode `0600` under
`.aia-dev/`. It exports the actual published credentials, including different
CTFd and Gitea usernames, and records the Python interpreter used for export.
It places no orders. The generated kubeconfig is also kept under `.aia-dev/`.
Post-deploy reuses that token/CA kubeconfig and checks access with `oc whoami`;
it does not perform another password login or replace the exported identity.
Catalog and legacy invocations without `aia_kubeconfig` retain their password
login. The role uses the `oc` binary found on the caller's path.

### Runtime readiness and repeatable network evidence

Post-deploy measures connectivity in an `always` block, including when a processed
student's configuration fails. Connectivity failures are asserted after the serial
loop, so one bad runtime path does not prevent sampling the remaining students.
The playbook returns nonzero if any required runtime path fails. It tests:

- Control machine to shared HTTPS endpoints and every advertised VM SSH address.
- AAP's Default execution environment to those endpoints, using a personal ad hoc
  command against the inventory's controller-local `localhost` host.
- An actual Ansible `ping` from AAP to `rhel`, using the personal Machine
  credential. If the runtime TCP probe finds no reachable SSH address, the ping
  is marked blocked instead of waiting for every SSH connection timeout.
- The managed VM back to Gitea, AAP, CTFd, and Dev Spaces over HTTPS.

Reports include timestamps, the source hostname, resolved addresses, per-address
results, TLS and HTTP status, and AAP command URLs. They do not contain passwords,
tokens, challenge flags, or HTTP response bodies. An undeployed treasure page's
HTTP 503 is permitted after successful TCP/TLS negotiation; a shared service's
HTTP 503 still fails readiness. HTTP response headers have a minimum 10-second
deadline, allowing the router's backend retries before an expected 503, while
TCP/TLS connections retain the configured shorter deadline. A partial DNS
address failure fails the default
strict gate even if another address works. Certificate validation remains enabled.

Every sample writes a distinct private JSON file next to the exported kubeconfig
(`run-id.instructor-guid.sample-id.readiness.json`). Catalog runs use their
`output_dir`. Both paths publish the latest report as ConfigMap `aia-readiness`
in the instructor's lab namespace before asserting success. The instructor can
retrieve that evidence using their existing OpenShift login:

```bash
oc get configmap aia-readiness -n instructor-<guid>-lab \
  -o jsonpath='{.data.report\.json}'
```

Repeat the probes without rerunning repository configuration, importing content,
or loading the event vault:

```bash
cd automation/ansible
ansible-playbook -i ../../.aia-dev/runs/<run-id>.students.inventory.json \
  validate_connectivity.yml
```

Override `aia_readiness_connect_timeout` (3 seconds per address),
`aia_readiness_job_timeout` (300 seconds per AAP command), or
`aia_readiness_report_dir` when needed. `aia_readiness_require_all_addresses`
defaults to true. `aia_readiness_enabled: false` explicitly skips this gate;
skipping it does not establish connectivity readiness. Ansible check mode also
skips probes because they launch AAP command records. Probe jobs remain as audit
evidence; only a probe's own command is canceled if it exceeds its deadline.

The AAP host's `localhost` interpreter follows `ansible_playbook_python` so the
gate uses the selected execution environment's Python. Control-machine SSH
discovery tries alternative advertised addresses within a 30-second connect
budget, with at most 3 seconds per address. This handles a bad first DNS address;
it cannot repair a network path where every destination address is unreachable.

To validate Dev Spaces alone, use the same exported inventory:

```bash
cd automation/ansible
ansible-playbook -i ../../.aia-dev/runs/<run-id>.students.inventory.json \
  validate_devspaces.yml
```

This entry point needs no event vault and does not change any resources.
For an older leaderboard, explicitly run `configure_devspaces.yml` with the
same inventory to repair its workspace policy, then rerun validation.
The repair entry point patches the existing CheCluster without recreating it.
It assumes the saved-workspace limit already accommodates two workspaces.

The storage default applies to newly created workspaces. Existing per-user
workspaces and their PVCs are left intact; save or back up uncommitted work and
recreate those workspaces with per-workspace storage when ready. Do not simply
change their storage attribute and assume files have moved. Each new workspace
has separate storage, including its own repository checkout and local files.

All students' event flags are resolved and checked before service changes.
Use `--tags preflight` to run just this validation. Override
`aia_event_flags_file` with an absolute path for a different event vault.
The default load includes 25 base challenges. Set `aia_enable_advanced=true`
only after deploying a verifier and supplying a valid `aia_student.verifier_url`;
the provisioned placeholder URL is not a verifier integration.

For each student, the playbook authenticates as their existing Gitea user,
checks or repairs missing forks, and verifies `main` and `treasurehunt` on
`ansible-survival-kit` plus `main` on `aia-instructor`. It creates or updates
only `aia-instructor/ISLAND_ACCESS.md`, with service links and account names.
Existing learner work is preserved. No passwords or answers are written to
Gitea. An unchanged guide produces no Git commit on rerun.

Before repository configuration, the playbook connects to each VM using the
student CI's published SSH endpoint, reads `hostname -f`, verifies the GUID and
passwordless sudo, then configures AAP using that instructor's own account:

- Inventory **Island Adventure**, scoped to `instructor-<guid>`'s organization.
- Host `rhel`, using the published SSH hostname/port; its private VM FQDN is
  recorded as `aia_managed_node_fqdn`. The host belongs to group `web`.
- Host `localhost` with local connection for Hello World/Secret Message.
- Machine credential **Managed Node SSH**, storing the published SSH password
  in AAP, with sudo/root escalation. No password is put in host variables.

Other inventories, projects, credentials, and templates are retained. Existing
managed Machine credential secrets are not rewritten on rerun; explicitly set
`aia_rotate_machine_credential=true` after a published SSH password rotation.
Module 4 selects this inventory and Machine credential for the Apache template.
The student CI creates a per-instructor `treasure` HTTPS Route in the VM's
hosting cluster, targeting the existing `bastion` Service's `bastion-80` port.
It publishes `managed_node_web_url` before Showroom is deployed. The exporter
validates that URL against the student GUID and includes it in the access guide.
Module 4 links to the VM's real Apache page after the learner's job succeeds.
The Route and Service survive VM stop/start; the namespace is removed with the
student CI. No proxy pod or SSH credential is needed for the web endpoint.

Older CIs without `managed_node_web_url` keep the documented SSH/curl fallback.
The shared leaderboard does not create this Route: each student VM runs in its
own RHDP hosting cluster and sandbox namespace.

The instructor answer vault in each Gitea fork is re-keyed in memory with
`aia_lab_password` (the CI's published `password` field). The filter first tries
that key and returns existing ciphertext unchanged when it already works. On
initial setup it uses `aia_instructor_vault_source_password`, defaulting to a
private file selected by `aia_instructor_vault_password_file` (repository-root
`.vault-password`). Supply that secret explicitly from RHDP's private mechanism
when integrating into a CI. Only the existing file's ciphertext is committed,
with its Git SHA as a concurrency guard; student edits/answers are retained.
No decrypted answer file or workspace password file is written. The organizer's
source vault and survival-kit challenge vaults retain their original keys.

For access-only updates that leave CTFd accounts/content/scores untouched, run:

```bash
ansible-playbook -i ../../.aia-dev/runs/<run-id>.students.inventory.json \
  post_deploy.yml --tags student_access --vault-password-file ../../.vault-password
```

The regular event preflight still runs. Use `--tags aap` for just the AAP step.

The playbook adds the student's repository link to relevant CTFd challenge
descriptions and invokes the retained empty-board content loader. It then
reconciles flag values through CTFd's API, patching existing flag IDs only when
values differ. The flag step plans all changes first and rejects missing or
ambiguous challenges and unexpected flag counts. It does not delete objects.
Use `--tags flags` for a flag-only update; preflight still runs. Populated
boards are preserved, including submissions and scores. Existing challenge
descriptions, points, hints, and prerequisites are not updated in place by this
entry point. It keeps the existing
`lab-user` CTFd account and `instructor-<guid>` Gitea account, and presents their
mapping in the access guide. It does not create AAP projects or job templates. Managed-node inventories and
Machine credentials are configured as described below.

## Instructor playbooks without credential exports

The upstream `l3acon/aia-instructor` root playbooks use the instructor's current
`oc login` and their provisioned lab password in a local vault password file.
They derive `instructor-<guid>-ctfd` from
`oc whoami`, validate its `island` route, and run the CTFd 3.7.7 content runner
inside `deploy/ctfd` using namespaced `pods/exec` access. `namespaces.yml` already
binds the instructor to the `admin` ClusterRole in that personal namespace, so
this workflow needs no new cluster permissions, credential exports, or access
Secret. The shared Gitea namespace remains restricted.

`update-challenges.yml` preserves IDs/solves and unchanged hint purchases;
`deploy.yml` clears personal challenge progress while retaining accounts,
passwords, branding, and settings. Both retain challenges absent from YAML;
hide unwanted challenges explicitly. `--check` rolls back the content transaction.
The handout generator lists service links and existing usernames; distribute
the provisioned lab password separately. That same password unlocks the
instructor clone's re-keyed answer vault. No random player accounts are created.

Organizer post-deploy still uses the private exported inventory and event vault.
Those control-host inputs are not learner inputs and must not be distributed.
Post-deploy initializes the board and generates an access guide explaining the
two instructor inputs. It does not set a workspace vault password.

## Moving post-deploy into the student CI

The current student CI installs this directory as the
`ansible_island_adventure.automation` collection and invokes
`instructor_config` in `post_software_workloads`. That role still bootstraps an
empty CTFd board. Merging the Dev Spaces feature does not enable challenge
loading in the student CI; keep running post-deploy during this test phase.

Before enabling that integration:

- Adapt the per-student inputs from provisioned workload variables, rather than
  running the RHDP catalog exporter inside the CI. Validate GUID, repository
  owner, service URLs, and the distinct CTFd/Gitea usernames as post-deploy does.
- Run student content configuration after CTFd, Gitea forks, accounts, and
  service credentials are ready. Reuse the collection roles and flag module;
  keep the leaderboard's workspace policy in leaderboard provisioning.
- Supply real event flags through RHDP's private secret mechanism or an
  explicitly unlocked event vault. A collection install does not include the
  root `ansible-testing/vars` files used by this local playbook. Package the
  challenge definitions and make the flag input explicit before moving it.
  Never publish the organizer's source vault key or decrypted answers to Gitea
  or Showroom. Student Gitea answer vaults use the published lab password.
- Include `oc`, `timeout`, the Python/Ansible dependencies above, and the
  required Kubernetes access in the execution environment. Use collection
  qualified role and module names once installed.
- Test initial provisioning, rerun, and failure recovery: 25 challenges with
  real flags, usable student/admin logins, correct repository links and
  devfiles, version answers that match the deployed AAP and workspace tools,
  and preserved learner commits, solves, scores, and account access.

For the main-event review, walk through the deployed Showroom Modules 03–05
using a student's published credentials, start both repository workspaces,
verify the Ansible utilities, and complete representative base and treasure-hunt
challenges before enabling the CI integration.

Run the offline CTFd regression tests with `ansible-playbook`, GNU coreutils
`timeout`, PyYAML, and Jinja2 installed:

```bash
python3 -m unittest discover -s automation/ansible/tests -v
```

For legacy local playbook development, the following commands remain available;
they require the older `.cnv.env` layout and local Ansible dependencies:

```bash
cd ansible-testing
ansible-playbook deploy.yml --vault-password-file ../.vault-password
```

For tag-scoped updates (e.g., CTFd challenge changes only):

```bash
ansible-playbook deploy.yml --tags ctfd --vault-password-file ../.vault-password
```
