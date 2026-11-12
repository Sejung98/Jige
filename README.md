> *Jige (지게) is the Korean A-frame carrier that hauled heavy loads on one's back. This one hauls datasets from public archives onto your HPC.*

# Jige: Genomics DataOps Team

[![tests](https://github.com/Sejung98/Jige/actions/workflows/tests.yml/badge.svg)](https://github.com/Sejung98/Jige/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Ask in plain language. Jige finds public biological datasets, downloads them as verified jobs on your own HPC, and prepares nf-core pipelines. Every number comes from a live API call, and nothing runs without your approval.**

[한국어 README](README.ko.md)

## Quick start

On a Linux HPC login node with Python ≥ 3.10, `curl` and [Claude Code](https://code.claude.com/docs) (Claude subscription or API key):

```bash
git clone https://github.com/Sejung98/Jige.git
cd Jige && ./install.sh          # creates .venv, installs Python deps, copies config.example.yaml to config.yaml
vi config.yaml                   # scheduler, partition, data paths (see "Configuration" below)
claude                           # start Claude Code in this folder; trust it and allow the dataops MCP server
```

Then ask, for example: *"How many TCGA-BRCA WGS BAMs are open access, and how many TB?"*

Download and pipeline jobs also need `gdc-client`, `pyega3` and Nextflow ≥ 25.10.4 (with Java 17+), plus Singularity/Apptainer from your site. `conda env create -f environment.yml` installs a tested set of the first three.

## Databases it can fetch from

<table>
<tr>
<td align="center" width="25%"><a href="https://portal.gdc.cancer.gov/"><img src="docs/logos/gdc.png" width="200" alt="NCI GDC"></a><br><sub><b>NCI GDC</b><br>TCGA · TARGET · CPTAC</sub></td>
<td align="center" width="25%"><a href="https://www.ncbi.nlm.nih.gov/"><img src="docs/logos/ncbi.png" width="200" alt="NCBI"></a><br><sub><b>NCBI SRA · GEO · dbGaP</b><br>reads · processed · controlled</sub></td>
<td align="center" width="25%"><a href="https://www.ebi.ac.uk/ena/browser/"><img src="docs/logos/ena.png" width="200" alt="ENA"></a><br><sub><b>ENA</b><br>raw reads (INSDC)</sub></td>
<td align="center" width="25%"><a href="https://www.ddbj.nig.ac.jp/"><img src="docs/logos/ddbj.png" width="200" alt="DDBJ"></a><br><sub><b>DDBJ</b><br>raw reads (INSDC)</sub></td>
</tr>
<tr>
<td align="center"><a href="https://ega-archive.org/"><img src="docs/logos/ega.png" width="200" alt="EGA"></a><br><sub><b>EGA</b><br>controlled human data</sub></td>
<td align="center"><a href="https://www.ebi.ac.uk/biostudies/arrayexpress"><img src="docs/logos/arrayexpress.png" width="200" alt="ArrayExpress"></a><br><sub><b>ArrayExpress</b><br>functional genomics</sub></td>
<td align="center"><a href="https://www.encodeproject.org/"><img src="docs/logos/encode.png" width="200" alt="ENCODE"></a><br><sub><b>ENCODE</b><br>regulatory genomics</sub></td>
<td align="center"><a href="https://cellxgene.cziscience.com/"><img src="docs/logos/cellxgene.png" width="200" alt="CZ CELLxGENE"></a><br><sub><b>CZ CELLxGENE</b><br>single-cell h5ad</sub></td>
</tr>
<tr>
<td align="center"><a href="https://www.cbioportal.org/"><img src="docs/logos/cbioportal.png" width="200" alt="cBioPortal"></a><br><sub><b>cBioPortal</b><br>processed cancer genomics</sub></td>
<td align="center"><a href="https://www.ebi.ac.uk/pride/"><img src="docs/logos/pride.png" width="200" alt="PRIDE"></a><br><sub><b>PRIDE</b><br>proteomics</sub></td>
<td align="center"><a href="https://zenodo.org/"><img src="docs/logos/zenodo.png" width="200" alt="Zenodo"></a><br><sub><b>Zenodo</b><br>paper data with a DOI</sub></td>
<td align="center"><a href="https://nf-co.re/"><img src="docs/logos/nfcore.png" width="200" alt="nf-core"></a><br><sub><b>nf-core</b><br>rnaseq · sarek · fetchngs</sub></td>
</tr>
</table>

Plus any public HTTPS file you point it at (Ensembl/GENCODE references, gnomAD, 1000 Genomes, …) through `url_inspect`.
<sub>Logos are trademarks of their owners, shown only to identify supported resources ([sources](docs/logos/SOURCES.md)).</sub>

## What a request looks like

Abridged. The numbers are from the [benchmark run](benchmarks/REPORT.md) on 2026-10-30.

```
You:  How many TCGA-BRCA WGS BAMs are open access, and how many TB?
Jige: gdc-discovery → gdc_search_files(project="TCGA-BRCA", experimental_strategy="WGS", data_format="BAM")
      open: 0 files · controlled: 2,106 files, 536.67 TB, 952 cases
      + query_id and the exact POST https://api.gdc.cancer.gov/files filter. Nothing is downloaded.

You:  Download the GRCh38 peak files of ENCODE ENCSR356KRQ.
Jige: encode-discovery → 12 bed files, 95.87 MB, all with md5 → manifest
      transfer → curl job script → peer-reviewer: PASS → "Submit this job?"
You:  go ahead
Jige: submit_job(confirm=True) → job finished → verify_download: 12/12 files, md5 12/12 PASS
```

The language model only plans and fills in parameters. Downloads and computation are done by established tools (gdc-client, nf-core/fetchngs, pyega3, curl, nf-core pipelines) as scheduler jobs.

## Why not just ask a chat LLM?

On the same five small requests, Jige was compared with a plain chat LLM (same model, no tools) ([report](benchmarks/REPORT.md)):

- Jige answered all five from live APIs, with file names, sizes and checksums. The plain LLM got one number right and gave no file-level facts.
- The plain LLM's download commands, run exactly as written, fetched the intended files in 1 of 4 cases. One saved a 404 page under a `.bed.gz` name without an error, one fetched only `robots.txt` and exited 0, and one assumed paired-end file names that did not exist. Jige downloaded all three requested datasets and verified them (sizes, plus md5 where the archive provides it).
- The plain LLM "corrected" a real accession (SRR390278) into a different, well-known one. Jige looked it up first.

## What changes for you

| Step | By hand today | With Jige |
|---|---|---|
| Find data | Portal filters, GEO→SRA mapping, adding up file sizes | Ask; the answer comes from the API, with the query shown |
| Download | Manifests, client flags, `sbatch` scripts; stuck when compute nodes are offline | Job script generated and routed to a node with internet (compute / transfer partition / login node) |
| Check | Missing or truncated files, md5 by hand | Manifest comparison, size and md5/sha checks, resumable re-runs |
| Samplesheet | Hundreds of CSV rows, paired/single mistakes | Generated in the nf-core/rnaseq or sarek schema |
| Run pipeline | Version, reference, profile, work dir, `-resume` every time | Version-pinned script with a site config |
| Catch mistakes | Usually days later | Before submission: species/reference mismatch, lost mates, disk space, controlled-data handling |
| Provenance | "Which version did I download?" | Every query, script hash, review and job stored in SQLite |
| Monitoring | `squeue` and log digging | "How are yesterday's jobs?" → status and error summary |

Not in scope: downstream statistics or biological interpretation, applying for DAC/dbGaP access, or providing compute.

## How it works

```mermaid
flowchart TB
    U([Researcher]) <-->|request / approval| O

    subgraph CC["Claude Code session (started in this folder)"]
        O["Orchestrator<br/>(main session · CLAUDE.md)"]
        O --> D1[gdc / sra / ega<br/>discovery]
        O --> D2[study-files / encode /<br/>singlecell / cbioportal<br/>discovery]
        O --> TR[transfer]
        O --> PR[processing]
        O --> RV["peer-reviewer<br/>(read-only)"]
    end

    subgraph MCP["dataops MCP server (Python, stdio)"]
        DT["27 tools: discovery · prepare_download · verify_download<br/>build_samplesheet · prepare_pipeline · preflight_check · record_review<br/>submit_job (dry run by default) · job_status · list_jobs"]
        DB[("SQLite: queries · artifacts · reviews · jobs")]
    end

    D1 & D2 & TR & PR & RV --> DT
    O -->|after your approval| DT
    DT -->|live APIs| API[("GDC · ENA · NCBI · EGA · BioStudies<br/>ENCODE · CELLxGENE · cBioPortal · PRIDE · Zenodo")]
    DT -->|sbatch / qsub / nohup| SCH["Slurm · PBS · login node"]
    SCH --> JOBS["gdc-client · fetchngs · pyega3 · curl<br/>nf-core/rnaseq · nf-core/sarek"]
    DT --- DB
```

### Safety by design

| Principle | How it is enforced |
|---|---|
| Separate roles | Each subagent's `tools` list only contains its own MCP tools. Discovery agents cannot download; transfer and processing agents cannot submit; the reviewer is read-only apart from recording its verdict. |
| Grounded answers | Every discovery result carries a `query_id` and the exact API calls (`api_queries`) it came from, stored in SQLite. Agents report numbers only from tool results. |
| Reasoning ≠ execution | Scripts come from tested Jinja2 templates. `.claude/settings.json` denies Claude's shell from running `sbatch`, `qsub`, `srun`, `salloc`, `nextflow run`, `gdc-client` or `pyega3` and from reading credential files. This is a backstop that applies only to Claude sessions started in this folder; the server checks below are the real guard. |
| Approval before execution | `submit_job` defaults to `dry_run=True`. A real submission needs `confirm=True`, `dry_run=False`, a peer-review PASS that matches the **sha256 of the script and all its inputs**, and a free slot under `max_concurrent_jobs`. Claude Code also asks you on every `submit_job` call. |
| Controlled data | Requires an explicit acknowledgement (DAC approval, institutional rules, storage location). Tokens and keys are passed as paths and never read; files must be mode 600; scripts use `umask 077`; log output is redacted. |

The automatic review (`preflight_check`) catches, among others: input species ≠ reference species; an aligned BAM's assembly ≠ the pipeline reference (e.g. GDC's GRCh38.d1.vd1 vs sarek's default GATK.GRCh38); paired-end metadata with a missing mate; sarek tools that need `sex` while it is `NA`; not enough disk; unpinned pipeline versions; iGenomes references on offline compute nodes; scripts or inputs edited after generation.

## Configuration

`install.sh` creates `.venv`, installs `mcp`, `httpx`, `pyyaml`, `jinja2`, `pytest` and `respx`, copies `config.example.yaml` to `config.yaml` (mode 600), checks that the MCP server starts, and reports which external tools are missing (it never installs them). Keys to fill in:

| Key | Meaning |
|---|---|
| `scheduler` | `slurm`, `pbs` or `local` |
| `partition`, `account` | Default partition/queue and billing account |
| `transfer_node_mode` | Where downloads run: `compute` (nodes have internet), `dtn` (transfer partition), `login` |
| `transfer_partition` | Only for `dtn` |
| `container` | `singularity`, `apptainer` or `docker` |
| `data_root`, `work_dir`, `singularity_cache` | Large-storage paths |
| `gdc_token_path`, `ega_credentials_path`, `dbgap_key_path` | Credential **paths** for controlled data (`chmod 600`) |
| `tools`, `module_loads` | Paths to gdc-client/pyega3/nextflow and setup commands if they are not on `PATH` inside jobs |
| `references` | Local fasta/gtf with `assembly` and `species` (required if compute nodes are offline) |

Check compute-node internet with `srun --pty curl -sI https://api.gdc.cancer.gov/status`. For heavy SRA/GEO use, export `NCBI_API_KEY` before starting `claude`: NCBI allows 10 requests/s with a key and 3 without, and Jige spaces its requests to match.

## More examples

| Request | Route |
|---|---|
| "Download the RNA-seq of PRJNA… and run nf-core/rnaseq" | `sra_search` → you confirm → fetchngs job → review → approval → submit → `verify_download` → `build_samplesheet` + `prepare_pipeline` → review → approval → submit |
| "Get the count matrix from GSE…" | `geo_files` → curl job (size-checked; GEO has no checksums) |
| "ENCODE CTCF ChIP-seq peaks in K562, GRCh38" | `encode_search` → `encode_files(assembly="GRCh38", output_type=…)` → curl job with md5 |
| "Single-cell lung datasets with COVID-19" | `cellxgene_search(tissue="lung", disease="COVID-19")` → h5ad manifest |
| "WGS BAMs for the patients in this list" (TCGA barcodes or GDC case UUIDs) | `gdc_search_files(..., case_file=…)` → one manifest per patient batch; `summary.case_filter` lists patients with no matching files |
| "TCGA breast cancer mutation and clinical tables" | `cbioportal_search(keyword="breast")` → study tarballs, hg19/hg38 flagged |
| "Files of PXD…" / "data of 10.5281/zenodo.…" | `pride_files` / `zenodo_files` → curl job with md5 |
| "We have dbGaP approval for phs…" | `sra_search` flags protected runs → `prepare_download(use_dbgap=True)` with your `.ngc` key |
| "What happened to the jobs I submitted yesterday?" | `list_jobs(since_hours=48)` + `job_status` → states and log errors |

<details>
<summary><b>Supported databases in detail</b></summary>

| Database | What you get | Access | Downloaded with | Subagent |
|---|---|---|---|---|
| NCI GDC | Aligned reads (BAM), variants (VCF/MAF), expression counts, methylation, CNV, clinical | open + controlled (dbGaP) | gdc-client | `gdc-discovery` |
| SRA / ENA / DDBJ | FASTQ per run, any organism or assay | open | nf-core/fetchngs | `sra-discovery` |
| NCBI GEO → SRA | Raw reads behind a GSE/GSM | open | nf-core/fetchngs | `sra-discovery` |
| dbGaP (via SRA) | Protected human runs | controlled | fetchngs `--dbgap_key` (.ngc, SRA Toolkit) | `sra-discovery` |
| EGA | BAM/CRAM/VCF/FASTQ per dataset | controlled | pyega3 | `ega-discovery` |
| NCBI GEO (files) | Supplementary files, series matrix (count matrices, microarray, RAW.tar) | open | curl manifest | `study-files-discovery` |
| ArrayExpress | Processed data, MAGE-TAB (raw reads link to ENA) | open | curl manifest | `study-files-discovery` |
| ENCODE | Peaks, signal tracks, alignments, FASTQ | open | curl manifest (md5) | `encode-discovery` |
| CZ CELLxGENE | Curated single-cell AnnData (.h5ad) | open | curl manifest | `singlecell-discovery` |
| cBioPortal | Mutations, CNA, expression, clinical per study | open | curl manifest (study tarball) | `cbioportal-discovery` |
| PRIDE | Raw spectra, search results | open | curl manifest (md5) | `study-files-discovery` |
| Zenodo | Anything deposited with a DOI | open | curl manifest (md5) | `study-files-discovery` |
| Direct URLs | References, gnomAD, 1000 Genomes, … | open | curl manifest | `study-files-discovery` |

`list_databases` returns this catalog to the agent at runtime, including what is *not* supported and why.

</details>

<details>
<summary><b>Is this enough for public data?</b></summary>

For sequencing-centred genomics the set covers the main public routes: every raw-read archive (the three INSDC members mirror each other), the two large controlled-access human archives plus dbGaP-protected SRA data, the main cancer, functional, regulatory and single-cell resources, and the places where papers leave processed files.

| Kind of data | Covered by | Not covered (yet) |
|---|---|---|
| Raw sequencing reads | SRA, ENA, DDBJ, GEO→SRA | China NGDC GSA (public files can go through `url_inspect`) |
| Controlled human genomes | GDC controlled, EGA, dbGaP-protected SRA | UK Biobank, All of Us, AnVIL/Terra (analysis happens in cloud enclaves), JGA |
| Cancer genomics | GDC (raw and harmonised), cBioPortal (processed) | ICGC: the data portal closed in June 2024; legacy raw data is mostly at EGA |
| Expression / functional genomics | GEO files, ArrayExpress | Expression Atlas, Single Cell Expression Atlas |
| Regulatory genomics | ENCODE | Roadmap, 4DN, IGVF |
| Single cell | CELLxGENE | HCA Data Portal, Broad Single Cell Portal |
| Proteomics | PRIDE | MassIVE, jPOST |
| References and population variation | `url_inspect` (Ensembl, GENCODE, UCSC, gnomAD, 1000 Genomes) | — |
| Metabolomics, imaging | — | MetaboLights, Metabolomics Workbench, TCIA, IDR |

New sources are cheap to add: a discovery module only has to produce a URL manifest (`url, filename, size, checksum`). Download, verification, review and submission are shared.

</details>

<details>
<summary><b>MCP tools (27)</b></summary>

| Group | Tools |
|---|---|
| Catalog / environment | `list_databases`, `check_environment` |
| GDC | `gdc_search_files`, `gdc_search_cases`, `gdc_get_field_values` |
| SRA / ENA / GEO reads / dbGaP | `sra_search` |
| EGA | `ega_search` |
| Study files | `geo_files`, `arrayexpress_files`, `pride_files`, `zenodo_files`, `url_inspect` |
| Portals | `encode_search`, `encode_files`, `cellxgene_search`, `cbioportal_search` |
| Records | `get_query`, `list_queries` |
| Transfer | `prepare_download`, `verify_download` |
| Processing | `build_samplesheet`, `prepare_pipeline` |
| Review | `preflight_check`, `record_review` |
| Execution | `submit_job`, `job_status`, `list_jobs` |

</details>

## Tests

```bash
.venv/bin/python -m pytest -q            # unit tests; HTTP mocked with respx, some with real captured API responses
.venv/bin/python -m pytest -m live -s    # smoke tests against the real APIs (needs internet)
```

The unit tests run on every push (Python 3.10 and 3.12). They cover the safety rules (dry run by default, refusal without `confirm`, without review, after edits, outside the scripts folder, above the concurrency limit) and an end-to-end run of the generated curl download job against a local HTTP server. On 2026-11-12 all 14 live tests passed, and a download job went end to end on a Slurm cluster (submit → COMPLETED → md5 PASS).

## Limitations

- **Controlled data needs DAC approval and must follow your institution's rules.** The tool records your acknowledgement and checks permissions, but cannot verify the scope of an approval.
- Download jobs have run end to end with Slurm and with the local runner. Generated nf-core pipeline jobs are unit-tested and reviewed but have not yet been run end to end through Jige. PBS support targets PBS Pro/OpenPBS syntax and has not been run on a real PBS cluster.
- GDC RNA-seq is distributed as aligned BAM and STAR counts, not FASTQ; nf-core/sarek FASTQ input must be paired-end.
- Offline compute nodes need the `prefetch` commands returned by `prepare_pipeline` (pipeline, plugins, containers) and local references.
- dbGaP downloads use a repository key (`.ngc`); JWT carts only work on AWS/GCP.
- Disk estimates are heuristics; quotas may differ from `df`. Keep `state_dir` on a filesystem with working `flock` (SQLite locking).

<details>
<summary><b>References</b></summary>

Resources and tools (verified in PubMed):

1. Ewels PA, et al. The nf-core framework for community-curated bioinformatics pipelines. *Nat Biotechnol.* 2020;38:276-278. [doi:10.1038/s41587-020-0439-x](https://doi.org/10.1038/s41587-020-0439-x)
2. Di Tommaso P, et al. Nextflow enables reproducible computational workflows. *Nat Biotechnol.* 2017;35:316-319. [doi:10.1038/nbt.3820](https://doi.org/10.1038/nbt.3820)
3. Garcia M, et al. Sarek: a portable workflow for whole-genome sequencing analysis of germline and somatic variants. *F1000Res.* 2020;9:63. [doi:10.12688/f1000research.16665.2](https://doi.org/10.12688/f1000research.16665.2)
4. Hanssen F, et al. Scalable and efficient DNA sequencing analysis on different compute infrastructures aiding variant discovery. *NAR Genom Bioinform.* 2024;6:lqae031. [doi:10.1093/nargab/lqae031](https://doi.org/10.1093/nargab/lqae031)
5. Patro R, et al. Salmon provides fast and bias-aware quantification of transcript expression. *Nat Methods.* 2017;14:417-419. [doi:10.1038/nmeth.4197](https://doi.org/10.1038/nmeth.4197)
6. Kurtzer GM, Sochat V, Bauer MW. Singularity: scientific containers for mobility of compute. *PLoS One.* 2017;12:e0177459. [doi:10.1371/journal.pone.0177459](https://doi.org/10.1371/journal.pone.0177459)
7. Grossman RL, et al. Toward a shared vision for cancer genomic data. *N Engl J Med.* 2016;375:1109-1112. [doi:10.1056/NEJMp1607591](https://doi.org/10.1056/NEJMp1607591)
8. Zhang Z, et al. Uniform genomic data analysis in the NCI Genomic Data Commons. *Nat Commun.* 2021;12:1226. [doi:10.1038/s41467-021-21254-9](https://doi.org/10.1038/s41467-021-21254-9)
9. Weinstein JN, et al. The Cancer Genome Atlas Pan-Cancer analysis project. *Nat Genet.* 2013;45:1113-1120. [doi:10.1038/ng.2764](https://doi.org/10.1038/ng.2764)
10. Katz K, et al. The Sequence Read Archive: a decade more of explosive growth. *Nucleic Acids Res.* 2022;50:D387-D390. [doi:10.1093/nar/gkab1053](https://doi.org/10.1093/nar/gkab1053)
11. O'Cathail C, et al. The European Nucleotide Archive in 2024. *Nucleic Acids Res.* 2025;53:D49-D55. [doi:10.1093/nar/gkae975](https://doi.org/10.1093/nar/gkae975)
12. Freeberg MA, et al. The European Genome-phenome Archive in 2021. *Nucleic Acids Res.* 2022;50:D980-D987. [doi:10.1093/nar/gkab1059](https://doi.org/10.1093/nar/gkab1059)
13. Mailman MD, et al. The NCBI dbGaP database of genotypes and phenotypes. *Nat Genet.* 2007;39:1181-1186. [doi:10.1038/ng1007-1181](https://doi.org/10.1038/ng1007-1181)
14. Clough E, et al. NCBI GEO: archive for gene expression and epigenomics data sets: 23-year update. *Nucleic Acids Res.* 2024;52:D138-D144. [doi:10.1093/nar/gkad965](https://doi.org/10.1093/nar/gkad965)
15. Athar A, et al. BioStudies Database: a platform for life sciences study data dissemination. *J Mol Biol.* 2026;438:169874. [doi:10.1016/j.jmb.2026.169874](https://doi.org/10.1016/j.jmb.2026.169874)
16. Luo Y, et al. New developments on the Encyclopedia of DNA Elements (ENCODE) data portal. *Nucleic Acids Res.* 2020;48:D882-D889. [doi:10.1093/nar/gkz1062](https://doi.org/10.1093/nar/gkz1062)
17. Abdulla S, et al. CZ CELLxGENE Discover: a single-cell data platform for scalable exploration, analysis and modeling of aggregated data. *Nucleic Acids Res.* 2025;53:D886-D900. [doi:10.1093/nar/gkae1142](https://doi.org/10.1093/nar/gkae1142)
18. Gao J, et al. Integrative analysis of complex cancer genomics and clinical profiles using the cBioPortal. *Sci Signal.* 2013;6:pl1. [doi:10.1126/scisignal.2004088](https://doi.org/10.1126/scisignal.2004088)
19. Perez-Riverol Y, et al. The PRIDE database at 20 years: 2025 update. *Nucleic Acids Res.* 2025;53:D543-D553. [doi:10.1093/nar/gkae1011](https://doi.org/10.1093/nar/gkae1011)

API documentation and client source code used for the implementation are cited at the top of each module in `dataops_mcp/` (GDC API docs, ENA Portal API, NCBI E-utilities, EGA metadata API, BioStudies API via the Bioconductor ArrayExpress package, ENCODE `encoded` schemas, CELLxGENE curation API spec, cBioPortal OpenAPI, PRIDE `pridepy`, Zenodo `zenodo-rdm`, nf-core pipeline schemas).

</details>

## License

MIT. See [LICENSE](LICENSE). Database logos in `docs/logos/` belong to their respective owners. Changes are listed in [CHANGELOG.md](CHANGELOG.md).
