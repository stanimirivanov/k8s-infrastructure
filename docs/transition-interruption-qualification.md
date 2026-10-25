# Disposable transition interruption qualification

- Status: Tooling available; live interruption/recovery matrix pending

## TL;DR

- Use `scripts.consumer_transition_drill` only on a separately approved, fresh
  `qual-` predecessor. Never inject failure into `fleetiq-local` or reuse the
  already-completed memory-transition target.
- Preview is offline. `interrupt --execute` stops after one selected successful
  effect, retaining the journal, original nodes and backups. It does not recover.
- Inspect the stopped target; obtain separate recovery approval; use the ordinary
  `scripts.consumer_transition recover` command with the same request and profile.
- Each matrix case needs its own original predecessor and operation. No automatic
  deletion, downgrade, history reset or resource reclamation is included.
- Minimal memory-transition evidence does not remove the eight-CPU/24Gi engine
  gate for the approved full installation or authorize Stage B/D06.

## Evidence and remaining gates

The 2026-10-25 operator-authorized minimal run used provider revision
`fe0da10fe630c961f7f18cc0fd4e1ccf32be43aa`. Its protected receipt remains with
operator state, outside Git; it is not a portable adoption or promotion record.

| Evidence | Status and limit |
|:--|:--|
| Both original nodes enlarged from 2 CPUs/4Gi to 2 CPUs/5Gi | Passed; memory growth only |
| Original identity, config/access bytes and repair/versioned backups | Preserved and verified |
| Completed recovery idempotence | Passed before scheduler fixtures changed the namespace inventory |
| Positive controls and CPU/memory over-envelope rejection on both nodes | Passed with 1750m/4508Mi envelopes; probe Pods UID-deleted |
| All seven deliberately interrupted native boundaries and subsequent explicit recovery | Pending live matrix below; offline fixtures are not live evidence |
| CPU enlargement and engine-capacity rebasing | Pending on a capable, separately approved target |
| Fresh full-profile and complete provider/consumer composition | Pending; minimum engine eight CPUs/24Gi plus the approved host/storage ledger |
| Retained FleetIQ transition and Stage B activation | Pending separate relevant qualification, eligibility, handoff and approvals |

The four-CPU local host cannot satisfy the approved full-profile CPU gate by
freeing memory or changing kubelet reservations. Host selection/relocation or a
separately reviewed resource-design change is required; this PR does neither.
See the [full-installation gates](migrations/fleetiq-full-installation-transition.md).

## Fixed checkpoint matrix

Each stop occurs after the named command/write returns successfully and before
its durable intention is advanced. It is a controlled exception, not a process
kill, power loss, partial disk write or engine failure. The normal transition CLI
has no interruption flag or environment variable; callbacks are local to this
explicit drill execution.

| `--checkpoint` | Retained journal at stop | Recovery evidence required |
|:--|:--|:--|
| `control-plane-cap` | nodes / control-plane cap-intent | Recognize approved cap with predecessor config; continue safely |
| `control-plane-write` | nodes / control-plane write-intent | Recognize exact successor config and original/versioned backups |
| `control-plane-restart` | nodes / control-plane restart-intent | Revalidate identity/readiness; at most one restart per unverified node per recovery execution |
| `worker-cap` | nodes / worker cap-intent | Preserve verified control-plane node; resume worker only |
| `worker-write` | nodes / worker write-intent | Preserve control plane and recognize worker successor config |
| `worker-restart` | nodes / worker restart-intent | Preserve control plane; resolve the uncommitted restart completion |
| `owner-commit` | finalizing / both nodes verified | Recognize exact successor owner bytes and complete journal without repeating node mutations |

The command reports `interrupted-as-requested`, with live recovery still pending.
Exit zero means only that the selected boundary was reached with the expected
journal/ownership state. Every unrelated native/validation failure exits nonzero;
never relabel it as an expected interruption. Save sanitized output privately.

## Prepare and approve one case

1. Select a new `qual-` consumer, exclusive loopback HTTPS port and protected
   durable state root. The successful earlier target has completed history and
   scheduler fixtures: do not reset or downsize it to make another predecessor.
