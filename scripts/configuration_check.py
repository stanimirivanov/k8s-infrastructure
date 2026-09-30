"""Validate reviewed local substrate selections without reading live state."""

from __future__ import annotations

from . import argocd, local_kind


def main() -> int:
    """Check the committed kind profile and Argo CD release selection."""
    local_kind.load_profile()
    version = argocd.validate_configuration()
    print(f"Validated local kind profile and official Argo CD {version} bootstrap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
