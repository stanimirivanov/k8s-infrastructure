# Local-kind lifecycle

- Status: Cutover-ready provider release
- Profile: `perfeng-local`
- Existing environment entry point: `perfeng-environment` until consumer cutover

## TL;DR

- The proven kind lifecycle and its relevant Git history live here.
- Commands remain offline previews unless `--execute` is supplied.
- Live execution verifies the exact cluster, Docker endpoint, kubeconfig and
  node identity and bounds each external command to 600 seconds.
- `perfeng-environment` remains the supported entry point for the existing
  `perfeng-local` cluster until it pins and invokes this provider release.
- The profile preserves the cluster name, context, Kubernetes image, node
  topology and kubeconfig filename needed for a later cutover.
- Do not invoke this release against the existing environment independently of
  the reviewed consumer cutover.

## Current ownership boundary

This provider release establishes executable substrate behavior, but merging it
does not mutate or adopt an existing cluster. The existing cluster, dedicated
kubeconfig, Argo CD installation and persistent workloads remain operated
through `perfeng-environment` until that repository selects this revision and
changes its command surface.

There is deliberately no Task entry in this repository during the staged
handoff. The default state root is this checkout, so it cannot adopt the
existing Perfeng cluster, whose dedicated kubeconfig remains under
`perfeng-environment`. Access to that cluster requires selecting the consumer
state root explicitly, and the provider then verifies the kubeconfig against
the named kind cluster before using it.

The infrastructure lifecycle includes only:

- cluster creation;
- exact cluster and kubeconfig identity checks;
- node and CoreDNS health;
- reuse of a healthy owned cluster; and
- explicitly confirmed destruction.

Perfeng namespace bootstrap was not carried across. Consumer namespaces and
workloads remain environment composition.

## Reviewed profile

The profile is stored at
`clusters/local-kind/perfeng-local/cluster-config.yaml` and is validated against
the compatibility identity in `scripts/local_kind.py`.

It preserves:

- cluster name `perfeng-local`;
- context `kind-perfeng-local`;
- kubeconfig filename `.local/perfeng-local.kubeconfig` under the selected
  state root;
- kind `v0.31.0`;
- Kubernetes `v1.35.0` using the reviewed immutable node-image digest;
- one control-plane node; and
- dedicated performance-generator and SUT worker labels.

Changing any profile field fails offline validation until the compatibility
identity and its tests are reviewed together.

## Offline preview

Install the locked environment, then preview the lifecycle:

```sh
uv sync --locked
uv run --locked python -m scripts.local_kind up
uv run --locked python -m scripts.local_kind ensure
uv run --locked python -m scripts.local_kind health
uv run --locked python -m scripts.local_kind down
```

Preview the existing Perfeng state boundary explicitly without accessing it:

```powershell
uv run --locked python -m scripts.local_kind health `
  --state-root ..\perfeng-environment
```

Preview reads the checked-in profile and prints exact command arrays. It does
not invoke Docker, kind or kubectl, create `.local`, read a kubeconfig or contact
a cluster.

The destructive preview shows the exact named cluster and dedicated kubeconfig.
It does not delete either resource.

## Explicit execution

After reviewing the preview, add `--execute` to operate the selected state root:

```powershell
uv run --locked python -m scripts.local_kind health --execute `
  --state-root ..\perfeng-environment
```

The actions are:

- `up` creates only when neither the named cluster nor its kubeconfig exists;
- `ensure` creates an absent target or health-checks the complete existing
  identity;
- `health` verifies the cluster, kubeconfig, nodes and CoreDNS without changing
  desired state; and
- `down` additionally requires `--confirm-delete perfeng-local` and permanently
  removes cluster-local data.

Live commands reject redirected state, remote Docker endpoints, the wrong kind
version, incomplete or foreign identities, unhealthy nodes and timeouts. A
failed creation is retained for inspection and is never automatically deleted.

## Consumer cutover gates

The provider implementation is ready for immutable selection. Transferring the
existing environment still requires a separate consumer issue and pull request
that proves all of the following:

1. `perfeng-environment` consumes this implementation from an immutable merged
   infrastructure revision and changes its operator commands in the same
   review.
2. The selected state root preserves or deliberately migrates the existing
   dedicated kubeconfig without changing cluster identity.
3. Preview, status, health and destroy retain their target-verification and
   bounded-execution behavior.
4. Provider health followed by local platform health, smoke and
   restart-persistence acceptance pass before the old implementation is
   removed.
5. Rollback restores one active owner without replacing the cluster, Argo CD,
   credentials, volumes or retained evidence.

A top-level infrastructure Task command may be added only when a supported
consumer or standalone operator workflow needs it. Live CI is not introduced
for a developer-owned local cluster.

## Troubleshooting

### Profile validation fails

Run the repository tests and compare the YAML with the exact identity in
`load_profile`. Do not relax validation to accept an unreviewed node image,
public API address, topology or label.

### The named cluster exists without the selected kubeconfig

Select the state root containing the dedicated kubeconfig. Do not regenerate or
copy credentials merely to bypass the identity check. Before the consumer
cutover, continue using its supported operator command.

### Preview shows an unexpected path

Run from the repository checkout with the locked Python environment. Do not
copy the profile or use a symlinked state path as a workaround. Path and state
handoff are part of the consumer cutover review.

### Existing cluster needs diagnosis

Before the consumer cutover, use the current `perfeng-environment`
troubleshooting guide. After cutover, run this provider's `health` action first,
then diagnose consumer workloads through their owning environment.

### An external command times out

Each Docker, kind or kubectl process is stopped after 600 seconds. Inspect the
named local target and retained state. Do not increase the timeout or recreate
the cluster without diagnosing the blocked dependency.

## Provenance

The implementation was imported through a filtered-history merge from
`perfeng-environment`. See the
[migration record](migrations/local-kind-history-import.md) for the donor
revision, selected paths and commit mapping.
