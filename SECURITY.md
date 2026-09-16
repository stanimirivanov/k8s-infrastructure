# Security policy

## TL;DR

- Do not disclose suspected vulnerabilities in public issues or pull requests.
- Use GitHub private vulnerability reporting when available.
- If no private channel exists, request one without sensitive details.
- The current default branch is the only supported line.
- Test only infrastructure and accounts you own or are authorized to assess.

## Supported versions

The project has no supported production release. Security fixes target the
current default branch. When releases begin, replace this section with an
explicit supported-version table and disclosure policy.

## Reporting a vulnerability

Use the repository Security page's **Report a vulnerability** action when it is
available. If private vulnerability reporting is unavailable, do not publish
the report. Request a private contact without naming the affected system,
exploit, account, region, data or reproduction details.

Include, where applicable:

- the affected revision, provider, module and configuration;
- impact and conditions required to reproduce it;
- minimal reproduction steps or a proof of concept;
- redacted logs or plans with credentials, account IDs and personal data
  removed;
- known mitigations; and
- whether and where the issue has already been disclosed.

Do not retain, alter or disclose accessed data beyond what is necessary to
demonstrate the issue safely.

## Triage and disclosure

Maintainers SHOULD acknowledge a private report, validate severity and scope,
and agree on a communication path before disclosure. Response times are
best-effort until a staffed process exists.

Reporter and maintainers SHOULD coordinate disclosure after a fix or effective
mitigation exists. Maintainers MAY publish a GitHub security advisory that
credits the reporter unless anonymity is requested.

## Security expectations for contributions

Contributors MUST follow [AGENTS.md](AGENTS.md),
[CONTRIBUTING.md](CONTRIBUTING.md) and the
[engineering standards](docs/development/engineering-standards.md). A change
affecting credentials, IAM, network exposure, Terraform state, cluster access,
cost controls or execution of untrusted input MUST document its threat and
failure model and receive focused security review.
