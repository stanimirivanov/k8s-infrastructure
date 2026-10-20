"""Derive closed submission fields from the exact pinned Workflow CRD schema."""

from __future__ import annotations

import re


def forbidden_fields(schema: dict, path: str, allowed: set[str]) -> str:
    """Typed CEL objects cannot be compared to map literals or iterated as maps.

    Enumerate every declared disallowed field instead. Kubernetes prunes or
    rejects undeclared fields in these closed structural objects; an open schema
    would defeat that boundary and must require explicit compatibility review.
    """
    properties = schema.get("properties", {})
    if (
        schema.get("type") != "object"
        or not properties
        or schema.get("x-kubernetes-preserve-unknown-fields", False)
        or schema.get("additionalProperties") not in (None, False)
        or not allowed <= set(properties)
        or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in properties)
    ):
        raise ValueError("Workflow submission requires reviewed closed structural fields")
    denied = sorted(set(properties) - allowed)
    return " && ".join(f"!has({path}.{name})" for name in denied) or "true"


def submission_guards(crd: dict) -> dict[str, str]:
    """No manually maintained override list can silently miss a new schema field."""
    versions = [version for version in crd["spec"]["versions"] if version["name"] == "v1alpha1"]
    if len(versions) != 1:
        raise ValueError("Expected exactly one reviewed Workflow v1alpha1 schema")
    schema = versions[0]["schema"]["openAPIV3Schema"]["properties"]
    spec = schema["spec"]
    reference = spec["properties"]["workflowTemplateRef"]
    arguments = spec["properties"]["arguments"]
    parameter = arguments["properties"]["parameters"]["items"]
    return {
        "spec": forbidden_fields(
            spec, "object.spec", {"workflowTemplateRef", "arguments", "suspend"}
        ),
        "reference": forbidden_fields(reference, "object.spec.workflowTemplateRef", {"name"}),
        "arguments": forbidden_fields(arguments, "object.spec.arguments", {"parameters"}),
        "parameter": forbidden_fields(
            parameter, "object.spec.arguments.parameters[0]", {"name", "value"}
        ),
        "status": forbidden_fields(schema["status"], "object.status", set()),
    }
