"""DataOps MCP server (stdio).

Run: python -m dataops_mcp.server
In the `mcp` package 2.x, FastMCP was renamed to MCPServer, so both are supported.
"""

import inspect
import os
import shutil
import traceback
from typing import Any, Callable, Optional, Union

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
from mcp.types import ToolAnnotations

from . import (biostudies, catalog, cbioportal, cellxgene, ega, encode, gdc, geo, pride, processing, review,
               scheduler, sra, transfer, urlfiles, zenodo)
from .config import load_config
from .http import make_client
from .security import check_secret_file
from .state import StateDB

INSTRUCTIONS = """\
Genomics DataOps tools. Rules:
1) Metadata numbers (file counts, sizes, accessions) must come only from these tools' results. Never make them up from memory.
2) Every result has a query_id and api_queries (the API calls it is based on). Cite both when reporting to the user.
3) prepare_* tools only generate scripts. Execution happens only through submit_job.
4) submit_job defaults to dry_run=True. Use confirm=True, dry_run=False only after a peer-reviewer PASS and explicit user approval.
"""

server = _Server(name="dataops", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
_TOOL_PARAMS = inspect.signature(server.tool).parameters

StrOrList = Optional[Union[str, list[str]]]


def tool(annotations: ToolAnnotations, meta: Optional[dict] = None) -> Callable:
    kwargs: dict[str, Any] = {"annotations": annotations}
    if meta and "meta" in _TOOL_PARAMS:
        kwargs["meta"] = meta
    return server.tool(**kwargs)


def _ctx():
    cfg = load_config()
    return cfg, StateDB(cfg)


def _run(fn: Callable[[], dict]) -> dict:
    try:
        out = fn()
        out.setdefault("ok", True)
        return out
    except (transfer.SubmissionRefused, transfer.ControlledAccessNotAcknowledged) as exc:
        return {"ok": False, "refused": True, "error_type": type(exc).__name__, "error": str(exc)}
    except Exception as exc:  # tool errors are returned in structured form
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "trace": traceback.format_exc(limit=3)[-1500:] if os.environ.get("DATAOPS_DEBUG") else None,
        }


# ---------------------------------------------------------------- environment
@tool(READ_ONLY)
def check_environment() -> dict:
    """Check the config summary, whether the required CLIs (gdc-client, nextflow, pyega3, sbatch/qsub) exist, and
    credential file permissions. Never reads credential contents."""

    def go() -> dict:
        cfg, _ = _ctx()
        tools = cfg.get("tools", {})
        return {
            "config": cfg.public_summary(),
            "cli": {name: shutil.which(cmd) for name, cmd in {
                "gdc-client": tools.get("gdc_client", "gdc-client"),
                "nextflow": tools.get("nextflow", "nextflow"),
                "pyega3": tools.get("pyega3", "pyega3"),
                "singularity": "singularity",
                "apptainer": "apptainer",
                "java": "java",
                "md5sum": "md5sum",
            }.items()},
            "scheduler_cli": scheduler.available_scheduler_tools(),
            "credentials": [
                check_secret_file(cfg.path("gdc_token_path"), "GDC token"),
                check_secret_file(cfg.path("ega_credentials_path"), "EGA credentials"),
            ],
        }

    return _run(go)


@tool(READ_ONLY)
def list_databases() -> dict:
    """Catalog of supported databases (domain, data types, access level, responsible tool/subagent, download method)
    plus unsupported resources and why. Contains no numbers (numbers come only from the discovery tools)."""
    return _run(catalog.list_databases)


