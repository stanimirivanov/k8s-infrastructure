# Minimal-demo namespace and operation delegation

Status: installed qualification requires a separately authorized disposable run.

## TL;DR

- The provider installs narrow Roles, identity bindings, quotas and admission
  rules for a named minimal-demo target.
- Use explicit `<consumer>-apps`, `<consumer>-data` and `<consumer>-demo-support`
  workload namespaces. The provider owns the separate launcher namespace.
- Approve one exact application or data Job attempt at a time. The attempt
  binds one launcher identity, one Job name and one reviewed Pod specification.
- Qualify the permissions and filesystem on a disposable target. Keep the
  receipt, Pods and retained claim for inspection.

## Ownership and scope

This gate implements [M10-PR18](https://github.com/stanimirivanov/k8s-infrastructure/issues/63).
Use the [runtime contract](https://github.com/stanimirivanov/fleetiq-deploy/issues/91#issuecomment-7070957050)
and [resource ledger](https://github.com/stanimirivanov/fleetiq-deploy/issues/92#issuecomment-7071204249)
for consumer-specific Jobs, places and production claims. This provider gate
installs infrastructure authority. The consumer owns application, data and
support desired state and remains responsible for workload credentials and
operation receipts.

Prepare the three workload namespaces with the exact consumer label
`k8s-infrastructure.io/consumer` and Pod Security Admission enforcement
`baseline` at `v1.35`. The provider binds `demo-reconciler` from the separate
`<consumer>-demo-operations` namespace into those exact workload namespaces.
The reconciler's Role grants a reviewed set of namespaced workload and service
kinds. Database control resources are confined to the data namespace. Service
admission limits this identity to private ClusterIP Services. Namespace creation,
cluster API/RBAC changes and direct routine Job creation belong to separate
authority.

## Approve an operation

An operation document comes from a reviewed workflow. It names one concrete
Job attempt in the application or data namespace and includes one regular
container and, where needed, a bounded initializer. For example, a neutral
application attempt with the filesystem probe image uses this input:

```json
{
  "name": "neutral-attempt-01",
  "namespace": "example-apps",
  "deadlineSeconds": 60,
  "podSpec": {
    "securityContext": {"runAsUser": 65532, "runAsGroup": 65532},
    "containers": [{
      "name": "operation",
      "image": "docker.io/library/python:3.12.16-slim-bookworm@sha256:0f086103b026048cc50cb5da25ec435c023c5241a83fc44daea593377abc61c4",
      "command": ["python", "-I", "-B", "-c", "print('neutral operation')"],
      "resources": {
        "requests": {"cpu": "50m", "memory": "32Mi"},
        "limits": {"cpu": "200m", "memory": "64Mi"}
      }
    }]
  }
}
```

Review every image, command, credential reference, mount and resource bound.
Store the document outside Git and supply references instead of secret values.
Use the preview's digest to identify the approved attempt. Render its exact
Job manifest offline into a private file for the launcher:

```sh
(
  umask 077
  uv run --locked --offline python -m scripts.minimal_delegation job \
    --consumer example --docker-context default --state-root /private/state \
    --https-port 18443 --operation /private/attempt.json > /private/attempt-job.json
)
```

The provider creates a digest-named launcher and a separate runtime ServiceAccount.
The launcher can submit the exact new Job and read that Job and its reviewed
template. Native admission binds the launcher to the reviewed PodSpec. The
runtime account carries no API grants or mounted API token. The Job has one
completion, no controller retry budget, an explicit deadline and worker
placement. The application namespace keeps simulator Jobs with ingestion. The
data namespace can host a separately reviewed administrative operation.

## Configure and inspect

Use the same exact context, private state root and HTTPS port selected at cold
bootstrap. Complete the owned [network](minimal-network.md) and [database](minimal-database.md)
provider setup before installing delegation. The workload namespace owner
creates the named namespaces first. Preview is offline:

```sh
uv run --locked --offline python -m scripts.minimal_delegation configure \
  --consumer example --docker-context default --state-root /private/state \
  --https-port 18443 --operation /private/attempt.json
```

After separate approval for the exact target, repeat with
`--confirm-target example-local --execute`. Omit `--operation` to select the
base namespace delegation. The provider installs quota and admission controls
before enabling the corresponding RoleBindings. Run `health` with the same
target and operation inputs to inspect the installed objects. `health` uses
the same explicit target confirmation for live reads.

## Disposable qualification and evidence

Use a fresh `qual-` consumer and the same reviewed source for the capped
bootstrap, network and database receipts. Preview `qualify` to see the two
digest-qualified worker probe images. Stage them on the exact capped worker
in the separately approved preparation window. Use the [database guide's
staging procedure](minimal-database.md#scope-and-prerequisites).

After authorization for this disposable target, run the native gate in the
admitted tooling cgroup:

```sh
uv run --locked --offline python -m scripts.minimal_delegation qualify \
  --consumer qual-demo-01 --docker-context default --state-root /private/state \
  --https-port 18443 --request /private/bootstrap-request.json \
  --host-id sha256:CURRENT_BOOT_DIGEST --confirm-target qual-demo-01-local --execute
```

The gate checks positive and forbidden API operations as the exact scoped
identities. It submits one neutral Job as the launcher and verifies its
terminal success and exact Pod owner. Each workload namespace opts into `local-retained`
Filesystem RWO claims of whole 1–4Gi, with at most four and 8Gi requested per
namespace. The quotas measure requested storage. The installed filesystem
probe binds one neutral 1Gi application claim, verifies its Retain PV identity
and worker affinity, and checks a local ext4 mount. Two bounded Pods exercise
SQLite and POSIX lock exclusion with UID/GID 65532 and private 0700 directories
and 0600 files. Once the original writer exits, a replacement reads both
SQLite state and the checkpoint persisted by file fsync, rename and directory
fsync. The local-path helper gets its own bounded CPU and memory allocation.
The test also checks that an unrelated object survives unchanged.

The shared qualification deadline is 900 seconds. Individual filesystem Pod
waits are at most 90 seconds within that deadline. The private
`<target>.delegation-qualification.json` records source, target, admission,
binding, checks and outcomes. A failed gate leaves its neutral objects and
the last completed phase for diagnosis. Keep kubeconfig, raw diagnostics and
credentials outside Git. Whole-target cleanup uses the separately confirmed
[cold-bootstrap removal procedure](minimal-bootstrap.md#failure-diagnosis-and-explicit-cleanup).

## Withdrawal and retained state

Verify that the exact attempt is unused or that its Job is terminal and its
evidence has been collected. Withdraw that attempt's RoleBinding from the
workload namespace using provider authority. Select the name shown as
`launcher` in the preview, and verify the binding’s UID, subject and ownership
labels first. Keep the admission policy and template while any authorized
execution or cleanup is pending. Keep shared APIs, namespaces, Jobs under
inspection and all retained PVCs/PVs. Use a new reviewed attempt for subsequent
operations. A replacement Pod test establishes local persistence on the same
worker. Loss of that worker or the kind cluster remains a separate recovery
condition.
