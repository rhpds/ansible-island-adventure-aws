# RHDP setup for AIA organizer event credentials

The AWS Leaderboard catalog item needs to provision the organizer event key so
subsequent Student items can finish automatically. Previously, the organizer's
local post-deploy command supplied `.vault-password`; moving those tasks into
RHDP also requires delivering that credential to the deployer.

The requested maintainer action is to create a named Vault credential on the
RHDP deployer controllers and enable the catalog hook described below. The key
itself must be transferred through the approved private channel. It is not a
catalog form input or a value to add to a GitHub PR.

## Responsibilities and deployment flow

| Owner | Change |
| --- | --- |
| AIA repository maintainers | Ship `leaderboard_event_credentials`, its create-only Secret module, and the credential health gate. |
| RHDP platform maintainers | Register the organizer Vault credential and grant the deployer identity permission to use it on every eligible controller. |
| AgnosticV maintainer | Merge the AWS Leaderboard catalog hook after the collection revision and private credential are available. |

The flow is RHDP Vault credential → Leaderboard deployer → protected OpenShift
Secret → Student deployer → event content and personal instructor vaults.
Instructors continue to use their individual provisioned lab passwords.

## Register the private credential before enabling the catalog hook

On every controller eligible to run the AWS Leaderboard workload job, create:

| Setting | Value |
| --- | --- |
| Credential name | `aia-event` |
| Credential type | Ansible Vault / Vault |
| Organization | `RHPDS`, consistent with the event deployer configuration |
| Vault password | The organizer-supplied key matching the packaged `roles/student_post_provision/files/flags.vault` |
| Vault identifier | Leave blank for the packaged Ansible Vault 1.1 file's default identity. Avoid a second conflicting default-identity credential on the same job. |
| Prompt on launch | Disabled; unattended provisioning must receive the credential. |
| Access | The RHDP identity launching jobs must have permission to use the credential. |

The current event controller configuration selects randomly between
`event0.apps.ocpv-infra01.dal12.infra.demo.redhat.com` and
`event1.apps.ocpv-infra02.wdc07.infra.demo.redhat.com`. Confirm the active
scheduler/controller configuration and provision the credential on every
eligible backend, including future additions. Keep its name unique among the
credentials visible to the launching identity; the runner resolves it by name.

These are RHDP's infrastructure deployer controllers. The AAP instance installed
inside the Leaderboard cluster is the students' lab application and is not the
place to register this platform credential.

If an existing private RHDP mechanism supplies the matching Vault credential to
these jobs already, maintainers may use that mechanism instead. Confirm an actual
Leaderboard job receives it; credentials attached to a parent/base-cluster job
do not automatically establish the handoff to Student jobs.

## Apply the catalog integration

The integration is confined to
`rh1-2027/lb1663-rh1-ans-island-leaderboard-aws/common.yaml` in `rhpds/agnosticv`:

```yaml
__meta__:
  deployer:
    credentials:
      - aia-event

aia_leaderboard_require_event_credentials: true

workloads:
  # Existing workloads remain in their existing order.
  - ansible_island_adventure.automation.keycloak_ocp_logout_fix
  - ansible_island_adventure.automation.leaderboard_event_credentials
  - ansible_island_adventure.automation.leaderboard_health
```

Merge these entries into the existing mappings/lists. Retain any other required
deployer credentials. The catalog PR pins `aia_aws_revision` to the reviewed
collection commit containing these roles so the new hook cannot run against an
older collection. Update that pin deliberately for future collection releases.
No change to the AIA repository's top-level `catalog-info.yaml` is needed.

