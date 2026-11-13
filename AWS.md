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

This is a sanitized source snapshot based on upstream commit
565df07cdfcfeb6211f713fb7348d16adacfb223. Assistant settings, hooks and the
original Publishing House registration are excluded from publication.
The original CNV repository and workflow are not modified or re-registered.

## Validation boundaries

Static/unit checks do not establish cloud readiness. Integration testing must
cover ODF health and CephFS RWX across nodes, AAP installation, router access
to EC2, AAP/Dev Spaces SSH, Showroom, storage cleanup and full lab behavior.
The student namespaces/accounts lifecycle needs end-to-end verification.
