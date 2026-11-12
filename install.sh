#!/usr/bin/env bash
# Jige install script
#   ./install.sh               # default install
#   PYTHON=python3.11 ./install.sh
#   ./install.sh --run-tests   # run unit tests after install
# External tools (gdc-client, nextflow, pyega3, etc.) are not installed automatically; they are only checked, with hints.
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
PYTHON="${PYTHON:-python3}"
RUN_TESTS=0
for arg in "$@"; do
  case "$arg" in
    --run-tests) RUN_TESTS=1 ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- 1. Python >= 3.10
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python not found: $PYTHON  (e.g. run module load python/3.11 and try again)" >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or later is required (current: $("$PYTHON" -V 2>&1))." >&2
  echo "e.g. module load python/3.11 && PYTHON=python3.11 ./install.sh  or set up 3.10+ with conda/uv" >&2
  exit 1
fi
ok "Python: $("$PYTHON" -V 2>&1)"

# ---------------------------------------------------------------- 2. venv + dependencies
if [ ! -x .venv/bin/python ]; then
  say "Creating .venv"
  "$PYTHON" -m venv .venv
fi
say "Installing dependencies (mcp, httpx, pyyaml, jinja2, pytest, respx)"
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e ".[test]"
ok "Package installed"

# ---------------------------------------------------------------- 3. config.yaml
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  chmod 600 config.yaml
  ok "Created config.yaml (copied from config.example.yaml). You must edit it."
else
  ok "Keeping existing config.yaml"
fi
STATE_DIR="$(.venv/bin/python -c 'from dataops_mcp.config import load_config; print(load_config().state_dir)')"
mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"
ok "state_dir: $STATE_DIR (mode 700)"

# ---------------------------------------------------------------- 4. check .mcp.json
if ! .venv/bin/python - <<'PY'
import json, sys
cfg = json.load(open(".mcp.json"))
srv = cfg["mcpServers"]["dataops"]
ok = srv["command"].endswith("/.venv/bin/python") and srv["args"][-1] == "dataops_mcp.server"
sys.exit(0 if ok else 1)
PY
then
  warn "The dataops server in .mcp.json does not point to .venv/bin/python. Restore the repository's default .mcp.json."
fi
if .venv/bin/python -c 'import asyncio; from dataops_mcp import server; print(len(asyncio.run(server.server.list_tools())))' >/tmp/dataops_tools.$$ 2>&1; then
  ok "MCP server loads: $(cat /tmp/dataops_tools.$$) tools (.mcp.json -> .venv/bin/python -m dataops_mcp.server)"
else
  warn "MCP server failed to load:"; cat /tmp/dataops_tools.$$
fi
rm -f /tmp/dataops_tools.$$

# ---------------------------------------------------------------- 5. check external tools (install hints only)
check_tool() {
  local name="$1" hint="$2"
  if command -v "$name" >/dev/null 2>&1; then
    ok "$name: $(command -v "$name")"
  else
    warn "$name not found → $hint"
  fi
}
say "Checking external tools (not installed automatically; conda env create -f environment.yml provides gdc-client, nextflow, pyega3)"
check_tool curl        "needed for URL manifest downloads (GEO, ArrayExpress, ENCODE, CELLxGENE, cBioPortal, PRIDE, Zenodo)"
check_tool claude      "install Claude Code: https://code.claude.com/docs (e.g. npm install -g @anthropic-ai/claude-code)"
check_tool gdc-client  "GDC Data Transfer Tool: download the binary from https://gdc.cancer.gov/access-data/gdc-data-transfer-tool and put it on PATH, or set its path in config.yaml tools.gdc_client"
check_tool nextflow    "Nextflow >= 25.10.4 required: curl -s https://get.nextflow.io | bash (requires Java 17+) or module load nextflow"
check_tool java        "Java 17 or later (for Nextflow): e.g. module load java/17"
check_tool pyega3      "only needed for EGA downloads: pip install pyega3 (separate environment recommended) or conda install -c bioconda pyega3"
check_tool nf-core     "needed to pre-download containers for offline compute nodes: pip install nf-core"
if command -v sbatch >/dev/null 2>&1; then ok "sbatch: $(command -v sbatch)";
elif command -v qsub >/dev/null 2>&1; then ok "qsub: $(command -v qsub)";
else warn "sbatch/qsub not found → without a scheduler, set scheduler: local in config.yaml"; fi
if command -v apptainer >/dev/null 2>&1; then ok "apptainer: $(command -v apptainer)";
elif command -v singularity >/dev/null 2>&1; then ok "singularity: $(command -v singularity)";
else warn "singularity/apptainer not found → module load singularity, or check the container value in config.yaml"; fi

# ---------------------------------------------------------------- 6. credential permissions
.venv/bin/python - <<'PY'
from dataops_mcp.config import load_config
from dataops_mcp.security import check_secret_file
cfg = load_config()
for key, label in (("gdc_token_path", "GDC token"), ("ega_credentials_path", "EGA credentials")):
    r = check_secret_file(cfg.path(key), label)
    if r["exists"]:
        print(("[ok] " if r["ok"] else "[warn] ") + f"{label}: {r['path']} ({r['mode']})" + ("" if r["ok"] else " -> " + r["warning"]))
    else:
        print(f"[info] {label}: not present yet ({r['path']}) - only needed for controlled data")
PY

# ---------------------------------------------------------------- 7. tests (optional)
if [ "$RUN_TESTS" = 1 ]; then
  say "Running unit tests"
  .venv/bin/python -m pytest -q
fi

cat <<EOF

Next steps
  1) Edit config.yaml: partition, account, transfer_node_mode, data_root, work_dir, singularity_cache, references
  2) Check compute-node internet access: srun --pty curl -sI https://api.gdc.cancer.gov/status  (or qsub -I)
  3) Run claude in this folder:  cd "$ROOT" && claude
     On first launch, trust the folder and allow the dataops MCP server.
EOF
