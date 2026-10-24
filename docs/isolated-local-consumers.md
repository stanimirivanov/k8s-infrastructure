# Isolated local consumer clusters

- Status: Base capability; live acceptance must be reported separately
- Profile: `local-kind/isolated-local`

## TL;DR

- Use this profile for FleetIQ and future projects; the existing Perfeng path is
  unchanged and `perfeng` remains a reserved consumer ID.
- Supply consumer ID, local Docker context and a private state root explicitly.
- Preview is offline. Every live action requires `--execute`; removal additionally
  requires the exact target name and permanently destroys cluster-local data.
- Base readiness does not qualify NetworkPolicy, ingress/TLS, controllers,
  persistent storage, recovery or physical-host performance isolation.
- Run `make verify` and `make validate`; authorize a concrete disposable target
  separately before claiming live qualification.

## Selection and ownership

The provider serves multiple projects. This first neutral profile uses separate
clusters because Perfeng's generator/SUT workers are reserved. A consumer `fleetiq`
gets cluster `fleetiq-local`, context `kind-fleetiq-local` and three files under
the selected state root: `.local/fleetiq-local.kubeconfig`,
`.local/fleetiq-local.kind.yaml` and `.local/fleetiq-local.owner.json`.

The owner record binds profile identity, creation nonce, selected Docker
context/endpoint, exact node IDs and observed cluster incarnation. A matching
name or kubeconfig alone never grants adoption. Ordinary health and handoff
observe only; ensure reuses only a complete healthy identity or creates a fully
absent target. It never repairs a partial or foreign target implicitly.

State roots must be operator-private ordinary directories. Linked paths and
junctions are refused. Files use mode 0600 and the state directory mode 0700;
on Windows the operator must also provide an appropriately restricted inherited
ACL. No global kubeconfig or credential store is changed. Do not commit owner
records or kubeconfig bytes, and do not copy another consumer's credentials.

## Profile and resource envelope

The checked-in profile pins kind `v0.31.0`, Kubernetes `v1.35.0` by node-image
digest, Linux/amd64 nodes, loopback API access and dynamic API port selection.
One control plane and one worker carry `k8s-infrastructure.io/consumer` and
`k8s-infrastructure.io/role` labels. No workload namespace, host mount or
application host port is provisioned.

Creation requires a Linux Docker engine with at least four CPUs and 12 GiB RAM.
After kind bootstrap, the owned control plane is limited to one CPU/2 GiB,
the worker to two CPUs/4 GiB, and each to 2048 PIDs with no container swap.
These limits are inspected before success. Existing or unrelated containers are
never updated. Initial kind bootstrap precedes these limits and can transiently
use more resources; coordinate an idle host window before execution.

Select `--profile isolated-argo-local` explicitly for the delegated Argo capability:
its control plane uses two CPUs/4 GiB and its worker two CPUs/4 GiB (4 CPUs/8 GiB
total). Native I02 testing found that the smaller base control plane failed
liveness during Argo OpenAPI schema discovery. Both profiles keep the same engine
minimum, node-image pin, PID and swap restrictions. The profile name and budget
digest are part of the ownership record: switching profiles refuses existing
state rather than resizing/adopting it. Recreate only an explicitly authorized
disposable target; an existing consumer needs a separately reviewed migration.

Engine minimums do not prove free host capacity. Separate clusters share the
engine, kernel, storage and host. They do not isolate Perfeng benchmark timing.
The handoff reports raw machine capacity, Docker budgets and a verified safe
scheduler envelope separately. New nodes configure kubelet reservations before
readiness; health rejects inflated legacy allocatable values. Retained nodes need
an explicitly approved [capacity repair](node-capacity.md), never implicit ensure
repair. Consumer workload budgets and placement still need their own acceptance.

## Full-installation profile

`isolated-network-full-local` implements the approved larger resource design for
**fresh targets only**, using the same pinned networking/database/Argo assets as
the existing network profile. It requires explicit `--https-port` and keeps the
same loopback-only API/HTTPS bindings, PID limit and no-swap policy.

| Resource | Control plane | Worker |
|:--|--:|--:|
| Docker cap | 4 CPUs / 6Gi | 4 CPUs / 14Gi |
| Verified Pod envelope | 3750m / 5532Mi | 3750m / 13724Mi |

Creation refuses engines below eight CPUs/24Gi before reserving state or creating
nodes. The operator must additionally verify the
[host prerequisites and transition gates](migrations/fleetiq-full-installation-transition.md):
32Gi physical RAM, 24Gi actually available, 30Gi free backing storage and an idle
window. The runtime engine minimum is not a free-host-capacity sensor. Bootstrap
is temporarily uncapped, as for existing profiles. Live full-stack capacity and
performance qualification are pending; health proves the declared foundation
checks and explicitly reports full-installation acceptance as unqualified.

