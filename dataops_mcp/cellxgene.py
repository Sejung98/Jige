"""CZ CELLxGENE Discover single-cell dataset discovery.

Sources (checked 2026-10-30, chanzuckerberg/single-cell-data-portal backend/curation/api/curation-api.yml):
- base: https://api.cellxgene.cziscience.com/curation/v1  (public GETs need no auth; no pagination)
- GET /collections               : list of public collections. datasets are previews (no assets):
                                   dataset_id, assay, disease, organism, tissue (each [{label, ontology_term_id}])
- GET /collections/{collection_id}: full dataset metadata + assets[{filetype, url, filesize}]
  filetype: H5AD, RDS (not generated for new versions since late 2024), ATAC_FRAGMENT, ATAC_INDEX.
  filesize -1 = unknown. No checksums.
- 410 = withdrawn (tombstoned) collection
- Asset URLs are keyed by dataset_version_id (immutable version) -> use them as-is for reproducibility.
"""

from __future__ import annotations

from typing import Any

import httpx

from . import http, urlfiles
from .config import Config
from .state import StateDB
from .util import counts, write_tsv

CXG_API = "https://api.cellxgene.cziscience.com/curation/v1"
ASSET_TYPES = ("H5AD", "RDS", "ATAC_FRAGMENT", "ATAC_INDEX")
DATASET_COLUMNS = ["dataset_id", "dataset_version_id", "title", "collection_id", "collection_name", "organism",
                   "tissue", "disease", "assay", "cell_count", "files"]


def _labels(items: Any) -> list[str]:
    return [str(i.get("label")) for i in items or [] if isinstance(i, dict) and i.get("label")]


def _match(values: list[str], wanted: list[str]) -> bool:
    if not wanted:
        return True
    low = [v.lower() for v in values]
    return any(w.lower() in v for w in wanted for v in low)


def _as_list(v: Any) -> list[str]:
    if not v:
        return []
    return [v] if isinstance(v, str) else list(v)


def search(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    organism: Any = None,
    tissue: Any = None,
    disease: Any = None,
    assay: Any = None,
    text: str | None = None,
    collection_id: str | None = None,
    asset_types: list[str] | None = None,
    max_collections: int = 30,
) -> dict[str, Any]:
    types = [t.upper() for t in (asset_types or ["H5AD"])]
    if set(types) - set(ASSET_TYPES):
        raise ValueError(f"asset_types must be chosen from {ASSET_TYPES}.")
    crit = {"organism": _as_list(organism), "tissue": _as_list(tissue), "disease": _as_list(disease),
            "assay": _as_list(assay)}
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    notes: list[str] = []

    if collection_id:
        coll_ids = [collection_id]
    else:
        listing = http.request(client, log, "GET", f"{CXG_API}/collections", retries=retries).json()
        coll_ids = []
        for c in listing:
            if text and text.lower() not in f"{c.get('name', '')} {c.get('description', '')}".lower():
                continue
            if any(all(_match(_labels(d.get(k)), v) for k, v in crit.items()) for d in c.get("datasets", [])):
                coll_ids.append(c["collection_id"])
        if len(coll_ids) > max_collections:
            notes.append(f"Fetched {max_collections} of {len(coll_ids)} matching collections. Narrow the criteria.")
            coll_ids = coll_ids[:max_collections]

    rows, datasets = [], []
    for cid in coll_ids:
        try:
            coll = http.request(client, log, "GET", f"{CXG_API}/collections/{cid}", retries=retries).json()
        except http.ApiError as exc:
            if "HTTP 410" in str(exc) or "HTTP 404" in str(exc):
                notes.append(f"collection {cid}: withdrawn or not found")
                continue
            raise
        for d in coll.get("datasets", []):
            if not all(_match(_labels(d.get(k)), v) for k, v in crit.items()):
                continue
            files = []
            for a in d.get("assets", []):
                if str(a.get("filetype", "")).upper() not in types or not a.get("url"):
                    continue
                size = a.get("filesize")
                row = urlfiles.make_row(a["url"], subdir=cid, size=size if isinstance(size, int) and size > 0 else None)
                rows.append(row)
                files.append(row["filename"])
            datasets.append({
                "dataset_id": d.get("dataset_id"),
                "dataset_version_id": d.get("dataset_version_id"),
                "title": d.get("title"),
                "collection_id": cid,
                "collection_name": coll.get("name"),
                "organism": ",".join(_labels(d.get("organism"))),
                "tissue": ",".join(_labels(d.get("tissue"))),
                "disease": ",".join(_labels(d.get("disease"))),
                "assay": ",".join(_labels(d.get("assay"))),
                "cell_count": d.get("cell_count"),
                "files": ",".join(files),
            })
    extra = {
        "n_datasets": len(datasets),
        "n_collections": len({d["collection_id"] for d in datasets}),
        "total_cells": sum(int(d["cell_count"] or 0) for d in datasets),
        "by_organism": counts(d["organism"] for d in datasets),
        "by_assay": counts(d["assay"] for d in datasets),
        "by_disease": dict(list(counts(d["disease"] for d in datasets).items())[:15]),
        "asset_types": types,
        "checksums": "The CELLxGENE API provides no checksums; files are verified by size only.",
    }
    params = {"criteria": crit, "text": text, "collection_id": collection_id, "asset_types": types}
    out = urlfiles.save_url_query(cfg, db, "cellxgene", rows, params, log.calls, extra)
    if datasets:
        meta_path = write_tsv(cfg.manifests_dir / f"{out['query_id']}.datasets.tsv", datasets, DATASET_COLUMNS)
        out["datasets_path"] = str(meta_path)
        out["datasets_preview"] = datasets[:8]
    out["notes"] = notes + ["File names are dataset_version_id (immutable version). "
                            "Use the datasets_path table to map them to dataset titles."]
    if "RDS" in types:
        out["notes"].append("RDS (Seurat) is not generated for new versions since late 2024, so many datasets lack it.")
    return out
