"""Zenodo record file discovery.

Sources (checked 2026-10-30):
- GET https://zenodo.org/api/records/{id} (Accept: application/json, Zenodo legacy JSON)
  files[] = {key, size, checksum: "md5:<hex>", links.self: ".../api/records/{id}/files/{key}/content"}
  Source: zenodo/zenodo-rdm legacy/serializers/schemas/zenodojson.py, zenodo_get client
- DOI 10.5281/zenodo.{N}: N is either a version record or a concept record. A request for a concept id redirects
  to the latest version (invenio-rdm-records resources.py). The httpx client follows redirects.
- The guest rate limit is conservatively taken as 60 requests per minute (developers.zenodo.org rate-limit docs).
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from . import http, urlfiles
from .config import Config
from .state import StateDB

ZENODO_API = "https://zenodo.org/api/records"
_PATTERNS = (
    re.compile(r"10\.5281/zenodo\.(\d+)$", re.I),
    re.compile(r"zenodo\.org/(?:api/)?records?/(\d+)(?:[/?#].*)?$", re.I),
    re.compile(r"^(\d+)$"),
)


def record_id(value: str) -> str:
    v = value.strip()
    for pat in _PATTERNS:
        m = pat.search(v)
        if m:
            return m.group(1)
    raise ValueError(f"Not a Zenodo record number, 10.5281/zenodo.N DOI, or zenodo.org/records/N URL: {value!r}")


def files(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    record: str,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> dict[str, Any]:
    rid = record_id(record)
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    # a concept id redirects to the latest version (the client follows redirects)
    rec = http.request(client, log, "GET", f"{ZENODO_API}/{rid}", headers={"Accept": "application/json"},
                       retries=retries).json()
    meta = rec.get("metadata") or {}
    rows = []
    for f in rec.get("files") or []:
        key = f.get("key") or f.get("filename")
        url = (f.get("links") or {}).get("self") or f"https://zenodo.org/records/{rec.get('id', rid)}/files/{key}"
        rows.append(urlfiles.make_row(url, filename=key, size=f.get("size", f.get("filesize")),
                                      checksum=f.get("checksum")))
    rows = urlfiles.filter_rows(rows, include, exclude)
    resolved = str(rec.get("id") or rec.get("recid") or rid)
    extra = {
        "record_id": resolved,
        "requested_id": rid,
        "resolved_to_latest_version": resolved != rid,
        "concept_record_id": rec.get("conceptrecid"),
        "title": meta.get("title") or rec.get("title"),
        "doi": rec.get("doi") or meta.get("doi"),
        "version": meta.get("version"),
        "publication_date": meta.get("publication_date"),
        "license": (meta.get("license") or {}).get("id") if isinstance(meta.get("license"), dict) else meta.get("license"),
        "resource_type": (meta.get("resource_type") or {}).get("type") if isinstance(meta.get("resource_type"), dict) else None,
    }
    out = urlfiles.save_url_query(cfg, db, "zenodo", rows, {"record": record, "include": include,
                                                              "exclude": exclude}, log.calls, extra)
    if not rec.get("files"):
        out["hint"] = "No files. The record may be restricted or metadata-only."
    return out