2. Review current host/guest headroom, physical backing storage, other projects,
   temporarily uncapped bootstrap and the selected profile's engine floor.
   Reserve an idle/quiesced window. Review one live case at a time; retaining
   evidence does not authorize accumulating clusters beyond available capacity.
3. Prepare the ordinary [transition request](retained-profile-transition.md#step-2-prepare-the-reviewed-request)
   from original IDs/UIDs, capacity observations and config hashes after separately
   authorized predecessor creation. Use a unique operation ID and clean merged
   provider revision. Keep both old/new capacity objects strictly CPU/memory-only.
4. Preview with the exact intended target and checkpoint:

```sh
uv run --locked --offline python -m scripts.consumer_transition_drill preview \
  --consumer qual-case --docker-context local-engine \
  --state-root ../qual-case-private --https-port 18445 \
  --successor-profile isolated-network-transition-test-local \
  --checkpoint control-plane-cap
```

5. Obtain exact named-target approval for private preflight/backup/config access,
   approved cap growth, bounded kubelet restarts, the selected deliberate stop,
   retained partial state and agreed stop conditions. Normal production guards
   still apply. A CLI confirmation flag is not human authorization.
6. Only after approval, repeat the preview command with action `interrupt`, plus
   `--request ../qual-case-private/request.json --execute
   --confirm-quiesced qual-case-local`. Without `--execute`, even `interrupt`
   remains an offline preview and never reads the request or private state.

The drill refuses any existing transition journal or lock, including completed
history; no guard file is automatically removed. If the chosen effect cannot be
reached, preserve state and investigate; do not retry against that history.

## Inspect and separately approve recovery

1. Retain all files and nodes. Inspect using the ordinary transition CLI with the
   same exact request and explicit successor selection:

```sh
uv run --locked --offline python -m scripts.consumer_transition inspect \
  --consumer qual-case --docker-context local-engine \
  --state-root ../qual-case-private --https-port 18445 \
  --successor-profile isolated-network-transition-test-local \
  --request ../qual-case-private/request.json --execute
```

2. Prepare a new exact recovery approval from the recorded phase, node caps,
   config/backup hashes, original identities, foundation readiness and current
   headroom. Include any possible repeated restart at the ambiguous boundary.
   Do not rewrite owner/request/journal, remove guards or automatically recover.
3. After approval, use the same ordinary command with action `recover` and
   `--confirm-quiesced qual-case-local`. It retains the original transition guards;
   there is no qualification-specific recovery implementation or bypass.
4. Verify final owner/profile, original identities, config/access bytes,
   repair/versioned backups, selected allocatable and all foundation readiness.
   For a worker/owner stop, prove the verified control plane was not restarted or
   reconfigured. Record before/after kubelet start markers and private file hashes.
5. Prove completed recovery idempotence before creating scheduler fixtures.
   Namespace/controller identities are frozen by the journal; adding the probe
   namespace changes that inventory and prevents a later completed-recovery
   check from representing an unchanged foundation.
6. Run [scheduler qualification](capacity-qualification.md) only under its separate
   approved scope. Preserve its non-preempting PriorityClass and namespace, and
   UID-delete successful Pods as specified. Any whole-cluster removal or later
   case requires its own named-target approval and resource review.

No interruption, recovery or complete-installation acceptance follows from
merging this tooling. Keep each matrix result, source revision, exact request
hash and checkpoint distinct; a completed-success receipt cannot fill a missing
interrupted-recovery row.

## Offline verification and troubleshooting

`make validate` exercises the fixed boundary callbacks and explicit ordinary
recovery for both reviewed successors using realistic Kubernetes capacity maps.
Existing Linux CI additionally checks real private backup/write/symlink scripts.
Neither suite replaces the live matrix or eligible-host/full-stack gates.

If a native operation fails before the selected callback, the command withholds
private details and exits nonzero. Inspect the exact target through the normal
procedure; the durable journal, not the planned checkpoint, determines recovery.
If a callback/journal assertion fails after an effect, retain all state and
prepare a separate investigation. Never add an environment override, broaden
accepted hashes or delete history to make qualification pass.
