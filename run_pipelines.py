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
import os
import subprocess
import sys
import time
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
    script: str | None = None  # module (via -m) unless this is set to a plain script path
    post: Callable[[Path], None] | None = None  # (output_dir) -> None, run after a successful call


PIPELINES = [
    Pipeline(
        "biblio", "biblio-etl",
        lambda out: ["-m", "biblio_etl.cli", "--source", "oai", "--stream",
                     "--out", str(out / "biblio.nt")],
    ),
    Pipeline(
        "archive", "archive-etl",
        lambda out: ["-m", "archive_etl.cli", "--source", "oai", "--stream",
                     "--out", str(out / "archive.nt")],
    ),
    Pipeline(
        "findingaid", "findingaid-etl",
        lambda out: ["-m", "findingaid_etl.cli", "--source", "oai", "--stream",
                     "--out", str(out / "findingaid.nt")],
    ),
    Pipeline(
        "authorities", "authorities-etl",
        lambda out: ["-m", "authorities_etl.cli", "--source", "oai", "--stream",
                     "--out", str(out / "authorities.nt")],
    ),
    Pipeline(
        "events", "events-etl",
        lambda out: ["-m", "events_etl.cli", "--source", "web",
                     "--out", str(out / "events.ttl")],
    ),
    Pipeline(
        "orcid", "orcid-etl",
        lambda out: ["-m", "orcid_etl.cli", "--out", str(out / "orcid.ttl")],
    ),
    Pipeline(
        "dataverse", "dataverse-etl",
        lambda out: ["dataverse_to_rdf.py", "--out-dir", str(out / "dataverse")],
        # dataverse_to_rdf.py always names its own output file
        # knaw-huc-dataverse.ttl inside --out-dir -- flatten it to dataverse.ttl
        # so every pipeline's output lands as one predictably-named file
        # directly in output_dir, matching what triplestore's Qleverfile
        # expects (sources/dataverse.ttl) and what every other pipeline here
        # already does via --out.
        post=lambda out: (out / "dataverse" / "knaw-huc-dataverse.ttl").replace(out / "dataverse.ttl"),
    ),
]


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
        pipeline.post(output_dir)
    return ok, duration


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipelines-root", type=Path, default=DEFAULT_PIPELINES_ROOT,
                         help="directory containing the sibling pipeline repos (default: parent of this repo)")
    parser.add_argument("--output-dir", type=Path, default=THIS_REPO / "output",
                         help="where each pipeline's RDF file is written (default: ./output)")
    parser.add_argument("--only", help="comma-separated subset of pipeline names to run, e.g. biblio,events")
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

    results = []
    for pipeline in selected:
        print(f"-> {pipeline.name} ...", flush=True)
        ok, duration = run_one(pipeline, args.pipelines_root, output_dir, log_dir)
        status = "ok" if ok else "FAILED"
        print(f"   {status} ({duration:.1f}s)", flush=True)
        results.append((pipeline.name, ok, duration))

    print()
    print(f"{'pipeline':<12} {'status':<8} {'seconds':>8}")
    for name, ok, duration in results:
        print(f"{name:<12} {'ok' if ok else 'FAILED':<8} {duration:>8.1f}")
    print(f"\nlogs: {log_dir}")
    print(f"output: {output_dir}")

    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
