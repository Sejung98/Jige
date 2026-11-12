# CHANGELOG

## 0.1.0 — 2026-11-12

First tagged release.

- **English sources.** Code, comments, tool and error messages, subagent definitions, `CLAUDE.md`, config template, install script and benchmark records are now in English. `README.ko.md` stays in Korean. The orchestrator still accepts approval in any language ("submit it", "go ahead", or the same in any language).
- **EGA:** file lists work again. The metadata API answers list endpoints with HTTP 206 even when the page holds every item; `ega_search` now accepts 206 there.
- **NCBI E-utilities:** requests are spaced per host (3/s without `NCBI_API_KEY`, 10/s with it), and a 429/5xx retry waits for the server's `Retry-After` when it is longer than the backoff. Fixes the HTTP 429 failures in `sra_search` and `geo_files`.
- **Log redaction:** long file names and paths (e.g. TCGA BAM names) are no longer masked as tokens in job log tails; JWT and base64 strings still are.
- **Name:** the Python package is now `jige` (was `genomics-dataops-team`); the HTTP User-Agent is `Jige/<version> (+https://github.com/Sejung98/Jige)`. Existing installs: `.venv/bin/pip uninstall -y genomics-dataops-team && ./install.sh`.
- **New files:** `environment.yml` (conda env with tested gdc-client, Nextflow, pyega3 versions; adds `tenacity`, which the bioconda gdc-client 2.7.0 package does not declare) and a GitHub Actions workflow running the unit tests on Python 3.10 and 3.12.
- **README:** quick start first, an example request, the benchmark summary, and collapsible reference sections. The install command no longer clones an old branch.
- **Benchmarks:** the internal host name in the 2026-10-30 records is replaced by `lab-server`.
- Tests: 108 unit tests pass; all 14 live tests passed on 2026-11-12 (one transient NCBI failure seen in about 25 repeated runs, not reproduced). A download job ran end to end on Slurm (submit → COMPLETED → md5 PASS).

## 2026-11-12 — GDC patient list filter

- Added a patient filter to `gdc_search_files` and `gdc_search_cases`.
  - `case`: case submitter_id (TCGA barcode etc.) or GDC case UUID. A list, or a string separated by commas or spaces. Mixing the two ID kinds is rejected.
  - `case_file`: a patient list file. One ID per line (`#` comments, blank lines and a column name on the first line are skipped). For a table with a header line, choose the column with `case_column` (`.csv` is comma-separated, anything else tab-separated).
  - Malformed lines are reported by line number only, without their content. Credential paths (token and key paths in the config, anything under `.ssh`/`.gdc`/`.ega`, names containing token, credential, creds, etc.) cannot be used as list files (`security.is_secret_path`).
- Result `summary.case_filter`: requested, matched and unmatched patient counts, the unmatched list (first 50), and the list file path and sha256. `case_warning` is set when there are mismatches.
- In tool output, filter value lists longer than 50 are shortened to the first 5 plus a count. The query record (SQLite) stores the full list.
- Docs: `.claude/agents/gdc-discovery.md` rule 6, `CLAUDE.md` routing table, README example table.
- Added 8 tests (`tests/test_gdc.py`).
- Checked against the live GDC API on an on-prem Slurm install with a real 860-patient TCGA-BRCA barcode list (860/860 matched, 1,759 WGS BAM files) and a 100-patient batch (100/100 matched).

### Known issues at the time (`pytest -m live`, 2026-11-12; all resolved in 0.1.0)
- EGA `/datasets/{acc}/files` returns its data with HTTP 206 (partial content), but `http.request` treated only 200 as success, so the `ega_search` file list failed.
- NCBI E-utilities returned HTTP 429 (no API key, back-to-back requests).
- GEO supplementary listings returned HTTP 403 for the old User-Agent (curl's default got 200). Not seen again with the new User-Agent.
