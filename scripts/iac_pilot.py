"""Plan and provision registered local kind targets on the laptop's Docker daemon."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import Any

import yaml

from . import argocd
from .iac_pilot_runtime import Runtime
from .iac_pilot_state import (
    NAME,
    Target,
    digest,
    encoded,
    fingerprint,
    mapping,
    read_file,
    store,
    uid,
)
from .local_kind import CommandFailure

ACTIONS = (
    "preflight",
    "observe",
    "plan-create",
    "apply-create",
    "plan-repeat",
    "bootstrap",
    "plan-destroy",
    "apply-destroy",
    "recover",
)
PROFILES = {
    "pilot": (NAME, "kind-74"),
    "retained-434": ("perfeng-retained-434", "kind-434"),
}
RETAINED_ACTIONS = {
    "preflight",
    "observe",
    "plan-create",
    "apply-create",
    "plan-repeat",
    "recover",
    "bootstrap",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


class Rehearsal:
    def __init__(self, runtime: Runtime, daemon: str):
        self.runtime = runtime
        self.target = runtime.target
        self.daemon = daemon
        self.identity = self.target.directory / "identity.json"
        self.pending = self.target.directory / "pending.json"
        self.bootstrap_record = self.target.directory / "bootstrap.json"

    def record(self, path: Path, data: dict[str, Any]) -> None:
        store(path, encoded(data))

    def cluster_identity(self) -> dict[str, Any]:
        if os.path.lexists(self.identity):
            value = mapping(read_file(self.identity, 131072))
            require(
                value.get("daemon") == self.daemon,
                "Recorded cluster belongs to another Docker daemon",
            )
            return value
        return {}

    def snapshot(self, action: str) -> dict[str, Any]:
        before = self.runtime.snapshot(cluster_uid=action != "create")
        owned = self.runtime.exists_in_state()
        identity = self.cluster_identity()
        if action == "create":
            require(
                not before["nodes"]
                and not owned
                and not identity
                and not os.path.lexists(self.target.kubeconfig)
                and not os.path.lexists(self.bootstrap_record),
                "Create requires an unused cluster name, kubeconfig and state",
            )
        else:
            require(
                owned and bool(before["nodes"]) and before["uid"] == identity.get("uid"),
                "Live cluster UID or OpenTofu ownership differs from the recorded target",
            )
        return before

    def observe(self) -> None:
        observed = {
            "daemon": self.daemon,
            "identity": self.cluster_identity(),
            "live": self.runtime.snapshot(cluster_uid=True, tolerate_unreachable=True),
            "stateOwnsTarget": self.runtime.exists_in_state(),
            "unfinished": mapping(read_file(self.pending, 131072))["action"]
            if os.path.lexists(self.pending)
            else None,
        }
        print(encoded(observed).decode(), end="")

    def plan(self, action: str) -> None:
        if action == "create":
            self.runtime.preflight(self.runtime.docker_info())
        before = self.snapshot(action)
        module = self.runtime.initialize()
        with self.runtime.staged_plan() as plan:
            options = ["-destroy"] if action == "destroy" else []
            self.runtime.tofu(
                "plan",
                "-no-color",
                "-input=false",
                "-lock-timeout=0s",
                *options,
                f"-var=cluster_name={self.target.name}",
                f"-out={plan}",
                timeout=300,
            )
            drift = self.runtime.inspect(plan, action)
            require(before == self.snapshot(action), "Target changed during planning")
            if action == "repeat":
                require(not drift, "OpenTofu reported drift during the repeat plan")
                print("The refreshed repeat plan has zero changes.")
                return
            payload = read_file(plan)
            plan_hash = digest(payload)
            plan_path = self.target.directory / f"{action}.tfplan"
            review_path = self.target.directory / f"{action}-plan.txt"
            store(plan_path, payload)
            store(
                review_path,
                self.runtime.tofu("show", "-no-color", str(plan)).encode(),
            )
            self.record(
                self.target.directory / f"{action}-plan.json",
                {
                    "action": action,
                    "daemon": self.daemon,
                    "statePath": str(self.target.state),
                    "before": before,
                    "module": module,
                    "plan": plan_hash,
                },
            )
        print(
            f"Review {review_path} and pass --plan-sha256 {plan_hash} to apply-{action} --execute."
        )

    def apply(self, action: str, reviewed_hash: str) -> None:
        before = self.snapshot(action)
        receipt = mapping(read_file(self.target.directory / f"{action}-plan.json", 131072))
        require(
            receipt.get("plan") == reviewed_hash, "Supplied digest does not match the saved plan"
        )
        require(
            receipt.get("action") == action
            and receipt.get("daemon") == self.daemon
            and receipt.get("statePath") == str(self.target.state)
            and receipt.get("before") == before,
            "Saved plan is stale or belongs to another target",
        )
        require(
            self.runtime.initialize() == receipt["module"], "The module or dependency lock changed"
        )
        with self.runtime.staged_plan(self.target.directory / f"{action}.tfplan") as plan:
            require(fingerprint(plan) == reviewed_hash, "Saved-plan bytes changed since review")
            self.runtime.inspect(plan, action)
            require(
                self.runtime.docker_info()["ID"] == self.daemon and self.snapshot(action) == before,
                "The target changed before apply",
            )
            self.record(self.pending, receipt)
            self.runtime.tofu(
                "apply",
                "-no-color",
                "-input=false",
                "-lock-timeout=0s",
                str(plan),
                write=True,
                timeout=600,
            )
            if action == "create":
                self.finish_create()
                print("Three nodes are Ready. CoreDNS and local-path storage checks passed.")
            else:
                require(
                    not self.runtime.nodes() and not self.runtime.exists_in_state(),
                    "Teardown did not remove the target and its state entry",
                )
                self.clear_identity()
            self.finish(receipt)

    def finish_create(self, expected_uid: str | None = None) -> None:
        require(self.runtime.exists_in_state(), "OpenTofu state does not own the created target")
        current_uid = self.runtime.publish_config()
        require(expected_uid is None or current_uid == expected_uid, "Live cluster UID changed")
        self.record(self.identity, {"daemon": self.daemon, "uid": current_uid})
        current = self.snapshot("repeat")
        self.runtime.verify_ready(current["nodes"])

    def clear_identity(self) -> None:
        for path in (self.target.kubeconfig, self.bootstrap_record, self.identity):
            path.unlink(missing_ok=True)

    def finish(self, receipt: dict[str, Any]) -> None:
        self.record(self.target.directory / "last-operation.json", receipt)
        self.pending.unlink()

    def recover(self) -> None:
        receipt = mapping(read_file(self.pending, 131072))
        require(
            receipt.get("daemon") == self.daemon
            and receipt.get(
                "statePath", str(self.target.state) if self.target.name == NAME else None
            )
            == str(self.target.state),
            "Unfinished operation belongs to another target",
        )
        action = receipt["action"]
        before = receipt["before"]
        current = self.runtime.snapshot(cluster_uid=True, tolerate_unreachable=True)
        owns = self.runtime.exists_in_state()
        identity = self.cluster_identity()
        if action == "create" and owns and identity and current["uid"] == identity["uid"]:
            self.finish_create(identity["uid"])
        elif (
            action == "create"
            and owns
            and not identity
            and not before["nodes"]
            and before["uid"] is None
            and current["uid"] not in {None, "unreachable"}
        ):
            self.runtime.verify_state_kubeconfig()
            self.finish_create(current["uid"])
            print(
                "Recovered the completed create. Three nodes are Ready and DNS and storage checks passed."
            )
        elif action == "destroy" and not owns and not current["nodes"]:
            self.clear_identity()
        elif (
            action == "create"
            and before == {"state": "absent", "nodes": {}, "uid": None}
            and current["state"] != "absent"
            and not current["nodes"]
            and not owns
            and not identity
            and not os.path.lexists(self.target.kubeconfig)
            and mapping(read_file(self.target.state)).get("outputs") == {}
        ):
            # A failed create can write state metadata even after all resources roll back.
            print("Recovered the rolled-back create and preserved the empty OpenTofu state.")
        elif current == before and owns == (action == "destroy"):
            require(
                action == "destroy" or not os.path.lexists(self.target.kubeconfig),
                "A kubeconfig remains from the interrupted create",
            )
        else:
            raise ValueError(
                "State and live resources need inspection. Use observe and keep the pending record."
            )
        self.finish(receipt)

    def bootstrap(self, expected_uid: str | None = None) -> None:
        before = self.snapshot("repeat")
        if expected_uid is not None:
            require(
                before["uid"] == expected_uid,
                "Live cluster UID differs from the approved bootstrap UID",
            )
        manifest = self.runtime.render_argo()
        desired = {"uid": before["uid"], "manifest": digest(manifest)}
        recorded = (
            mapping(read_file(self.bootstrap_record, 131072))
            if os.path.lexists(self.bootstrap_record)
            else {}
        )
        if recorded:
            require(
                all(recorded.get(key) == value for key, value in desired.items()),
                "The pinned Argo CD manifest changed for this cluster",
            )
        kube = [*self.runtime.kube_prefix(), "-n", argocd.NAMESPACE]
        installed = self.runtime.run(argocd.namespace_command(kube))
        argocd.validate_live_namespace(installed, allow_missing=not recorded.get("installation"))
        previous = recorded.get("installation")
        require(self.snapshot("repeat") == before, "The target changed before Argo CD bootstrap")
        if not recorded:
            self.record(self.bootstrap_record, desired)
        self.runtime.run(
            argocd.apply_command(kube, ["-f", "-"], fresh=True),
            write=True,
            data=manifest,
            timeout=240,
        )
        current = self.runtime.argo_inventory()
        require(self.snapshot("repeat") == before, "The target changed during Argo CD bootstrap")
        if previous is not None:
            require(
                current == previous, "Argo CD identity or controller generation changed on repeat"
            )
        self.record(self.bootstrap_record, {**desired, "installation": current})
        print("The pinned Argo CD installation is Ready.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--profile", choices=PROFILES, default="pilot")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--plan-sha256")
    parser.add_argument("--expected-cluster-uid", help="Recorded kube-system UID for bootstrap")
    args = parser.parse_args(argv)
    name, directory = PROFILES[args.profile]
    if args.profile == "retained-434" and args.action not in RETAINED_ACTIONS:
        print("Retirement of this retained profile requires a separate workflow.")
        return 1
    if not args.execute:
        print(f"Offline preview {args.action} on {name} using the laptop's default Docker daemon.")
        return 0
    try:
        expected_uid = args.expected_cluster_uid
        if expected_uid is not None:
            require(args.action == "bootstrap", "--expected-cluster-uid is only for bootstrap")
            expected_uid = uid(expected_uid)
        if args.profile == "retained-434" and args.action == "bootstrap":
            require(expected_uid is not None, "Retained bootstrap requires --expected-cluster-uid")
        apply = args.action.startswith("apply-")
        if apply:
            require(
                isinstance(args.plan_sha256, str)
                and bool(re.fullmatch(r"[0-9a-f]{64}", args.plan_sha256)),
                "Provide the reviewed plan digest with --plan-sha256",
            )
        else:
            require(args.plan_sha256 is None, "--plan-sha256 is only for apply")
        target = Target(Path.home() / ".local/state/perfeng" / directory, name)
        with target.locked() as lock:
            runtime = Runtime(target, lock)
            info = runtime.docker_info()
            rehearsal = Rehearsal(runtime, info["ID"])
            pending = os.path.lexists(rehearsal.pending)
            if args.action in {"preflight", "observe"}:
                if args.action == "preflight":
                    runtime.preflight(info)
                rehearsal.observe()
            elif args.action == "recover":
                require(pending, "There is no interrupted #74 operation")
                rehearsal.recover()
            else:
                require(not pending, "An interrupted operation needs observe and recover")
                if args.action == "bootstrap":
                    rehearsal.bootstrap(expected_uid)
                elif apply:
                    rehearsal.apply(args.action.removeprefix("apply-"), args.plan_sha256)
                else:
                    rehearsal.plan(args.action.removeprefix("plan-"))
    except ValueError as error:
        detail = (
            str(error)
            if type(error) is ValueError or isinstance(error, CommandFailure)
            else "invalid private input or command output"
        )
        print(f"Rehearsal stopped: {detail}")
        return 1
    except (OSError, KeyError, AttributeError, TypeError, yaml.YAMLError):
        print("Rehearsal stopped: private input or command output was unavailable or malformed.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
