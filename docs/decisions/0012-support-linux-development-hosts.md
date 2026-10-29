# ADR-0012: Support Linux development hosts

- Status: Accepted
- Date: 2026-10-29
- Milestone: M02 — Declarative Local Operations
- Deciders: Repository owner
- Supersedes: Host-support clauses in ADR-0001 and ADR-0002 dual-host harness guidance
- Superseded by:

## TL;DR

- Linux is the supported development and CI host.
- Remove Windows runner, binary, process and operator-instruction support.
- Preserve upstream dependency resolution and historical verification evidence.

## Context

The owner requested retirement of Windows development support across the project
repositories. Maintaining unused host adapters and CI jobs expands the supported
surface without serving the current workstation environment.

## Decision

Run the existing validation gates on Linux. Use Linux shell instructions and
POSIX process groups, permissions and locks. Tools that install native executables
select reviewed Linux artifacts only. Existing historical decisions retain their
original evidence; this decision supersedes their Windows support commitments.

## Alternatives

Retaining dual-host support would require Windows CI and platform-specific
maintenance. Removing upstream Windows lockfile packages would damage reproducible
resolution because upstream packages declare them for other targets.

## Consequences and compatibility

Windows is no longer a supported host. Linux acceptance requirements remain in
force. Application contracts, Kubernetes resource ownership, persisted state and
release artifact identities do not change. Foreign-path rejection remains a
security validation concern even when the input names an unsupported platform.

## Security and operations

No cluster, credential, provider, runtime or paid-resource operations are required.
Private state retains restrictive POSIX permissions. Restore host support through
a reviewed follow-up decision or revert this cleanup; no data migration is needed.

## Validation

Run repository-local verification on Linux and report unavailable prerequisites.
CI retains the existing Linux validation commands and security gates.
