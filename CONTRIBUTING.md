# Contributing to k8s-infrastructure

## TL;DR

- Deliver one coherent, verified infrastructure outcome per issue and pull
  request.
- Use MUST, SHOULD and MAY as defined below.
- Proceed only on local, reversible assumptions; stop for choices affecting
  state, cost, security, compatibility, ownership or external systems.
- Prepare changes without staging or committing, then print issue content for
  the repository owner.
- Run the exact checks and report unavailable checks as not run, never passed.
- Use exact Perfeng milestone titles in the form `MNN — Outcome` while this
  extraction is tracked by the Perfeng roadmap.

## Policy language and sources of truth

- **MUST** and **MUST NOT** identify requirements. A deviation needs an
  explicit reviewer-approved exception recorded in the issue or pull request.
- **SHOULD** and **SHOULD NOT** identify strong defaults. A deviation is allowed
  only when its reason and trade-off are recorded.
- **MAY** identifies an optional choice.

Lowercase guidance does not silently create another normative level.

| Concern | Canonical source |
|:--|:--|
| Contributor workflow, issue timing, escalation and completion | This document |
| Concise contributor and agent entry point | [AGENTS.md](AGENTS.md) |
| Infrastructure engineering standards | [docs/development/engineering-standards.md](docs/development/engineering-standards.md) |
| Task-routed documentation and verification tiers | [Documentation map](docs/README.md) and [harness](docs/development/harness.md) |
| Dependency admission and updates | [Dependency policy](docs/development/dependency-policy.md) |
| Architecture decisions | [docs/decisions](docs/decisions/README.md) |
| Substrate/consumer handoff | [docs/architecture/substrate-capability.md](docs/architecture/substrate-capability.md) |
| Cross-repository milestones | [Perfeng roadmap](https://github.com/stanimirivanov/perfeng-environment/blob/main/docs/roadmap/milestones.md) |
| Current responsibility and status | [README.md](README.md) |

When sources conflict, the narrower source governs its stated concern. An
accepted ADR governs its decision until superseded. A contributor MUST surface
an unresolved conflict rather than quietly choosing the convenient rule.

## Before starting

A contributor MUST:

- inspect the branch and working tree and preserve unrelated changes;
- read the affected stack, state/backend contract, provider documentation,
  capability contract and accepted ADRs;
- identify the smallest independently reviewable observable outcome;
- identify affected credentials, state, resources, cost, consumers, rollout,
  recovery and rollback; and
- determine the applicable workflow under [Issue timing](#issue-timing).

A contributor MUST NOT discard, overwrite, reformat or incorporate unrelated
work merely to obtain a clean diff. Destructive operations and externally
visible writes outside the task require explicit authorization.

An ADR MUST be proposed when a change establishes or revises a durable decision
about repository responsibility, infrastructure state, security, cost policy,
provider support, compatibility, foundational technology or deployment
topology.

## Ambiguity and escalation

Before escalating, inspect relevant code, tests, configuration, plan fixtures,
documentation, ADRs and issue history and perform safe read-only investigation.

A contributor MAY proceed with a documented assumption only when it is local,
reversible and inexpensive; remains in scope; does not change persistent state,
cost exposure, security, compatibility, ownership or external systems; does not
weaken acceptance; and would not materially change reviewer understanding.

A contributor MUST stop and request a decision when missing information could
materially change outcome, scope, provider/account/region, state ownership,
resource replacement, cost, credentials, network exposure, destructive action,
production or third-party state, or a choice between materially different
designs. State the decision, evidence, options, consequences and safe remaining
work.

## Issue timing

- With an issue number, follow its scope and milestone. A material mismatch
  follows the escalation rules.
- Without an issue number, prepare the smallest coherent requested change and
  print a complete proposed issue in the completion report. The owner creates
  the issue and then creates or renames the branch with its number.
- Contributors acting for the owner MUST NOT stage, commit, push, create or
  modify issues, create pull requests, mutate infrastructure, publish releases
  or change milestones unless explicitly requested.
- Read-only investigation and offline validation MAY occur before an issue
  exists.

Creating GitHub records, cloud resources, state backends, clusters, credentials,
budgets or releases is an externally visible write and requires explicit
authority.

## Milestones and required issue

Use exact published titles in the form `MNN — Short outcome`. GitHub owns live
assignment; the centralized Perfeng roadmap owns extraction sequencing until
this repository establishes work independent of Perfeng.

Every implementation issue uses:

```markdown
**Repository:** k8s-infrastructure
**Milestone:** MNN — Outcome

## Goal

Describe the problem and observable result.

## Scope

- Included behavior and boundaries.

## Design decisions

- Choices, assumptions, state/compatibility effects and ADRs.

## Security and operations

- Trust boundaries, credentials, cost, external effects, rollout and recovery.

## Acceptance criteria

- [ ] Observable behavior and verification evidence.
- [ ] Relevant rejection, failure and recovery behavior.
- [ ] Documentation, compatibility and operational effects.

## Verification

- Required offline and explicitly authorized live checks.

## Out of scope

- Explicit exclusions and deferred work.
```

Acceptance criteria MUST describe observable behavior or verifiable invariants,
not implementation activities. Known limitations MUST be visible.

## Pull-request-sized work

A pull request MUST solve one coherent infrastructure problem, leave supported
environments recoverable, include code, tests, policy and documentation needed
for that outcome, preserve existing state unless replacement is the reviewed
purpose, and prove important rejection and recovery behavior.

Split independent provider support, state migration, cluster cutover, dependency
updates and broad cleanup. Temporary duplication during history-preserving
extraction is allowed only until a separately verified cutover; two active
owners for one resource are never allowed.

## Development workflow

1. Select the workflow under [Issue timing](#issue-timing).
2. Prepare one focused change without staging, committing or pushing for the
   owner.
3. Add or update tests for observable behavior and important rejection paths.
4. Implement the smallest coherent solution and update documentation.
5. Format and run the exact repository checks.
6. Review the diff for secrets, state files, generated plans, machine paths,
   assumptions, cost/identity drift and unrelated changes.
7. Print the canonical [completion report](#completion-report).
8. The owner creates or confirms the issue and branch, may adjust the change,
   commits with `[#N]`, pushes and opens the pull request.

Do not store task-specific issue bodies, PR descriptions, branch instructions
or handoff notes in repository documentation.

## External operations and destructive actions

Read-only inspection is allowed when relevant. Filesystem, cloud, Kubernetes,
registry, GitHub and other mutations MUST remain within scope and authority.

- Validation and preview are offline and side-effect-free.
- Plan is read-only with respect to managed infrastructure and writes only to a
  reviewed local output when explicitly requested.
- Apply and destroy are separate, explicit, target-verified and bounded.
- Credentials MUST NOT appear in arguments, logs, plans, fixtures or Git.
- State replacement, import, move, deletion, credential rotation and repository
  history changes require explicit authorization and verified targets.
- Partial operations retain non-secret evidence for diagnosis and safe retry.
- Live acceptance runs only when the issue requires it or the owner requests it.

## Verification and constrained environments

Bootstrap the locked environment, then run the offline repository checks:

```sh
make bootstrap
make verify
```

Before handoff, run `make validate`, which adds the lock and vulnerability
checks. `make verify` uses installed dependencies offline after bootstrap.
`make supply-chain` may need current advisory data. A provider or cluster
implementation adds its own format, validation, security, and plan checks to
the appropriate Make target and CI job in the same pull request. The tiers and
prerequisites are defined in the [harness](docs/development/harness.md).

When a check cannot execute because of sandbox, network, credentials, missing
tools/services, platform limits or another constraint, report it as **not run**,
state the blocker and safe attempts, list evidence that did run, state residual
risk and where it should run. Never weaken a check or claim an unexecuted check
passed. Report failures even when apparently unrelated.

## Documentation

Every long narrative document MUST contain `## TL;DR` immediately after its
title and status metadata. A document is long when it has at least 800 words,
more than five second-level sections, or is an architecture, security,
migration or operational guide readers consult selectively. Templates are
excluded.

The TL;DR SHOULD state purpose, reader, outcome and next action in three to
seven bullets. Operational changes update troubleshooting guidance in the same
pull request. Accepted ADRs are historical records; supersede rather than
rewrite them.

## Pull request description

A pull request MUST state the linked issue and milestone; outcome and
exclusions; assumptions and unresolved questions; state, compatibility,
security and cost effects; verification including checks not run; rollout,
recovery and rollback; external authorization; and known limitations.

## Review checklist

- [ ] MUST: The issue uses the required structure and exact milestone title.
- [ ] MUST: The change is one coherent, independently mergeable outcome.
- [ ] MUST: Provider, cluster, state and consumer ownership remain explicit.
- [ ] MUST: Validation is offline; external mutation is explicit and bounded.
- [ ] MUST: State, resource identities and credentials are preserved or their
      migration is explicitly accepted.
- [ ] MUST: Network and IAM access are least privilege.
- [ ] MUST: Cost assumptions fail closed and budgets are not called caps.
- [ ] MUST: Success and relevant failure/recovery behavior are tested.
- [ ] MUST: Documentation and troubleshooting reflect operational behavior.
- [ ] MUST: Checks passed or unavailable checks are reported precisely.
- [ ] MUST: No secrets, state, plans, kubeconfigs or unrelated churn exist.
- [ ] MUST: An ADR exists when the decision meets the ADR threshold.

## Completion report

After every work item, print the repository, exact milestone, proposed issue
title, complete issue body, assumptions and limitations, verification outcomes
distinguished as passed/failed/not run, and a suggested `[#N]` commit subject.
When an issue exists, repeat or amend it so delivered scope remains auditable.
Printing the report does not publish external records.
