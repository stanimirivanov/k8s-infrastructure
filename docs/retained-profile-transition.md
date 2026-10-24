# Retained network-profile transition

- Status: Implementation available; live disposable qualification pending
- Supported pair: `isolated-network-local` → `isolated-network-full-local`

## TL;DR

- Preview offline first. Execute only after the exact target, idle/quiesced window,
  private-file access, node enlargement and restart scope are approved.
- Use `scripts.consumer_transition`; changing `--profile` or editing owner state
  is not a migration. This command never resizes/restarts Docker or replaces nodes.
- The engine must already meet eight CPUs/24Gi. Reconcile other clusters' capacity
  effects separately; this command does not grant engine-change authority.
- Preserve the private journal and both original/versioned backups on failure.
  Review the recorded phase before explicitly using `recover`.
- This tooling is not live qualification or Stage B/D06 acceptance. Follow the
  [full-installation transition plan](migrations/fleetiq-full-installation-transition.md).

## Preconditions and inputs

Use a clean checkout of the exact reviewed merged Git revision. Archive-only
source provenance is not supported by this first transition command; it refuses
unbound or dirty source. The predecessor must have an existing ready ownership
record, original nodes/incarnation and the already-corrected predecessor capacity
policy. The policy can have been derived before an explicitly reviewed engine
change; do not infer its prior capacity from the current config.

The target must be foundation-only and scheduling/configuration quiesced. Argo,
application controllers/Pods and CNPG database desired state are refused. This
initial transition also refuses all PV/PVC, StatefulSet, Job and CronJob objects.
Cilium, Envoy, Cilium operator, CoreDNS and local-path provisioner must exist;
all observed foundation controllers and both uncordoned nodes must be Ready. Their UIDs/generations and
namespace UIDs are held stable; changing Pod identities on restart is expected.
Observations are not an atomic scheduling or configuration admission lock.

Use the operator-private existing state root outside Git, with an appropriate
Windows ACL when applicable. Do not copy it from another cluster. The following
relative paths remain under that root:

| File | Purpose |
|:--|:--|
| `.local/<consumer>-local.owner.json` | Existing claim; only final ownership replacement changes it |
| `.local/<consumer>-local.kind.yaml` | Preserved byte-for-byte; both profiles use the same kind configuration |
| `.local/<consumer>-local.kubeconfig` | Preserved byte-for-byte; never include contents in evidence |
| `.local/<consumer>-local.transition.json` | Private durable request, predecessor owner, hashes and phase history boundary |
| `.local/<consumer>-local.transition.lock` | Retained OS-lock file; process death releases the lock, not the file |

The journal preserves the predecessor owner record and current phase/stages;
it is not an append-only event log. Node configuration bytes stay inside their
original nodes. Never commit the request, journal, owner or access files.

## Step 1: Preview without private reads

Substitute the reviewed consumer, local Docker context, existing private state
root and original HTTPS port. The example uses a neutral target:

```sh
uv run --locked python -m scripts.consumer_transition preview \
  --consumer example --docker-context default \
  --state-root ../example-private-state --https-port 18443
```

Without `--execute`, every action is a pure preview: no subprocess, request/owner
read, download, lock file or cluster mutation. It reports the exact profile pair,
digests, engine minimum and scheduler envelopes. Selecting a different listener,
consumer/context/state root or profile digest requires a new review.

## Step 2: Prepare the reviewed request

Create a UTF-8 JSON file outside Git in the protected state root. Its required
shape is below; replace every angle-bracket placeholder with independently
reviewed evidence. Placeholders intentionally fail validation.

```json
{
  "format": "isolated-profile-transition.v1",
  "operationId": "<unique 32 lowercase hex characters>",
  "providerRevision": "<exact clean provider Git commit, 40 lowercase hex>",
  "predecessorDigest": "<predecessor profileDigest from preview>",
  "successorDigest": "<successor profileDigest from preview>",
  "incarnation": "<original kube-system namespace UUID>",
  "nodes": {
    "control-plane": {
      "containerId": "<original 64 lowercase hex container ID>",
      "uid": "<original Kubernetes node UUID>",
      "oldConfigSha256": "<reviewed predecessor config SHA-256, 64 lowercase hex>",
      "oldMachineCapacity": {"cpu": "<recorded prior quantity>", "memory": "<recorded prior quantity>"},
      "newMachineCapacity": {"cpu": "<current stable quantity>", "memory": "<current stable quantity>"}
    },
    "worker": {
      "containerId": "<original 64 lowercase hex container ID>",
      "uid": "<original Kubernetes node UUID>",
      "oldConfigSha256": "<reviewed predecessor config SHA-256, 64 lowercase hex>",
      "oldMachineCapacity": {"cpu": "<recorded prior quantity>", "memory": "<recorded prior quantity>"},
      "newMachineCapacity": {"cpu": "<current stable quantity>", "memory": "<current stable quantity>"}
    }
  }
}
```

