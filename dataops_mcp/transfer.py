"""Transfer (download) preparation and verification, job submission and status.

Principles:
- prepare_download only *generates* a script. It never runs it.
- submit_job defaults to dry_run=True. It submits for real only when confirm=True,
  dry_run=False, and a peer-reviewer review (PASS) matches the current script hash.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import yaml

from . import render, scheduler
from .config import Config
from .security import check_secret_file, dir_exposure, nearest_existing, redact
from .state import ACTIVE_STATES, StateDB, now_iso
from .urlfiles import URL_SOURCES
from .util import fmt_bytes, has_accession, read_tsv

SUBMITTABLE_KINDS = ("download", "pipeline", "verify")


class SubmissionRefused(PermissionError):
    pass


class ControlledAccessNotAcknowledged(PermissionError):
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _load_query(db: StateDB, query_id: str) -> dict[str, Any]:
    q = db.get_query(query_id)
    if q is None:
        raise KeyError(f"query_id {query_id!r} not found. Use a query_id from a discovery result.")
    return q


def default_dest(cfg: Config, query: dict[str, Any]) -> Path:
    return (cfg.path("data_root") / query["source"] / query["query_id"]).resolve()  # type: ignore[operator]


def disk_check(path: Path, need_bytes: int | None, headroom: float) -> dict[str, Any]:
    target = nearest_existing(path)
    usage = shutil.disk_usage(target)
    need = int((need_bytes or 0) * headroom)
    return {
        "checked_path": str(target),
        "free_bytes": usage.free,
        "free_human": fmt_bytes(usage.free),
        "required_bytes": need,
        "required_human": fmt_bytes(need),
        "headroom": headroom,
        "ok": usage.free >= need,
    }


def _nf_profile(cfg: Config) -> str:
    return cfg["container"]


def nextflow_site_context(cfg: Config, executor_mode: str, local_res: scheduler.Resources) -> dict[str, Any]:
    """executor_mode: 'scheduler' (submit tasks to the scheduler) or 'local' (run within the current node/allocation)."""
    sched = cfg["scheduler"]
    if executor_mode == "local" or sched == "local":
        executor = "local"
    else:
        executor = {"slurm": "slurm", "pbs": "pbspro"}[sched]
    cluster_options = ""
    if executor == "slurm" and cfg.get("account"):
        cluster_options = f"--account={cfg['account']}"
    elif executor == "pbspro" and cfg.get("account"):
        cluster_options = f"-A {cfg['account']}"
    extra = cfg.path("nextflow_extra_config")
    return {
        "executor": executor,
        "queue": cfg.get("partition", "") if executor != "local" else "",
        "cluster_options": cluster_options,
        "queue_size": int(cfg["max_concurrent_jobs"]),
        "local_cpus": local_res.cpus,
        "local_memory": scheduler.nf_memory(local_res.mem),
        "container": cfg["container"],
        "container_cache": str(cfg.path("singularity_cache")),
        "extra_include": str(extra) if extra else "",
    }


# ---------------------------------------------------------------------------
# prepare_download
# ---------------------------------------------------------------------------
def prepare_download(
    cfg: Config,
    db: StateDB,
    query_id: str,
    dest: str | None = None,
    acknowledge_controlled_access: bool = False,
    nf_core_pipeline: str | None = "rnaseq",
    download_method: str | None = None,
    use_dbgap: bool = False,
) -> dict[str, Any]:
    query = _load_query(db, query_id)
    source = query["source"]
    if use_dbgap and source != "sra":
        raise ValueError("use_dbgap can only be used with SRA queries.")
    if not query.get("manifest_path"):
        raise ValueError(
            "This query has no download list (manifest). "
            "Use a result from gdc_search_files (GDC), sra_search (SRA), or ega_search(include_files=True) (EGA)."
        )
    access = query.get("access") or "open"
    if use_dbgap:
        access = "controlled"  # protected dbGaP runs are DAC-approved data
    dest_path = Path(dest).expanduser().resolve() if dest else default_dest(cfg, query)
    warnings: list[str] = []
    secret_check = None

    if access == "controlled":
        if not acknowledge_controlled_access:
            raise ControlledAccessNotAcknowledged(
                "This is controlled-access data. Confirm with the user (1) DAC/dbGaP approval, (2) institutional data "
                "security policy, and (3) access permissions on the storage location, then call again with "
                "acknowledge_controlled_access=True."
            )
        exposure = dir_exposure(dest_path)
        if exposure["world_accessible"]:
            warnings.append(
                f"Storage location {exposure['checked_path']} ({exposure['mode']}) is open to other users. "
                "Keep controlled data in a location accessible only to the owner or an approved group."
            )

    total_bytes = query["summary"].get("total_bytes")
    disk = disk_check(dest_path, total_bytes, float(cfg["disk_headroom"]))
    if not disk["ok"]:
        warnings.append(
            f"Insufficient free disk space: need {disk['required_human']} (headroom {disk['headroom']}x), "
            f"available {disk['free_human']} @ {disk['checked_path']}"
        )

    runner = cfg.transfer_runner()
    partition = cfg.get("transfer_partition") if cfg["transfer_node_mode"] == "dtn" else None
    res = render.resources_from(cfg, "transfer_job", partition=partition)
    tools = cfg.get("tools", {})
    umask = "077" if access == "controlled" else "027"
    meta: dict[str, Any] = {
        "source": source,
        "access": access,
        "dest": str(dest_path),
        "total_bytes": total_bytes,
        "transfer_node_mode": cfg["transfer_node_mode"],
        "acknowledged_controlled_access": bool(acknowledge_controlled_access),
    }
    inputs = [query["manifest_path"]]

    if source == "gdc":
        token_path = None
        if access == "controlled":
            token_path = cfg.path("gdc_token_path")
            secret_check = check_secret_file(token_path, "GDC token")
            if not secret_check["exists"]:
                raise FileNotFoundError(secret_check["warning"] + " Download a token from the GDC Data Portal and place it at the config path.")
            if not secret_check["ok"]:
                warnings.append(secret_check["warning"])
            meta["secret_path"] = str(token_path)
        context = {
            "dest": str(dest_path),
            "manifest": query["manifest_path"],
            "gdc_client": tools.get("gdc_client", "gdc-client"),
            "n_processes": int(cfg.get("transfer_job", {}).get("connections", 8)),
            "retry_amount": 5,
            "client_log": str(cfg.state_dir / "logs" / f"{query_id}.gdc-client.log"),
            "token_path": str(token_path) if token_path else "",
        }
        out = render.write_script(
            cfg, db, template="gdc_download.sh.j2", kind="download", name=f"gdc_{query_id}", group=query_id,
            runner=runner, resources=res, context=context, meta=meta, query_id=query_id, inputs=inputs, umask=umask,
        )
    elif source == "sra":
        runinfo = read_tsv(query["manifest_path"])
        runs = [r["run_accession"] for r in runinfo if r.get("run_accession")]
        no_fastq = [r["run_accession"] for r in runinfo if not r.get("fastq_ftp")]
        if no_fastq and download_method != "sratools":
            warnings.append(
                f"{len(no_fastq)} runs have no ENA FASTQ URL. Consider download_method='sratools'."
            )
        ids_file = cfg.manifests_dir / f"{query_id}.ids.csv"
        ids_file.write_text("\n".join(runs) + "\n", encoding="utf-8")
        version = str(cfg.pipeline("fetchngs").get("version", ""))
        run_dir = (cfg.state_dir / "runs" / f"{query_id}_fetchngs").resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        params: dict[str, Any] = dict(cfg.pipeline("fetchngs").get("default_params") or {})
        params.update({"input": str(ids_file), "outdir": str(dest_path)})
        if nf_core_pipeline:
            params["nf_core_pipeline"] = nf_core_pipeline
        if download_method:
            params["download_method"] = download_method
        if use_dbgap:
            ngc = cfg.path("dbgap_key_path")
            secret_check = check_secret_file(ngc, "dbGaP repository key (.ngc)")
            if not secret_check["exists"]:
                raise FileNotFoundError(secret_check["warning"] + " Set config.dbgap_key_path to the path of the .ngc file obtained from dbGaP.")
            if not secret_check["ok"]:
                warnings.append(secret_check["warning"])
            # Protected runs are not on ENA FTP, so fetch them via SRA Toolkit.
            params["dbgap_key"] = str(ngc)
            params["download_method"] = "sratools"
            meta["secret_path"] = str(ngc)
        params_file = run_dir / "params.yaml"
        params_file.write_text(yaml.safe_dump(params, sort_keys=True, allow_unicode=True), encoding="utf-8")
        # In transfer-node/login-node mode, tasks must run on the same node to have internet access.
        executor_mode = "scheduler" if cfg["transfer_node_mode"] == "compute" else "local"
        site = nextflow_site_context(cfg, executor_mode, res)
        site_config = run_dir / "nextflow.dataops.config"
        site_config.write_text(render.render("nextflow_site.config.j2", generated_at=now_iso(), **site),
                               encoding="utf-8")
        work_dir = (cfg.path("work_dir") / f"{query_id}_fetchngs").resolve()  # type: ignore[operator]
        context = {
            "pipeline": "fetchngs",
            "version": version,
            "profile": _nf_profile(cfg),
            "site_config": str(site_config),
            "params_file": str(params_file),
            "work_dir": str(work_dir),
            "outdir": str(dest_path),
            "launch_dir": str(run_dir),
            "nextflow": tools.get("nextflow", "nextflow"),
            "offline": False,
            "container": cfg["container"],
            "container_cache": str(cfg.path("singularity_cache")),
        }
        meta.update({"pipeline": "fetchngs", "version": version, "params": params, "work_dir": str(work_dir),
                     "executor": site["executor"], "n_runs": len(runs)})
        inputs += [str(ids_file), str(params_file), str(site_config)]
        out = render.write_script(
            cfg, db, template="nextflow_run.sh.j2", kind="download", name=f"fetchngs_{query_id}", group=query_id,
            runner=runner, resources=res, context=context, meta=meta, query_id=query_id, inputs=inputs, umask=umask,
        )
    elif source == "ega":
        creds = cfg.path("ega_credentials_path")
        secret_check = check_secret_file(creds, "EGA credentials")
        if not secret_check["exists"]:
            raise FileNotFoundError(secret_check["warning"])
        if not secret_check["ok"]:
            warnings.append(secret_check["warning"])
        files = read_tsv(query["manifest_path"])
        acc = query["params"]["accession"]
        # Whole-dataset requests are fetched per EGAD; otherwise per file.
        if acc.startswith("EGAD"):
            accessions = [acc]
        elif acc.startswith("EGAS"):
            accessions = sorted({f["dataset_accession"] for f in files if f.get("dataset_accession")})
        else:
            accessions = [f["accession_id"] for f in files]
        context = {
            "dest": str(dest_path),
            "accessions": accessions,
            "pyega3": tools.get("pyega3", "pyega3"),
            "connections": int(cfg.get("transfer_job", {}).get("connections", 8)),
            "credentials_path": str(creds),
            "log_dir": str(cfg.state_dir / "logs"),
        }
        meta.update({"secret_path": str(creds), "ega_accessions": accessions})
        out = render.write_script(
            cfg, db, template="ega_download.sh.j2", kind="download", name=f"ega_{query_id}", group=query_id,
            runner=runner, resources=res, context=context, meta=meta, query_id=query_id, inputs=inputs, umask=umask,
        )
    elif source in URL_SOURCES:
        rows = read_tsv(query["manifest_path"])
        files = [
            {"url": r["url"], "rel": "/".join(x for x in (r.get("subdir"), r["filename"]) if x),
             "size": r.get("size", ""), "checksum": r.get("checksum", "")}
            for r in rows
        ]
        no_checksum = sum(1 for f in files if not f["checksum"])
        if no_checksum:
            warnings.append(f"{no_checksum} files have no checksum from the repository; only size is checked.")
        context = {"dest": str(dest_path), "manifest": query["manifest_path"], "files": files}
        meta.update({"n_files": len(files)})
        out = render.write_script(
            cfg, db, template="url_download.sh.j2", kind="download", name=f"{source}_{query_id}", group=query_id,
            runner=runner, resources=res, context=context, meta=meta, query_id=query_id, inputs=inputs, umask=umask,
        )
    else:
        raise ValueError(f"unknown source: {source}")

    return {
        "query_id": query_id,
        "source": source,
        "access": access,
        "dest": str(dest_path),
        "script_path": out["script_path"],
        "log_path": out["log_path"],
        "runner": out["runner"],
        "where_it_runs": {
            "compute": "compute node (scheduler job)",
            "dtn": f"transfer partition {cfg.get('transfer_partition')} (scheduler job)",
            "login": "login node (nohup background)",
        }[cfg["transfer_node_mode"]],
        "disk": disk,
        "secret_file": secret_check,
        "warnings": warnings,
        "script_text": out["script_text"],
        "next_step": "After peer-reviewer review (preflight_check → record_review) and user approval, call submit_job.",
    }


# ---------------------------------------------------------------------------
# verify_download
# ---------------------------------------------------------------------------
def _digest(path: Path, algo: str) -> str:
    h = hashlib.new(algo)  # for integrity checks (md5/sha1/sha256)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _cs(value: str | None, algo: str = "md5") -> str | None:
    return f"{algo}:{value.lower()}" if value else None


def _expected_files(cfg: Config, query: dict[str, Any], dest: Path) -> list[dict[str, Any]]:
    """Expected files from the manifest: {label, path, size, checksum('algo:hex'), size_strict}."""
    src = query["source"]
    rows = read_tsv(query["manifest_path"])
    out: list[dict[str, Any]] = []
    if src == "gdc":
        for r in rows:
            out.append({"label": r["filename"], "path": dest / r["id"] / r["filename"],
                        "size": int(r["size"]) if r.get("size") else None, "checksum": _cs(r.get("md5")),
                        "size_strict": True})
    elif src == "sra":
        all_fastq = list(dest.rglob("*.fastq.gz"))
        for r in rows:
            if not r.get("fastq_ftp"):
                continue
            urls = r["fastq_ftp"].split(";")
            sizes = (r.get("fastq_bytes") or "").split(";")
            md5s = (r.get("fastq_md5") or "").split(";")
            run = r["run_accession"]
            mine = [p for p in all_fastq if has_accession(p.name, run)]
            # If a paired run also has an unpaired file (SRRxxx.fastq.gz), fetchngs downloads only _1/_2.
            mates = [i for i, u in enumerate(urls) if u.endswith(("_1.fastq.gz", "_2.fastq.gz"))]
            keep = mates if len(mates) == 2 else list(range(len(urls)))
            for i, url in enumerate(urls):
                if i not in keep:
                    continue
                size = int(sizes[i]) if i < len(sizes) and sizes[i] else None
                name = url.rsplit("/", 1)[-1]
                # fetchngs may rename files, so match on run accession + original suffix (_1/_2).
                suffix = name.split(run, 1)[-1]
                match = [p for p in mine if p.name.endswith(suffix)] or [p for p in mine if size and p.stat().st_size == size]
                out.append({"label": name, "path": match[0] if match else dest / "fastq" / name, "size": size,
                            "checksum": _cs(md5s[i]) if i < len(md5s) and md5s[i] else None,
                            "size_strict": True})
    elif src == "ega":
        by_name: dict[str, Path] = {}
        for p in dest.rglob("*"):
            if p.is_file():
                by_name.setdefault(p.name, p)
        for r in rows:
            name = Path(r.get("filename") or r["accession_id"]).name
            plain = name[:-4] if name.endswith(".cip") else name
            path = by_name.get(plain) or by_name.get(name) or dest / plain
            # EGA metadata sizes may refer to the encrypted file, so treat mismatches as warnings only
            out.append({"label": plain, "path": path, "size": int(r["size"]) if r.get("size") else None,
                        "checksum": _cs(r.get("checksum")), "size_strict": False})
    elif src in URL_SOURCES:
        for r in rows:
            rel = "/".join(x for x in (r.get("subdir"), r["filename"]) if x)
            out.append({"label": rel, "path": dest / rel, "size": int(r["size"]) if r.get("size") else None,
                        "checksum": r.get("checksum") or None, "size_strict": True})
    return out


def verify_download(
    cfg: Config, db: StateDB, query_id: str, dest: str | None = None, check_md5: bool = False
) -> dict[str, Any]:
    query = _load_query(db, query_id)
    dest_path = Path(dest).expanduser().resolve() if dest else default_dest(cfg, query)
    if not dest_path.exists():
        raise FileNotFoundError(f"Download location does not exist: {dest_path}")
    expected = _expected_files(cfg, query, dest_path)
    missing, size_bad, size_warn, md5_bad = [], [], [], []
    present: list[dict[str, Any]] = []
    for e in expected:
        p: Path = e["path"]
        if not p.exists():
            missing.append(e["label"])
            continue
        present.append(e)
        if e["size"] is not None and p.stat().st_size != e["size"]:
            (size_bad if e["size_strict"] else size_warn).append(
                {"file": e["label"], "expected": e["size"], "actual": p.stat().st_size})
    partial = [str(p.relative_to(dest_path)) for pattern in ("*.partial", "*.part")
               for p in dest_path.rglob(pattern)]

    md5_job = None
    md5_checked = False
    present_bytes = sum(e["path"].stat().st_size for e in present)
    limit = float(cfg.get("md5_inline_limit_gb", 20)) * 1e9
    with_cs = [e for e in present if e["checksum"]]
    if check_md5:
        if present_bytes <= limit:
            for e in with_cs:
                algo, want = e["checksum"].split(":", 1)
                if _digest(e["path"], algo) != want:
                    md5_bad.append(e["label"])
            md5_checked = True
        else:
            lists: dict[str, str] = {}
            for algo in ("md5", "sha1", "sha256"):
                lines = [f"{e['checksum'].split(':', 1)[1]}  {e['path'].relative_to(dest_path)}\n"
                         for e in with_cs if e["checksum"].startswith(algo + ":")]
                if lines:
                    path = cfg.manifests_dir / f"{query_id}.{algo}"
                    path.write_text("".join(lines), encoding="utf-8")
                    lists[algo] = str(path)
            res = render.resources_from(cfg, "transfer_job")
            out = render.write_script(
                cfg, db, template="md5_verify.sh.j2", kind="verify", name=f"checksum_{query_id}", group=query_id,
                runner=cfg["scheduler"], resources=res,
                context={"dest": str(dest_path), "lists": lists,
                         "report": str(cfg.state_dir / "logs" / f"{query_id}.checksum.report")},
                meta={"source": query["source"], "access": query.get("access"), "dest": str(dest_path)},
                query_id=query_id, inputs=list(lists.values()),
            )
            md5_job = out["script_path"]

    extra: dict[str, Any] = {}
    if query["source"] == "sra":
        sheet = dest_path / "samplesheet" / "samplesheet.csv"
        extra["fetchngs_samplesheet"] = str(sheet) if sheet.exists() else None

    if missing or size_bad or md5_bad:
        status = "FAIL"
    elif partial:
        status = "INCOMPLETE"
    else:
        status = "PASS"
    report = {
        "query_id": query_id,
        "dest": str(dest_path),
        "status": status,
        "expected_files": len(expected),
        "found_files": len(present),
        "found_bytes": present_bytes,
        "found_human": fmt_bytes(present_bytes),
        "missing": missing[:50],
        "n_missing": len(missing),
        "size_mismatch": size_bad[:50],
        "size_warnings": size_warn[:50],
        "md5_mismatch": md5_bad[:50],
        "md5_checked_inline": md5_checked,
        "md5_job_script": md5_job,
        "files_without_checksum": len(present) - len(with_cs),
        "partial_files": partial[:50],
        "note": ("The gdc-client, pyega3, fetchngs (FTP), and URL download scripts verify checksums right after download. "
                 "This step checks file presence/size against the manifest; with check_md5=True it re-verifies "
                 "checksums (md5/sha1/sha256)."),
        **extra,
    }
    db.save_verification(query_id, str(dest_path), status, report)
    return report


# ---------------------------------------------------------------------------
# submit_job / job_status / list_jobs
# ---------------------------------------------------------------------------
def _resolve_script(cfg: Config, script_path: str) -> Path:
    p = Path(script_path).expanduser().resolve()
    root = cfg.scripts_dir.resolve()
    if root not in p.parents:
        raise SubmissionRefused(f"Only scripts generated under {root} can be submitted: {p}")
    if not p.exists():
        raise FileNotFoundError(str(p))
    return p


def refresh_active(cfg: Config, db: StateDB, run=subprocess.run) -> None:
    for job in db.list_jobs(limit=500):
        if job["status"] in ACTIVE_STATES and job["scheduler_job_id"]:
            st = scheduler.query_status(job["runner"], job["scheduler_job_id"], Path(job["script_path"]), run=run)
            if st != "UNKNOWN" and st != job["status"]:
                db.update_job_status(job["job_id"], st)


def submit_job(
    cfg: Config,
    db: StateDB,
    script_path: str,
    confirm: bool = False,
    dry_run: bool = True,
    run=subprocess.run,
) -> dict[str, Any]:
    script = _resolve_script(cfg, script_path)
    art = db.get_artifact(script)
    if art is None or art["kind"] not in SUBMITTABLE_KINDS:
        raise SubmissionRefused("Not a job script generated and registered by the DataOps tools.")
    meta = art["meta"]
    digest = render.bundle_digest(script, meta.get("inputs", []))
    if digest != meta.get("bundle_sha256"):
        raise SubmissionRefused(
            "The script or its input files (manifest/samplesheet/params) changed after generation. "
            "Regenerate with the prepare_* tool and review again."
        )
    review = db.latest_review(str(script))
    review_ok = bool(review and review["verdict"] == "PASS" and review["script_sha256"] == digest)
    runner = meta["runner"]
    command = scheduler.submit_command(runner, script)
    blocking: list[str] = []
    if not review_ok:
        blocking.append("No peer-reviewer PASS on record, or it does not match the current script hash.")

    if not dry_run and not confirm:
        raise SubmissionRefused(
            "Cannot submit for real without confirm=True. Show the plan to the user, get explicit approval, "
            "then call with confirm=True, dry_run=False."
        )
    if dry_run or not confirm:
        job_id = f"dry-{uuid.uuid4().hex[:8]}"
        db.add_job(job_id, None, runner, art["kind"], art["query_id"], str(script), digest, "DRY_RUN",
                   meta.get("log_path"), note="dry run")
        return {
            "submitted": False,
            "dry_run": True,
            "would_run": " ".join(command) if runner != "local" else f"nohup {' '.join(command)} &",
            "runner": runner,
            "script_path": str(script),
            "review_passed": review_ok,
            "blocking": blocking,
            "to_submit": "after user approval: submit_job(script_path, confirm=True, dry_run=False)",
        }

    if not review_ok:
        raise SubmissionRefused(blocking[0] + " The script must pass peer-reviewer review first.")
    refresh_active(cfg, db, run=run)
    active = db.count_active_jobs()
    limit = int(cfg["max_concurrent_jobs"])
    if active >= limit:
        raise SubmissionRefused(f"{active} jobs are running concurrently; max_concurrent_jobs={limit} reached.")

    log_path = Path(meta.get("log_path") or cfg.state_dir / "logs" / f"{script.stem}.log")
    sched_id = scheduler.submit(runner, script, log_path, run=run)
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    db.add_job(job_id, sched_id, runner, art["kind"], art["query_id"], str(script), digest, "SUBMITTED",
               str(log_path))
    return {
        "submitted": True,
        "dry_run": False,
        "job_id": job_id,
        "scheduler_job_id": sched_id,
        "runner": runner,
        "script_path": str(script),
        "log_path": str(log_path),
    }


def _tail(path: str | None, n: int) -> list[str]:
    if not path or not Path(path).exists():
        return []
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 64_000))
        lines = fh.read().decode("utf-8", "replace").splitlines()[-n:]
    return [redact(line) for line in lines]


def job_status(cfg: Config, db: StateDB, job_id: str, log_lines: int = 30, run=subprocess.run) -> dict[str, Any]:
    job = db.get_job(job_id)
    if job is None:
        raise KeyError(f"job {job_id!r} not found.")
    status = job["status"]
    if status != "DRY_RUN" and job["scheduler_job_id"]:
        st = scheduler.query_status(job["runner"], job["scheduler_job_id"], Path(job["script_path"]), run=run)
        if st != "UNKNOWN":
            status = st
            db.update_job_status(job["job_id"], st)
    return {
        "job_id": job["job_id"],
        "scheduler_job_id": job["scheduler_job_id"],
        "runner": job["runner"],
        "kind": job["kind"],
        "query_id": job["query_id"],
        "status": status,
        "submitted_at": job["submitted_at"],
        "script_path": job["script_path"],
        "log_path": job["log_path"],
        "log_tail": _tail(job["log_path"], log_lines),
    }


def list_jobs(
    cfg: Config,
    db: StateDB,
    status: str | None = None,
    since_hours: float | None = None,
    limit: int = 20,
    include_dry_run: bool = False,
    run=subprocess.run,
) -> dict[str, Any]:
    refresh_active(cfg, db, run=run)
    since = None
    if since_hours:
        since = (dt.datetime.now().astimezone() - dt.timedelta(hours=since_hours)).isoformat(timespec="seconds")
    jobs = db.list_jobs(status=status, since=since, limit=limit, include_dry_run=include_dry_run)
    return {
        "jobs": [
            {k: j[k] for k in ("job_id", "scheduler_job_id", "runner", "kind", "query_id", "status",
                               "submitted_at", "log_path")}
            for j in jobs
        ],
        "n": len(jobs),
        "active": db.count_active_jobs(),
        "max_concurrent_jobs": int(cfg["max_concurrent_jobs"]),
    }
