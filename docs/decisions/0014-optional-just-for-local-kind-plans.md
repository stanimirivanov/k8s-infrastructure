# ADR-0014: Optional just interface for registered local kind plans

- Status: Proposed
- Date: 2026-11-28
- Related work: Infrastructure #81 and #87
- Supersedes in part: [ADR 0001's operator tooling for registered local kind planning](0001-repository-boundary-and-tooling.md)

## TL;DR

- Offer `just plan` to operators of the registered local OpenTofu kind profiles
  on Linux AMD64. Keep the native runbook as a supported planning interface.
- Stage the selected tracked inputs from reviewed Git `HEAD` and save the same
  private evidence for either interface.
- Use the pinned upstream release and run both interfaces in offline fixtures.

## Context

[ADR 0001](0001-repository-boundary-and-tooling.md) anticipated Go Task
as an operator interface. [ADR 0013](0013-declarative-kind-provisioning.md)
established the registered kind profiles with separate state and saved-plan
review. Operators need recipe discovery and a short planning invocation.
[Issue #81](https://github.com/stanimirivanov/k8s-infrastructure/issues/81)
counts executable logic embedded in a task file as scripting.

## Decision

Use an optional `justfile` for these registered kind profiles. Bare `just`
lists the available recipes. `just plan` takes the profile and intent as
positional arguments. The recipe contains the Bash orchestration for the
existing native tools. The Markdown procedure uses the same target lock,
backend, archive inputs and review evidence so that either plan can enter
the native approved-apply procedure.

Pin the maintained `casey/just` 1.59.0 Linux AMD64 musl archive to the
repository's SHA-256. It is distributed under CC0-1.0. The installation
verifies the archive before executing the binary. Its operational boundary
is the task invocation, and the documented native commands provide a
replacement path. [Release](https://github.com/casey/just/releases/tag/1.59.0) ·
[Versioned source](https://github.com/casey/just/tree/1.59.0) ·
[Checksum pin](../../clusters/local-kind/iac-pilot/just-linux-amd64.sha256).

## Alternatives

The Markdown-only procedure has one fewer prerequisite and requires the
operator to repeat the entire command block. GNU Make already serves
repository verification. Go Task provides required inputs and task discovery.
Both runners would still need a Bash sequence to hold the target lock and
check identity through the final evidence write. `just` supplies discovery
and a single Bash recipe with native positional arguments.

## Consequences and verification

The task file and runbook must stay compatible as the procedure evolves.
The GitHub-hosted `make kind-plan-check` job exercises both interfaces against
dedicated scratch paths. The native hosted lifecycle rehearsal covers the kind
substrate. Operations holds the private plan and source revision for
separate approval of the absolute path and SHA-256. This scope covers
planning for the registered local targets. [Issue #87](https://github.com/stanimirivanov/k8s-infrastructure/issues/87).
