# AWS-specific automation — RHDPCD-1137

This repository is an independent source copy of rhpds/ansible-island-adventure.
AWS-only edits must not be pushed to that upstream CNV repository.
The collection namespace is retained because each catalog installs only one implementation.

## Storage and topology

The AWS leaderboard uses a multinode OpenShift 4.21 pool and three
m6a.4xlarge workers. The existing, unmodified core_workloads ODF role runs
before AAP. ODF uses the established stable-4.20 channel for OCP 4.21 and
three 500Gi gp3-csi backing volumes (three-way replication, not 1.5Ti usable).
Hub requests ocs-storagecluster-cephfs with ReadWriteMany. gp3-csi remains
the default for normal RWO claims. There is no Hub single-node pinning.

ODF adds storage cost, memory/CPU overhead and provisioning time. Capacity
for the intended class size still requires a live test; three workers are
not a validated concurrent-student capacity guarantee.

## Student adapter

The AWS AgV item sets the private DNS zone to `<guid>.internal.` (and its
no-trailing-dot counterpart to `<guid>.internal`), overriding the generic
cloud-vms-base `example.com` default. The existing AWS provider then sets the
VM hostname to `rhel.<guid>.internal` during infrastructure setup, before AAP
inventory provisioning. This preserves the custom workload's GUID-bound
hostname and passwordless-sudo checks; those checks must not be disabled.

The AWS AgV item supplies aia_aws_managed_node_host/ip/port/web_url from the
provisioned EC2 inventory. aws_student_access creates a selectorless Service,
Endpoints and an edge-TLS OpenShift Route to the student's EC2 HTTP port.
The Apache challenge is still installed by the learner, not by bootstrap.
student_readiness uses the EC2 SSH endpoint, not CNV NodePorts. Showroom is
explicitly authenticated to the leaderboard, with the token masked in logs.
Showroom content uses this AWS repository on main. The Antora playbook
reads its local content/ directory; the original CNV source remains independent.

## Repository and deployment refs

Public source: https://github.com/rhpds/ansible-island-adventure-aws
The default branch is main. The development catalog selects
RHDPCD-1137-lb1663-aws; publish both refs when updating the initial release.
Public read access avoids embedding Git credentials in AgV.

The local workshop deployment utility pauses new launches (`spec.concurrency: 0`)
when it observes a failed student CI. RHDP otherwise starts replacement instances,
even when failed infrastructure is retained. Already-running jobs continue and
existing claims remain available for diagnosis. Fix the failure before explicitly
restoring concurrency or ordering a fresh student workshop. A replacement already
being created when the failure is observed can still need separate cleanup.

This is a sanitized source snapshot based on upstream commit
565df07cdfcfeb6211f713fb7348d16adacfb223. Assistant settings, hooks and the
original Publishing House registration are excluded from publication.
The original CNV repository and workflow are not modified or re-registered.

## Student CI health check

The corresponding AWS catalog update enables `instructor_config_post_provision`. After initial
account/service bootstrap, `student_post_provision` prepares the personal AAP
inventory and Machine credential, verifies repository branches, re-keys the
instructor vault, writes `ISLAND_ACCESS.md`, loads the 25 real event challenges
and synchronizes their answers without replacing existing challenge IDs or solves.
This is part of the student CI; a manual `post_deploy.yml` run is maintenance,
not a required step for a newly provisioned student.

The organizer key unlocks `roles/student_post_provision/files/flags.vault`, the
same encrypted event data used by the CNV post-deploy playbook. The CNV workflow
ran that playbook locally with `--vault-password-file ../../.vault-password`.
Moving the tasks into a CI also requires delivering that key to the deployer.

The student role first tries the job's Ansible Vault credentials. When those do
not unlock the event, it reads Secret `aia-automation/aia-event-vault`, key
`password`, on the leaderboard. That namespace has no instructor RoleBindings;
instructors retain admin access only to their own lab and CTFd namespaces. The
organizer key is cleared from working facts on success and failure. The role
validates the answers before changing personal repositories or challenge content.
Each instructor's repository vault uses their provisioned lab password.

The deployment utility can seed this Secret from the organizer's private file,
using the leaderboard's published API token and CA. It stores only the file path
and Secret identity in private run state. An existing different key is never
overwritten. To deploy a fresh AWS environment with a serial canary rollout:

