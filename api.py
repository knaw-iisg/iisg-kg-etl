#!/usr/bin/env python3
"""Serve each pipeline's output file over HTTP so it can be downloaded directly.

Stdlib only, matching run_pipelines.py -- no new dependency for what's just
static file serving over a fixed, small set of known names. Pipeline names
and filenames come from run_pipelines.PIPELINES, so the two scripts can't
drift apart.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from run_pipelines import PIPELINES, THIS_REPO

CONTENT_TYPES = {
    ".ttl": "text/turtle; charset=utf-8",
    ".nt": "application/n-triples; charset=utf-8",
}
CHUNK_SIZE = 1024 * 1024


def make_handler(output_dir: Path) -> type[BaseHTTPRequestHandler]:
    files_by_name = {p.name: p.output_file for p in PIPELINES}

    class Handler(BaseHTTPRequestHandler):
        server_version = "iisg-kg-etl-api/1"

        def _send_json(self, status: HTTPStatus, payload: object) -> None:
            body = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _graph_listing(self) -> list[dict]:
            listing = []
            for pipeline in PIPELINES:
                path = output_dir / pipeline.output_file
                entry = {
                    "name": pipeline.name,
                    "file": pipeline.output_file,
                    "url": f"/graphs/{pipeline.name}",
                    "available": path.is_file(),
                }
                if entry["available"]:
                    stat = path.stat()
                    entry["size_bytes"] = stat.st_size
                    entry["modified"] = dt.datetime.fromtimestamp(
                        stat.st_mtime, tz=dt.timezone.utc
                    ).isoformat()
                listing.append(entry)
            return listing

        def _serve_graph(self, name: str) -> None:
            output_file = files_by_name.get(name)
            if output_file is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": f"no such pipeline '{name}'", "known": sorted(files_by_name)},
                )
                return
            path = output_dir / output_file
            if not path.is_file():
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": f"'{name}' has not produced output yet (expected {path})"},
                )
                return
            content_type = CONTENT_TYPES.get(path.suffix, "application/octet-stream")
            size = path.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{output_file}"')
            self.end_headers()
            with path.open("rb") as f:
                while chunk := f.read(CHUNK_SIZE):
                    self.wfile.write(chunk)

        def do_GET(self) -> None:  # noqa: N802 (stdlib's naming convention)
            path = urlsplit(self.path).path

            if path == "/graphs":
                self._send_json(HTTPStatus.OK, self._graph_listing())
                return

            if path.startswith("/graphs/"):
                name = path.removeprefix("/graphs/")
                # Accept an optional .nt/.ttl suffix (e.g. /graphs/dataverse.nt) for
                # bulk loaders that pick their parser from the URL's extension
                # rather than the Content-Type header.
                for ext in (".nt", ".ttl"):
                    if name.endswith(ext):
                        name = name[: -len(ext)]
                        break
                self._serve_graph(name)
                return

            if path == "/":
                links = "\n".join(
                    f'<li><a href="/graphs/{p.name}">{p.name}</a> ({p.output_file})</li>'
                    for p in PIPELINES
                )
                body = f"<p>Available graphs (see <a href=\"/graphs\">/graphs</a> for JSON):</p><ul>{links}</ul>".encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=THIS_REPO / "output",
                         help="directory holding each pipeline's output file (default: ./output)")
    parser.add_argument("--host", default="127.0.0.1",
                         help="address to bind to (default: 127.0.0.1 -- put a reverse proxy in front for public access)")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.output_dir))
    print(f"serving {args.output_dir} on http://{args.host}:{args.port} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
