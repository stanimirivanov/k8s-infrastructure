# Dormant local-kind lifecycle

- Status: Imported, execution disabled
- Profile: `perfeng-local`
- Active owner: `perfeng-environment`

## TL;DR

- The proven kind lifecycle and its relevant Git history now live here.
- Commands are offline previews; `--execute` fails before Docker, kind or
  kubectl is invoked.
- `perfeng-environment` remains the only supported owner of the existing
  `perfeng-local` cluster.
- The profile preserves the cluster name, context, Kubernetes image, node
  topology and kubeconfig filename needed for a later cutover.
- Do not activate this implementation until the documented ownership and
  consumer-compatibility gates pass.

## Current ownership boundary

This import establishes code and provenance, not operational ownership. The
existing cluster, dedicated kubeconfig, Argo CD installation and persistent
workloads remain managed through `perfeng-environment`.

The imported command deliberately has no Task entry and refuses live execution.
This prevents two repositories from managing the same cluster while the
consumer handoff and release boundary are unfinished.

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

## Deliberate execution refusal

Every live form currently fails:

```sh
uv run --locked python -m scripts.local_kind health --execute
```

The command exits nonzero with:

```text
Live execution remains owned by perfeng-environment until the reviewed ownership cutover
```

The refusal occurs before process execution. `--confirm-delete perfeng-local`
does not bypass it.

## Activation gates

Activation requires a separate issue and pull request that proves all of the
following:

1. `perfeng-environment` consumes an immutable reviewed infrastructure revision
   rather than retaining a competing implementation.
2. The selected state root preserves or deliberately migrates the existing
   dedicated kubeconfig without changing cluster identity.
3. Preview, status, health and destroy retain their target-verification and
   bounded-execution behavior.
4. Local platform health, smoke and restart-persistence acceptance pass before
   the old implementation is removed.
5. Rollback restores one active owner without replacing the cluster, Argo CD,
   credentials, volumes or retained evidence.

Task commands and live CI are added only at activation. Importing the Argo CD
bootstrap remains a separate change.

## Troubleshooting

### Profile validation fails

Run the repository tests and compare the YAML with the exact identity in
`load_profile`. Do not relax validation to accept an unreviewed node image,
public API address, topology or label.

### `--execute` reports the ownership refusal

This is the expected state, not a missing dependency. Continue using the
supported `perfeng-environment` command until the cutover is merged.

### Preview shows an unexpected path

Run from the repository checkout with the locked Python environment. Do not
copy the profile or use a symlinked state path as a workaround. Path and state
handoff are part of the later activation review.

### Existing cluster needs diagnosis

Use the current `perfeng-environment` troubleshooting guide. This repository
does not yet claim live health or recovery authority.

## Provenance

The implementation was imported through a filtered-history merge from
`perfeng-environment`. See the
[migration record](migrations/local-kind-history-import.md) for the donor
revision, selected paths and commit mapping.
