# Ansible Island Adventure — CNV Deployer

This documents the legacy local deployer. For the current two-CI deployment flow
and SSO-enabled ordering CLI, see the [root README](../README.md#quick-start).

Deploys the full Ansible Island Adventure CTF environment on a shared
OpenShift CNV cluster provisioned from RHDP.

## Prerequisites

- `oc` CLI (logged out is fine — the playbook handles auth)
- Ansible collections: `kubernetes.core`, `community.general`
- Python packages: `kubernetes`, `passlib`, `pyyaml`
- An RHDP **OCP CNV Multi-Cloud** cluster with `create_multi_user: true`
- An AAP subscription manifest file (`.zip`)

### Install dependencies

```bash
ansible-galaxy collection install -r requirements.yml
pip install kubernetes passlib pyyaml
```

## Setup

### 1. Provision a cluster from RHDP

Order an **OCP4 Cluster (Multi-Cloud)** with:
- `ocp_cloud_provider: cnv`
- `create_multi_user: true`
- `num_users_param: <number of instructors>`
- `worker_instance_count_param: 5` (minimum for 2 instructors; scale as needed)

Copy the ResourceClaim YAML from the catalog item's YAML tab into
`.cnv.env` at the project root (one level up from this directory).

### 2. Create the vault password

The challenge flags are encrypted with `ansible-vault`. Create
`.vault-password` with a random secret:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > .vault-password
```

Then encrypt the flags file:

```bash
ansible-vault encrypt vars/flags.yml --vault-password-file .vault-password
```

> The `ansible.cfg` in this directory auto-loads `.vault-password` so
> you don't need `--vault-password-file` on every run.

A copy of the password is also kept in `.env` for reference. Both files
are gitignored.

### 3. Set the manifest path

Edit `vars/main.yml` and set `aap_manifest_path` to the location of
your AAP subscription manifest:

```yaml
aap_manifest_path: "/path/to/manifest.zip"
```

## Deploy

### Full deploy (all instructors)

```bash
ansible-playbook deploy.yml
```

This uses every `userN` from the CNV cluster as an instructor.

### Deploy for specific instructors

```bash
ansible-playbook deploy.yml -e '{"instructor_users": ["user1", "user2"]}'
```

### What it does

| Phase | Description |
|-------|-------------|
| **0 — Cluster setup** | Installs AAP 2.7 operator, Dev Spaces operator, deploys shared AAP platform (Gateway + Controller), uploads subscription manifest, creates CheCluster |
| **0 — Shared Gitea** | Deploys Gitea + PostgreSQL in the `gitea` namespace |
| **1 — Per-instructor infra** | Creates `userN-ctfd` and `userN-lab` namespaces, RBAC, MariaDB + Redis + CTFd + Verifier, managed node pods, AAP org + instructor user |
| **2 — Gitea config** | Creates student Gitea users, forks `ansible-survival-kit` repos |
| **2 — CTFd config** | Builds per-instructor CTFd archives (challenges, flags, users), runs setup, imports via `/admin/import` |
| **2 — AAP config** | Creates Workshop Inventory with student managed nodes, Machine Credential, student AAP users |
| **3 — Handouts** | Outputs per-instructor URLs + credentials, publishes via `set_stats` for Showroom |

### Idempotent re-runs

The playbook is designed to be re-run safely. Existing resources are
updated in place, CTFd import is skipped if challenges already exist,
and API calls that return 400/409 (duplicate) are treated as success.

> **Note:** Student passwords are regenerated on each run. If you need
> stable passwords across runs, save the credentials from the first run.

## Architecture

```
Cluster-wide:
├── aap              (ns) → AnsibleAutomationPlatform CR (Gateway + Controller)
├── openshift-devspaces   → CheCluster (shared IDE)
└── gitea            (ns) → Gitea + PostgreSQL (shared Git hosting)

Per instructor (userN):
├── userN-ctfd       (ns) → MariaDB, Redis, CTFd, Verifier
└── userN-lab        (ns) → 2 × managed node pods (UBI + SSHD)

AAP (shared):
└── Organization per instructor → Inventory, Credentials, Student users
```

## Configuration

All configurable values are in `vars/main.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `students_per_instructor` | `2` | Number of students per instructor |
| `instructor_users` | `[]` (all) | Limit to specific cluster users |
| `aap_channel` | `stable-2.7` | AAP operator channel |
| `aap_manifest_path` | `/home/matt/manifests/manifest.zip` | Subscription manifest |
| `ctfd_admin_password` | `supersecret123!` | CTFd admin password |
| `gitea_admin_password` | `supersecret123!` | Gitea admin password |
| `verifier_image` | `quay.io/l3acon/ctf-verifier:latest` | Verifier container image |

## Vault-encrypted flags

Challenge flag values live in `vars/flags.yml` (encrypted). To edit:

```bash
ansible-vault edit vars/flags.yml
```

Challenge definitions (descriptions, hints, categories) are in
`vars/challenges.yml` (plaintext) and reference flags via
`{{ ctfd_flags.<key> }}`.

## Output

After deploy, each instructor's personalized environment info is:
- Printed to the console
- Published via `ansible.builtin.set_stats` under the key
  `island_adventure` for consumption by Showroom or other lab frameworks

## Project structure

```
deploy/
├── deploy.yml                              Main playbook
├── ansible.cfg                             Auto-loads vault password
├── requirements.yml                        Ansible collection deps
├── vars/
│   ├── main.yml                            Configuration defaults
│   ├── flags.yml                           Challenge flags (vault-encrypted)
│   └── challenges.yml                      Challenge definitions
├── roles/
│   ├── cluster_setup/                      AAP + Dev Spaces operators, shared AAP instance
│   ├── shared_gitea/                       Gitea + PostgreSQL deployment
│   ├── instructor/                         Per-instructor namespaces, CTFd, managed nodes, AAP org
│   ├── configure_gitea/                    Student users + repo forks
│   ├── configure_ctfd/                     CTFd archive build + import
│   └── configure_aap/                      Inventory, credentials, student users
├── .vault-password                         Vault password (gitignored)
└── .env                                    Vault password reference (gitignored)
```
