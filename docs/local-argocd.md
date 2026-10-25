# Local Argo CD bootstrap

- Status: Cutover-ready provider release
- Profile: `local-kind/perfeng-local`
- Existing environment entry point: `perfeng-environment` through its immutable selection

## TL;DR

- The official Argo CD `v3.5.2` bootstrap and controller-readiness logic now
  live here with their relevant Git history.
- Redis `8.2.3-alpine` uses the reviewed Docker Hub multi-platform digest and
  `IfNotPresent`; other components retain the upstream selection.
- Validation and command previews are offline; live access requires explicit
  `--execute` and the selected state root.
- This repository owns only installation and controller readiness. Consumers
  continue to own Projects, Applications, namespaces and repository credentials.
- Perfeng records the cutover as accepted and remains the supported operator
  entry point through its immutable provider selection; this merge does not update it.
- Bootstrap accepts only a missing namespace, this repository's namespace, or
  the exact reviewed legacy Perfeng ownership identity.

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

Merging this provider release does not contact or change the existing local
installation. Until consumer cutover, `perfeng-environment` remains the
supported operator entry point for bootstrap, health and recovery. This
repository deliberately adds no Task command or live CI path during the staged
handoff.

## Reviewed release and layout

The Kustomization is stored at
`clusters/local-kind/perfeng-local/addons/argocd/kustomization.yaml`. Its
resource URL names the official `argoproj/argo-cd` repository, semantic release
`v3.5.2` and `manifests/install.yaml` path directly. This keeps the selected
release understandable. [ADR 0011](decisions/0011-pin-local-argocd-redis-image.md)
records one reviewed exception: Kustomize selects canonical
`docker.io/library/redis` at index digest
`sha256:08ad0b1d280850169a790dba1393ff7a90aef951fc19632cf4d3ce4f78e679ba`,
the same multi-platform content as upstream Redis `8.2.3-alpine` on ECR Public.
The single-object `redis-pull-policy.yaml` patch changes only the `redis`
container's policy to `IfNotPresent`. A cached immutable image needs no tag
refresh; an absent image still requires a successful registry pull.

Offline validation rejects:

- a branch, floating alias, commit-only identifier or malformed semantic tag;
- a non-HTTPS URL, unexpected host, repository or manifest path;
- query strings and fragments;
- any image transformation or patch other than that exact Redis selection;
- changed patch targets, container fields or pull policies;
- another Argo CD release without reviewing its Redis selection together; and
- an unexpected namespace name, owner or substrate label.

The reviewed non-HA installation contains six Deployments and the
`argocd-application-controller` StatefulSet. Every planned readiness command
has a 300-second Kubernetes rollout timeout. A failed live check reports
the exact controller, desired and ready replica counts, and bounded Pod phase,
container readiness, restart count and waiting reason. Diagnostic text removes
embedded URL credentials and GitHub-shaped tokens.

This change is limited to `local-kind/perfeng-local`. Isolated/delegated
consumer assets retain their independent selections. No new credential, cache
service, CI job or registry fallback is installed. Docker Hub still has public
pull limits; cold-pull availability requires separate live acceptance.

### Explicit rollout and rollback

Merging this provider commit changes no running cluster or consumer lock.
Perfeng must first select the merged provider revision in a separate reviewed
change, then use its supported explicit bootstrap path. Reapplying changes the
Redis Pod template and can replace its Pod. Namespace, Deployment, Secret and
consumer Application ownership remain unchanged; no credential reset is part
of this change. Readiness must still pass through the existing bounded checks.

Rollback selects the previous reviewed provider revision and explicitly
reapplies it through that same owner. The former registry and `Always` policy
return, including their pull-throttling exposure. Do not delete the namespace,
Secrets or consumer resources as a rollback shortcut.

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

The bootstrap sequence verifies cluster health and the existing namespace
identity, applies the reviewed Kustomization using server-side apply and
explicit conflict handling, verifies the new infrastructure owner, then checks
each controller. The health sequence verifies cluster health, requires the
infrastructure-owned namespace and performs only controller rollout checks.
Neither sequence contains consumer reconciliation or credential creation.

## Explicit execution

After reviewing the preview, execution requires the intended state root:

```powershell
uv run --locked python -m scripts.argocd bootstrap --execute `
  --state-root ..\perfeng-environment
uv run --locked python -m scripts.argocd health --execute `
  --state-root ..\perfeng-environment
```

Bootstrap accepts three states:

- the namespace is absent and may be created;
- it already has the complete `k8s-infrastructure` identity; or
- it has the exact legacy `perfeng-environment` owner and
  `perfeng.io/purpose: gitops-control-plane` label used by the reviewed handoff.

Any other existing namespace fails before apply. After apply, the namespace
must report the complete infrastructure identity before controller readiness is
checked. `health` never applies configuration and accepts only the new owner.

## Consumer cutover gates

The provider is ready for immutable selection. Transferring the existing
installation still requires a consumer change that proves all of the following:

1. `perfeng-environment` pins the merged infrastructure revision and replaces
   its installer invocation with this provider entry point.
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

Removing the old implementation follows successful live acceptance rather than
occurring in the consumer-switch pull request.

## Troubleshooting

### Release validation fails

Compare the Kustomization with the exact URL constraints in
`scripts/argocd.py`. Use the official reviewed semantic release URL and exact
Redis digest/patch. Additional overrides, a redirected patch path or a changed
release are refused. Review the image and policy together when upgrading.

### Redis image pulls are throttled

The prior upstream Redis selection used ECR Public with `Always`. Two jobs in
Perfeng run 40214268159 retained `ImagePullBackOff` and UID-correlated
`rate-limit` events after init completed. Those reports do not attribute the
quota to a particular account or IP. Check the consumer's selected provider
revision before expecting the new Docker Hub digest and `IfNotPresent` policy;
this provider PR does not update that lock.

For a new failure, inspect the Pod waiting reason and bounded event evidence.
Docker Hub also enforces pull limits; network, TLS and missing-manifest errors
require their own diagnosis. Preserve the immutable digest and reviewed
identity while resolving access or availability. Do not substitute a floating
tag, invent a credential reset or increase readiness deadlines without evidence.

### Namespace validation fails

The checked-in namespace is intentionally exact. Do not relax it to adopt an
arbitrary existing namespace. Existing-owner migration belongs to the cutover,
where the live target and rollback can be verified.

### Bootstrap rejects the existing namespace owner

Inspect only the namespace name and labels. Bootstrap accepts the exact reviewed
legacy owner for migration but refuses arbitrary namespaces. Do not relabel or
delete the namespace manually as a workaround.

### Preview points at the wrong kubeconfig

Pass the intended absolute or workspace-relative `--state-root`. The preview
always derives `.local/perfeng-local.kubeconfig` beneath it and uses context
`kind-perfeng-local`; it never falls back to the ambient kubeconfig.

### A live controller is unhealthy

The command prints bounded Deployment/StatefulSet and Pod readiness evidence
while withholding command output that may contain credentials. Before consumer
cutover, continue recovery through the active `perfeng-environment` workflow.
After cutover, correct the reviewed provider configuration or dependency and
retry; do not delete the namespace, Applications or Secrets.

## Provenance

The implementation was imported through a filtered-history merge from
`perfeng-environment`. See the
[migration record](migrations/argocd-history-import.md) for the donor revision,
selected paths, commit mapping and post-import adaptations.
