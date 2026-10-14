# ADR-0002: Provide resource metrics with verified TLS and explicit certificate operations

- Status: Proposed
- Date: 2026-10-14
- Milestone: M04 — Kubernetes Benchmark Decision Loop
- Deciders: Repository owner
- Supersedes: None
- Superseded by: None

## TL;DR

- Package a reusable Metrics Server chart; consumers own their Argo CD selection.
- Verify both aggregation and kubelet TLS; do not introduce a kind-only bypass.
- Keep serving bootstrap, individual CSR approval and certificate provisioning explicit.
- Do not deploy this unmerged provider or put placeholder revisions/CAs in consumer selection.

## Context

The local Kubernetes benchmark qualification correctly reports interference UNKNOWN when the
resource Metrics API is absent. Five other passing checks do not establish resource usage.
The existing kind kubelets select local serving-certificate files without serving bootstrap.
Installing a default scraper and disabling certificate verification would hide a missing trust
boundary, not complete qualification. The owner chose verified TLS with explicit certificate setup.

## Decision

Own project-neutral packaging in `k8s-infrastructure`. The consumer selects the merged immutable
revision, workload namespace/placement and public serving CA in a narrowly scoped Argo CD
Application/Project. Neither the infrastructure bootstrap nor the operator certificate CLI owns
consumer Applications. No competing Helm install or broad grant to the Perfeng workload project
is introduced.

Use the official Metrics Server 0.9.0 image with its observed registry manifest digest and disclose
its coarse admission-only purpose. The scraper trusts the cluster CA for kubelet serving leaves.
The APIService trusts an explicit, independent serving CA. The chart creates neither private
material nor an insecure aggregation route. Non-root/read-only execution and bounded resources
are part of the rendering contract. One installation per cluster is supported.

For the reviewed existing kind cluster, provide opt-in commands to preserve and patch only the
kubelet serving-TLS configuration, restart one kubelet at a time, inspect CSRs and approve exactly
one named request after checking its authenticated node identity, signature, server-only usages,
freshness and current address SANs. Recheck node/container incarnation before effects and preserve
the API resource version/UID on approval. Do not install an always-on CSR approver.

Generate the Metrics Server serving Secret once in memory, pass private bytes only through stdin,
and expose only public CA/Secret-name values to consumer selection. Existing owned immutable
material is validated/reused without rotation. The signing key is discarded; certificate renewal
and CA/Secret replacement are explicit operator work, not automatic repair.

## Alternatives and consequences

- `--kubelet-insecure-tls` or aggregation verification bypass: easier on kind but explicitly rejected.
- A permanent CSR approver/cert-manager: useful for unattended PKI but adds controllers and approval
  policy beyond this local prerequisite. No automatic serving-CSR approval is introduced here.
- Full monitoring stack first: broader than a coarse admission prerequisite; M05 still owns broad
  observability. Metrics Server is not a benchmark measurement source.
- Recreate the cluster with a new TLS profile: risks retained state and is unnecessary for the
  reviewed existing cluster. Preserve original node-local files and apply an explicit rollout.

This introduces a small certificate-handling dependency and maintenance duty. Kubelet rotation
requires new reviewed approvals, and serving certificates expire; the operating guide defines
diagnosis, refusal and explicit renewal. Shared Docker-host contention remains outside the
metrics capability's proof. Node/cluster administrators and exclusive operator maintenance are
trust assumptions, not threats solved by CSR input validation.

## Compatibility, rollout and rollback

Existing kind profile, infrastructure lock, consumer releases and runtime bundle bytes remain
unchanged. Merge this provider before consumer selection. The follow-up consumer PR must pin the
real merge revision and public CA, reconcile the chart and prove fresh node metrics before
benchmark qualification/promotion resumes. No synthetic CA or future commit SHA is deployable.

Partial certificate operations retain backups/Secrets for safe inspection. Never force replacement,
bulk-approve CSRs or recreate the cluster on failure. Application rollback preserves TLS material,
node backups, volumes and other workloads. Node rollback is a separate reviewed file restoration
and restart, not a hidden compensation step.

## Validation

Run `make validate`, including strict Helm schema/lint/render checks on Windows and Linux,
CSR/certificate tests, private-stdin and ownership refusal tests, and interrupted-rollout tests.
Live rollout, signer issuance and scrape TLS still require explicit acceptance on the named
cluster; mocks and a rendered chart do not prove those boundaries.
See the [operating guide](../resource-metrics.md).
