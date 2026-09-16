# Kubernetes infrastructure engineering standards

## TL;DR

- Keep provider implementations explicit and share validation before
  abstraction.
- Separate offline validation, plan, apply, status and destroy.
- Treat state, target identity, credentials, network exposure and cost as
  security boundaries.
- Preserve cluster and resource identities during extraction and migration.
- Keep Argo CD installation separate from consumer Application ownership.
- Test success, refusal, timeout, partial failure and recovery behavior.

## Policy strength

Normative terms use the meanings in
[CONTRIBUTING.md](../../CONTRIBUTING.md#policy-language-and-sources-of-truth).
MUST and MUST NOT are requirements; SHOULD and SHOULD NOT are strong defaults
requiring a recorded reason to deviate; MAY is optional.

## Architecture and ownership

Organize code around provider profiles and independently operable capabilities:
substrate lifecycle, access delivery, Argo CD bootstrap, capability
qualification, cost policy and recovery.

- Provider networking, IAM, storage and Kubernetes resources remain explicit.
- Share parsing, validation and policy only when semantics are genuinely equal.
- Do not hide provider choices behind a lowest-common-denominator module.
- The infrastructure repository owns the substrate and Argo CD installation;
  consumers own Applications and workload desired state.
- State backend, credentials and kubeconfig are operator-controlled inputs, not
  repository artifacts or consumer handoff documents.

Use SOLID and patterns as reasoning tools, not quotas. Prefer composition,
precise names, small validators and direct provider configuration over generic
managers, inheritance hierarchies or speculative frameworks.

## Python and command design

- Use Python from `.python-version`, exact dependencies from `uv.lock`, Ruff
  formatting and the configured type checker.
- Keep import-time behavior side-effect-free.
- Separate parsing, identity/policy validation, process execution and CLI
  reporting so core behavior is testable without a live provider or cluster.
- Type public functions and operational boundaries. Normalize external data
  before core logic uses it.
- Build subprocess arguments as sequences, never interpolated shell strings.
- Capture text as UTF-8 with deliberate error behavior, bound execution and
  diagnostic size, preserve exit status and redact sensitive output.
- Use precise errors for invalid input, identity mismatch, missing prerequisite,
  timeout, dependency failure, partial operation and internal defect.
- Do not add lint-suppression comments merely to silence findings.
- Comments and docstrings explain contracts and non-obvious constraints rather
  than syntax.

## Terraform and provider stacks

- Pin Terraform/OpenTofu and provider versions; commit the dependency lock.
- Keep state remote or in an explicitly protected local path outside Git.
- Never pass secrets with `-var` command arguments or commit secret tfvars.
- Format and validate every root. Run static/security checks selected by the
  implementation before a plan can be accepted.
- Plan and apply use the same reviewed variables, backend, provider account,
  region and locked dependencies.
- Persist a plan only when required for a reviewed apply; treat it as sensitive,
  exclude it from Git and delete it through an explicit cleanup path.
- Use stable resource addresses and `moved`/`import` blocks for reviewed state
  transitions. Do not disguise replacement as refactoring.
- Outputs expose only the non-secret capability handoff.

## Kubernetes and Argo CD

- Use a dedicated kubeconfig and exact context; never rely on the ambient
  default context.
- Verify provider, cluster name, server identity and ownership labels before
  mutation.
- Pin Kubernetes and controller releases according to the provider/profile
  contract. Mutable branches are not deployment inputs.
- Install Argo CD declaratively and prove each controller's rollout and
  readiness with bounded waits.
- Do not create consumer Projects, Applications, namespaces or workload
  credentials in the infrastructure bootstrap.
- Keep infrastructure controller ownership disjoint from consumer GitOps
  ownership; never run competing reconcilers for one resource.
- Destruction states retained resources and requires an exact confirmation
  distinct from apply.

## Identity, idempotency and recovery

- Define identity from stable provider/account/project/compartment, region,
  cluster, context, backend and state/workspace values.
- Refuse redirected state, an unexpected context or an unowned existing
  resource.
- Idempotency means the same reviewed input converges on the same owned target;
  it does not mean adopting or replacing arbitrary matching names.
- A partial operation records completed phase and safe inspection without
  leaking credentials.
- Recovery does not silently rotate credentials, replace state, delete retained
  data, widen a firewall or select a billable fallback.
- State migration and ownership cutover have explicit preconditions, backup,
  validation and rollback.

## Security, networking and cost

- Apply least privilege to provider identities, state backends, Kubernetes
  access and CI permissions.
- Administrative endpoints are source-restricted or private. Public
  `0.0.0.0/0` access is not a default.
- Bound resource counts, sizes, retries, concurrency and execution time.
- Fail closed when a free/low-cost profile selects an unapproved SKU, size,
  region, architecture or supporting resource.
- Quotas are enforcement where available. Budgets and anomaly alerts are
  delayed notification and MUST NOT be described as spending caps.
- Live changes include provider inventory and billing-eligibility verification.
- Never fall back from unavailable free capacity to paid capacity implicitly.

## Testing and validation

Use the lowest boundary that proves behavior:

- pure input, identity, capability and cost-policy tests;
- orchestration tests with process/provider boundaries replaced;
- Terraform format, validate, static analysis and reviewed plan assertions;
- Kustomize rendering and Kubernetes schema/policy checks;
- focused live acceptance only for behavior mocks cannot prove; and
- consumer acceptance after a separately reviewed ownership cutover.

Tests are deterministic, isolated and independent of network, credentials,
wall clock, locale, host paths and order unless one is the subject. Defect fixes
SHOULD begin with a failing regression test. Never weaken assertions or extend
timeouts without diagnosing the cause.

## Documentation and dependencies

- Document prerequisites, target identity, preview behavior, mutations,
  healthy signals, failure classes, cost assumptions, recovery and destroy.
- Operational behavior and troubleshooting change together.
- Long documents follow the TL;DR policy in CONTRIBUTING.
- Accepted ADRs remain historical and are superseded rather than rewritten.
- Dependencies and external tools need a current use, compatible license,
  maintained release, exact lock/checksum and proportionate replacement
  boundary.

## Required verification

Run `make validate`. Each provider or cluster implementation adds its exact
offline format, validation, policy and plan checks to the command surface and
CI. Live validation is required only when the issue explicitly authorizes a
named target.
