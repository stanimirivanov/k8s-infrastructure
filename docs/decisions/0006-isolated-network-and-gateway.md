# ADR 0006: Fresh-profile Cilium networking and consumer-owned HTTPS routes

- Status: Proposed
- Date: 2026-10-20

## TL;DR

- Select a new `isolated-network-local` target; never replace a retained CNI.
- Use pinned Cilium 1.20.3, its Envoy-based Gateway API implementation, and
  Gateway API 1.6.3. Helm renders upstream packages; infrastructure owns schemas.
- Publish one explicitly selected loopback HTTPS port; consumers own Gateways,
  routes, policies, hostnames and certificate Secret references.
- Qualify real traffic and verified TLS separately from controller readiness.
- Do not export CA keys, install host trust, expose admin endpoints or infer
  production isolation/recovery from a disposable local test.

## Context

The existing isolated profiles use kind's default CNI. Creating NetworkPolicy
objects there does not prove enforcement. A retained-cluster CNI or kube-proxy
replacement changes connectivity, state and ownership and needs maintenance
planning. FleetIQ and later consumers need a reusable networking/trust interface,
with the same Kubernetes routing technology available for future production use.

## Decision

Add a separately selected fresh-cluster profile. Disable kind's default CNI and
kube-proxy at creation, retain the reviewed node image, and bootstrap Cilium
before base node readiness. Keep existing profile configuration and ownership
digests unchanged. Bind the chosen host port into the new owner record and kind
configuration. Inspect actual Docker port bindings: the API and HTTPS listener
must be exclusively loopback. An occupied listener fails; no automatic alternate
port or existing-cluster migration is permitted.

Use Cilium's supported CNI, policy engine and Gateway API controller with its
Envoy data plane, rather than another independent Gateway controller. Pin the
official chart, all 22 Cilium CRDs and the seven required standard Gateway CRDs
by SHA-256. Verify bytes before parsing/executing, render Helm locally and apply
with an explicit field manager. Disable operator CRD creation and remove its
unused schema-writing permissions. Existing APIs require matching provider
ownership and normalized schemas. Changes to the selected releases or values
require an explicit compatibility transition, never forced conflicts.

The local topology has two nodes at two CPUs/4 GiB each. Two Cilium agents, two
Envoy agents and one operator request 350m CPU/768 MiB in total. Exact limits,
replica counts and upstream image digests are checked in rendered output. These
are controller allocations, not a promise of free capacity for every consumer
combination. Physical host, kernel, storage and Docker engine remain shared.

The Gateway uses host networking only on the consumer worker. Its port 8443 is
published to an explicitly chosen host port on 127.0.0.1. No port-forward is used
as ingress and no administrative HTTP listener is published. Consumers supply
HTTPS Gateways, HTTPRoutes, hostnames and same-namespace TLS Secret references.
Use `allowedRoutes.namespaces.from: Same` unless a separate delegation is reviewed.
Cross-namespace backend or certificate references need an explicit consumer trust
decision; no wildcard ReferenceGrant is installed.

Cilium ingress has a reserved ingress identity. Consumers author the policy
allowing that identity to their selected backends as well as ordinary
same-consumer paths. Installing the CNI does not create a blanket application
isolation policy. FleetIQ's policy desired state and acceptance remain FleetIQ's.
On this profile only, delegated Argo application roles may reconcile namespaced
Gateways, HTTPRoutes and CiliumNetworkPolicies; no cluster-wide Cilium/Gateway
resource, ReferenceGrant or API ownership is delegated.

## Trust and security

Production certificate lifecycle remains consumer-owned. Accept public CA bundles
and certificate/Secret references; do not hand off private CA keys, kubeconfig
bytes or provider credentials. Do not mutate host trust stores. Local qualification
uses an ephemeral root generated in memory with the existing `cryptography`
library, sends only the leaf/key to a neutral Secret over private stdin, and
discards the root key. SSL clients must verify chain and DNS hostname.

CNI agents require node networking/BPF mounts and elevated capabilities. This is
a trusted infrastructure boundary, not a sandbox for untrusted CNI operators.
The profile is local Linux/amd64 kind, with the required Linux eBPF, cgroup and
TPROXY facilities. Non-local administration, durable certificate issuance,
identity/SSO, DNS and recovery require their own provider qualification.

## Operations and recovery

Preview is offline. Creation journals exact nodes/incarnation before CNI writes.
Failures retain private ownership state. Explicit `network apply --execute`
recovers only the same new-profile target after ownership and schema checks;
ordinary ensure never repairs partial targets. No in-place CNI uninstall is
provided: reviewed removal destroys the confirmed dedicated target and its local
data using I01's immutable node-ID checks. Retained-cluster upgrade/migration is
separate work. Perfeng's identities, controllers and node placement remain unchanged.

## Alternatives

- NetworkPolicy objects on kindnet alone: rejected because enforcement is absent.
- Add another independent CNI and Gateway controller: viable, but unnecessary
  controllers and upgrade boundaries for this first supported profile.
- Replace a retained CNI during application apply: rejected as an unreviewed
  connectivity/ownership migration.
- Port-forward or insecure/self-trusted browser shortcuts: rejected as ingress
  and trust qualification.

## Verification

Offline tests cover identity/listeners, source integrity, foreign/schema refusal,
credential/privilege injection, pure preview, accurate network-result parsing and
real SSL trusted/untrusted/wrong-host behavior. A separate network gate checks the
official assets and real Helm output. The original repository/Argo gates remain.

Exact-target live qualification must show healthy cross-node paths before policy,
same-consumer allows, cross-consumer ingress/egress denials, DNS and required
provider/registry egress, accepted/rejected route attachments, trusted/untrusted/
wrong-host TLS and preserved Host/Origin/cookies. Failures from Kubernetes exec
are not accepted as network denial. Report live evidence and unqualified features
separately; neither CRD presence nor this ADR is an acceptance pass.

References: [kind installation](https://docs.cilium.io/en/stable/installation/kind/),
[Gateway API](https://docs.cilium.io/en/stable/network/servicemesh/gateway-api/gateway-api/),
[system requirements](https://docs.cilium.io/en/stable/operations/system_requirements/),
[DNS policy](https://docs.cilium.io/en/stable/security/dns/).
