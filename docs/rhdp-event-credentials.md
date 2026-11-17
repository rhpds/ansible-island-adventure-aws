# RHDP encrypted include for AIA organizer credentials

Review links: [AIA implementation PR](https://github.com/rhpds/ansible-island-adventure-aws/pull/1)
and [AgnosticV catalog PR](https://github.com/rhpds/agnosticv/pull/29067).
The catalog PR remains a draft until maintainers wire the approved encrypted
include into the Leaderboard workload configuration.

## Variable requested from maintainers

Please store the organizer event key under this exact Ansible variable name:

```text
aia_organizer_vault_password
```

Use RHDP's existing approved encrypted AgnosticV include mechanism. The value
is the organizer key matching `roles/student_post_provision/files/flags.vault`,
which is the same source key used by the organizer's local `.vault-password`.
It is separate from the RHDP platform key used to decrypt the include and from
each instructor's provisioned lab password.

Make the variable available to the **AWS Leaderboard workload deployer job**.
Defining it there is sufficient: the Leaderboard role provisions the existing
protected Secret handoff that Student jobs already consume. Supplying the same
variable directly to Student jobs is also supported when maintainers choose to
include it there.

The encrypted include path and encryption procedure belong to RHDP maintainers.
Use an event-scoped include in the approved directory; the public catalog change does not invent a
new secret file or commit an unencrypted placeholder. The catalog PR must remain
a draft until the selected include actually supplies this variable to the job.

## Changes and ownership

| Owner | Action |
| --- | --- |
| RHDP maintainer | Store `aia_organizer_vault_password` in the approved encrypted include and include it in the AWS Leaderboard workload configuration. Keep it encrypted through catalog rendering and decrypt only within the deployer using the existing mechanism. |
| AIA repository maintainers | Ship the variable-aware bootstrap and Student unlock paths, create-only Secret handling, and readiness checks. |
| AgnosticV maintainer | Review the pinned collection revision and workload ordering, confirm the private include is connected, then enable the catalog PR. |

No new named credential on the event controllers is required. Existing RHDP
platform credentials decrypt the approved include through the platform's normal
mechanism.

Do not expose the variable through catalog parameters, form defaults, plaintext
public configuration, `agnosticd_user_info`, published user data, Showroom,
repository access guides, or exported student inventory. Transfer the source key
through the approved private channel rather than a GitHub comment or PR body.

## Automation behavior

The Leaderboard bootstrap role reads `aia_organizer_vault_password` and uses it to
unlock the packaged encrypted event before changing Kubernetes resources. The
value must be a nonempty string, including a value decrypted from an inline
Ansible Vault variable. An explicitly supplied empty, invalid or incorrect value
fails provisioning; it does not silently fall back to a different credential.

After validating the key, bootstrap checks instructor authorization, then creates
Namespace `aia-automation` and Secret `aia-event-vault`, key `password`. Secret
creation uses POST-only handling. Matching existing keys are accepted; a
malformed or different existing key fails rather than being overwritten,
including during concurrent creation.

The existing Student role resolves its key in this order:

1. `aia_organizer_vault_password`, when the approved include supplies it directly.
2. Existing job Vault credentials, when the organizer variable is absent.
3. The protected Leaderboard Secret, when the variable is absent and existing
   job credentials do not unlock the event.

Legacy local maintenance can still use its existing Vault password file or the
local Secret-seeding helper. Those are not required for fresh deployments once
the maintainer include and catalog hook are enabled.

All key-bearing tasks suppress logs and diffs. Derived decrypted answers and
key facts are cleared on success and failure. The input variable remains owned
by RHDP's secret-delivery mechanism and is never copied into published user
data. Instructors continue using their individual lab passwords.

## Catalog integration

The catalog PR updates the AWS Leaderboard and Student collection revisions in
`rh1-2027/lb1663-rh1-ans-island-{leaderboard,student}-aws/common.yaml` in
`rhpds/agnosticv`. Maintainers must connect their selected encrypted include to
the Leaderboard workload job. The Leaderboard hook enables:

```yaml
aia_leaderboard_require_event_credentials: true

workloads:
  # Retain the existing workloads before these entries.
  - ansible_island_adventure.automation.keycloak_ocp_logout_fix
  - ansible_island_adventure.automation.leaderboard_event_credentials
  - ansible_island_adventure.automation.leaderboard_health
```

Both items pin `aia_aws_revision` to the reviewed variable-aware implementation
commit. Update the pins deliberately for future automation releases. Student
jobs support both direct variable use and the protected Secret handoff; they do
not need a matching include when only the Leaderboard receives the variable.
No change to the AIA repository's
top-level `catalog-info.yaml` is needed.

## Readiness and acceptance checks

The fifteenth Leaderboard health check, `event_credentials`, independently
confirms that the stored key decrypts the packaged event. SubjectAccessReviews
must deny an instructor identity direct reads plus listing and watching Secrets,
both unrestricted and restricted to the key's resource name. Authorization
review errors fail the check. The existing fourteen service/storage checks
remain required.

`aia_event_vault_probe_users` accepts additional real instructor usernames;
`aia_event_vault_probe_groups` defines the groups evaluated by those reviews.
Include any additional instructor groups if the authentication policy changes.
Only measured status (`aia_event_credentials_status: passed` or `failed`) is
published alongside aggregate health. Bootstrap skips destruction and check
mode; stop/start does not rotate the key.

1. Confirm the include uses the existing approved encryption mechanism and its
   variable reaches the Leaderboard workload job. Keep the organizer key
   encrypted in stored catalog configuration; do not materialize it as a public
   parameter to test delivery.
2. Order a fresh AWS Leaderboard through RHDP's ordinary UI. Do not seed a Secret
   manually or use `--organizer-vault-password-file`.
3. Require native success, `aia_event_credentials_status: passed`, and
   `aia_leaderboard_health_status: passed`, including the fifteenth check.
4. Order at least two Students. Each must publish
   `aia_post_provision_status: complete`, `aia_event_challenge_count: 25`, and
   `aia_health_status: passed`, including actual AAP execution and VM connectivity.
   No manual post-deploy playbook should be required.
5. Verify real instructor access after provisioning. As an organizer with an
   explicit Leaderboard kubeconfig, expect `no` for all five checks below; each
   denial returns exit 1:

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

6. Re-run bootstrap with the same variable and confirm that the Secret UID and
   key remain unchanged. An explicitly empty or incorrect variable must fail
   before event/key mutation. If the variable is absent and no legacy job
   credential matches, bootstrap must also fail. Check that teardown skips
   credential bootstrap.

Tests exercise a platform-encrypted include carrying a different organizer key,
variable precedence, incorrect/empty supplied values, redaction, cleanup,
concurrent Secret creation, instructor read denial and lifecycle guards.

## Troubleshooting and rotation

| Symptom | Check |
| --- | --- |
| Variable is absent in the workload job | Connect the encrypted include to the Leaderboard workload configuration, not only its base OpenShift component. |
| Explicit variable cannot unlock the event | Verify the supplied organizer key matches the packaged event. The platform key used to decrypt the include may be different. |
| Existing Secret is rejected | Investigate the event/key versions privately; bootstrap deliberately refuses implicit rotation. |
| Instructor authorization succeeds | Inspect namespace and cluster RBAC; retain the failed readiness gate. |
| Student cannot resolve a key | Check its selected Leaderboard API and that Leaderboard credential bootstrap completed, or its direct include variable if supplied. |

Rotation requires coordinated updates to the encrypted include, packaged event
vault, protected cluster Secret, and any upstream instructor source vault sharing
that organizer key. Change an existing Secret only through the approved organizer
procedure, then re-run credential and Student health checks. Personal instructor
lab passwords are separate credentials.