# ---------------------------------------------------------------- discovery: GDC
@tool(READ_ONLY)
def gdc_search_files(
    project: StrOrList = None,
    primary_site: StrOrList = None,
    disease_type: StrOrList = None,
    sample_type: StrOrList = None,
    data_category: StrOrList = None,
    data_type: StrOrList = None,
    data_format: StrOrList = None,
    experimental_strategy: StrOrList = None,
    workflow_type: StrOrList = None,
    access: StrOrList = None,
    case: StrOrList = None,
    case_file: Optional[str] = None,
    case_column: Optional[str] = None,
    max_files: int = 50000,
) -> dict:
    """Search GDC /files (live API). Returns a summary (file count, total size, case count) and the path of a
    gdc-client manifest TSV, saved under a query_id. Example values: project='TCGA-BRCA', experimental_strategy='WGS',
    data_format='BAM', access='open'|'controlled', workflow_type='STAR - Counts'. Check value spelling with
    gdc_get_field_values.
    To narrow by patient: case=['TCGA-A2-A04P', ...] (case submitter_id or GDC case UUID, do not mix) or
    case_file=path to a case list file (one per line, '#' comments; for a table with a header row, case_column=column
    name). Use case_file for many cases. summary.case_filter reports requested, matched and unmatched case counts."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return gdc.search_files(
                cfg, db, client, project=project, primary_site=primary_site, disease_type=disease_type,
                sample_type=sample_type, data_category=data_category, data_type=data_type,
                data_format=data_format, experimental_strategy=experimental_strategy,
                workflow_type=workflow_type, access=access, case=case, case_file=case_file,
                case_column=case_column, max_files=max_files,
            )

    return _run(go)


@tool(READ_ONLY)
def gdc_search_cases(
    project: StrOrList = None,
    primary_site: StrOrList = None,
    disease_type: StrOrList = None,
    sample_type: StrOrList = None,
    data_category: StrOrList = None,
    data_type: StrOrList = None,
    data_format: StrOrList = None,
    experimental_strategy: StrOrList = None,
    workflow_type: StrOrList = None,
    access: StrOrList = None,
    case: StrOrList = None,
    case_file: Optional[str] = None,
    case_column: Optional[str] = None,
    max_cases: int = 20000,
) -> dict:
    """Search GDC /cases (live API). Returns the number of cases matching the criteria (file criteria included) and
    the project/primary_site distribution. case/case_file/case_column work as in gdc_search_files (use them to check
    which cases in a list match the criteria)."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return gdc.search_cases(
                cfg, db, client, project=project, primary_site=primary_site, disease_type=disease_type,
                sample_type=sample_type, data_category=data_category, data_type=data_type,
                data_format=data_format, experimental_strategy=experimental_strategy,
                workflow_type=workflow_type, access=access, case=case, case_file=case_file,
                case_column=case_column, max_cases=max_cases,
            )

    return _run(go)


@tool(READ_ONLY)
def gdc_get_field_values(
    field: Optional[str] = None,
    endpoint: str = "files",
    search: Optional[str] = None,
    filters: Optional[dict[str, Union[str, list[str]]]] = None,
) -> dict:
    """List GDC field values (facets) or field names (_mapping).
    With field: values of that field and their counts
    (e.g. field='experimental_strategy', filters={'project': 'TCGA-BRCA'}).
    Without field: field names of the endpoint (filter by substring with search)."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return gdc.get_field_values(cfg, db, client, field=field, endpoint=endpoint, search=search,
                                        filters_criteria=filters)

    return _run(go)


# ---------------------------------------------------------------- discovery: SRA / EGA
@tool(READ_ONLY)
def sra_search(accession: str, library_strategy: Optional[str] = None, max_runs: int = 20000) -> dict:
    """Look up run-level metadata for SRA/ENA/DDBJ/GEO accessions (PRJNA, PRJEB, SRP, ERP, SRX, SRR, SAMN, GSE, GSM;
    several separated by commas) via the ENA Portal API filereport (GEO is resolved through NCBI E-utilities).
    Runs can be filtered by library_strategy (e.g. 'RNA-Seq', 'WGS')."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return sra.search(cfg, db, client, accession=accession, library_strategy=library_strategy,
                              max_runs=max_runs)

    return _run(go)