Sources: the original protected ownership record provides incarnation/container
IDs; original Kubernetes observations provide node UIDs and prior raw capacity.
Current public Node observations provide new raw capacity. Use the exact original
container's `/var/lib/kubelet/config.yaml` SHA-256 from an explicitly authorized
bounded inspection. Do not export its contents. Both capacities use Kubernetes
CPU/memory quantity strings; old capacity is evidence, not a guessed host size.

The request permits only the reviewed profile pair and binds both digests,
source, distinct IDs/UIDs and config hashes. Any changed request requires a new
review; `recover` requires the identical request saved in the journal.

## Step 3: Obtain read-only preflight approval and inspect

Obtain authorization for the exact target and bounded private reads, including
original repair-backup hashes. Then run:

```sh
uv run --locked python -m scripts.consumer_transition inspect \
  --consumer example --docker-context default \
  --state-root ../example-private-state --https-port 18443 \
  --request ../example-private-state/transition-request.json --execute
```

Initial inspection checks both configs and all operation-specific backup/temp
paths, original owner/endpoint/incarnation/IDs, stable new machine capacity,
engine minimum, source revision, foundation inventory and workload requests.
It verifies the known predecessor policy and computes desired hashes without
creating a journal or changing the cluster. `readyForApproval` means those
preflight checks passed, not that live qualification or execution is approved.
Host free memory/disk and other project demand remain separately reviewed gates.

For a pending journal, inspection holds the existing OS lock and reports its
phase/stages after the known phase-specific identity, caps, hash and backup
checks; it does not retry work. A partially created backup at the backup phase
can still require further private inspection before recovery is approved.
A missing/malformed lock or journal is a stop condition. Initial inspection does
not create a lock; apply creates the private lock before its repeated preflight.

## Step 4: Approve and execute the bounded transition

After disposable qualification and review of the exact request hash, backup
references, identities, capacity, private state and disruption scope, obtain
separate execution approval. Confirm the idle window and quiesced scheduling
and configuration. The confirmation flag is an operational assertion, not a
replacement for approval.

```sh
uv run --locked python -m scripts.consumer_transition apply \
  --consumer example --docker-context default \
  --state-root ../example-private-state --https-port 18443 \
  --request ../example-private-state/transition-request.json \
  --confirm-quiesced example-local --execute
```

The command preflights both nodes, durably reserves the journal and backs up both
configs before changing either cap. Original repair backups at
`/var/lib/kubelet/config.before-allocatable.yaml` are hash-bound and never written.
New private backups use
`/var/lib/kubelet/config.profile-transition-<operationId>.before.yaml`; staged
writes use the corresponding `.next` path. Symlinks, unknown hashes and stale
paths refuse; no guard file is automatically removed.

Control plane then worker: journal cap intent, enlarge only the original
container's CPU/memory/no-swap budget, verify it, journal write intent, replace
the config with a hash guard, journal restart intent, restart that kubelet, then
verify allocatable, Ready/pressure and foundation readiness before advancing.
Each execution issues at most one restart per unverified node. A later explicit
recovery may repeat a restart whose completion was not durably recorded.

Finalization proves both full envelopes and foundation readiness, journals the
commit boundary, atomically replaces only the owner record, then marks completion.
The original nonce, endpoint, container IDs, incarnation, kind/access bytes and
predecessor owner evidence survive. Full-stack acceptance remains pending.

## Failure and explicit recovery

A failed operation retains state and stops. Ordinary health, ensure, capacity
repair and removal refuse an unfinished journal, even if owner replacement
already occurred. Do not rerun `apply`, switch profile flags, alter owner state,
delete locks/backups, force a restart, recreate nodes or clear private state.

Use the same request and `inspect --execute` to prepare the exact recovery scope.
After separate approval, repeat Step 4 with action `recover`. Recovery accepts
only known phase-specific caps/config hashes, exact original identities and
stable capacity. A completed staged file may be renamed only when its exact
recorded hash matches; partial/foreign bytes stop for further review. Original
and versioned backups must still match. A previously verified node losing Ready
or allocatable blocks work on the next node.

Interruption after owner replacement is recognized from the exact predecessor
or successor owner bytes at the recorded finalization boundary. Recovery verifies
the full target before completing the journal; it does not repeat node changes.
Completed recovery is read-only/idempotent and still verifies the target.

Corrupt/truncated journals or partial backup bytes require a separate recovery
proposal preserving all evidence. No automatic reconstruction or rollback is
provided. If validation cannot explain the state, stop rather than broadening
accepted hashes. Rollback/downsize needs a separate demand and safety assessment.
Completed journal/lock files remain as history even after separately authorized
cluster removal; a future reused name needs an explicit state-history decision.

## Verification boundaries

`make validate` exercises pure request/policy checks, mocked native interruptions,
private real-file ownership finalization and platform locking. Linux CI also
executes actual shell backup/write/symlink fixtures. These tests do not establish
live scheduler enforcement, full-stack pressure/throttling, trusted ingress or
recovery on the persistent cluster. Disposable qualification and the D06 consumer
handoff remain subsequent work under the transition plan.
