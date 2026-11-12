"""Jinja2 template rendering and registration of generated artifacts.

Generated scripts are written only under state_dir/scripts/, and a combined hash (bundle sha256) of the script and
its input files (manifest, samplesheet, params, nextflow config) is recorded.
Review (record_review) and submission (submit_job) are valid only while this hash matches.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import shlex
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from . import scheduler
from .config import Config
from .state import StateDB, sha256_file

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,
)
_env.filters["q"] = lambda v: shlex.quote(str(v))


def render(template: str, **context: Any) -> str:
    return _env.get_template(template).render(**context)


def bundle_digest(script: Path, inputs: list[str]) -> str:
    h = hashlib.sha256()
    for p in [str(script), *sorted(inputs)]:
        h.update(p.encode())
        h.update(b"\0")
        h.update(sha256_file(p).encode() if Path(p).exists() else b"MISSING")
        h.update(b"\n")
    return h.hexdigest()


def resources_from(cfg: Config, section: str, partition: str | None = None) -> scheduler.Resources:
    sec = cfg.get(section, {}) or {}
    return scheduler.Resources(
        cpus=int(sec.get("cpus", 1)),
        mem=str(sec.get("mem", "4G")),
        time=str(sec.get("time", "24:00:00")),
        partition=cfg.get("partition", "") if partition is None else partition,
        account=cfg.get("account", ""),
        qos=cfg.get("qos", ""),
    )


def write_script(
    cfg: Config,
    db: StateDB,
    *,
    template: str,
    kind: str,
    name: str,
    group: str,
    runner: str,
    resources: scheduler.Resources,
    context: dict[str, Any],
    meta: dict[str, Any],
    query_id: str | None,
    inputs: list[str],
    umask: str = "027",
) -> dict[str, Any]:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    script = (cfg.scripts_dir / group / f"{kind}_{stamp}.sh").resolve()
    script.parent.mkdir(parents=True, exist_ok=True)
    log_path = (cfg.state_dir / "logs" / f"{group}_{kind}_{stamp}.log").resolve()
    text = render(
        template,
        directives=scheduler.directives(runner, name, resources, log_path),
        generated_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        kind=kind,
        query_id=query_id,
        runner=runner,
        umask=umask,
        exit_file=str(scheduler.exit_file_for(script)),
        module_loads=cfg.get("module_loads") or [],
        **context,
    )
    script.write_text(text, encoding="utf-8")
    os.chmod(script, 0o750)
    full_meta = dict(meta)
    full_meta.update({
        "runner": runner,
        "log_path": str(log_path),
        "inputs": [str(Path(i).resolve()) for i in inputs],
        "resources": resources.__dict__,
    })
    full_meta["bundle_sha256"] = bundle_digest(script, full_meta["inputs"])
    db.register_artifact(script, kind, query_id, full_meta)
    return {
        "script_path": str(script),
        "log_path": str(log_path),
        "runner": runner,
        "bundle_sha256": full_meta["bundle_sha256"],
        "script_text": text,
    }
