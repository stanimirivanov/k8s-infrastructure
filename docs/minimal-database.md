# Minimal-demo database controller qualification

Status: installed qualification requires an explicitly authorized disposable run.

## TL;DR

- Start from the same disposable target and source that passed [minimal-demo
  Cilium qualification](minimal-network.md).
- Stage the five digest-qualified provider controller images on the owned worker
  before applying the database provider selection.
- The gate checks worker placement, finite effective Pod resources, certificate
  issuance and stop-before-start controller replacement.
- Keep the private qualification receipt. Removal is a separate operation
  against the exact disposable target.

## Scope and prerequisites

Use this gate for [M10-PR17](https://github.com/stanimirivanov/k8s-infrastructure/issues/62).
The [approved candidate ledger](https://github.com/stanimirivanov/fleetiq-deploy/issues/92#issuecomment-7071204249)
sets these five worker allocations. CPU is in millicores and memory is in MiB.

| Controller | CPU request / limit | Memory request / limit |
| --- | ---: | ---: |
| cert-manager | 50 / 500 | 128 / 256 |
| cert-manager CA injector | 50 / 500 | 64 / 128 |
| cert-manager webhook | 50 / 500 | 64 / 128 |
| CloudNativePG operator | 100 / 500 | 192 / 384 |
| Barman Cloud Plugin | 50 / 500 | 64 / 128 |

The target needs the capped control plane and worker, the original admission
request, an authorized tooling cgroup, and a successful Cilium receipt from the
same source and cluster incarnation. Start on a quiescent, disposable `qual-`
target. Keep consumer databases and retained volumes outside this exercise.
The [database capability guide](database-and-storage.md) covers ownership,
storage, and existing profile qualification. The application PostgreSQL budget,
connection pool, and backup/restore acceptance remain deployment-owned.

Review the [database asset pins](../clusters/local-kind/isolated-network-local/database/releases.json)
and run `make database-asset-check` to verify the public assets and rendered
provider inventory. In a separately authorized preparation window, stage each
reviewed controller image into the Kubernetes CRI on the owned worker. Obtain
the worker's immutable container ID from the matching cold-bootstrap receipt.
Use one digest-qualified image per invocation and charge staging to the approved
image and tooling budgets. For example:

```sh
docker --context default exec "<owned-worker-id>" \
  crictl --image-endpoint unix:///run/containerd/containerd.sock pull "<image-ref>"
```

## Commands

Complete the [bootstrap and Cilium sequence](minimal-network.md#commands) with
the same target input and source revision. Preview the database gate without
`--execute`. It reports the intended selection without reaching the target.

```sh
uv run --locked --offline python -m scripts.database qualify \
  --profile minimal-demo --consumer qual-minimal-db-01 \
  --docker-context default --state-root /private/state --https-port 18443
```

After separately authorizing database qualification on that target, run the same
command in the reviewed tooling cgroup with the private admission input and
current host boot identity:

```sh
uv run --locked --offline python -m scripts.database qualify \
  --profile minimal-demo --consumer qual-minimal-db-01 \
  --docker-context default --state-root /private/state --https-port 18443 \
  --request /private/minimal-request.json --host-id sha256:CURRENT_BOOT_DIGEST --execute
```

## Startup, evidence and recovery

The gate uses the pinned cert-manager, CNPG and Barman resources. It observes
controller startup on the worker, waits for the reviewed Barman issuer and
client/server certificates, and exercises CNPG admission with a server dry-run.
It creates a new self-signed certificate after replacing the cert-manager
controller to confirm that issuance resumes. The probe creates no PostgreSQL
instance, PVC or Barman backup.

Replacement proceeds one controller at a time. The old Pod must terminate
before the replacement starts. Server dry-run and live Pod inspection check the
complete admitted container inventory, including any initializer, sidecar or
overhead. Live evidence records effective resource bounds, placement, lifecycle
and peaks under the enclosing node caps. The operation has a 1,200-second shared
deadline. Individual CRD, admission and termination waits run for at most 90
seconds, and rollout and certificate waits for at most 120 seconds. The shared
deadline can shorten any stage. Liveness or rendered resources alone cannot
establish an installed result.

Inspect `<target>.database-qualification.json` beneath the target state root's
private `.local` directory. Use its last completed phase and sanitized failure
details to distinguish an identity or host admission refusal, missing image,
controller readiness failure, certificate failure or replacement failure. Keep
the detailed state and kubeconfig private. A failed smaller allocation blocks
qualification. Use a newly admitted disposable target after reviewing a fix.

Removal requires separate authorization and exact confirmation. This command
destroys the selected disposable cluster and its node-local contents:

```sh
uv run --locked --offline python -m scripts.minimal_bootstrap down \
  --consumer qual-minimal-db-01 --docker-context default --state-root /private/state \
  --https-port 18443 --confirm-delete qual-minimal-db-01-local --execute
```
