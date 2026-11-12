"""NCBI GEO processed-data (supplementary, series matrix) discovery.

Raw reads (FASTQ) are handled by sra.py (GSE -> SRA run); this module covers the files deposited in GEO itself.

Sources (checked 2026-10-30):
- E-utilities esummary db=gds record fields (accession, title, gdstype, taxon, n_samples, suppfile, ftplink,
  bioproject, extrelations, pubmedids): R package GEOquery R/searchGEO.R and captured live responses
- Directory layout: https://ftp.ncbi.nlm.nih.gov/geo/series/{stub}/{GSE}/suppl/ , .../matrix/
  stub = last 1-3 digits replaced with 'nnn' (GSE1234 -> GSE1nnn, GSE123 -> GSEnnn)
  GEOquery R/getGEOSuppFiles.R (gsub("\\d{1,3}$", "nnn", acc)), getDirListing()
- Directory listings are Apache HTML indexes (sizes are approximations like '17M'), so exact sizes come from
  HEAD Content-Length.
- GEO provides no checksums (size-only verification).
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from . import http, urlfiles
from .config import Config
from .sra import EUTILS, _eutils_params
from .state import StateDB

GEO_HTTPS = "https://ftp.ncbi.nlm.nih.gov/geo"
ACC_RE = re.compile(r"^(GSE|GSM)\d+$")
_HREF = re.compile(r'<a href="([^"?/][^"]*)">', re.I)


def stub(accession: str) -> str:
    return re.sub(r"\d{1,3}$", "nnn", accession)


def series_dir(accession: str) -> str:
    kind = "series" if accession.startswith("GSE") else "samples"
    return f"{GEO_HTTPS}/{kind}/{stub(accession)}/{accession}"


def parse_index(html: str) -> list[str]:
    """File names from an Apache directory index, skipping parent/sort links, subdirectories and filelist.txt."""
    names = []
    for href in _HREF.findall(html):
        if href.startswith(("http", "/")) or href.endswith("/"):
            continue
        if href == "filelist.txt":
            continue
        names.append(href)
    return names


def _esummary(cfg: Config, client: httpx.Client, log: http.ApiLog, acc: str, retries: int) -> dict[str, Any]:
    es = http.request(client, log, "GET", f"{EUTILS}/esearch.fcgi",
                      params=_eutils_params(cfg, db="gds", term=f"{acc}[ACCN]", retmode="json"), retries=retries).json()
    uids = es.get("esearchresult", {}).get("idlist", [])
    if not uids:
        raise ValueError(f"{acc} not found in GEO.")
    summ = http.request(client, log, "GET", f"{EUTILS}/esummary.fcgi",
                        params=_eutils_params(cfg, db="gds", id=",".join(uids[:500]), retmode="json"),
                        retries=retries).json()
    result = summ.get("result")
    if not result:
        raise http.ApiError(f"esummary response has no result: {str(summ)[:200]}")
    for uid in result.get("uids", uids):
        rec = result.get(uid, {})
        if str(rec.get("accession", "")).upper() == acc:
            return rec
    raise ValueError(f"No {acc} record in the esummary result.")


def _list_dir(client: httpx.Client, log: http.ApiLog, url: str, retries: int) -> list[str] | None:
    try:
        resp = http.request(client, log, "GET", url + "/", retries=retries)
    except http.ApiError as exc:
        if "404" in str(exc):
            return None
        raise
    if re.search(r"recaptcha|Checking your browser", resp.text, re.I):
        raise http.ApiError(f"{url}: NCBI returned a bot-check page. Try again later.")
    return parse_index(resp.text)


def _head_size(client: httpx.Client, log: http.ApiLog, url: str, retries: int) -> int | str:
    try:
        resp = http.request(client, log, "HEAD", url, retries=retries)
    except http.ApiError:
        return ""
    return int(resp.headers.get("content-length") or 0) or ""


def files(
    cfg: Config,
    db: StateDB,
    client: httpx.Client,
    *,
    accession: str,
    include_matrix: bool = True,
    include_samples: bool = False,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    max_samples: int = 200,
) -> dict[str, Any]:
    acc = accession.strip().upper()
    if not ACC_RE.match(acc):
        raise ValueError(f"Must be a GSE or GSM accession: {accession!r}")
    log = http.ApiLog()
    retries = int(cfg.get("http", {}).get("retries", 3))
    rec = _esummary(cfg, client, log, acc, retries)
    base = series_dir(acc)

    candidates: list[tuple[str, str]] = []  # (url, subdir)
    for name in _list_dir(client, log, f"{base}/suppl", retries) or []:
        candidates.append((f"{base}/suppl/{name}", "suppl"))
    if include_matrix and acc.startswith("GSE"):
        for name in _list_dir(client, log, f"{base}/matrix", retries) or []:
            candidates.append((f"{base}/matrix/{name}", "matrix"))
    samples = [s.get("accession") for s in rec.get("samples", []) if s.get("accession")]
    notes: list[str] = []
    if include_samples and acc.startswith("GSE"):
        for gsm in samples[:max_samples]:
            sdir = series_dir(gsm)
            for name in _list_dir(client, log, f"{sdir}/suppl", retries) or []:
                candidates.append((f"{sdir}/suppl/{name}", f"samples/{gsm}"))
        if len(samples) > max_samples:
            notes.append(f"Queried only {max_samples} of {len(samples)} GSMs (max_samples).")

    rows = urlfiles.filter_rows([urlfiles.make_row(u, subdir=d) for u, d in candidates], include, exclude)
    for row in rows:
        row["size"] = _head_size(client, log, row["url"], retries)

    sra_studies = [x.get("targetobject") for x in rec.get("extrelations", []) if x.get("relationtype") == "SRA"]
    if sra_studies or rec.get("bioproject"):
        notes.append("For raw reads (FASTQ), use sra_search(accession='%s')." % (rec.get("bioproject") or acc))
    if any(r["filename"].endswith("_RAW.tar") for r in rows) and not include_samples:
        notes.append("_RAW.tar bundles the per-sample files. To download them per sample, set include_samples=True.")
    extra = {
        "accession": acc,
        "title": rec.get("title"),
        "gdstype": rec.get("gdstype"),
        "taxon": rec.get("taxon"),
        "n_samples": rec.get("n_samples"),
        "suppfile_types": rec.get("suppfile"),
        "bioproject": rec.get("bioproject"),
        "sra_studies": sra_studies,
        "pubmed_ids": rec.get("pubmedids", []),
        "release_date": rec.get("pdat"),
        "checksums": "GEO provides no checksums; files are verified by size only.",
    }
    params = {"accession": acc, "include_matrix": include_matrix, "include_samples": include_samples,
              "include": include, "exclude": exclude}
    out = urlfiles.save_url_query(cfg, db, "geo", rows, params, log.calls, extra)
    out["notes"] = notes
    if not rows:
        out["hint"] = "No public supplementary/matrix files. The series may have raw reads only (sra_search)."
    return out