The implementation uses the existing runner path:
[named deployer credentials](https://github.com/rhpds/babylon-runner/blob/4b9e815e7ed0a3f51e278859b0a86b157987f95d/internal/handler/tower_launch.go#L400)
are looked up and associated with job templates in
[the Tower client](https://github.com/rhpds/babylon-runner/blob/4b9e815e7ed0a3f51e278859b0a86b157987f95d/internal/clients/tower.go#L530).
Maintainers should confirm their deployed runner/governor supports that setting
before enabling the hook. No shared runner or core workload change is required
by this implementation.

## Bootstrap and health behavior

The bootstrap role first unlocks and validates the packaged event using the
job's Vault credentials. It checks instructor authorization before writing a
key, then creates Namespace `aia-automation` and Secret
`aia-event-vault`, key `password`. Its Secret module uses create-only API calls;
it never patches or replaces an existing Secret, including during concurrent
creation. A matching existing key is accepted; a missing, malformed or different
key in an existing Secret fails provisioning.

The credential check then independently reads the stored Secret, confirms it
unlocks the packaged event, and submits SubjectAccessReviews requiring denial
for an instructor identity with the standard authenticated OpenShift groups.
Reviews cover direct reads plus listing and watching Secrets, both unrestricted
and restricted to the key's resource name. All must be denied.
`aia_event_vault_probe_users` can include additional real instructor usernames;
`aia_event_vault_probe_groups` defines the groups evaluated by those reviews.
Include any additional instructor groups if the authentication policy changes.
An authorization evaluation error fails verification.

The catalog enables a fifteenth leaderboard health check, `event_credentials`.
The existing fourteen storage, workload and service checks remain in place.
RHDP user data publishes only the measured credential status,
`aia_event_credentials_status: passed` or `failed`, alongside aggregate health.
The key, its encoded form and decrypted
answers are hidden from task output and diffs, cleared from working facts, and
excluded from published user data. The namespace receives no instructor
RoleBindings from this role.

Bootstrap is skipped during destruction and check mode. Lifecycle stop/start
does not rotate the key. Deleting the Leaderboard cluster removes its Secret.
The Student role already consumes this Secret automatically when its own job
Vault credentials do not unlock the event; no Student catalog change is needed.

## Acceptance checks

1. Make the collection revision available and register the private credential
   before merging/enabling the catalog hook.
2. Order a fresh AWS Leaderboard through the ordinary RHDP UI. Do not use the
   local `--organizer-vault-password-file` helper or seed a Secret manually.
3. Require native job success, `aia_event_credentials_status: passed`, and
   `aia_leaderboard_health_status: passed`. The retained health report must have
   `event_credentials.status: passed` as well as the existing checks.
4. Order at least two Student items against that leaderboard. Each must publish
   `aia_post_provision_status: complete`, `aia_event_challenge_count: 25`, and
   `aia_health_status: passed`, including real AAP execution and SSH/return-path
   checks. No manual post-deploy playbook should be necessary.
5. Check a real instructor account after provisioning. As an organizer with an
   explicit leaderboard kubeconfig, run the following. Expect `no` for all five
   checks; each denial returns exit 1:

   ```sh
   for verb in get list watch; do
     oc --kubeconfig "$LB_KUBECONFIG" auth can-i "$verb" secret/aia-event-vault \
       --namespace aia-automation --as="instructor-${STUDENT_GUID}" \
       --as-group=system:authenticated --as-group=system:authenticated:oauth
   done
   for verb in list watch; do
     oc --kubeconfig "$LB_KUBECONFIG" auth can-i "$verb" secrets \
       --namespace aia-automation --as="instructor-${STUDENT_GUID}" \
       --as-group=system:authenticated --as-group=system:authenticated:oauth
   done
   ```

6. Re-run bootstrap/verification with the same credential: the Secret UID and
   key must remain unchanged. Verify destruction skips both credential roles
   even when the event Vault credential is unavailable.

The collection tests cover missing job credentials before API mutation,
incorrect existing keys, concurrent creation with conflicting keys, instructor
access rejection, key redaction/cleanup, and lifecycle guards. They also check
that credential failure makes the aggregate leaderboard health report fail.

## Troubleshooting and rotation

| Symptom | Maintainer check |
| --- | --- |
| Bootstrap cannot unlock the event | Check the credential attachment, key, Vault identifier and matching policy. A named credential working on one controller does not prove the other controller has it. |
| Existing key is rejected | Compare the intended event/key versions privately. The role deliberately does not overwrite an inconsistent existing Secret. |
| Instructor isolation fails | Inspect the SubjectAccessReview and namespace/cluster RBAC. Do not bypass the health gate. |
| Student cannot find the Secret | Confirm it targets the intended Leaderboard API and that this Leaderboard completed credential bootstrap. |

Rotation requires a coordinated update to the packaged event vault, the RHDP
credential, the stored cluster Secret, and any upstream instructor source vault
that shares the organizer key. Perform the existing-Secret change explicitly
through the approved organizer procedure, then re-run credential and student
health checks. Personal instructor lab passwords are separate credentials.

For older deployments awaiting this integration, the local key-seeding helper
remains available as a maintenance fallback. Fresh deployments using the
integrated catalog hook should not depend on it.
