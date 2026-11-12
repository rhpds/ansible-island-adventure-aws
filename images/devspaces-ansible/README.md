# Ansible Island Adventure workspace image

This image extends the upstream UBI9-based `ansible/ansible-devspaces` image,
preserving its Ansible Development Tools, Python dependencies, and Dev Spaces
runtime. It adds shared collections needed by the instructor playbooks. No
credentials or vault passwords are included in the build context or image.

Build using only this directory as the context:

```sh
podman build --platform linux/amd64 -f Containerfile -t REGISTRY/aia-devspaces-ansible:VERSION .
podman push REGISTRY/aia-devspaces-ansible:VERSION
```

Pin the resulting digest in the instructor Gitea `.devfile.yaml`. Validate the
image in a real DevWorkspace, including the editor and Ansible CLI commands.
The cluster's internal registry is suitable for this development deployment;
other clusters require a published copy in a registry they can reach.

To build and publish with OpenShift instead of a local container engine:

```sh
oc apply -n aia-workspace-images -f images/devspaces-ansible/buildconfig.yaml
oc start-build -n aia-workspace-images aia-devspaces-ansible \
  --from-dir=images/devspaces-ansible --follow
oc get istag -n aia-workspace-images aia-devspaces-ansible:latest \
  -o jsonpath='{.image.dockerImageReference}'
```

Create `aia-workspace-images` first and grant `system:image-puller` in that
namespace to the instructor Dev Spaces service accounts. The devfile image
reference must use the registry service hostname and the returned digest.

Students configure their vault password with the instructor devfile's
`setup-vault-password` task. The password is supplied separately by the event
organizer and must match the vault in their instructor repository.

## Development deployment, 2026-10-30

Cluster: `cluster-4v2zk`. Successful OpenShift build:
`aia-workspace-images/aia-devspaces-ansible-lkkhj`.

Published image:

```text
image-registry.openshift-image-registry.svc:5000/aia-workspace-images/aia-devspaces-ansible@sha256:be4540ba1fa8cede4a158471caa702d6f4b1b9adf4851493f2fe01e3b57bed09
```

The instructor repositories and starter template in this cluster's Gitea use
this digest. The live workspace `instructor-rv8fv-devspaces/aia-instructor-lzdw`
was verified with the image, including the Che-Code editor runtime.

The upstream `l3acon/aia-instructor` devfile now uses the public, digest-pinned
`ghcr.io/ansible/ansible-devspaces` base directly and installs the same extra
collections with a `postStart` command. Future Gitea imports therefore work
without this cluster's internal registry. Using this custom prebuilt image on
another cluster still requires publishing a copy that the cluster can reach.

Verified tools include Ansible Core 2.21.5, ansible-lint 26.10.1,
ansible-navigator 26.10.0, ansible-builder 3.1.1, and Molecule 26.10.0.
