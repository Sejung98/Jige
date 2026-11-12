"""Preflight checks for the peer-reviewer, and review records.

preflight_check is read-only. It does not modify scripts, samplesheets or parameters.
record_review saves the review result to the audit record (DB); if the automatic checks contain
a FAIL, it is recorded as FAIL even if the reviewer gave PASS. submit_job allows an actual submission
only when there is a latest PASS record matching the current script bundle hash.

Checks: reference match, paired/single distinction, free disk space, controlled-access handling,
pinned pipeline version, (extra) script integrity, credential exposure, offline readiness.
"""

from __future__ import annotations

import csv
import re
import shutil
from pathlib import Path
from typing import Any

from . import render
from .config import Config
from .security import check_secret_file, dir_exposure, nearest_existing, script_has_inline_secret
from .state import StateDB
from .util import fmt_bytes, read_tsv

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
SEMVER_FLAG = re.compile(r"-r\s+(\d+\.\d+\.\d+)\b")
SEX_REQUIRED_TOOLS = {"ascat", "controlfreec", "varlociraptor"}


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _disk(path: Path, need: int | None, label: str) -> dict[str, str]:
    if not need:
        return _check(f"disk_space:{label}", WARN, f"Could not check free disk space because the expected {label} size is unknown.")
    target = nearest_existing(path)
    free = shutil.disk_usage(target).free
    if free < need:
        return _check(f"disk_space:{label}", FAIL,
                      f"need {fmt_bytes(need)} > available {fmt_bytes(free)} ({target})")
    return _check(f"disk_space:{label}", PASS, f"need {fmt_bytes(need)}, available {fmt_bytes(free)} ({target})")


def _controlled_checks(cfg: Config, meta: dict, text: str, paths: dict[str, str]) -> list[dict[str, str]]:
    out = []
    if meta.get("source") and meta.get("acknowledged_controlled_access") is False:
        out.append(_check("controlled_access:ack", FAIL, "No record that the user confirmed (acknowledged) DAC approval and institutional rules."))
    if "umask 077" not in text:
        out.append(_check("controlled_access:umask", FAIL, "Scripts for controlled data must use umask 077."))
    for label, p in paths.items():
        exp = dir_exposure(Path(p))
        if exp["world_accessible"]:
            out.append(_check(f"controlled_access:{label}", FAIL,
                              f"{exp['checked_path']}({exp['mode']}) is open to other users."))
        elif exp["group_accessible"]:
            out.append(_check(f"controlled_access:{label}", WARN,
                              f"{exp['checked_path']}({exp['mode']}) is group-accessible. Check that every group member is covered by the approval."))
        else:
            out.append(_check(f"controlled_access:{label}", PASS, f"{exp['checked_path']} owner-only"))
    if meta.get("secret_path"):
        sc = check_secret_file(meta["secret_path"], "credential")
        if not sc["exists"]:
            out.append(_check("controlled_access:credential", FAIL, sc["warning"]))
        elif not sc["ok"]:
            out.append(_check("controlled_access:credential", WARN, sc["warning"]))
        else:
            out.append(_check("controlled_access:credential", PASS, f"permissions {sc['mode']}"))
    out.append(_check("controlled_access:policy", WARN,
                      "Automatic checks cannot verify the DAC approval itself. A person must confirm the approved scope of data use and the institutional rules."))
    return out


def _read_sheet(path: str) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _download_checks(cfg: Config, db: StateDB, art: dict, text: str) -> list[dict[str, str]]:
    meta = art["meta"]
    checks = []
    headroom = float(cfg["disk_headroom"])
    tb = meta.get("total_bytes")
    checks.append(_disk(Path(meta["dest"]), int(tb * headroom) if tb else None, "dest"))
    if meta.get("transfer_node_mode") != cfg["transfer_node_mode"]:
        checks.append(_check("transfer_placement", WARN,
                             f"transfer_node_mode changed {meta.get('transfer_node_mode')} -> "
                             f"{cfg['transfer_node_mode']} after the script was generated. Regenerate it."))
    if meta.get("pipeline") == "fetchngs":
        m = SEMVER_FLAG.search(text)
        ok = bool(m and m.group(1) == meta.get("version"))
        checks.append(_check("version_pinned", PASS if ok else FAIL,
                             f"nf-core/fetchngs -r {meta.get('version')}" if ok else "fetchngs version is not pinned to X.Y.Z."))
        if meta.get("executor") != "local" and cfg["transfer_node_mode"] != "compute":
            checks.append(_check("transfer_placement", FAIL,
                                 "Compute nodes have no internet, but fetchngs tasks are sent to the scheduler."))
        q = db.get_query(art["query_id"]) if art.get("query_id") else None
        if q and q.get("manifest_path"):
            layouts = {(r.get("library_layout") or "").upper() for r in read_tsv(q["manifest_path"])}
            target = (meta.get("params") or {}).get("nf_core_pipeline")
            if target == "sarek" and "SINGLE" in layouts:
                checks.append(_check("paired_single", FAIL, "sarek supports paired-end FASTQ only, but there are SINGLE runs."))
            elif len(layouts - {""}) > 1:
                checks.append(_check("paired_single", WARN, f"PAIRED/SINGLE mixed: {sorted(layouts)}"))
            else:
                checks.append(_check("paired_single", PASS, f"layout: {sorted(layouts - {''}) or ['unknown']}"))
    if meta.get("access") == "controlled":
        checks += _controlled_checks(cfg, meta, text, {"dest": meta["dest"]})
    return checks


