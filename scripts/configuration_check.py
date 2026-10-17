"""Validate reviewed local substrate selections without reading live state."""

from __future__ import annotations

from . import argocd, local_kind, workflows


def main() -> int:
    """Check the committed kind and controller release selections."""
    local_kind.load_profile()
    version = argocd.validate_configuration()
    workflow_release = workflows.load_selection()
    workflows.validate_namespace()
    print(
        f"Validated local kind profile, official Argo CD {version} bootstrap "
        f"and Argo Workflows {workflow_release['version']} selection."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
