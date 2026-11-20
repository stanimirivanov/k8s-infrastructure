# Domain docs

This repository uses a single domain context for product-neutral Kubernetes substrate.
Read root `GLOSSARY.md` when present and relevant accepted decisions in
`docs/decisions/` before exploring the affected domain. These decision records remain
in their existing location and retain their identities and history.

Use defined terminology in issues, code discussions and documentation. Surface
conflicts with accepted decisions; supersede decisions explicitly rather than
silently rewriting their meaning.

The `domain-modeling` skill creates glossary entries and ADRs when terms or
decisions are resolved. If the glossary or decision directory is absent, proceed
without scaffolding empty documents. A future `GLOSSARY-MAP.md`, if explicitly
introduced, routes to the relevant bounded contexts.

Shared FleetIQ product semantics belong to `stanimirivanov/fleetiq-platform`;
cross-application installation and integration decisions belong to
`stanimirivanov/fleetiq-deploy`. Reference the owner's documents rather than copy
definitions. Keep repository-specific extensions here. Infrastructure terminology
remains product-neutral. Use repository URLs for external references so this
checkout works independently of sibling directories.

Record a durable decision when changing compatibility, persisted meaning, security,
deployment topology, foundational technology or ownership. Infrastructure decisions also cover state, cost policy and provider support.
