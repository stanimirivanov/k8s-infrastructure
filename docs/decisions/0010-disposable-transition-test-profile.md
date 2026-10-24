# ADR 0010: Disposable memory-transition testing profile

- Status: Proposed
- Date: 2026-10-24

## TL;DR

- Add a separate `qual-`-only successor for testing the transition protocol on a
  four-CPU engine. Preserve the full-installation resource design and minimums.
- Grow two 2-CPU/4Gi network nodes to two 2-CPU/5Gi nodes; require 4 CPUs/14Gi
  engine capacity and separately reviewed live headroom.
- Explicit selection and the exact successor digest bind preview, request,
  apply, recovery and completed ownership. Never fall back automatically.
- This proves neither CPU growth nor full-stack capacity or D06 acceptance.

## Context

The current host has four logical CPUs. It cannot meet the approved full-profile
8-CPU/24Gi engine requirement. The owner requested a minimal profile for testing.
The transition/recovery protocol can be exercised independently of full-profile
capacity acceptance, provided the evidence and permitted target scope are explicit.

## Decision

Add `isolated-network-transition-test-local` to the reviewed network profiles,
with two 2-CPU/5Gi nodes and a 4-CPU/14Gi minimum engine. Its predecessor remains
`isolated-network-local` with two 2-CPU/4Gi nodes. This keeps the existing baseline
foundation and increases memory by 1Gi on both nodes. Each successor Pod envelope
is 1750m CPU/4508Mi memory. The 14Gi engine floor includes 4Gi above node caps;
it does not reserve that memory or prove host/guest headroom.

Only `qual-` consumers may construct or select the test profile. Keep the default
full successor, all existing profile bytes/digests and the v1 request/journal
format. The CLI's explicit `--successor-profile` must match the reviewed request's
successor digest. Recovery and completed ownership resolve only that recorded
reviewed digest; no arbitrary profile or changed request is adopted.

Use the existing transition phases, original/versioned backups, guarded writes,
node restarts and ownership finalization. Do not add a reduced-verification path.
All foundation-only, identity, private-state, drift and concurrency checks remain.

## Alternatives

- Lowering full-installation minimums would change its accepted design without
  workload evidence and conflate protocol proof with full-stack acceptance.
- Smaller predecessor nodes would introduce a new cold-start stability question
  while testing a separate capability. Keep the existing network predecessor.
- Per-run arbitrary budget flags would undermine reviewed profile identities.
- A second transition implementation would duplicate recovery logic and drift.

## Consequences and compatibility

The test profile and every relevant preview/result identify their limited scope.
It can prove memory-cap enlargement, policy changes, original identity/byte/backup
preservation and interrupted-operation recovery. It does not prove CPU-cap growth,
engine-capacity rebasing unless separately exercised, complete controller/app
placement, performance, backup/restore or persistent FleetIQ acceptance.

Full-profile qualification on a capable host remains required by the
[transition plan](../migrations/fleetiq-full-installation-transition.md). The test
profile cannot transition retained `fleetiq-local`, whose consumer lacks `qual-`.
It is not a production or retained installation profile.

## Security and operations

No live operation is authorized by this ADR. Existing projects' actual demand,
backing disk, host/guest memory, bootstrap peaks and an idle window must be
reviewed before an exact disposable execution request. CPU/memory caps are not
reservations. Never stop/delete other clusters or change Docker settings to make
this test fit without separate authorization. Preserve failed state; there is no
automatic cleanup, rollback, downgrade or host relocation.

## Validation

Check exact budgets/envelopes, engine-floor refusal, `qual-` scope rejection,
explicit CLI/request agreement, unchanged full defaults/digests and shared
transition/recovery behavior. Run `make validate`; native Linux shell fixtures
remain part of Linux CI. Live disposable execution and full qualification are
separate gates, not implied by mocked tests or profile selectability.