@tool(READ_ONLY)
def ega_search(accession: str, include_files: bool = True) -> dict:
    """Look up a study (EGAS)/dataset (EGAD)/file (EGAF) through the EGA public metadata API. Returns the file list and
    total size. All EGA data is controlled access."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return ega.search(cfg, db, client, accession=accession, include_files=include_files)

    return _run(go)


# ---------------------------------------------------------------- discovery: ENCODE / single-cell / cBioPortal
@tool(READ_ONLY)
def encode_search(
    assay_title: StrOrList = None,
    biosample: StrOrList = None,
    target: StrOrList = None,
    organism: StrOrList = None,
    assembly: StrOrList = None,
    search_term: Optional[str] = None,
    limit: int = 500,
) -> dict:
    """Search ENCODE experiments (released).
    e.g. assay_title='ATAC-seq'|'TF ChIP-seq'|'Histone ChIP-seq'|'total RNA-seq', biosample='K562', target='CTCF',
    organism='Homo sapiens', assembly='GRCh38'. Returns an experiment list TSV and distributions."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return encode.search(cfg, db, client, assay_title=assay_title, biosample=biosample, target=target,
                                 organism=organism, assembly=assembly, search_term=search_term, limit=limit)

    return _run(go)


@tool(READ_ONLY)
def encode_files(
    experiments: Optional[list[str]] = None,
    from_query_id: Optional[str] = None,
    assembly: Optional[str] = None,
    output_type: StrOrList = None,
    file_format: StrOrList = None,
    preferred_default_only: bool = False,
) -> dict:
    """Save the released files (list, sizes, md5) of ENCODE experiments (ENCSR...) as a manifest. Also accepts an
    encode_search query_id.
    e.g. assembly='GRCh38', output_type='conservative IDR thresholded peaks', file_format='bed'."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return encode.files(cfg, db, client, experiments=experiments, from_query_id=from_query_id,
                                assembly=assembly, output_type=output_type, file_format=file_format,
                                preferred_default_only=preferred_default_only)

    return _run(go)


@tool(READ_ONLY)
def cellxgene_search(
    organism: StrOrList = None,
    tissue: StrOrList = None,
    disease: StrOrList = None,
    assay: StrOrList = None,
    text: Optional[str] = None,
    collection_id: Optional[str] = None,
    asset_types: Optional[list[str]] = None,
) -> dict:
    """Search single-cell datasets in CZ CELLxGENE Discover (partial label match, case-insensitive). e.g. tissue='lung',
    disease='COVID-19', assay='10x 3', organism='Homo sapiens'. Default asset is H5AD."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return cellxgene.search(cfg, db, client, organism=organism, tissue=tissue, disease=disease, assay=assay,
                                    text=text, collection_id=collection_id, asset_types=asset_types)

    return _run(go)


@tool(READ_ONLY)
def cbioportal_search(keyword: Optional[str] = None, study_ids: Optional[list[str]] = None) -> dict:
    """Search cBioPortal studies (keyword: partial match on name, ID or cancer type) or give study_ids. Returns
    per-study sample counts and reference genome, and a datahub tarball manifest."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return cbioportal.search(cfg, db, client, keyword=keyword, study_ids=study_ids)

    return _run(go)


# ---------------------------------------------------------------- discovery: study files
@tool(READ_ONLY)
def geo_files(
    accession: str,
    include_matrix: bool = True,
    include_samples: bool = False,
    include: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
) -> dict:
    """List the supplementary files and series matrix files (with sizes) of GEO processed data (GSE/GSM) and save
    them as a manifest. include/exclude are filename globs (e.g. ['*.h5', '*counts*'], exclude=['*_RAW.tar']).
    For raw reads use sra_search."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return geo.files(cfg, db, client, accession=accession, include_matrix=include_matrix,
                             include_samples=include_samples, include=include, exclude=exclude)

    return _run(go)


