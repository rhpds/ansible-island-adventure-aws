# Ansible Island Adventure — AWS automation

AWS-specific source copy of [Ansible Island Adventure](https://github.com/rhpds/ansible-island-adventure),
based on upstream commit `565df07cdfcfeb6211f713fb7348d16adacfb223`.
Development ticket: [RHDPCD-1137](https://redhat.atlassian.net/browse/RHDPCD-1137).

The existing CNV repository remains unchanged. This repository provides the
AWS version of the custom Ansible collection under `automation/ansible`.
The collection keeps its original `ansible_island_adventure.automation` namespace;
AWS catalog items install this implementation instead of the CNV implementation.

## Deployment

New AgnosticV catalog items:

- `rh1-2027/lb1663-rh1-ans-island-leaderboard-aws`
- `rh1-2027/lb1663-rh1-ans-island-student-aws`

The leaderboard uses a multinode AWS OpenShift cluster with three workers.
ODF uses gp3-backed storage and supplies CephFS RWX for AAP Hub. Student
managed nodes use EC2 SSH endpoints and an OpenShift HTTPS route for the
Apache challenge. See [AWS.md](AWS.md) for architecture and validation limits.

The student catalog integration completes event content, repository access and
the instructor vault during provisioning, then gates handoff on full student
health. The leaderboard has its own final storage/service gate. See
[catalog integration and health-check entry points](AWS.md#provisioning-gates-and-the-images-health-checks).

Showroom content is served from this AWS repository on main, using site.yml
and the local content/ directory. The original CNV content repository remains
independent. Inherited utility scripts retain CNV ordering defaults and must
not be used to order AWS environments. This copy is not registered as
the original Publishing House project and does not publish workflow updates.

## Antora GitHub Pages preview

Lab guide: https://rhpds.github.io/ansible-island-adventure-aws/

The `Publish Antora GitHub Pages` workflow builds `site.yml` with Antora and
publishes the generated static `www/` artifact using GitHub Pages Actions.
It runs on content/build changes to `main`, or can be dispatched manually.
No Jekyll or separate `gh-pages` source branch is used. Node dependencies are
version-pinned in `package-lock.json`, and Actions are pinned by commit SHA.

To build the same preview locally:

```sh
npm ci --ignore-scripts
npm run docs:build -- --url https://rhpds.github.io/ansible-island-adventure-aws
```

The preview contains placeholders, not live environment credentials. Its build
sets example SSH attributes only for the static preview; RHDP Showroom still
uses `site.yml` directly and receives the actual deployment's user data.

## Tests

Local deployment state belongs under the ignored `.aia-dev/` directory.
The top-level `catalog-info.yaml` is platform metadata and must not be rewritten
for local tests or committed with deployment-specific changes. Devfile-generated
assistant permissions use `.aia-dev/local-catalog-info.yaml` for local metadata.

Install Ansible, PyYAML, Jinja2 and the dependencies used by the collection tests,
then run:

```sh
python -m unittest discover -s automation/ansible/tests -v
```

These are local tests, not evidence of a successful AWS deployment. Live
integration testing of ODF/RWX, AAP, student access and cleanup remains required.
