"""NCI Genomic Data Commons (GDC) discovery.

Reference docs (checked 2026-10-30, source text on the develop branch of the NCI-GDC/gdc-docs repository):
- Search & Retrieval: https://docs.gdc.cancer.gov/API/Users_Guide/Search_and_Retrieval/
  * endpoints /files, /cases, /{endpoint}/_mapping
  * parameters filters, fields, size, from, facets / POST recommended for large queries
  * facets response: data.aggregations.<field>.buckets[{key, doc_count}]
- Downloading Files / Manifest: https://docs.gdc.cancer.gov/API/Users_Guide/Downloading_Files/
  * manifest columns for gdc-client: id, filename, md5, size, state

The manifest is built directly from the same search response instead of a separate return_type=manifest call,
so the summary numbers and the manifest are guaranteed to come from the same API response.

Case (patient) filter: a case submitter_id such as a TCGA barcode (cases.submitter_id) or a GDC case UUID
(cases.case_id). The list is passed directly (case) or as a file (case_file; for a table, case_column).
summary.case_filter records the requested, matched and unmatched case counts so that typos or cases without
the data are not silently dropped.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx

from . import http
from .config import Config
from .security import is_secret_path
from .state import StateDB, new_query_id
from .util import as_list, counts, fmt_bytes, write_tsv

GDC_API = "https://api.gdc.cancer.gov"

# logical filter name -> (files endpoint field, cases endpoint field)
FILTER_FIELDS: dict[str, tuple[str, str]] = {
    "project": ("cases.project.project_id", "project.project_id"),
    "primary_site": ("cases.primary_site", "primary_site"),
    "disease_type": ("cases.disease_type", "disease_type"),
    "sample_type": ("cases.samples.sample_type", "samples.sample_type"),
    "data_category": ("data_category", "files.data_category"),
    "data_type": ("data_type", "files.data_type"),
    "data_format": ("data_format", "files.data_format"),
    "experimental_strategy": ("experimental_strategy", "files.experimental_strategy"),
    "workflow_type": ("analysis.workflow_type", "files.analysis.workflow_type"),
    "access": ("access", "files.access"),
    # case: if the values are UUIDs, CASE_UUID_FIELDS below is used instead (build_filters)
    "case": ("cases.submitter_id", "submitter_id"),
}
CASE_UUID_FIELDS: tuple[str, str] = ("cases.case_id", "case_id")

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# TCGA-A2-A04P, TARGET-20-PADZCG, C3L-00001, UUIDs, etc. Rejects whitespace, quotes and long strings (tokens etc.).
_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Names treated as a header line and skipped (single-column list file without case_column)
CASE_HEADERS = {"case", "case_id", "case_submitter_id", "submitter_id", "gdc_case_id", "tcga_barcode",
                "barcode", "patient", "patient_id"}
MAX_CASES = 20000
MAX_CASE_FILE_BYTES = 5_000_000
# Value lists longer than this are shortened in tool output (the full list is stored in the query record)
COMPACT_OVER = 50

FILE_FIELDS = [
    "file_id",
    "file_name",
    "file_size",
    "md5sum",
    "state",
    "access",
    "data_category",
    "data_type",
    "data_format",
    "experimental_strategy",
    "analysis.workflow_type",
    "cases.case_id",
    "cases.submitter_id",
    "cases.project.project_id",
    "cases.samples.submitter_id",
    "cases.samples.sample_type",
]

CASE_FIELDS = ["case_id", "submitter_id", "project.project_id", "primary_site", "disease_type"]

MANIFEST_COLUMNS = ["id", "filename", "md5", "size", "state"]
METADATA_COLUMNS = [
    "file_id",
    "file_name",
    "file_size",
    "md5sum",
    "access",
    "data_category",
    "data_type",
    "data_format",
    "experimental_strategy",
    "workflow_type",
    "project_id",
    "case_id",
    "case_submitter_id",
    "sample_submitter_id",
    "sample_type",
]

# Reference genome of GDC harmonized data (Zhang et al. 2021, Nat Commun 12:1226)
GDC_REFERENCE = "GRCh38.d1.vd1"


def build_filters(endpoint: str, criteria: dict[str, Any]) -> dict | None:
    idx = 0 if endpoint == "files" else 1
    content = []
    for name, value in criteria.items():
        values = as_list(value)
        if not values:
            continue
        if name not in FILTER_FIELDS:
            raise ValueError(f"Unsupported filter: {name}. Available: {sorted(FILTER_FIELDS)}")
        field = _case_field(idx, values) if name == "case" else FILTER_FIELDS[name][idx]
        content.append({"op": "in", "content": {"field": field, "value": values}})
    if not content:
        return None
    return {"op": "and", "content": content}


def _case_field(idx: int, values: list[str]) -> str:
    kinds = {bool(_UUID.match(v)) for v in values}
    if kinds == {True, False}:
        raise ValueError("The case list mixes barcodes (submitter_id) and UUIDs (case_id). "
                         "Split it and query each kind separately.")
    return CASE_UUID_FIELDS[idx] if True in kinds else FILTER_FIELDS["case"][idx]


def _split_ids(value: Any) -> list[str]:
    """case argument: a list, or a string separated by commas/whitespace."""
    out: list[str] = []
    for v in as_list(value):
        out.extend(x for x in re.split(r"[,\s]+", v.strip()) if x)
    return out


def load_case_file(cfg: Config, path: str | os.PathLike, column: str | None = None) -> tuple[list[str], dict]:
    """Read a case list file. Without column: one ID per line ('#' comments and blank lines ignored; a first line
    that is a column name is skipped). With column: use that column of a TSV with a header row (comma-separated
    if .csv). Malformed lines are reported by line number only; their content is never shown."""
    secrets = [cfg.path("gdc_token_path"), cfg.path("ega_credentials_path"), cfg.path("dbgap_key_path")]
    p = Path(os.path.expanduser(str(path))).resolve()
    if is_secret_path(p, secrets):
        raise ValueError(f"A file that looks like a credential cannot be used as a case list: {p}")
    if not p.is_file():
        raise FileNotFoundError(f"Case list file not found: {p}")
    if p.stat().st_size > MAX_CASE_FILE_BYTES:
        raise ValueError(f"Case list file is too large (> {MAX_CASE_FILE_BYTES // 1_000_000} MB): {p}")
    raw = p.read_bytes()
    text = raw.decode("utf-8-sig")
    ids: list[str] = []
    bad: list[int] = []
    if column:
        reader = csv.DictReader(io.StringIO(text), delimiter="," if p.suffix.lower() == ".csv" else "\t")
        if column not in (reader.fieldnames or []):
            raise ValueError(f"Column {column!r} not found. Available columns: {reader.fieldnames}")
        for lineno, row in enumerate(reader, start=2):
            v = (row.get(column) or "").strip()
            if not v:
                continue
            if _CASE_ID.match(v):
                ids.append(v)
            else:
                bad.append(lineno)
    else:
        seen_data = False
        for lineno, line in enumerate(text.splitlines(), start=1):
            v = line.strip()
            if not v or v.startswith("#"):
                continue
            if "\t" in v or "," in v:
                raise ValueError(f"Line {lineno} has multiple columns. "
                                 "For a table file, pass the column name in case_column.")
            if not seen_data and v.lower() in CASE_HEADERS:
                seen_data = True
                continue
            seen_data = True
            if _CASE_ID.match(v):
                ids.append(v)
            else:
                bad.append(lineno)
    if bad:
        raise ValueError(f"{len(bad)} line(s) are not in case ID format (line numbers {bad[:20]}). "
                         "Their content is not shown.")
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise ValueError(f"Case list is empty: {p}")
    return ids, {"path": str(p), "sha256": hashlib.sha256(raw).hexdigest(), "column": column, "n_ids": len(ids)}


def collect_cases(cfg: Config, case: Any = None, case_file: str | None = None,
                  case_column: str | None = None) -> tuple[list[str], dict]:
    """Merged case ID list from case (given directly) and case_file (list file), plus source info.
    ([], {}) if neither is given."""
    if case_column and not case_file:
        raise ValueError("case_column must be used together with case_file.")
    direct = _split_ids(case)
    bad = [i for i, v in enumerate(direct, start=1) if not _CASE_ID.match(v)]
    if bad:
        raise ValueError(f"case has {len(bad)} value(s) not in case ID format (positions {bad[:20]}).")
    ids = list(direct)
    source: dict[str, Any] = {}
    if direct:
        source["direct"] = len(direct)
    if case_file:
        file_ids, src = load_case_file(cfg, case_file, case_column)
        ids.extend(file_ids)
        source["file"] = src
    ids = list(dict.fromkeys(ids))
    if len(ids) > MAX_CASES:
        raise ValueError(f"{len(ids)} cases given; at most {MAX_CASES} cases can be queried at once. Split the list.")
    return ids, source


def case_match(requested: list[str], found: set[str], source: dict, truncated: bool) -> dict[str, Any]:
    matched = [c for c in requested if c in found]
    unmatched = [c for c in requested if c not in found]
    out: dict[str, Any] = {
        "requested": len(requested),
        "matched": len(matched),
        "unmatched_n": len(unmatched),
        "unmatched": unmatched[:COMPACT_OVER],
        "source": source,
    }
    if truncated:
        out["note"] = "Results were truncated, so the unmatched count may be larger than the true number."
    return out


def compact(obj: Any, limit: int = COMPACT_OVER) -> Any:
    """For tool output: shorten long filter value lists to the first 5 + a count.
    Not used for storage (query records)."""
    if isinstance(obj, dict):
        return {k: (f"{v[:5]} … total {len(v)} (full list in the get_query record)"
                    if k == "value" and isinstance(v, list) and len(v) > limit else compact(v, limit))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [compact(x, limit) for x in obj]
    return obj


def _flatten_file_hit(hit: dict) -> dict[str, Any]:
    cases = hit.get("cases") or []
    samples = [s for c in cases for s in (c.get("samples") or [])]
    return {
        "file_id": hit.get("file_id") or hit.get("id"),
        "file_name": hit.get("file_name"),
        "file_size": int(hit.get("file_size") or 0),
        "md5sum": hit.get("md5sum"),
        "state": hit.get("state"),
        "access": hit.get("access"),
        "data_category": hit.get("data_category"),
        "data_type": hit.get("data_type"),
        "data_format": hit.get("data_format"),
        "experimental_strategy": hit.get("experimental_strategy"),
        "workflow_type": (hit.get("analysis") or {}).get("workflow_type"),
        "project_id": ",".join(sorted({(c.get("project") or {}).get("project_id", "") for c in cases})),
        "case_id": ",".join(c.get("case_id", "") for c in cases),
        "case_submitter_id": ",".join(c.get("submitter_id", "") for c in cases),
        "sample_submitter_id": ",".join(s.get("submitter_id", "") for s in samples),
        "sample_type": ",".join(sorted({s.get("sample_type", "") for s in samples})),
        "_case_ids": [c.get("case_id") for c in cases if c.get("case_id")],
    }


def _paginate(
    client: httpx.Client,
    log: http.ApiLog,
    endpoint: str,
    filters: dict | None,
    fields: list[str],
    max_records: int,
    page_size: int,
    retries: int,
) -> tuple[list[dict], int]:
    hits: list[dict] = []
    total = 0
    offset = 0
    while True:
        body: dict[str, Any] = {
            "fields": ",".join(fields),
            "format": "JSON",
            "size": min(page_size, max_records - len(hits)),
            "from": offset,
        }
        if filters:
            body["filters"] = filters
        resp = http.request(client, log, "POST", f"{GDC_API}/{endpoint}", json=body, retries=retries)
        data = resp.json().get("data", {})
        page = data.get("hits", [])
        total = int(data.get("pagination", {}).get("total", len(page)))
        hits.extend(page)
        offset += len(page)
        if not page or offset >= total or len(hits) >= max_records:
            break
    return hits, total


def search_files(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    project: Any = None,
    primary_site: Any = None,
    disease_type: Any = None,
    sample_type: Any = None,
    data_category: Any = None,
    data_type: Any = None,
    data_format: Any = None,
    experimental_strategy: Any = None,
    workflow_type: Any = None,
    access: Any = None,
    case: Any = None,
    case_file: str | None = None,
    case_column: str | None = None,
    max_files: int = 50000,
    page_size: int = 1000,
) -> dict[str, Any]:
    wanted, case_source = collect_cases(cfg, case, case_file, case_column)
    criteria = {
        "project": project,
        "primary_site": primary_site,
        "disease_type": disease_type,
        "sample_type": sample_type,
        "data_category": data_category,
        "data_type": data_type,
        "data_format": data_format,
        "experimental_strategy": experimental_strategy,
        "workflow_type": workflow_type,
        "access": access,
        "case": wanted,
    }
    criteria = {k: v for k, v in criteria.items() if as_list(v)}
    filters = build_filters("files", criteria)
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    hits, total = _paginate(client, log, "files", filters, FILE_FIELDS, max_files, page_size, retries)
    rows = [_flatten_file_hit(h) for h in hits]

    query_id = new_query_id("gdc")
    case_ids = {cid for r in rows for cid in r["_case_ids"]}
    total_bytes = sum(r["file_size"] for r in rows)
    access_values = {r["access"] for r in rows}
    access_level = "controlled" if "controlled" in access_values else ("open" if rows else None)

    manifest_path = None
    if rows:
        manifest_rows = [
            {"id": r["file_id"], "filename": r["file_name"], "md5": r["md5sum"], "size": r["file_size"],
             "state": r["state"]}
            for r in rows
        ]
        manifest_path = write_tsv(cfg.manifests_dir / f"{query_id}.manifest.tsv", manifest_rows, MANIFEST_COLUMNS)
        write_tsv(cfg.manifests_dir / f"{query_id}.metadata.tsv", rows, METADATA_COLUMNS)

    summary = {
        "n_files": len(rows),
        "api_total": total,
        "truncated": total > len(rows),
        "n_cases": len(case_ids),
        "total_bytes": total_bytes,
        "total_human": fmt_bytes(total_bytes),
        "by_access": counts(r["access"] for r in rows),
        "by_project": counts(r["project_id"] for r in rows),
        "by_data_format": counts(r["data_format"] for r in rows),
        "by_experimental_strategy": counts(r["experimental_strategy"] for r in rows),
        "by_workflow_type": counts(r["workflow_type"] for r in rows),
        "by_sample_type": counts(r["sample_type"] for r in rows),
        "reference_genome": GDC_REFERENCE,
    }
    if wanted:
        found = {c for r in rows for c in r["case_submitter_id"].split(",") if c} | case_ids
        summary["case_filter"] = case_match(wanted, found, case_source, summary["truncated"])
    params = {"endpoint": "files", "criteria": criteria, "filters": filters}
    if case_source:
        params["case_source"] = case_source
    db.save_query(query_id, "gdc", params, log.calls, summary, str(manifest_path) if manifest_path else None,
                  access_level)
    result = {
        "query_id": query_id,
        "summary": summary,
        "access": access_level,
        "manifest_path": str(manifest_path) if manifest_path else None,
        "metadata_path": str(cfg.manifests_dir / f"{query_id}.metadata.tsv") if rows else None,
        "preview": [{k: r[k] for k in METADATA_COLUMNS[:6] + ["case_submitter_id", "sample_type"]} for r in rows[:5]],
        "api_queries": compact(log.calls),
        "filters_json": json.dumps(compact(filters), ensure_ascii=False) if filters else None,
    }
    if wanted and summary["case_filter"]["unmatched_n"]:
        result["case_warning"] = (f"Of the {summary['case_filter']['requested']} requested cases, "
                                  f"{summary['case_filter']['unmatched_n']} have no files matching these criteria "
                                  "(ID typo, different project, or no such data). See summary.case_filter.unmatched.")
    if not rows:
        result["hint"] = ("Zero results. Check the spelling (including capitalization) of field values with "
                          "gdc_get_field_values. e.g. experimental_strategy='WGS', data_format='BAM', access='open'.")
    if summary["truncated"]:
        result["warning"] = (f"Only {len(rows)} of {total} records were retrieved "
                             f"because of the max_files={max_files} limit.")
    return result


def search_cases(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    project: Any = None,
    primary_site: Any = None,
    disease_type: Any = None,
    sample_type: Any = None,
    data_category: Any = None,
    data_type: Any = None,
    data_format: Any = None,
    experimental_strategy: Any = None,
    workflow_type: Any = None,
    access: Any = None,
    case: Any = None,
    case_file: str | None = None,
    case_column: str | None = None,
    max_cases: int = 20000,
    page_size: int = 1000,
) -> dict[str, Any]:
    wanted, case_source = collect_cases(cfg, case, case_file, case_column)
    criteria = {
        "project": project,
        "primary_site": primary_site,
        "disease_type": disease_type,
        "sample_type": sample_type,
        "data_category": data_category,
        "data_type": data_type,
        "data_format": data_format,
        "experimental_strategy": experimental_strategy,
        "workflow_type": workflow_type,
        "access": access,
        "case": wanted,
    }
    criteria = {k: v for k, v in criteria.items() if as_list(v)}
    filters = build_filters("cases", criteria)
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    hits, total = _paginate(client, log, "cases", filters, CASE_FIELDS, max_cases, page_size, retries)
    rows = [
        {
            "case_id": h.get("case_id") or h.get("id"),
            "submitter_id": h.get("submitter_id"),
            "project_id": (h.get("project") or {}).get("project_id"),
            "primary_site": h.get("primary_site"),
            "disease_type": h.get("disease_type"),
        }
        for h in hits
    ]
    query_id = new_query_id("gdc")
    cases_path = None
    if rows:
        cases_path = write_tsv(cfg.manifests_dir / f"{query_id}.cases.tsv", rows,
                               ["case_id", "submitter_id", "project_id", "primary_site", "disease_type"])
    summary = {
        "n_cases": total,
        "n_retrieved": len(rows),
        "truncated": total > len(rows),
        "by_project": counts(r["project_id"] for r in rows),
        "by_primary_site": counts(r["primary_site"] for r in rows),
        "by_disease_type": counts(r["disease_type"] for r in rows),
    }
    if wanted:
        found = {r["submitter_id"] for r in rows if r["submitter_id"]} | {r["case_id"] for r in rows if r["case_id"]}
        summary["case_filter"] = case_match(wanted, found, case_source, summary["truncated"])
    params = {"endpoint": "cases", "criteria": criteria, "filters": filters}
    if case_source:
        params["case_source"] = case_source
    db.save_query(query_id, "gdc", params, log.calls, summary, None, None)
    result = {
        "query_id": query_id,
        "summary": summary,
        "cases_path": str(cases_path) if cases_path else None,
        "preview": rows[:10],
        "api_queries": compact(log.calls),
        "note": "Case search does not create a manifest. Use gdc_search_files to download.",
    }
    if wanted and summary["case_filter"]["unmatched_n"]:
        result["case_warning"] = (f"Of the {summary['case_filter']['requested']} requested cases, "
                                  f"{summary['case_filter']['unmatched_n']} do not match these criteria. "
                                  "See summary.case_filter.unmatched.")
    return result


def get_field_values(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    field: str | None = None,
    endpoint: str = "files",
    search: str | None = None,
    filters_criteria: dict[str, Any] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    if endpoint not in ("files", "cases", "projects"):
        raise ValueError("endpoint must be one of files, cases, projects.")
    if endpoint == "projects" and filters_criteria:
        raise ValueError("filters are not supported on the projects endpoint. Use files or cases.")
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    query_id = new_query_id("gdc")
    if field:
        idx = 0 if endpoint == "files" else 1
        real_field = FILTER_FIELDS[field][idx] if field in FILTER_FIELDS and endpoint != "projects" else field
        params: dict[str, Any] = {"facets": real_field, "size": 0}
        filters = build_filters(endpoint, filters_criteria or {}) if filters_criteria else None
        if filters:
            params["filters"] = json.dumps(filters)
        resp = http.request(client, log, "GET", f"{GDC_API}/{endpoint}", params=params, retries=retries)
        aggs = resp.json().get("data", {}).get("aggregations", {})
        buckets = (aggs.get(real_field) or {}).get("buckets", [])
        values = sorted(({"value": b.get("key"), "count": b.get("doc_count")} for b in buckets),
                        key=lambda x: -(x["count"] or 0))
        summary = {"field": real_field, "endpoint": endpoint, "n_values": len(values)}
        db.save_query(query_id, "gdc", {"field": real_field, "endpoint": endpoint, "filters": filters},
                      log.calls, summary, None, None)
        return {"query_id": query_id, "field": real_field, "values": values[:limit], "api_queries": log.calls}

    resp = http.request(client, log, "GET", f"{GDC_API}/{endpoint}/_mapping", retries=retries)
    fields = resp.json().get("fields", [])
    if search:
        fields = [f for f in fields if search.lower() in f.lower()]
    summary = {"endpoint": endpoint, "n_fields": len(fields), "search": search}
    db.save_query(query_id, "gdc", {"mapping": endpoint, "search": search}, log.calls, summary, None, None)
    return {
        "query_id": query_id,
        "endpoint": endpoint,
        "fields": fields[:limit],
        "n_fields": len(fields),
        "logical_filters": {k: v[0 if endpoint == "files" else 1] for k, v in FILTER_FIELDS.items()},
        "api_queries": log.calls,
    }
