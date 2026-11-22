"""Preview admission offline or explicitly observe a bounded local Linux host."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

from . import host_admission as model
from . import host_observation


def read_json(path: Path, *, code: str) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise model.Refusal("Malformed JSON input", code=code) from None


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
                raise model.Refusal("Host observation requires --execute")
            print(json.dumps({"hostId": host_observation.host_id()}))
            return 0
        if args.request is None:
            raise model.Refusal("Explicit request required")
        request = read_json(args.request, code="invalid-request")
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
                raise model.Refusal("Preview is offline; --execute is unsupported")
            if args.observation:
                observed = read_json(args.observation, code="invalid-observation")
                result.update(
                    model.evaluate(
                        request, observed, host_id=args.host_id or "", now=int(time.time())
                    )
                )
        else:
            if not args.execute or not args.host_id or args.observation:
                raise model.Refusal(
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
        reason = error.code
    except (TimeoutError, subprocess.TimeoutExpired):
        reason = "observation-timeout"
    except FileNotFoundError:
        reason = "missing-prerequisite"
    except PermissionError:
        reason = "permission-denied"
    except subprocess.SubprocessError:
        reason = "measurement-failed"
    except OSError:
        reason = "host-evidence-unavailable"
    except Exception:
        # An unexpected defect is distinct from missing evidence. Never expose
        # exception text: it may contain private paths or subprocess arguments.
        reason = "internal-error"
    print(
        json.dumps(
            {
                "policy": model.POLICY,
                "admitted": False,
                "liveAdmission": False,
                "installedAcceptance": False,
                "reason": reason,
            }
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