```sh
.venv/bin/python utils/dev-environment.py bootstrap --new-leaderboard \
  --leaderboard-catalog babylon-catalog-dev/rh1-2027.lb1663-rh1-ans-island-leaderboard-aws.dev \
  --student-catalog babylon-catalog-dev/rh1-2027.lb1663-rh1-ans-island-student-aws.dev \
  --organizer-vault-password-file .vault-password \
  --workshop --workshop-user-count 4 --workshop-concurrency 1 \
  --keep-on-failure --accept-warnings
```

Use `new-student --leaderboard-file lb-creds.yaml` with the same AWS catalog and
key-file options to reuse a started leaderboard. The key handoff happens before
any student order. For direct RHDP UI ordering, an organizer must first seed the
same protected Secret or arrange the matching RHDP Vault credential. The student
CI completes post-provision configuration without a manual post-deploy run.
Local maintenance playbooks can still use `--vault-password-file`.

Babylon's runner also forwards `governor.spec.vars.vault_credentials` to the Tower
job's Vault credentials in
[`resolveVaultCredentials`](https://github.com/rhpds/babylon-runner/blob/4b9e815e7ed0a3f51e278859b0a86b157987f95d/internal/handler/tower_launch.go#L445).

### Provisioning gates and the image's health checks

The checks shown in `health_checks.png` are defined in
`automation/ansible/healthcheck.yml`,
`automation/ansible/plugins/modules/aia_student_health.py` and
`automation/ansible/plugins/modules/aia_connectivity.py`. Their self-contained Showroom copies live
under `qa-automation/`; tests require the copies to match.

RHDP's Babylon runner marks an item `healthy` when its deployer job succeeds;
that flag is not evidence that these application checks ran. The inspected
AgnosticD v2 path has no generic `health_checks` stage or matching configuration
key. The supported integration points are:

- Student (`cloud-vms-base`): `post_software_final_workloads.localhost` runs
  `ansible_island_adventure.automation.student_health` after post-software
  workloads, Showroom and user-data export. Its native implementation is
  `rhpds/agnosticd-v2:ansible/configs/cloud-vms-base/post_software.yml`.
- Leaderboard (`openshift-workloads`): the final `workloads` entry runs
  `ansible_island_adventure.automation.leaderboard_health` through
  `ansible/roles/openshift_workload_deployer/tasks/run_workload_on_clusters.yml`.
  This config's `post_software.yml` does not run the cloud-vms final-workload list.

Both gates retain reports in the deployment output before failing. The student
gate also publishes ConfigMap `instructor-<guid>-lab/aia-health`; its latest status
is included in RHDP user data. Missing challenges or a vault that the student
cannot unlock fail the full gate even if all network paths work. The gate selects
the leaderboard API and its serving CA explicitly, because the deployer may run
on a different management cluster than the student's Showroom.

The leaderboard gate checks three Ready workers, Available ODF / Ceph HEALTH_OK,
CephFS storage, bound AAP claims including RWX, successful AAP reconciliation,
ready shared deployments/statefulsets, Dev Spaces' two concurrent persistent
workspaces, and shared HTTPS endpoints. Declared/bound RWX storage is not a
cross-node read/write test, and route availability is not a browser/workspace
launch test. Preserve the separate end-to-end acceptance checks below.

Run `qa-automation/healthcheck.yml` with private published student fields, or
select one explicit student hostname from an exported leaderboard inventory.
Each invocation checks exactly one student and rejects multi-student targets. It validates
student service logins, repository ownership, AWS SSH identity/sudo and fresh
AAP/managed-node runtime connectivity without applying configuration. See
[health-check usage and boundaries](qa-automation/README.md).

The student Showroom `/stream/qa/e2e` entry point follows the participant and
instructor journey using only that student's credentials. It defaults to
read-only inspection; real exercises and destructive personal-board reset/update
require explicit GUID confirmations. See [E2E coverage and consent](qa-automation/E2E.md).

## Validation boundaries

Static/unit checks do not establish cloud readiness. Integration testing must
cover ODF health and CephFS RWX across nodes, AAP installation, router access
to EC2, AAP/Dev Spaces SSH, Showroom, storage cleanup and full lab behavior.
The student namespaces/accounts lifecycle needs end-to-end verification.
