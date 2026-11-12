"""Shared code for URL-manifest downloads.

Repositories downloaded as a 'list of file URLs' -- GEO supplementary files, ArrayExpress,
ENCODE, CELLxGENE, cBioPortal, PRIDE, Zenodo -- all use the same manifest format.

manifest TSV columns: url, filename, subdir, size, checksum
- size: bytes (blank if unknown)
- checksum: "md5:<hex>" / "sha1:<hex>" / "sha256:<hex>" (blank if unknown)
download location: <dest>/<subdir>/<filename>
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from . import http
from .config import Config
from .state import StateDB, new_query_id
from .util import fmt_bytes, read_tsv, write_tsv

URL_MANIFEST_COLUMNS = ["url", "filename", "subdir", "size", "checksum"]

# source names downloaded via a URL manifest
URL_SOURCES = ("geo", "arrayexpress", "encode", "cellxgene", "cbioportal", "pride", "zenodo", "url")

CHECKSUM_ALGOS = ("md5", "sha1", "sha256")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._+=-]")


def safe_relpath(part: str) -> str:
    """Strip unsafe characters and parent-directory traversal from a path component."""
    pieces = [p for p in str(part).replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(_SAFE_NAME.sub("_", p) for p in pieces)


def filename_from_url(url: str) -> str:
    name = unquote(Path(urlparse(url).path).name)
    return safe_relpath(name) or "download"


def normalize_checksum(value: str | None, default_algo: str = "md5") -> str:
    if not value:
        return ""
    value = str(value).strip()
    if ":" in value:
        algo, digest = value.split(":", 1)
        algo = algo.lower().replace("-", "")
    else:
        algo, digest = default_algo, value
    if algo not in CHECKSUM_ALGOS or not re.fullmatch(r"[0-9a-fA-F]+", digest):
        return ""
    return f"{algo}:{digest.lower()}"


def make_row(url: str, filename: str | None = None, subdir: str = "", size: Any = None,
             checksum: str | None = None, default_algo: str = "md5") -> dict[str, Any]:
    scheme = urlparse(url).scheme
    if scheme not in ("https", "http"):
        raise ValueError(f"Only https/http URLs are supported: {url}")
    try:
        size_val = int(size) if size not in (None, "") else ""
    except (TypeError, ValueError):
        size_val = ""
    return {
        "url": url,
        "filename": safe_relpath(filename) if filename else filename_from_url(url),
        "subdir": safe_relpath(subdir),
        "size": size_val,
        "checksum": normalize_checksum(checksum, default_algo),
    }


def save_url_query(
    cfg: Config,
    db: StateDB,
    source: str,
    rows: list[dict[str, Any]],
    params: dict[str, Any],
    api_calls: list[dict],
    summary_extra: dict[str, Any],
    access: str = "open",
) -> dict[str, Any]:
    """Write a URL manifest, save it as a query, and return the common summary."""
    if source not in URL_SOURCES:
        raise ValueError(source)
    query_id = new_query_id(source)
    manifest = None
    if rows:
        manifest = write_tsv(cfg.manifests_dir / f"{query_id}.urls.tsv", rows, URL_MANIFEST_COLUMNS)
    sizes = [int(r["size"]) for r in rows if r.get("size") not in ("", None)]
    total = sum(sizes) if sizes else None
    summary = {
        "n_files": len(rows),
        "n_files_with_size": len(sizes),
        "n_files_with_checksum": sum(1 for r in rows if r.get("checksum")),
        "total_bytes": total,
        "total_human": fmt_bytes(total),
        "download": "url",
        **summary_extra,
    }
    db.save_query(query_id, source, params, api_calls, summary, str(manifest) if manifest else None, access)
    out: dict[str, Any] = {
        "query_id": query_id,
        "summary": summary,
        "access": access,
        "manifest_path": str(manifest) if manifest else None,
        "preview": [{k: r[k] for k in ("filename", "subdir", "size", "checksum")} for r in rows[:8]],
        "api_queries": api_calls,
    }
    if rows and len(sizes) < len(rows):
        out["size_note"] = f"{len(rows) - len(sizes)} files have no size information, so the total size is a lower bound."
    return out


def read_url_manifest(path: str) -> list[dict[str, str]]:
    return read_tsv(path)


def inspect_urls(cfg: Config, db: StateDB, client: httpx.Client, urls: list[str], subdir: str = "") -> dict[str, Any]:
    """Check arbitrary public file URLs with HEAD (size, existence) and save them as a manifest. For long-tail resources."""
    if not urls:
        raise ValueError("urls is empty.")
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    rows, problems = [], []
    for url in urls:
        row = make_row(url, subdir=subdir)
        try:
            resp = http.request(client, log, "HEAD", url, retries=retries, ok_status=(200,))
            row["size"] = int(resp.headers.get("content-length") or 0) or ""
        except http.ApiError as exc:
            problems.append({"url": url, "error": str(exc)[:200]})
            continue
        rows.append(row)
    out = save_url_query(cfg, db, "url", rows, {"urls": urls, "subdir": subdir}, log.calls,
                         {"source_note": "user-specified URLs (existence and size checked with HEAD)"})
    if problems:
        out["unreachable"] = problems
    return out


def filter_rows(rows: list[dict[str, Any]], include: list[str] | None, exclude: list[str] | None) -> list[dict[str, Any]]:
    """Case-insensitive glob filter on filename/subpath. If include is given, at least one pattern must match."""
    from fnmatch import fnmatch

    def matches(row: dict[str, Any], patterns: list[str]) -> bool:
        rel = "/".join(x for x in (row.get("subdir"), row.get("filename")) if x).lower()
        name = str(row.get("filename", "")).lower()
        return any(fnmatch(name, p.lower()) or fnmatch(rel, p.lower()) for p in patterns)

    out = rows
    if include:
        out = [r for r in out if matches(r, include)]
    if exclude:
        out = [r for r in out if not matches(r, exclude)]
    return out
