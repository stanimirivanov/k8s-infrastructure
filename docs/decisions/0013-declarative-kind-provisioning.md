# ADR-0013: Declarative kind provisioning for new registered targets

- Status: Proposed
- Date: 2026-11-26
- Related work: Infrastructure #74 and Environment #434

## TL;DR

- Provision new registered kind targets with a pinned OpenTofu and kind provider.
- Keep a canonical private state path and a dedicated kubeconfig for each target.
- Bind the pinned Argo CD installation to the independently observed live UID.

## Context

Infrastructure needs a reviewed plan and an explicit resource inventory for
fresh local kind clusters. Existing imperative profiles already own their
targets. The new workflow uses the laptop's existing Docker daemon. Its proof
includes a capacity check because overlapping kind clusters exhausted the
host's inotify instance budget during qualification.

## Decision

Use the checksum-verified OpenTofu 1.13.10 and kind provider 0.0.4 combination
for the registered Linux AMD64 profiles. Use the checked-in read-only
dependency lock and a packed filesystem mirror. Verify the Docker endpoint,
exact plan and state before apply. Give each target one writer, separate
locked state, and an exact context. The existing imperative profiles remain
available for their registered targets.

Record the live Kubernetes UID after provisioning. Argo CD bootstrap verifies
that UID and applies the pinned installation declaratively with the
infrastructure field manager. Consumers own application definitions and
release selection. Preserve state and evidence that must survive in encrypted
off-host storage.

## Alternatives

The imperative kind command remains suitable for the profiles it already
owns. A matching offline Terraform comparison showed that either CLI could
read the selected archive. OpenTofu was selected for the qualified path
following the project owner's preference for an open-source implementation.
A separate VM was considered for capacity isolation. The existing Docker
daemon passed the live checks with one three-node cluster at a time.

## Consequences and verification

The pilot can be reviewed, applied and retired through separate saved plans.
Retained retirement and import need their own reviewed workflows. A consumer
registers a distinct profile before selecting an immutable provider revision.
The qualification covered node placement, DNS, PVC read/write, a zero-change
repeat plan and repeatable Argo CD bootstrap. Infrastructure changes and any
recovery are explicit operator actions.

[Infrastructure #74](https://github.com/stanimirivanov/k8s-infrastructure/issues/74) ·
[Environment #434](https://github.com/stanimirivanov/perfeng-environment/issues/434)
