# AWS student CI health check

The **leaderboard CI** owns shared OpenShift, AAP, Gitea, Keycloak and Dev
Spaces services. Each **student CI** owns its accounts, CTFd and EC2 managed
node. This playbook checks **exactly one selected student** per run; it does
not fan out across the leader's other students.

This entry point replaces the old CNV `.cnv.env`/first-instructor check. It
does not configure services, load challenges, edit repositories or install
Apache. It creates AAP ad hoc command audit records to measure real runtime
connectivity, and writes local private evidence. It does not overwrite the
provisioning readiness ConfigMap or trust a previously published `passed` value.

## Requirements

Run from the repository root with Ansible Core, `requests`, `PyYAML` and
`paramiko` installed in the Python interpreter used by Ansible. Use the same
virtual environment for inventory export and playbook execution. No event vault
or leader admin token is needed for these student-level probes. If the root
`ansible.cfg` references an unavailable vault file, override it with
`ANSIBLE_VAULT_PASSWORD_FILE=/path/to/your/vault-password-file` (the check never
loads encrypted event content).

## Run from the student's Showroom

Open the student's `/stream/qa/healthcheck` URL. The entry point automatically
loads that Showroom's private `/user_data/user_data.yml` mount when CLI student
fields were not supplied. It checks only that student; no credentials are stored
in Git. After publishing a new revision, restart only that student's Showroom
pod so its content checkout reloads. Pod restart requires permission in the
student's `showroom-<guid>` namespace.

The runner mounts only `qa-automation/`, not the full repository. Its local
`student_healthcheck.yml` and `library/` module copies make this directory
self-contained. When changing the corresponding collection playbook or modules,
refresh these copies as well; `test_student_health.py` checks byte-for-byte
consistency and verifies execution from an isolated QA directory.

## One student: published provision fields

Save the student's fields as a **private YAML mapping** outside Git, for example
`/private/student.yml`, with mode `0600`. Use valid YAML such as
`bastion_ssh_port: '22'`, not the UI's `! "22"` rendering. Do not put passwords
in command-line arguments. The fields required are:

- `guid`, `openshift_cluster_ingress_domain` (or `apps_domain`)
- `aap_url`, `aap_username`, `aap_password`
- `gitea_url`, `gitea_username`, `gitea_password`
- `ctfd_url`, `ctfd_admin_username`, `ctfd_admin_password`,
  `ctfd_student_username`, `ctfd_student_password`
- `devspaces_url`, `keycloak_url`, `keycloak_username`, `keycloak_realm`
- `bastion_public_hostname`, `bastion_ssh_port`, `bastion_ssh_user_name`,
  `bastion_ssh_password`
- Optional: `managed_node_web_url`

```bash
ansible-playbook -i localhost, qa-automation/healthcheck.yml \
  -e @/private/student.yml \
  -e ansible_python_interpreter="$(command -v python3)"
```

## Select one student from a leader's inventory

If you already use the development run exporter, select one explicit inventory
hostname. The exporter refreshes the leader and all student claims, but the
health check probes **only the selected student**. Group names and multi-host
patterns are rejected. Do not combine different leaders in one inventory.

```bash
python utils/dev-environment.py export-students --run <run-id>
ansible-playbook \
  -i .aia-dev/runs/<run-id>.students.inventory.json \
  qa-automation/healthcheck.yml \
  -e aia_healthcheck_hosts=instructor-<guid>
```

Without an explicit hostname, the target defaults to `localhost` and requires
the selected student's private provision fields. Missing inputs or invalid
targets fail rather than establish health.

## Checks and evidence

- Gitea authenticated username and ownership/access to `ansible-survival-kit`
  and `aia-instructor` repositories.
- Separate CTFd student/admin logins and authenticated challenge API. Empty
  challenge content is valid before the separate event-content load.
- Dev Spaces route and Keycloak realm discovery. These are **not** proof of a
  browser SSO login or of starting two persistent workspaces. Direct-password
  OAuth grants are deliberately not required.
- EC2 password SSH, exact AWS hostname `rhel.<guid>.internal`, passwordless sudo.
  Like provisioning, the first SSH connection accepts the catalog endpoint's
  host key; no global known-hosts files are changed.
- Fresh control-host, AAP execution-environment and managed-node HTTPS/network
  probes, plus AAP Ansible `ping` to the student's `rhel` inventory host.
  AAP selection checks the student's organization and published SSH endpoint.
- The optional treasure route can return HTTP 503 before the learner installs
  Apache; shared-service 503 fails. `verifier_url: not.available.com` is not a
  real service and is not checked.

A nonzero exit means at least one required check failed. Each run writes
`.aia-healthcheck/<guid>.<sample-id>.health.json` with mode `0600` in a `0700`
directory. Reports contain check status, timestamps and fresh runtime evidence
(including AAP job URLs), not passwords, tokens, cookies or response bodies.
Input/probe tasks suppress credential logging. Do not enable Ansible debug
logging or third-party callbacks that dump task variables for private inventories.

Override `aia_healthcheck_report_dir`, `aia_readiness_connect_timeout` (default
3 seconds per address) or `aia_readiness_job_timeout` (default 300 seconds per
AAP job) as needed. TLS verification defaults to enabled. `--check` skips actual
probes and therefore does **not** establish health.

This does not certify leader ODF/CephFS health, cross-node RWX, workspace launch,
Showroom, teardown, or the full learner workflow. Those require separate leader
and end-to-end acceptance checks. See [AWS.md](../AWS.md).
