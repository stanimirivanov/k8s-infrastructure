# Dormant local Argo CD bootstrap

- Status: Imported, execution disabled
- Profile: `local-kind/perfeng-local`
- Active owner: `perfeng-environment`

## TL;DR

- The official Argo CD `v3.5.2` bootstrap and controller-readiness logic now
  live here with their relevant Git history.
- Validation and command previews are offline; `--execute` fails before Docker,
  kind, kubectl or the network is accessed.
- This repository owns only installation and controller readiness. Consumers
  continue to own Projects, Applications, namespaces and repository credentials.
- `perfeng-environment` remains the active owner of the existing installation
  until a separate cutover passes compatibility and recovery gates.
- Use the checked-in preview commands for review, but continue using
  `perfeng-environment` for live local operations.

## Ownership boundary

This import separates the reusable GitOps controller from the desired state it
reconciles. The infrastructure responsibility is limited to:

- the `argocd` namespace identity;
- selection of the official, versioned install manifest;
- declarative server-side application of that installation;
- bounded rollout checks for every controller in the reviewed non-HA layout;
  and
- concise, redacted controller and Pod diagnostics when readiness fails.

The import deliberately excludes consumer Projects, root and child
Applications, repository credential Secrets, workload namespaces, release
locks and product health. Those remain consumer composition. Installing Argo CD
does not grant this repository authority over anything reconciled through it.

The existing local installation has not changed owner. Until cutover,
`perfeng-environment` remains the only supported tool for live bootstrap,
health and recovery. The dormant code has no Task command or CI path that can
contact a cluster.

## Reviewed release and layout

The Kustomization is stored at
`clusters/local-kind/perfeng-local/addons/argocd/kustomization.yaml`. Its
resource URL names the official `argoproj/argo-cd` repository, semantic release
`v3.5.2` and `manifests/install.yaml` path directly. This keeps the selected
release understandable without duplicating the component image versions already
tested by the upstream release.

Offline validation rejects:

- a branch, floating alias, commit-only identifier or malformed semantic tag;
- a non-HTTPS URL, unexpected host, repository or manifest path;
- query strings and fragments;
- image overrides or other unreviewed Kustomization fields; and
- an unexpected namespace name, owner or substrate label.

The reviewed non-HA installation contains six Deployments and the
`argocd-application-controller` StatefulSet. Every planned readiness command
has a 300-second Kubernetes rollout timeout. A failed future live check reports
the exact controller, desired and ready replica counts, and bounded Pod phase,
container readiness, restart count and waiting reason. Diagnostic text removes
embedded URL credentials and GitHub-shaped tokens.

## Offline validation and preview

Install the locked development environment and validate the selected release:

```sh
uv sync --locked
uv run --locked python -m scripts.argocd validate
```

Preview installation and readiness commands:

```sh
uv run --locked python -m scripts.argocd bootstrap
uv run --locked python -m scripts.argocd health
```

The preview prints JSON command arrays. It reads only checked-in YAML and builds
the dedicated kubeconfig path; it does not require that the kubeconfig or
cluster exist. It does not start a subprocess, render the remote Kustomization,
download the upstream manifest or contact Kubernetes.

To review compatibility with the currently active Perfeng state location,
select that path explicitly:

```powershell
uv run --locked python -m scripts.argocd health `
  --state-root ..\perfeng-environment
```

The future bootstrap sequence first applies the reviewed Kustomization using
server-side apply and explicit conflict handling, then checks each controller.
The health sequence contains only controller rollout checks. Neither sequence
contains consumer reconciliation or credential creation.

## Deliberate execution refusal

All live forms currently fail:

```sh
uv run --locked python -m scripts.argocd bootstrap --execute
uv run --locked python -m scripts.argocd health --execute
```

They exit nonzero with this ownership message:

```text
Live execution remains owned by perfeng-environment until the reviewed ownership cutover
```

The refusal occurs before configuration rendering, process execution or cluster
access. Providing another state root does not bypass it.

## Activation gates

Activation belongs to a separate change and requires all of the following:

1. `perfeng-environment` consumes a reviewed infrastructure revision and stops
   invoking its competing installer before this one is enabled.
2. The existing cluster, context, kubeconfig path, Argo CD namespace and
   controller resource identities are preserved.
3. Namespace ownership labels are migrated deliberately and verified without
   recreating the namespace or its contents.
4. Existing repository credentials and consumer Applications remain owned by
   `perfeng-environment`; the infrastructure command neither reads nor rewrites
   them.
5. Bootstrap, health, timeout diagnostics and safe retry behavior pass against
   the named local cluster.
6. Perfeng GitOps health, product smoke and persistence acceptance pass after
   cutover.
7. Rollback restores exactly one active installer without deleting the cluster,
   Argo CD custom resources, Secrets, persistent volumes or retained evidence.

Task commands and live CI may be introduced only with that activation. Removing
the old implementation follows successful cutover rather than occurring in the
same import.

## Troubleshooting

### Release validation fails

Compare the Kustomization with the exact URL constraints in
`scripts/argocd.py`. Use an official semantic release URL. Do not add an image
override merely to make validation pass; dependency selection changes require
their own review.

### Namespace validation fails

The checked-in namespace is intentionally exact. Do not relax it to adopt an
arbitrary existing namespace. Existing-owner migration belongs to the cutover,
where the live target and rollback can be verified.

### `--execute` reports the ownership refusal

This is the expected dormant state. Continue using the supported
`perfeng-environment` workflow until the cutover is accepted.

### Preview points at the wrong kubeconfig

Pass the intended absolute or workspace-relative `--state-root`. The preview
always derives `.local/perfeng-local.kubeconfig` beneath it and uses context
`kind-perfeng-local`; it never falls back to the ambient kubeconfig.

### A live controller is unhealthy today

Diagnose it through the active `perfeng-environment` troubleshooting guide.
This repository does not yet have authority to repair, replace or relabel the
installation.

## Provenance

The implementation was imported through a filtered-history merge from
`perfeng-environment`. See the
[migration record](migrations/argocd-history-import.md) for the donor revision,
selected paths, commit mapping and post-import adaptations.
