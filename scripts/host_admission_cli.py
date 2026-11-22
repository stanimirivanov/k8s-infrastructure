"""Preview admission offline or explicitly observe a bounded local Linux host."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

from . import host_admission as model
from . import host_observation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "observe", "host-id"))
    parser.add_argument("--request", type=Path)
    parser.add_argument("--observation", type=Path)
    parser.add_argument("--host-id")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "host-id":
            if not args.execute:
                raise ValueError("Host observation requires --execute")
            print(json.dumps({"hostId": host_observation.host_id()}))
            return 0
        if args.request is None:
            raise ValueError("Explicit request required")
        request = json.loads(args.request.read_text(encoding="utf-8"))
        model.validate(request)
        source_digest = model.digest(
            {
                name: (Path(__file__).parent / name).read_text(encoding="utf-8")
                for name in ("host_admission.py", "host_observation.py", "host_admission_cli.py")
            }
        )
        result = {
            "sourceDigest": source_digest,
            "policy": model.POLICY,
            "requestDigest": model.digest(request),
            "liveAdmission": False,
            "installedAcceptance": False,
        }
        if args.action == "preview":
            if args.execute:
                raise ValueError("Preview is offline; --execute is unsupported")
            if args.observation:
                observed = json.loads(args.observation.read_text(encoding="utf-8"))
                result.update(
                    model.evaluate(
                        request, observed, host_id=args.host_id or "", now=int(time.time())
                    )
                )
        else:
            if not args.execute or not args.host_id or args.observation:
                raise ValueError(
                    "Observe requires --execute and --host-id, without saved observations"
                )
            observed = host_observation.observe(request, expected_host_id=args.host_id)
            result.update(
                model.evaluate(
                    request, observed, host_id=host_observation.host_id(), now=int(time.time())
                )
            )
            result.update(liveAdmission=True, observation=observed)
        print(json.dumps(result, indent=2))
        return 0 if result.get("admitted", True) else 1
    except model.Refusal as error:
        print(
            json.dumps(
                {
                    "policy": model.POLICY,
                    "admitted": False,
                    "liveAdmission": False,
                    "installedAcceptance": False,
                    "reason": str(error),
                }
            )
        )
        return 1
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
        # Input and OS exceptions can contain private paths; never echo them.
        print(
            json.dumps(
                {
                    "policy": model.POLICY,
                    "admitted": False,
                    "liveAdmission": False,
                    "installedAcceptance": False,
                    "reason": "invalid-input-or-unavailable-host-evidence",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