The new profile is accepted by consumer lifecycle/capacity, network, database and
delegated Argo commands. Capability health and handoff still require their own
installation/observation; selecting the profile installs neither Argo nor a
consumer database or workload. No controller version, authority or workload
resource limit changes follow from the larger node budgets.

Use the following **offline** preview with a prepared private state directory:

```sh
uv run --locked python -m scripts.consumer_kind up \
  --profile isolated-network-full-local --consumer example \
  --docker-context default --state-root ../example-private-state --https-port 18443
```

Existing state with `isolated-network-local` must refuse this selection. Do not
edit its owner record, remove backups or recreate the target. The explicit
retained transition is a [separate explicit capability](retained-profile-transition.md)
with live qualification pending. Any execute,
cleanup, engine resize or qualification needs exact-target authorization.

## Offline preview

For policy-enforcing networking and HTTPS, select the separately qualified
[`isolated-network-local` profile](networking-and-ingress.md) with an explicit
`--https-port`. It disables the default CNI/kube-proxy only on a fresh target;
it cannot migrate/adopt an existing profile or retained Perfeng cluster.

Create an operator-private state directory outside Git first, then select it:

```sh
uv run --locked python -m scripts.consumer_kind up \
  --profile isolated-local --consumer fleetiq \
  --docker-context default --state-root ../consumer-state
```

Use the exact local socket context configured on your host; `default` is an
example selection. Preview reads checked-in configuration and validates paths,
but starts no process, reads no kubeconfig/owner record and creates no state.
It returns the kind configuration, resource envelope and requested operation.
Other preview actions are `ensure`, `health`, `handoff` and `down`.

## Explicit execution and handoff

After reviewing the exact target and obtaining execution authorization, repeat
the selected command with `--execute`. SSH/TCP Docker endpoints are rejected;
ambient Docker/Kubernetes redirections cannot override the explicit selection.
External commands inherit the existing bounded private-output adapter.

`health --execute` and `handoff --execute` require the original owner record,
endpoint, node IDs, kubeconfig and cluster incarnation to agree. They verify
node version/placement/readiness/pressure, container limits and CoreDNS rollout.
Creation allows up to 90 seconds for the complete node checks to converge after
Ready: bootstrap taints can disappear later than the Ready condition. The final
checks are unchanged; ordinary health/handoff never mask a failed observation
by waiting for recovery.
They report public identity/capacity, provider Git revision/dirty flag and
explicitly unqualified capabilities. Dirty or unmerged source observations
must not be promoted as a reviewed provider release.
When executing a verified Git archive, no provider `.git` directory exists;
source identity is marked `caller-required` rather than discovering a parent
repository or failing after cluster creation. The consumer must bind its
independently verified immutable selection to that observation.

For a disposable target, authorize `down --execute` with
`--confirm-delete fleetiq-local` only after inspecting its preview. Removal
verifies identity, deletes only its recorded node container IDs and anonymous volumes, proves no named node
containers remain and removes only its three state files. Other consumer state
is retained. This is not the cleanup path for retained application data.

## Failure and recovery

- Existing cluster or state: stop and select its original owner/state root.
  Creation will neither replace it nor regenerate access credentials.
- Wrong endpoint, node IDs, incarnation or configuration: stop; investigate the
  mismatch without relabeling, resetting the record or copying credentials.
- Failed creation: partial resources and state remain for bounded inspection.
  Health/ensure cannot promote them into a healthy identity. Confirmed removal
  works only when node IDs and incarnation were already positively observed;
  earlier failures require a separately reviewed recovery of the partial target.
- Interrupted deletion: state remains `deleting`. An exact retry verifies the
  endpoint, resumes only recorded immutable IDs that remain, and refuses
  replacement containers. Already absent IDs are not removed again.
- Unavailable API: starting removal cannot bypass incarnation verification. An
  already committed `deleting` journal can finish its verified immutable IDs
  without a functioning API; otherwise recover the owned control plane first.
- Resource/pressure failure: stop before consumer installation. Do not raise
  limits, consume reserved Perfeng capacity or replace a retained cluster as a repair.

## Capability progression

This I01 base uses kind's default networking and establishes no policy enforcement
claim. I03 must introduce and qualify an explicit policy-capable profile before
network isolation or trusted ingress can be consumed. It must not replace the
CNI of an existing retained target implicitly. The [I02 delegated Argo capability](delegated-argo.md)
adds separately qualified controllers/release authority; I04 qualifies storage/database interfaces. Consumers then qualify
their own installation and product behavior. See [ADR 0004](decisions/0004-isolated-local-consumer-profiles.md).
