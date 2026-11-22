# Minimal-demo Argo lifecycle

Status: installed qualification requires separate authorization for the selected disposable target.

## TL;DR

- Install the nine reviewed controller workloads with zero replicas. Resume
  explicitly during the deployment-owned maintenance phase.
- Suspend only after release Workflows are terminal. The provider closes new
  Workflow admission and waits for the actual owned Pods to terminate.
- Resume one controller at a time within the worker envelope. Keep the private
  receipt and use its phase to recover an interrupted operation.

## Ownership and prerequisites

This gate implements [M10-PR20](https://github.com/stanimirivanov/k8s-infrastructure/issues/65).
Deployment owns application quiescence, artifact compatibility, and the order
of maintenance phases. The provider operation observes Kubernetes controller
and Workflow state. Producer progress, database drain, and compatibility remain
separate evidence. [Approved runtime contract](https://github.com/stanimirivanov/fleetiq-deploy/issues/91#issuecomment-7070957050)

Use the same reviewed source, exact target, Docker context, HTTPS port and
private state root established at [cold bootstrap](minimal-bootstrap.md).
Complete the provider [network](minimal-network.md), [database controller](minimal-database.md),
and [namespace delegation](minimal-delegation.md) prerequisites. Prepare the
workload and `<consumer>-releases` namespaces with the exact consumer label and
`app.kubernetes.io/managed-by` set to the consumer. Use the bounded public
delegation document described in the [delegated Argo guide](delegated-argo.md).
Protect the delegation file and keep credentials in the reviewed Secret paths.

The provider owns `<consumer>-gitops-system` and `<consumer>-workflow-system`.
Minimal-demo preserves the explicit workload authority boundary established by
the namespace delegation. The release identities and Workflow controls in the
managed namespace use the approved template allowlist. Review source and
consumer workload authority in the deployment-owned release workflow.

Verify the public Argo manifests with `make delegated-argo-asset-check`.
The offline preview lists the reviewed immutable worker images. Stage them on
the exact worker before any resume, in a separately authorized preparation
window. Obtain the immutable worker ID from the cold-bootstrap receipt and use
one listed digest-qualified reference per invocation:

```sh
docker --context default exec "$OWNED_WORKER_ID" \
  crictl --image-endpoint unix:///run/containerd/containerd.sock pull "$IMAGE_REF"
```

The reviewed controller allocation per component is 50m/500m CPU and
64Mi/384Mi memory (request/limit). Each reviewed one-shot initializer has a
full 384Mi memory request and limit. The provider starts the nine controllers
serially and evaluates effective Pod requests and limits alongside all
observed active worker Pods, including init and overhead. Admission by
arithmetic governs whether to start another Pod. Measured fit is a separate
qualification. [Approved resource ledger](https://github.com/stanimirivanov/fleetiq-deploy/issues/92#issuecomment-7071204249)

## Preview and execute

Preview offline with the reviewed public delegation file:

```sh
uv run --locked --offline python -m scripts.minimal_argo install \
  --consumer qual-argo-01 --docker-context default --state-root /private/state \
  --https-port 18443 --delegation /private/argo-delegation.json
```

For a separately approved exact target, append
`--confirm-target qual-argo-01-local --execute`. Use the same selection for
every operation. Choose a shared deadline with `--timeout-seconds` from 10 to
900 seconds. The default is 300 seconds.

| Action | Observed outcome |
|:--|:--|
| `install` | Establish owned provider assets at zero replicas and observe the closed Workflow admission fence. |
| `status` | Compare live owned workload and Pod identities with the private lifecycle receipt. |
| `suspend` | Require terminal Workflows, close new Workflow admission and recheck, then observe each owned Pod disappear. |
| `resume` | Check compatible Argo application state and worker headroom, restore the recorded replicas one component at a time, and observe actual Pod readiness before reopening new Workflow admission. |

Use `Succeeded`, `Failed` or `Error` as terminal Workflow phases. A missing
phase, pending approval or ongoing execution prevents suspension. Close new
Workflow admission before scaling down. Termination and readiness come from
owned Pods and controller generations. A desired replica count alone supplies
insufficient evidence. Resume rejects active operations, enabled automated
sync and ApplicationSet state requiring separate review. Keep that state
paused or resolve it through the reviewed deployment workflow before retrying.

## Recovery

Keep `<target>.argo-lifecycle.json` in the target's private `.local` directory.
Its phase and exact controller UIDs bind the same source, assets and target to
the interrupted operation. If a deadline or interruption occurs, inspect
`status` using the original selection and compare the receipt with the
currently observed Pods. Repeat the pending operation with exact confirmation
after resolving the reported prerequisite. A partial resume can be suspended
again when Workflows remain terminal. An identity change requires explicit
review. Keep provider secrets, release state, shared APIs and consumer
resources in place while recovering.

## Disposable lifecycle and peak qualification

Use a separately approved `qual-` target. Complete a dormant install and an
initial explicit resume. Run the [resource peak collector](minimal-resource-peaks.md)
in its admitted tooling cgroup before another suspend/resume cycle. Use the
same original admission request, source and host boot digest. One bounded
choice for a 10-minute cycle uses `--duration-seconds 600 --interval-seconds 10`.
Use a shorter interval or a neutral controlled phase when the reviewed startup
can finish between samples. Confirm the collection window covers both operations
and retain its private JSONL receipt. Record actual controller termination and
restoration with separately approved `suspend` and `resume` invocations.

Compare the captured cycle from the same admitted tooling cgroup. The
qualification action reads the current target and the captured receipts:

```sh
uv run --locked --offline python -m scripts.minimal_argo qualify \
  --consumer qual-argo-01 --docker-context default --state-root /private/state \
  --https-port 18443 --delegation /private/argo-delegation.json \
  --request /private/minimal-request.json --host-id sha256:CURRENT_BOOT_DIGEST \
  --peak-receipt /private/state/.local/qual-argo-01.peaks.jsonl \
  --confirm-target qual-argo-01-local --execute
```

Qualification requires receipt identity agreement, witnessed lifecycle and
startup coverage, native peaks and tooling evidence from that window. Missing
init observations, pressure or stale target evidence block qualification.
Keep the private qualification receipt with the lifecycle and peak receipts.