def _reference_checks(meta: dict) -> list[dict[str, str]]:
    ref = meta.get("reference") or {}
    pipeline = meta.get("pipeline")
    checks = []
    if not ref.get("label"):
        return [_check("reference_match", FAIL, f"No reference (genome or fasta/reference) is set for {pipeline}.")]
    species_in = [s for s in (meta.get("species") or []) if s and s != "NA"]
    if len(set(species_in)) > 1:
        checks.append(_check("reference_match:species", FAIL, f"Input mixes multiple species: {species_in}"))
    elif species_in and ref.get("species"):
        same = species_in[0].lower() == str(ref["species"]).lower()
        checks.append(_check("reference_match:species", PASS if same else FAIL,
                             f"input species {species_in[0]} / reference {ref['label']}({ref['species']})"))
    else:
        checks.append(_check("reference_match:species", WARN,
                             f"Cannot determine the species of reference {ref['label']}. Add species to config.references."))
    if meta.get("input_type") == "bam":
        need = meta.get("input_reference")
        have = ref.get("assembly")
        step = (meta.get("params") or {}).get("step")
        if need and have != need:
            checks.append(_check("reference_match:assembly", FAIL,
                                 f"The aligned BAMs use reference {need}, but the pipeline reference is {have or ref['label']}. "
                                 f"With step={step}, the BAM header contigs must match the fasta exactly. "
                                 f"Register an entry with assembly: {need} in config.references and use it."))
        else:
            checks.append(_check("reference_match:assembly", PASS, f"BAM/reference assembly match: {need}"))
    if ref.get("source") == "igenomes-default":
        checks.append(_check("reference_match:explicit", WARN,
                             f"genome is not set, so the {pipeline} default {ref['label']} is used. Check that this is intended."))
    if ref.get("source") in ("igenomes", "igenomes-default") and pipeline == "rnaseq":
        checks.append(_check("reference_match:annotation", WARN,
                             "The nf-core/rnaseq docs recommend giving --fasta/--gtf explicitly instead of the outdated iGenomes annotations."))
    return checks


def _sheet_checks(meta: dict) -> list[dict[str, str]]:
    checks = []
    sheet = meta.get("samplesheet")
    rows = _read_sheet(sheet) if sheet and Path(sheet).exists() else []
    if not rows:
        return [_check("samplesheet", FAIL, f"Cannot read samplesheet: {sheet}")]
    file_cols = [c for c in ("fastq_1", "fastq_2", "bam", "bai") if c in rows[0]]
    missing = [r[c] for r in rows for c in file_cols if r.get(c) and not Path(r[c]).exists()]
    checks.append(_check("samplesheet:files_exist", FAIL if missing else PASS,
                         f"{len(missing)} missing file(s) (e.g. {missing[:3]})" if missing else f"files present for all {len(rows)} rows"))
    pipeline = meta.get("pipeline")
    if "fastq_1" in rows[0]:
        actual = {"PAIRED" if r.get("fastq_2") else "SINGLE" for r in rows}
        declared = set(meta.get("declared_layouts") or [])
        if pipeline == "sarek" and "SINGLE" in actual:
            checks.append(_check("paired_single", FAIL, "sarek FASTQ input allows paired-end (lane, fastq_2) only."))
        elif declared and actual != declared:
            checks.append(_check("paired_single", FAIL,
                                 f"Metadata layout {sorted(declared)} differs from samplesheet {sorted(actual)} (mates may be missing)."))
        else:
            checks.append(_check("paired_single", PASS, f"layout {sorted(actual)}"))
        if pipeline == "sarek":
            gz = all(r["fastq_1"].endswith((".fq.gz", ".fastq.gz")) for r in rows)
            checks.append(_check("samplesheet:gzip", PASS if gz else FAIL, "sarek accepts gzipped FASTQ only"))
    if pipeline == "sarek":
        tools = {t.strip() for t in str((meta.get("params") or {}).get("tools") or "").split(",") if t.strip()}
        step = (meta.get("params") or {}).get("step", "mapping")
        if not tools and step not in ("mapping", "markduplicates", "prepare_recalibration", "recalibrate"):
            checks.append(_check("sarek:tools", WARN, "tools is empty, so no variant calling will run."))
        if tools & SEX_REQUIRED_TOOLS and any((r.get("sex") or "NA") == "NA" for r in rows):
            checks.append(_check("sarek:sex", FAIL,
                                 f"{sorted(tools & SEX_REQUIRED_TOOLS)} need sex (XX/XY), but some samples have NA."))
        if "status" in rows[0] and {r.get("status") for r in rows} == {"0"} and tools & {"mutect2", "strelka", "manta", "ascat", "controlfreec", "msisensorpro"}:
            checks.append(_check("sarek:status", WARN, "All samples are normal (status=0). For somatic analysis, check the tumor/normal assignment."))
    return checks


