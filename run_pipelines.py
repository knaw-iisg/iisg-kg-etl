#!/usr/bin/env python3
"""Run all KNAW/IISG linked-data ETL pipelines and collect their output locally.

Each pipeline lives in its own sibling repo with its own venv. This script
just shells out to each one's existing CLI in turn -- it doesn't import any
pipeline code -- and keeps going if one of them fails, so a bad harvest in
one pipeline doesn't stop the others from producing a fresh file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

THIS_REPO = Path(__file__).resolve().parent
DEFAULT_PIPELINES_ROOT = THIS_REPO.parent  # siblings live in the same ~/git checkout


@dataclass
class Pipeline:
    name: str
    repo: str  # sibling directory name
    build_argv: Callable[[Path], list[str]]  # (output_dir) -> argv after the interpreter
    output_file: str  # filename this pipeline's output lands at, directly inside output_dir
    script: str | None = None  # module (via -m) unless this is set to a plain script path
    post: Callable[[Path, Path], None] | None = None  # (output_dir, venv_python) -> None, run after a successful call


_CONVERT_TTL_TO_NT_SCRIPT = (
    "import sys\n"
    "from rdflib import Graph\n"
    "g = Graph()\n"
    "g.parse(sys.argv[1], format='turtle')\n"
    "g.serialize(destination=sys.argv[2], format='nt', encoding='utf-8')\n"
)


def convert_to_nt(venv_python: Path, ttl_path: Path, nt_path: Path) -> None:
    """Turtle -> N-Triples, via the venv that already depends on rdflib to write the Turtle in the first place."""
    subprocess.run([str(venv_python), "-c", _CONVERT_TTL_TO_NT_SCRIPT, str(ttl_path), str(nt_path)], check=True)
    ttl_path.unlink()


def _dataverse_post(out: Path, venv_python: Path) -> None:
    # dataverse_to_rdf.py always names its own output file knaw-huc-dataverse.ttl
    # inside --out-dir -- flatten it to output_dir directly first, matching what
    # every other pipeline here already does via --out, then convert to N-Triples
    # like the rest.
    ttl_path = out / "dataverse.ttl"
    (out / "dataverse" / "knaw-huc-dataverse.ttl").replace(ttl_path)
    convert_to_nt(venv_python, ttl_path, out / "dataverse.nt")


PIPELINES = [
    Pipeline(
        "biblio", "biblio-etl",
        lambda out: ["-m", "biblio_etl.cli", "--source", "oai", "--stream",
                     "--out", str(out / "biblio.nt")],
        output_file="biblio.nt",
    ),
    Pipeline(
        "archive", "archive-etl",
        lambda out: ["-m", "archive_etl.cli", "--source", "oai", "--stream",
                     "--out", str(out / "archive.nt")],
        output_file="archive.nt",
    ),
    Pipeline(
        "findingaid", "findingaid-etl",
        lambda out: ["-m", "findingaid_etl.cli", "--source", "oai", "--stream",
                     "--out", str(out / "findingaid.nt")],
        output_file="findingaid.nt",
    ),
    Pipeline(
        "authorities", "authorities-etl",
        lambda out: ["-m", "authorities_etl.cli", "--source", "oai", "--stream",
                     "--out", str(out / "authorities.nt")],
        output_file="authorities.nt",
    ),
    Pipeline(
        "events", "events-etl",
        lambda out: ["-m", "events_etl.cli", "--source", "web",
                     "--out", str(out / "events.ttl")],
        output_file="events.nt",
        # events-etl's own CLI only writes Turtle (no --format flag) -- convert
        # to N-Triples here instead, via its own venv's rdflib, so every
        # pipeline's output ends up in the same format.
        post=lambda out, venv_python: convert_to_nt(venv_python, out / "events.ttl", out / "events.nt"),
    ),
    Pipeline(
        "orcid", "orcid-etl",
        lambda out: ["-m", "orcid_etl.cli", "--out", str(out / "orcid.ttl")],
        output_file="orcid.nt",
        post=lambda out, venv_python: convert_to_nt(venv_python, out / "orcid.ttl", out / "orcid.nt"),
    ),
    Pipeline(
        "dataverse", "dataverse-etl",
        lambda out: ["dataverse_to_rdf.py", "--out-dir", str(out / "dataverse")],
        output_file="dataverse.nt",
        post=_dataverse_post,
    ),
    Pipeline(
        "identity", "identity-etl",
        lambda out: ["-m", "identity_etl.cli", "--out", str(out / "identity.ttl")],
        output_file="identity.nt",
        post=lambda out, venv_python: convert_to_nt(venv_python, out / "identity.ttl", out / "identity.nt"),
    ),
]


def ping_healthchecks(event: str, body: str = "") -> None:
    """Notify healthchecks.io (if HEALTHCHECKS_PING_URL is set) that the run started/succeeded/failed.

    event is "start", "success", or "fail" -- per healthchecks.io's own ping API
    (bare URL = success, "/start" and "/fail" suffixes for the other two).
    Never raises: a dead network or misconfigured URL shouldn't fail the run itself.
    """
    url = os.environ.get("HEALTHCHECKS_PING_URL")
    if not url:
        return
    if event != "success":
        url = f"{url}/{event}"
    try:
        data = body.encode() if body else None
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except OSError as e:
        print(f"warning: healthchecks.io ping failed: {e}", file=sys.stderr)


def load_dotenv(path: Path) -> None:
    """Minimal KEY=VALUE loader for secrets like DATAVERSE_API_KEY. No new dependency."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def run_one(pipeline: Pipeline, pipelines_root: Path, output_dir: Path, log_dir: Path) -> tuple[bool, float]:
    repo_dir = pipelines_root / pipeline.repo
    venv_python = repo_dir / ".venv" / "bin" / "python"
    log_path = log_dir / f"{pipeline.name}.log"

    if not venv_python.exists():
        log_path.write_text(f"venv python not found at {venv_python}\n")
        return False, 0.0

    argv = [str(venv_python), *pipeline.build_argv(output_dir)]
    start = time.monotonic()
    with log_path.open("w") as log_file:
        log_file.write(f"$ {' '.join(argv)}\ncwd={repo_dir}\n\n")
        log_file.flush()
        result = subprocess.run(argv, cwd=repo_dir, stdout=log_file, stderr=subprocess.STDOUT)
    duration = time.monotonic() - start
    ok = result.returncode == 0
    if ok and pipeline.post is not None:
        pipeline.post(output_dir, venv_python)
    return ok, duration


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipelines-root", type=Path, default=DEFAULT_PIPELINES_ROOT,
                         help="directory containing the sibling pipeline repos (default: parent of this repo)")
    parser.add_argument("--output-dir", type=Path, default=THIS_REPO / "output",
                         help="where each pipeline's RDF file is written (default: ./output)")
    parser.add_argument("--only", help="comma-separated subset of pipeline names to run, e.g. biblio,events")
    parser.add_argument("--summary-json", type=Path,
                         help="also write a machine-readable run summary (timing + per-pipeline status) to this path")
    args = parser.parse_args()

    load_dotenv(THIS_REPO / ".env")

    selected = PIPELINES
    if args.only:
        wanted = {n.strip() for n in args.only.split(",")}
        selected = [p for p in PIPELINES if p.name in wanted]
        unknown = wanted - {p.name for p in PIPELINES}
        if unknown:
            parser.error(f"unknown pipeline name(s): {', '.join(sorted(unknown))}")

    run_id = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    output_dir = args.output_dir
    log_dir = THIS_REPO / "logs" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    ping_healthchecks("start")

    started_at = dt.datetime.now().astimezone()
    run_start = time.monotonic()
    results = []
    for pipeline in selected:
        print(f"-> {pipeline.name} ...", flush=True)
        ok, duration = run_one(pipeline, args.pipelines_root, output_dir, log_dir)
        status = "ok" if ok else "FAILED"
        print(f"   {status} ({duration:.1f}s)", flush=True)
        results.append((pipeline.name, ok, duration))
    finished_at = dt.datetime.now().astimezone()
    total_duration = time.monotonic() - run_start

    lines = [f"{'pipeline':<12} {'status':<8} {'seconds':>8}"]
    for name, ok, duration in results:
        lines.append(f"{name:<12} {'ok' if ok else 'FAILED':<8} {duration:>8.1f}")
    summary = "\n".join(lines)

    print()
    print(summary)
    print(f"\nlogs: {log_dir}")
    print(f"output: {output_dir}")

    all_ok = all(ok for _, ok, _ in results)

    if args.summary_json:
        args.summary_json.write_text(json.dumps({
            "run_id": run_id,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_s": round(total_duration, 1),
            "pipelines": [
                {"name": name, "status": "ok" if ok else "failed", "duration_s": round(duration, 1)}
                for name, ok, duration in results
            ],
        }, indent=2))

    ping_healthchecks("success" if all_ok else "fail", body=summary)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
