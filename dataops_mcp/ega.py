"""Public metadata discovery for the European Genome-phenome Archive (EGA).

Sources (checked 2026-10-30):
- Official docs "Metadata Public API": https://ega-archive.org/discovery/metadata/public-metadata-api/
  * base URL: https://metadata.ega-archive.org
  * object lookup pattern /{object}/{accession} (studies, datasets, files, samples, runs, analyses, dacs)
  * cross-lookup examples: /datasets/EGAD00001000645/files, /files/EGAF00000056146/dacs
  * list endpoints paginate with limit / offset
- The old API (https://ega-archive.org/metadata/v2/...) is not used.

Note: because of this session's network policy, the endpoints above could not be called directly at
implementation time. This module was written from the documentation and a public client
(ToolUniverse ega_tool, which uses fields such as accession_id, title, num_samples, access_type).
The docs give no response example for file objects, so several candidate keys are accepted for
their field names; confirm the real responses on a server with `pytest -m live`.

All EGA data files are controlled access, and downloading them requires DAC approval.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from . import http
from .config import Config
from .state import StateDB, new_query_id
from .util import fmt_bytes, write_tsv

EGA_METADATA_API = "https://metadata.ega-archive.org"

ACC_RE = re.compile(r"^EGA([SDF])\d{11}$")

_NAME_KEYS = ("filename", "file_name", "relative_path", "name", "display_file_name")
_SIZE_KEYS = ("size", "file_size", "unencrypted_size", "bytes")
_MD5_KEYS = ("unencrypted_checksum", "unencrypted_md5", "checksum", "md5", "md5_checksum")

FILE_COLUMNS = ["accession_id", "dataset_accession", "filename", "size", "checksum", "file_type"]


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def _get(client: httpx.Client, log: http.ApiLog, path: str, retries: int, params: dict | None = None) -> Any:
    # List endpoints (e.g. /datasets/{acc}/files) answer 206 Partial Content even when the page holds
    # every item (checked 2026-11-12 against EGAD00001000645: 42 files, HTTP 206). Pagination is still
    # driven by limit/offset in _list_paginated, so 206 is a normal answer here.
    resp = http.request(client, log, "GET", f"{EGA_METADATA_API}{path}", params=params, retries=retries,
                        ok_status=(200, 206))
    return resp.json()


def _as_items(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "items", "data", "response"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
    return []


def _list_paginated(client: httpx.Client, log: http.ApiLog, path: str, retries: int, limit: int = 1000,
                    max_items: int = 100000) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    offset = 0
    while len(items) < max_items:
        page = _as_items(_get(client, log, path, retries, params={"limit": limit, "offset": offset}))
        new = [p for p in page if str(p.get("accession_id", id(p))) not in seen]
        if not new:
            break
        for p in new:
            seen.add(str(p.get("accession_id", id(p))))
        items.extend(new)
        if len(page) < limit:
            break
        offset += limit
    return items


def _file_row(f: dict, dataset: str | None) -> dict[str, Any]:
    size = _first(f, _SIZE_KEYS)
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        size = None
    return {
        "accession_id": f.get("accession_id"),
        "dataset_accession": dataset,
        "filename": _first(f, _NAME_KEYS),
        "size": size,
        "checksum": _first(f, _MD5_KEYS),
        "file_type": f.get("file_type") or f.get("type"),
    }


def _dataset_brief(d: dict) -> dict[str, Any]:
    return {
        "accession_id": d.get("accession_id"),
        "title": d.get("title"),
        "num_samples": d.get("num_samples"),
        "access_type": d.get("access_type"),
        "technologies": d.get("technologies"),
        "dataset_types": d.get("dataset_types"),
        "policy_accession_id": d.get("policy_accession_id"),
    }


def search(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    accession: str,
    include_files: bool = True,
) -> dict[str, Any]:
    acc = accession.strip().upper()
    m = ACC_RE.match(acc)
    if not m:
        raise ValueError(f"Not an EGA accession: {accession!r} (EGAS/EGAD/EGAF + 11 digits)")
    kind = m.group(1)
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    out: dict[str, Any] = {}
    files: list[dict[str, Any]] = []
    datasets: list[dict[str, Any]] = []

    if kind == "S":
        study = _get(client, log, f"/studies/{acc}", retries)
        datasets = [_dataset_brief(d) for d in _list_paginated(client, log, f"/studies/{acc}/datasets", retries)]
        out["study"] = {k: study.get(k) for k in ("accession_id", "title", "study_type", "pubmed_ids",
                                                   "released_date", "description")}
        if include_files:
            for d in datasets:
                for f in _list_paginated(client, log, f"/datasets/{d['accession_id']}/files", retries):
                    files.append(_file_row(f, d["accession_id"]))
    elif kind == "D":
        ds = _get(client, log, f"/datasets/{acc}", retries)
        datasets = [_dataset_brief(ds)]
        if include_files:
            files = [_file_row(f, acc) for f in _list_paginated(client, log, f"/datasets/{acc}/files", retries)]
    else:
        f = _get(client, log, f"/files/{acc}", retries)
        files = [_file_row(f, None)]

    query_id = new_query_id("ega")
    manifest_path = None
    if files:
        manifest_path = write_tsv(cfg.manifests_dir / f"{query_id}.files.tsv", files, FILE_COLUMNS)
    sizes = [f["size"] for f in files if f["size"] is not None]
    total_bytes = sum(sizes) if sizes else None
    summary = {
        "accession": acc,
        "object_type": {"S": "study", "D": "dataset", "F": "file"}[kind],
        "n_datasets": len(datasets),
        "n_files": len(files),
        "n_files_with_size": len(sizes),
        "total_bytes": total_bytes,
        "total_human": fmt_bytes(total_bytes),
        "datasets": datasets,
    }
    params = {"accession": acc, "include_files": include_files}
    db.save_query(query_id, "ega", params, log.calls, summary, str(manifest_path) if manifest_path else None,
                  "controlled")
    result = {
        "query_id": query_id,
        "summary": summary,
        "access": "controlled",
        "manifest_path": str(manifest_path) if manifest_path else None,
        "preview": files[:5],
        "api_queries": log.calls,
        "note": "All EGA files are controlled access. Confirm DAC approval and institutional rules before downloading.",
        **out,
    }
    if files and len(sizes) < len(files):
        result["warning"] = "Size field not found for some files. The total size is a lower bound."
    return result