def _pipeline_checks(cfg: Config, art: dict, text: str) -> list[dict[str, str]]:
    meta = art["meta"]
    checks = []
    m = SEMVER_FLAG.search(text)
    ok = bool(m and m.group(1) == meta.get("version"))
    checks.append(_check("version_pinned", PASS if ok else FAIL,
                         f"nf-core/{meta.get('pipeline')} -r {meta.get('version')}" if ok
                         else "Pipeline version is not pinned to an X.Y.Z release tag."))
    checks += _reference_checks(meta)
    checks += _sheet_checks(meta)
    checks.append(_disk(Path(meta["work_dir"]), meta.get("estimated_work_bytes"), "work_dir"))
    if meta.get("offline"):
        ref = meta.get("reference") or {}
        if ref.get("source") in ("igenomes", "igenomes-default") and not (meta.get("params") or {}).get("igenomes_base"):
            checks.append(_check("offline_ready:reference", FAIL,
                                 "Compute nodes are offline but an iGenomes (S3) reference is used. Register a local reference."))
        cache = cfg.path("singularity_cache")
        if cfg["container"] in ("singularity", "apptainer"):
            filled = bool(cache and cache.exists() and any(cache.iterdir()))
            checks.append(_check("offline_ready:containers", PASS if filled else WARN,
                                 f"container cache {cache}" + ("" if filled else " is empty. Run the prefetch commands first.")))
    if meta.get("access") == "controlled":
        checks += _controlled_checks(cfg, meta, text, {"outdir": meta["outdir"], "work_dir": meta["work_dir"]})
    return checks


def preflight_check(cfg: Config, db: StateDB, script_path: str) -> dict[str, Any]:
    script = Path(script_path).expanduser().resolve()
    art = db.get_artifact(script)
    if art is None:
        return {"script_path": str(script), "verdict": FAIL,
                "checks": [_check("registered", FAIL, "Not a script generated by the DataOps tools.")],
                "defects": ["Not a script generated by the DataOps tools."], "bundle_sha256": None}
    meta = art["meta"]
    text = script.read_text(encoding="utf-8") if script.exists() else ""
    digest = render.bundle_digest(script, meta.get("inputs", []))
    checks = [_check("bundle_integrity", PASS if digest == meta.get("bundle_sha256") else FAIL,
                     "unchanged since generation" if digest == meta.get("bundle_sha256")
                     else "The script or its input files changed after generation.")]
    secrets = [Path(meta["secret_path"])] if meta.get("secret_path") else []
    checks.append(_check("no_inline_secret", FAIL if script_has_inline_secret(text, secrets) else PASS,
                         "credentials passed by file path only"))
    if meta.get("runner") in ("slurm", "pbs") and not (meta.get("resources") or {}).get("partition"):
        checks.append(_check("scheduler:partition", WARN, "partition is empty, so the cluster default partition will be used."))
    if art["kind"] == "download":
        checks += _download_checks(cfg, db, art, text)
    elif art["kind"] == "pipeline":
        checks += _pipeline_checks(cfg, art, text)
    verdict = FAIL if any(c["status"] == FAIL for c in checks) else PASS
    defects = [f"[{c['status']}] {c['name']}: {c['detail']}" for c in checks if c["status"] != PASS]
    return {
        "script_path": str(script),
        "kind": art["kind"],
        "verdict": verdict,
        "checks": checks,
        "defects": defects,
        "bundle_sha256": digest,
    }


def record_review(
    cfg: Config,
    db: StateDB,
    script_path: str,
    verdict: str,
    defects: list[str] | None = None,
    reviewer: str = "peer-reviewer",
) -> dict[str, Any]:
    verdict = verdict.upper()
    if verdict not in (PASS, FAIL):
        raise ValueError("verdict must be PASS or FAIL")
    auto = preflight_check(cfg, db, script_path)
    if auto["bundle_sha256"] is None:
        raise ValueError("Cannot record a review for an unregistered script.")
    final = PASS if verdict == PASS and auto["verdict"] == PASS else FAIL
    all_defects = list(defects or []) + auto["defects"]
    rid = db.save_review(auto["script_path"], auto["bundle_sha256"], final, auto["checks"], all_defects, reviewer)
    return {
        "review_id": rid,
        "script_path": auto["script_path"],
        "reviewer_verdict": verdict,
        "automatic_verdict": auto["verdict"],
        "final_verdict": final,
        "overridden_to_fail": verdict == PASS and final == FAIL,
        "defects": all_defects,
    }
