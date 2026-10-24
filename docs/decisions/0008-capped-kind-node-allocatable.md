# ADR 0008: Scheduler envelopes for capped isolated kind nodes

- Status: Proposed
- Date: 2026-10-24

## TL;DR

Use kubelet reservations to exclude host capacity inaccessible to a capped kind
node, plus explicit daemon/eviction headroom. Enforce the resulting scheduler
allocatable contract. Existing-target repair and resource enlargement remain
separate, approved operations; do not patch Node status or recreate retained nodes.

## Context

Docker quotas were enforced but Kubernetes advertised the shared VM's capacity
on each node. A non-empty allocatable sensor could accept grossly inflated
scheduling capacity. Full Argo plus FleetIQ also exceeds the current worker's
memory envelope, independently of this reporting defect.

## Decision

For isolated consumers only, exclude the raw-machine/container gap using
systemReserved, reserve 250m/512Mi for non-Pod daemons through kubeReserved,
set explicit 100Mi memory eviction and enforce on Pods. Raw capacity remains truthful
as a machine observation; handoffs identify it separately from the verified Pod
envelope. Validate exact quantities without float rounding.

Configure fresh nodes after enforcing Docker limits and before CNI/readiness.
Offer an explicit owned-target repair command for existing nodes. Preflight
both configurations before writes, refuse unknown reservation/manager policy,
retain original node-local bytes and hash-guard atomic replacement. Recheck
UIDs and original ownership before each restart; stop on partial failure.

Preserve original profiles and ownership records. A reviewed larger profile is
proposed in the operating guide; adding/selecting it and migrating persistent
resources are subsequent work. No engine resize, automatic profile switch,
implicit target replacement or consumer deployment follows from this ADR.

## Alternatives

- Patching Node status competes with kubelet and fabricates a physical capacity claim.
- Docker limits alone preserve the scheduler overcommit defect.
- Reserving a constant amount without observing machine capacity remains wrong
  when Docker Desktop's memory or CPU configuration changes.
- Silently mutating old health/ensure operations violates explicit recovery.

## Consequences and compatibility

Health now refuses inflated legacy allocatable values. Existing identities are
still recognized, but require separately authorized repair before readiness can
pass. Changed engine capacity or foreign reservations require explicit review.
A known partial restart can resume without rewriting an already-correct config;
stale backups/temporary files need operator inspection, never automatic removal.

## Security and operations

Only recorded node IDs, original context/incarnation and UID-bound targets may
be written. Configs/backups remain private inside nodes. No consumer credentials,
Node status, host settings, Perfeng resources, Docker caps or storage ownership
change. Kubelet restarts are disruptive and need a named target and idle window.

## Validation

Pure accounting and refusal regressions, mocked ownership/race/recovery tests,
Linux real-shell atomic-write fixtures and the existing repository gates.
Live disposable qualification and retained repair remain separately authorized;
offline tests do not prove live scheduler enforcement or complete-stack capacity.
