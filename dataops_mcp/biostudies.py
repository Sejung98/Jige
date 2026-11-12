"""ArrayExpress (EMBL-EBI BioStudies) processed-data discovery.

Sources (checked 2026-10-30):
- Bioconductor ArrayExpress package (rewritten on top of BioStudies) R/getAE.r:
  GET https://www.ebi.ac.uk/biostudies/api/v1/studies/{acc}  (section -> subsections -> files tables)
  GET https://www.ebi.ac.uk/biostudies/api/v1/studies/{acc}/info  (httpLink/ftpLink, file count)
  Download URL = {httpLink}/Files/{path}   NB: httpLink uses its own sharding rule, so never build it by hand
- The study JSON of a large study may not list every file, so the paged listing GET /studies/{acc}/files?limit=&offset=
  is used as a fallback (it returns only 25 by default, so limit/offset are set explicitly). Both field casings
  (size/Size, path/Name) are accepted.
- Raw sequencing reads are mostly in ENA (Type=ENA in section.links). Fetch those with sra_search.
- No checksums (size-only verification). Mirrors can serve truncated files with HTTP 200, so the download script
  checks sizes.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from . import http, urlfiles
from .config import Config
from .state import StateDB

BIOSTUDIES_API = "https://www.ebi.ac.uk/biostudies/api/v1"
ACC_RE = re.compile(r"^E-[A-Z]{4}-\d+$")

_SUBDIR = {"processed data": "processed", "raw data": "raw", "mage-tab files": "magetab", "array designs": "adf"}


def _attr(items: list[dict] | None, name: str) -> Any:
    for a in items or []:
        if str(a.get("name", "")).lower() == name.lower():
            return a.get("value")
    return None


def _flatten(x: Any) -> list[dict]:
    if isinstance(x, dict):
        return [x]
    out: list[dict] = []
    for item in x or []:
        out.extend(_flatten(item))
    return out


def walk_files(section: dict, current: str = "") -> list[dict[str, Any]]:
    """Collect the file list from the section tree of the study JSON."""
    stype = str(section.get("type") or "").lower()
    subdir = _SUBDIR.get(stype, current)
    found = [{"path": f.get("path") or f.get("name"), "size": f.get("size", f.get("Size")), "subdir": subdir}
             for f in _flatten(section.get("files")) if f.get("type", "file") == "file"]
    for sub in _flatten(section.get("subsections")):
        found.extend(walk_files(sub, subdir))
    return [f for f in found if f["path"]]


def ena_links(section: dict) -> list[str]:
    out = []
    for link in _flatten(section.get("links")):
        if str(_attr(link.get("attributes"), "Type") or "").upper() == "ENA" and link.get("url"):
            out.append(link["url"])
    for sub in _flatten(section.get("subsections")):
        out.extend(ena_links(sub))
    return sorted(set(out))


def files(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    accession: str,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> dict[str, Any]:
    acc = accession.strip().upper()
    if not ACC_RE.match(acc):
        raise ValueError(f"Not an ArrayExpress accession: {accession!r} (e.g. E-MTAB-9697, E-GEOD-70845)")
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    study = http.request(client, log, "GET", f"{BIOSTUDIES_API}/studies/{acc}", retries=retries).json()
    info = http.request(client, log, "GET", f"{BIOSTUDIES_API}/studies/{acc}/info", retries=retries).json()
    if not study or not info:
        raise ValueError(f"{acc} not found in BioStudies.")
    http_link = str(info.get("httpLink") or "").rstrip("/")
    if not http_link:
        raise http.ApiError(f"{acc} has no httpLink (the study may be private).")
    section = study.get("section") or {}
    listed = walk_files(section)

    expected = info.get("files")
    if isinstance(expected, int) and expected > len(listed):
        seen = {f["path"] for f in listed}
        offset, limit = 0, 200
        while True:
            page = http.request(client, log, "GET", f"{BIOSTUDIES_API}/studies/{acc}/files",
                                params={"limit": limit, "offset": offset}, retries=retries).json()
            items = page.get("items") or []
            for it in items:
                path = it.get("path") or it.get("Name") or it.get("name")
                if path and path not in seen and str(it.get("type", "file")).lower() == "file":
                    listed.append({"path": path, "size": it.get("size", it.get("Size")), "subdir": ""})
                    seen.add(path)
            total = (page.get("pagination") or {}).get("total", 0)
            offset += limit
            if not items or offset >= total:
                break

    rows = [urlfiles.make_row(f"{http_link}/Files/{f['path']}", filename=f["path"].rsplit("/", 1)[-1],
                              subdir="/".join(x for x in (f["subdir"], f["path"].rsplit("/", 1)[0] if "/" in f["path"] else "") if x),
                              size=f["size"])
            for f in listed]
    rows = urlfiles.filter_rows(rows, include, exclude)
    enas = ena_links(section)
    extra = {
        "accession": acc,
        "title": _attr(study.get("attributes"), "Title") or _attr(section.get("attributes"), "Title"),
        "release_date": _attr(study.get("attributes"), "ReleaseDate"),
        "study_type": _attr(section.get("attributes"), "Study type"),
        "organism": _attr(section.get("attributes"), "Organism"),
        "ena_accessions": enas,
        "checksums": "BioStudies provides no checksums; files are verified by size only.",
    }
    out = urlfiles.save_url_query(cfg, db, "arrayexpress", rows, {"accession": acc, "include": include,
                                                                    "exclude": exclude}, log.calls, extra)
    if enas:
        out["notes"] = [f"Raw reads are in ENA {', '.join(enas)}. For FASTQ, use sra_search(accession='{enas[0]}')."]
    return out
