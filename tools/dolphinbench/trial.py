"""A cheaper DolphinBench trial: one persona, optionally the first N of its tests, through the official runner code
(same apps, same grading, same evidence files). Not a submission: the package stage needs all three personas.

Run from the DolphinBench checkout, with this directory on PYTHONPATH:

    PYTHONPATH=<smartware>/tools/dolphinbench python <smartware>/tools/dolphinbench/trial.py \\
        --config <run.yaml> --persona morgan [--tests 50] --confirm-paid-calls

It never looks at test content: it only takes the first N entries of the persona's test list.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml

from harness.adapter import load_adapter
from harness.runner import ROOT, Runner, run_lock
from harness.submission import load_json, read_release


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--persona", choices=("morgan", "alex", "riley"), default="morgan")
    ap.add_argument("--tests", type=int, default=None, help="only the first N tests of the persona (default: all 200)")
    ap.add_argument("--sessions", type=int, default=None,
                    help="only the first N history messages (smoke runs only: later tests then lack their history)")
    ap.add_argument("--stage", choices=("ingest", "evaluate", "both"), default="both")
    ap.add_argument("--confirm-paid-calls", action="store_true")
    a = ap.parse_args(argv)
    if not a.confirm_paid_calls:
        print("This calls the agent model, the extraction model and the Azure grader: add --confirm-paid-calls.", file=sys.stderr)
        return 2
    config = yaml.safe_load(a.config.read_text())
    base = a.config.resolve().parent
    release_root = (base / config["release"]).resolve()
    output = (base / config["output"]).resolve()
    full = read_release(release_root)
    part = dict(full[a.persona])
    if a.sessions is not None:
        part["sessions"] = part["sessions"][: a.sessions]
    if a.tests is not None:
        part["tests"] = part["tests"][: a.tests]
    release = {a.persona: part}
    with run_lock(output):
        adapter = load_adapter(config["adapter"], config["options"], output)
        identity = {"config": config, "trial": {"persona": a.persona, "tests": a.tests, "sessions": a.sessions},
                    "adapter": adapter.identity(),
                    "runner_sha256": hashlib.sha256((ROOT / "harness/runner.py").read_bytes()).hexdigest()}
        runner = Runner(release, output, adapter, identity=identity, release_root=release_root, allow_paid=True)
        if a.stage in ("ingest", "both"):
            runner.ingest()
        if a.stage in ("evaluate", "both"):
            runner.evaluate()
            passed = total = 0
            for spec in part["tests"]:
                g = load_json(runner._path("grades", a.persona, str(spec["id"]).zfill(3)).read_bytes())
                total += 1
                passed += all(c["passed"] for c in g["checks"])
            costs = {p: load_json((output / "costs" / f"{p}.json").read_bytes())["total_cost_usd"]
                     for p in ("ingestion", "tests") if (output / "costs" / f"{p}.json").exists()}
            print(json.dumps({"persona": a.persona, "passed": passed, "tests": total,
                              "accuracy": round(passed / max(1, total), 4), "cost_usd": costs}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
