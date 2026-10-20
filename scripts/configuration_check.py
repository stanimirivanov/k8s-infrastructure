"""Validate reviewed local substrate selections without reading live state."""

from __future__ import annotations

from . import argocd, consumer_profile, delegated_argo_config, local_kind, workflows


def main() -> int:
    """Check the committed kind and controller release selections."""
    local_kind.load_profile()
    for name in consumer_profile.PROFILES:
        consumer_profile.load_profile(name=name)
    version = argocd.validate_configuration()
    workflow_release = workflows.load_selection()
    delegated = delegated_argo_config.selection()
    if delegated["workflows"] != workflow_release or delegated["argocd"]["version"] != version:
        raise ValueError("Legacy and delegated Argo selections require coordinated review")
    workflows.validate_namespace()
    print(
        f"Validated Perfeng and isolated consumer kind profiles, official Argo CD {version} bootstrap "
        f"and Argo Workflows {workflow_release['version']} selection."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