@tool(READ_ONLY)
def arrayexpress_files(
    accession: str, include: Optional[list[str]] = None, exclude: Optional[list[str]] = None
) -> dict:
    """Save the processed-data and MAGE-TAB files (list, sizes) of an ArrayExpress entry (E-MTAB-, E-GEOD-, etc.;
    EBI BioStudies) as a manifest. Also reports the linked ENA study (raw reads)."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return biostudies.files(cfg, db, client, accession=accession, include=include, exclude=exclude)

    return _run(go)


@tool(READ_ONLY)
def pride_files(
    accession: str,
    categories: Optional[list[str]] = None,
    include: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
) -> dict:
    """Save the file list, sizes and MD5 of a PRIDE proteomics project (PXD) as a manifest.
    categories selects RAW, SEARCH, RESULT, PEAK, OTHER, etc."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return pride.files(cfg, db, client, accession=accession, categories=categories, include=include,
                               exclude=exclude)

    return _run(go)


@tool(READ_ONLY)
def zenodo_files(record: str, include: Optional[list[str]] = None, exclude: Optional[list[str]] = None) -> dict:
    """Save the file list, sizes and MD5 of a Zenodo record (number, 10.5281/zenodo.N DOI or URL) as a manifest.
    A concept DOI resolves to the latest version."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return zenodo.files(cfg, db, client, record=record, include=include, exclude=exclude)

    return _run(go)


@tool(READ_ONLY)
def url_inspect(urls: list[str], subdir: str = "") -> dict:
    """Check HTTPS URLs of public files not in the catalog (e.g. Ensembl/GENCODE references, gnomAD, 1000 Genomes)
    with HEAD (existence, size) and save them as a download manifest."""

    def go() -> dict:
        cfg, db = _ctx()
        with make_client(cfg) as client:
            return urlfiles.inspect_urls(cfg, db, client, urls, subdir=subdir)

    return _run(go)


@tool(READ_ONLY)
def get_query(query_id: str) -> dict:
    """Look up a saved discovery result (summary, parameters, provenance API calls, manifest path) by query_id."""

    def go() -> dict:
        _, db = _ctx()
        q = db.get_query(query_id)
        if q is None:
            raise KeyError(f"query_id {query_id!r} not found")
        return q

    return _run(go)


@tool(READ_ONLY)
def list_queries(limit: int = 20) -> dict:
    """List recent discovery results."""

    def go() -> dict:
        _, db = _ctx()
        return {"queries": db.list_queries(limit)}

    return _run(go)


# ---------------------------------------------------------------- transfer
@tool(LOCAL_WRITE)
def prepare_download(
    query_id: str,
    dest: Optional[str] = None,
    acknowledge_controlled_access: bool = False,
    nf_core_pipeline: Optional[str] = "rnaseq",
    download_method: Optional[str] = None,
    use_dbgap: bool = False,
) -> dict:
    """Generate a download job script for query_id (does not run it). GDC=gdc-client, SRA/ENA/GEO=nf-core/fetchngs,
    EGA=pyega3. Controlled data requires acknowledge_controlled_access=True, set only after user confirmation.
    nf_core_pipeline/download_method are fetchngs options (download_method: ftp|sratools|aspera|fastq-dl).
    use_dbgap=True: download dbGaP-protected SRA runs with config.dbgap_key_path (.ngc) (controlled, confirmation
    required)."""

    def go() -> dict:
        cfg, db = _ctx()
        return transfer.prepare_download(cfg, db, query_id, dest=dest,
                                         acknowledge_controlled_access=acknowledge_controlled_access,
                                         nf_core_pipeline=nf_core_pipeline, download_method=download_method,
                                         use_dbgap=use_dbgap)

    return _run(go)


@tool(LOCAL_WRITE)
def verify_download(query_id: str, dest: Optional[str] = None, check_md5: bool = False) -> dict:
    """Check downloaded files against the manifest (presence, size, leftover .partial files). check_md5=True
    re-verifies md5 (for large data, generates an md5 verification job script instead)."""

    def go() -> dict:
        cfg, db = _ctx()
        return transfer.verify_download(cfg, db, query_id, dest=dest, check_md5=check_md5)

    return _run(go)


@tool(
    ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True),
    meta={"anthropic/requiresUserInteraction": True},
)
def submit_job(script_path: str, confirm: bool = False, dry_run: bool = True) -> dict:
    """Submit a generated job script to the scheduler. Default is dry_run=True (shows the command without submitting).
    Actual submission requires: confirm=True AND dry_run=False AND a peer-reviewer PASS record (matching the current
    hash) AND fewer running jobs than the concurrency limit. Never use confirm=True without explicit user approval."""

    def go() -> dict:
        cfg, db = _ctx()
        return transfer.submit_job(cfg, db, script_path, confirm=confirm, dry_run=dry_run)

    return _run(go)


@tool(READ_ONLY)
def job_status(job_id: str, log_lines: int = 30) -> dict:
    """Return job status (squeue/sacct, qstat, or local PID) and the log tail (sensitive strings masked)."""

    def go() -> dict:
        cfg, db = _ctx()
        return transfer.job_status(cfg, db, job_id, log_lines=log_lines)

    return _run(go)


@tool(READ_ONLY)
def list_jobs(
    status: Optional[str] = None,
    since_hours: Optional[float] = None,
    limit: int = 20,
    include_dry_run: bool = False,
) -> dict:
    """List submitted jobs (status refreshed). since_hours=48 means the last 48 hours."""

    def go() -> dict:
        cfg, db = _ctx()
        return transfer.list_jobs(cfg, db, status=status, since_hours=since_hours, limit=limit,
                                  include_dry_run=include_dry_run)

    return _run(go)


# ---------------------------------------------------------------- processing
@tool(LOCAL_WRITE)
def build_samplesheet(
    query_id: str,
    pipeline: str,
    dest: Optional[str] = None,
    strandedness: str = "auto",
    sample_column: str = "sample_accession",
    status_map: Optional[dict[str, int]] = None,
    sex_map: Optional[dict[str, str]] = None,
    patient_map: Optional[dict[str, str]] = None,
) -> dict:
    """Generate an nf-core samplesheet CSV from downloaded files.
    pipeline='rnaseq' (sample,fastq_1,fastq_2,strandedness)
    or 'sarek' (FASTQ: patient,sex,status,sample,lane,fastq_1,fastq_2 / GDC BAM: patient,sex,status,sample,bam,bai).
    status_map={'sample': 1} marks tumor; sex_map values are XX|XY|NA."""

    def go() -> dict:
        cfg, db = _ctx()
        return processing.build_samplesheet(cfg, db, query_id, pipeline, dest=dest, strandedness=strandedness,
                                            sample_column=sample_column, status_map=status_map,
                                            sex_map=sex_map, patient_map=patient_map)

    return _run(go)


@tool(LOCAL_WRITE)
def prepare_pipeline(
    pipeline: str,
    samplesheet: str,
    version: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    outdir: Optional[str] = None,
    run_name: Optional[str] = None,
) -> dict:
    """Generate a nextflow run script (+ params.yaml, site config) (does not run it). The version is pinned as X.Y.Z
    (config value if omitted). In params, set reference='<config.references name>' or genome='<iGenomes key>'."""

    def go() -> dict:
        cfg, db = _ctx()
        return processing.prepare_pipeline(cfg, db, pipeline, samplesheet, version=version, params=params,
                                           outdir=outdir, run_name=run_name)

    return _run(go)


# ---------------------------------------------------------------- review
@tool(READ_ONLY)
def preflight_check(script_path: str) -> dict:
    """[read-only] Automated check of a generated script, samplesheet and parameters: reference consistency,
    paired/single, free disk space, controlled-access handling, version pinning, integrity, credential exposure.
    Returns PASS/FAIL and a list of defects."""

    def go() -> dict:
        cfg, db = _ctx()
        return review.preflight_check(cfg, db, script_path)

    return _run(go)


@tool(LOCAL_WRITE)
def record_review(script_path: str, verdict: str, defects: Optional[list[str]] = None) -> dict:
    """Record the peer-reviewer verdict (PASS/FAIL) and defect list in the audit log. If the automated check FAILs,
    a PASS verdict is still recorded as FAIL. Does not modify the artifacts themselves."""

    def go() -> dict:
        cfg, db = _ctx()
        return review.record_review(cfg, db, script_path, verdict, defects)

    return _run(go)


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
