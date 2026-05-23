"""Web UI for the lead list QA & scoring tool.

Flow:
    GET  /                 -> upload form
    POST /preview          -> stash parsed CSV, redirect to /map/<token>
    GET  /map/<token>      -> field-mapping form (auto-prefilled)
    POST /process/<token>  -> run pipeline with chosen mapping
    GET  /results/<token>  -> summary + scored leads table
    GET  /download/<token> -> cleaned CSV

Run:  python app.py
"""

from __future__ import annotations

import csv
import io
import secrets
import threading
import time
from collections import OrderedDict
from typing import Any

from flask import (
    Flask, Response, abort, redirect, render_template, request, url_for,
)

from lead_scorer import (
    CLEAN_FIELDNAMES, EXPECTED_FIELDS, RunStats,
    auto_detect_mapping, process_rows,
)


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB upload cap


# --- In-process store --------------------------------------------------------
# Single keyed cache. Each entry has a 'kind' ('upload' or 'result') so the
# routes can validate they're looking at the right artifact.

STORE_TTL = 60 * 60  # 1 hour
_store: dict[str, dict] = {}
_store_lock = threading.Lock()


def _gc() -> None:
    now = time.time()
    with _store_lock:
        for k in [k for k, v in _store.items() if now - v["created"] > STORE_TTL]:
            _store.pop(k, None)


def _put(kind: str, payload: dict) -> str:
    _gc()
    token = secrets.token_urlsafe(12)
    with _store_lock:
        _store[token] = {"kind": kind, "created": time.time(), **payload}
    return token


def _get(token: str, kind: str) -> dict:
    with _store_lock:
        entry = _store.get(token)
    if not entry or entry["kind"] != kind:
        abort(404)
    return entry


# --- Routes ------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html")


@app.post("/preview")
def preview():
    file = request.files.get("csv")
    if not file or not file.filename:
        return render_template("index.html", error="Please choose a CSV file."), 400
    try:
        text = file.stream.read().decode("utf-8-sig", errors="replace")
    except Exception as e:
        return render_template("index.html", error=f"Could not read file: {e}"), 400

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return render_template("index.html", error="CSV appears to be empty."), 400

    headers = list(reader.fieldnames or [])
    if not headers:
        return render_template("index.html", error="CSV has no header row."), 400

    token = _put("upload", {
        "filename": file.filename,
        "headers": headers,
        "rows": rows,
    })
    return redirect(url_for("map_fields", token=token))


@app.get("/map/<token>")
def map_fields(token: str):
    entry = _get(token, "upload")
    detected = auto_detect_mapping(entry["headers"])
    return render_template(
        "map.html",
        token=token,
        filename=entry["filename"],
        headers=entry["headers"],
        rows_count=len(entry["rows"]),
        sample_rows=entry["rows"][:3],
        fields=EXPECTED_FIELDS,
        detected=detected,
    )


@app.post("/process/<token>")
def process_with_mapping(token: str):
    entry = _get(token, "upload")
    enrich = request.form.get("enrich") == "on"

    mapping: dict[str, str] = {}
    for f in EXPECTED_FIELDS:
        src = (request.form.get(f"map[{f['key']}]") or "").strip()
        if src and src not in entry["headers"]:
            return render_template("index.html",
                                   error=f"Mapping for {f['label']} references unknown column."), 400
        mapping[f["key"]] = src

    if not mapping.get("email"):
        return _render_map_error(entry, mapping, "Email is required — please map it.")

    stats, kept = process_rows(entry["rows"], enrich=enrich, mapping=mapping)
    result_token = _put("result", {
        "filename": entry["filename"],
        "stats": stats,
        "kept": kept,
        "mapping": mapping,
    })
    return redirect(url_for("results", token=result_token))


def _render_map_error(entry: dict, mapping: dict, msg: str):
    return render_template(
        "map.html",
        token=_find_token(entry),
        filename=entry["filename"],
        headers=entry["headers"],
        rows_count=len(entry["rows"]),
        sample_rows=entry["rows"][:3],
        fields=EXPECTED_FIELDS,
        detected=mapping,
        error=msg,
    ), 400


def _find_token(entry: dict) -> str:
    with _store_lock:
        for k, v in _store.items():
            if v is entry:
                return k
    abort(404)


@app.get("/results/<token>")
def results(token: str):
    result = _get(token, "result")
    stats: RunStats = result["stats"]
    kept = result["kept"]
    dropped = sum(stats.drops.values())
    sorted_kept = sorted(kept, key=lambda r: r["score"], reverse=True)
    return render_template(
        "results.html",
        token=token,
        filename=result["filename"],
        stats=stats,
        dropped=dropped,
        rows=sorted_kept,
        tier_counts=[
            ("Tier 1", stats.tiers.get("Tier 1", 0)),
            ("Tier 2", stats.tiers.get("Tier 2", 0)),
            ("Tier 3", stats.tiers.get("Tier 3", 0)),
        ],
        drop_reasons=sorted(stats.drops.items(), key=lambda x: -x[1]),
    )


@app.get("/download/<token>")
def download(token: str):
    result = _get(token, "result")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CLEAN_FIELDNAMES, extrasaction="ignore")
    writer.writeheader()
    for r in result["kept"]:
        writer.writerow(r)
    base = (result["filename"] or "leads").rsplit(".", 1)[0]
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{base}_clean.csv"'},
    )


if __name__ == "__main__":
    app.run(debug=True)
