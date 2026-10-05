# iisg-kg-etl

Runs all eight KNAW/IISG linked-data ETL pipelines in one go and collects
their output locally:
[biblio-etl](https://github.com/knaw-iisg/biblio-etl),
[archive-etl](https://github.com/knaw-iisg/archive-etl),
[findingaid-etl](https://github.com/knaw-iisg/findingaid-etl),
[authorities-etl](https://github.com/knaw-iisg/authorities-etl),
[events-etl](https://github.com/knaw-iisg/events-etl),
[orcid-etl](https://github.com/knaw-iisg/orcid-etl),
[dataverse-etl](https://github.com/knaw-iisg/dataverse-etl) and
[identity-etl](https://github.com/knaw-iisg/identity-etl).

This repo doesn't import any of their code -- it shells out to each
pipeline's own CLI, in its own venv, in sequence. If one pipeline fails the
others still run; the run exits non-zero overall if any of them failed.

## Public instance

This repo's combined output feeds
[triplestore](https://github.com/knaw-iisg/triplestore), which serves the
merged result as a public knowledge graph: browsable at
**https://kb.zijdeman.nl**
([iisg-kb-viewer](https://github.com/knaw-iisg/iisg-kb-viewer)), queryable
directly at **https://sparql.zijdeman.nl**, or via QLever's own query UI at
**https://kg.zijdeman.nl**.

## Setup

Expects each pipeline repo to be checked out as a sibling directory with its
own `.venv` already set up (`python -m venv .venv && .venv/bin/pip install
-e .`, per that repo's own README). Point `--pipelines-root` elsewhere if
your checkouts don't live next to this repo.

dataverse-etl needs `DATAVERSE_API_KEY` to see restricted datasets. Either
export it before running, or drop it in a local `.env` file here
(`DATAVERSE_API_KEY=...`, gitignored, one `KEY=VALUE` per line).

## Usage

```bash
python run_pipelines.py
```

Output lands in `output/` (one file per pipeline, gitignored -- this is
local scratch space, not a release artifact). Per-pipeline logs go to
`logs/<run-id>/<pipeline>.log`.

```bash
python run_pipelines.py --only biblio,events   # run a subset
python run_pipelines.py --output-dir /path/to/triplestore/sources
```

## Downloading each pipeline's raw output

```bash
python api.py   # serves output/ on http://127.0.0.1:8787
```

| Endpoint | Returns |
|---|---|
| `GET /graphs` | JSON listing of all eight pipelines: name, filename, availability, size, last-modified |
| `GET /graphs/<name>` | that pipeline's output file (e.g. `/graphs/biblio` -> `biblio.nt`), as a download |

This serves whatever's currently in `--output-dir` directly (stdlib only,
no new dependency) -- independent of the merged, indexed copy that
[triplestore](https://github.com/knaw-iisg/triplestore) serves over SPARQL.
Binds to localhost only; put a reverse proxy (e.g. Caddy, as in
triplestore's own [`deploy/`](https://github.com/knaw-iisg/triplestore/tree/main/deploy))
in front for public access, same pattern as the other three public services.

## Notes

- The four MARC/OAI-PMH pipelines (biblio, archive, findingaid, authorities)
  run with `--stream`, writing N-Triples instead of building one in-memory
  graph -- the safer default for an unbounded daily harvest of the full
  catalog.
- orcid-etl's personally-identifying curation data (`colleagues.yaml`, its
  ORCID cache, its quality report) stays in that repo's own `--data-dir`;
  only the resulting Turtle file is redirected into `output/`. Same for
  identity-etl's `identities.yaml`.
- Not yet wired into cron/systemd -- that's the next step, once the script
  itself has proven reliable running by hand.
