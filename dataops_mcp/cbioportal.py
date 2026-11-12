"""cBioPortal study discovery and datahub tarball manifest.

Sources (checked 2026-10-30, cBioPortal/cbioportal, cbioportal-frontend, and cBioPortalData source):
- GET https://www.cbioportal.org/api/studies?keyword=..&projection=DETAILED  (no auth; omitting paging returns all)
  keyword: each whitespace-separated word is a case-insensitive substring match (AND) on study name, ID, cancer type
  study fields: studyId, name, description, cancerTypeId, cancerType{name}, allSampleCount, referenceGenome,
  pmid, citation, publicStudy, readPermission
- GET /api/studies/{studyId} (404 if absent)
- Full study data: https://datahub.assets.cbioportal.org/{studyId}.tar.gz
  Downloadable studies: https://datahub.assets.cbioportal.org/study_list.json (array of IDs)
  (same URLs as cbioportal-frontend urls.ts and cBioPortalData R/cBioDataPack.R)
- Tarball sizes/checksums are not published -> try HEAD Content-Length; if absent, the size is unknown.
"""

from __future__ import annotations

from typing import Any

import httpx

from . import http, urlfiles
from .config import Config
from .state import StateDB
from .util import counts

CBIO_API = "https://www.cbioportal.org/api"
DATAHUB = "https://datahub.assets.cbioportal.org"


def search(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    keyword: str | None = None,
    study_ids: list[str] | None = None,
    max_studies: int = 50,
) -> dict[str, Any]:
    if not keyword and not study_ids:
        raise ValueError("keyword or study_ids is required.")
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    studies: list[dict[str, Any]] = []
    if study_ids:
        for sid in study_ids:
            try:
                studies.append(http.request(client, log, "GET", f"{CBIO_API}/studies/{sid}", retries=retries).json())
            except http.ApiError as exc:
                if "HTTP 404" not in str(exc):
                    raise
    if keyword:
        found = http.request(client, log, "GET", f"{CBIO_API}/studies",
                             params={"keyword": keyword, "projection": "DETAILED"}, retries=retries).json()
        studies.extend(found if isinstance(found, list) else [])
    uniq: dict[str, dict] = {}
    for s in studies:
        if s.get("studyId") and s.get("readPermission", True) is not False:
            uniq.setdefault(s["studyId"], s)
    selected = list(uniq.values())
    truncated = len(selected) > max_studies
    selected = selected[:max_studies]

    try:
        available = set(http.request(client, log, "GET", f"{DATAHUB}/study_list.json", retries=retries).json())
    except (http.ApiError, ValueError):
        available = None
    rows, missing = [], []
    for s in selected:
        sid = s["studyId"]
        if available is not None and sid not in available:
            missing.append(sid)
            continue
        url = f"{DATAHUB}/{sid}.tar.gz"
        size: Any = ""
        try:
            resp = http.request(client, log, "HEAD", url, retries=retries)
            size = int(resp.headers.get("content-length") or 0) or ""
        except http.ApiError:
            pass
        rows.append(urlfiles.make_row(url, size=size))
    brief = [{
        "studyId": s.get("studyId"),
        "name": s.get("name"),
        "cancer_type": (s.get("cancerType") or {}).get("name") or s.get("cancerTypeId"),
        "allSampleCount": s.get("allSampleCount"),
        "referenceGenome": s.get("referenceGenome"),
        "pmid": s.get("pmid"),
    } for s in selected]
    extra = {
        "n_studies": len(selected),
        "truncated": truncated,
        "total_samples": sum(int(s.get("allSampleCount") or 0) for s in selected),
        "by_reference_genome": counts(s.get("referenceGenome") for s in selected),
        "by_cancer_type": counts(b["cancer_type"] for b in brief),
        "studies": brief,
        "not_on_datahub": missing,
    }
    out = urlfiles.save_url_query(cfg, db, "cbioportal", rows, {"keyword": keyword, "study_ids": study_ids},
                                  log.calls, extra)
    if len([g for g in extra["by_reference_genome"] if g != "NA"]) > 1:
        out["warning"] = f"hg19 and hg38 studies are mixed: {extra['by_reference_genome']}"
    if missing:
        out["notes"] = [f"No datahub tarball for: {missing}. Download from the portal UI or pick another version."]
    return out
