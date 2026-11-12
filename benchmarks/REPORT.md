# Public data download comparison: DataOps team vs plain LLM (2026-10-30, lab-server)

## Line of argument

- Across the same five requests, only the team gave numbers, file names, sizes and checksums from real values. The plain LLM answered from memory and got one fact right (P1: "0 open-access WGS BAMs") but gave no file-level facts for the other four; the team answered all five requests from live API queries → supported
- When the plain LLM's commands were run as written, without edits, only one case (Zenodo) downloaded all the intended files and finished verification. For GEO, wget fetched only robots.txt and stopped; for ENCODE, the placeholder ENCFFxxxxxx caused a 404 HTML page to be saved under a .bed.gz name; for SRA, the download failed because it assumed paired file names. The team downloaded all three (GEO 2 files 1.35 MB, ENCODE 12 files 95.87 MB, Zenodo 1 file 433 KB) without manual changes, and all passed size and md5 verification → supported
- The plain LLM suspected that SRR390278 was a typo for SRR390728, and that suspicion was wrong. What it recalled about SRR390728 (human RNA-seq, paired, 7,178,576 spots) matched the ENA record exactly, but SRR390278 is a different run that really exists (Drosophila ChIP-seq, single-end, 17,978,113 reads) → supported
- This comparison tested only the team's tool path (MCP tool functions, job scripts, verification), not the orchestration in which Claude picks tools and parameters during a conversation. The review PASS record and the submission approval were also inserted automatically by the benchmark script → inconclusive

## 1. Both approaches were run on the same requests to check whether their results match the real data

The requests are five small, public datasets (P1 TCGA-BRCA open-access WGS BAM count and size, P2 GEO GSE137303 count matrix, P3 GRCh38 peak bed files of ENCODE ENCSR356KRQ, P4 SRA SRR390278 metadata, P5 Zenodo record 3490058 files).

The plain LLM side (arm B) is a separate session of the same model asked to answer like a normal chat, without tools or internet; its answers are in [`baseline_plain_llm.md`](baseline_plain_llm.md). Its factual claims were checked against the team's results, and the shell commands in its answers were run as written, without changes, on lab-server.

The team side (arm A) is [`live_comparison.py`](live_comparison.py) running the team's real code path as is. The sequence is live API query, manifest save, download job script generation, automatic review record, check of the default `submit_job` dry run, `confirm=True` submission (local runner, curl), wait for completion, and `verify_download(check_md5=True)`; the results are in [`results/2026-10-30_lab-server/team_results.json`](results/2026-10-30_lab-server/team_results.json). The whole run took 3 min 32 s, of which 3 min went to downloading the 95.87 MB of ENCODE files.

## 2. What each approach gave for each request, and what the real values were

| Request | Real value (checked by the team via API) | Plain LLM answer | Result of running the plain LLM's commands as written | Team download · verification |
|---|---|---|---|---|
| P1 TCGA-BRCA WGS BAM | 0 open. Total 2,106 files · 536.67 TB · 952 cases, all controlled | 0 open (correct). Total estimated as "several hundred to 1,000-odd files, hundreds of TB" | API query command worked (open 0, controlled 2,106 · 536.67 TB) | N/A (numbers only) |
| P2 GEO GSE137303 | Tomato (Solanum lycopersicum) southern tomato virus study. 1 suppl file (GSE137303_count.txt.gz, 1,344,528 B) + 1 series matrix | Did not know the study, file names or sizes. Assumed a RAW.tar or NCBI-generated human/mouse counts | wget -r fetched only robots.txt. The NCBI count URL gave a 0 B file. gzip check failed | 2 files 1.35 MB, size verification PASS (GEO has no checksums) |
| P3 ENCODE ENCSR356KRQ | 12 GRCh38 bed peak files, 95.87 MB, all with md5. 4 of them are IDR thresholded peak beds (3.6\~4.2 MB) | Did not know the IDR peak accessions, sizes or md5. Estimated the size as "2\~5 MB" | Search command worked (4 bed and 4 bigBed, with md5). The download command was a placeholder, so a 404 HTML page (23,563 B) was saved as .bed.gz | 12 files 95.87 MB, md5 12/12 PASS |
| P4 SRA SRR390278 | Drosophila ChIP-seq, single-end, 17,978,113 reads, 1 FASTQ 447.47 MB | Did not know the metadata. Suggested it might be a typo for SRR390728 | ENA query command worked. FTP wget looked for _1/_2 files and both failed (the single-end file has a different name) | N/A (numbers only) |
| P5 Zenodo 3490058 | 1 poster PDF 433,244 B, md5 a1399249… | Did not know file names, sizes or md5 | Worked. md5 OK after download | 1 file 433 KB, md5 PASS |

The only number the plain LLM got right was the policy knowledge in P1 that 0 files are open access (actually 0); its estimate of the total controlled size ("1,000-odd files") was below the real 2,106. For P2, P3 and P5 it said it did not know and gave lookup commands instead, which can be seen as the desirable behavior, since it did not invent numbers. But running those commands to check the real values and choose files is still left to a person.

## 3. The plain LLM's lookup commands mostly worked; the failures were in the downloads

All five lookup commands (P1 GDC, P3 ENCODE search, P4 ENA filereport, P5 Zenodo, P2 directory listing) worked as written. This means its memory of API endpoints and field names was mostly correct.

The downloads produced three kinds of failure.

(1) Silent failure (P2). `wget -r` obeyed robots.txt on ftp.ncbi.nlm.nih.gov and fetched no files, yet the exit code was 0. The NCBI-generated count URL does not exist for this series (tomato), so a 0 B file was left behind → a failure where someone who does not inspect the output may believe the download succeeded.

(2) A slot to fill by hand (P3). Running the download command with `ACC=ENCFFxxxxxx` as is makes `curl -L` save the 404 page as `ENCFFxxxxxx.bed.gz` without an error (no `--fail`) → choosing an accession from the search results and filling it in is left to a person.

(3) A wrong assumption (P4). It assumed SRR390278 was paired and tried to fetch `_1`/`_2` files, and both failed. It did note the single-end file name in a comment → a person who reads the layout in the ENA query result and picks the right one can download it.

The team handled the same steps with a manifest built directly from the query results, so none of these three occurred. Right after the download, the script compared file sizes against the manifest, and for repositories that provide checksums (ENCODE, Zenodo) it also checked md5. GEO provides no checksums, so only sizes were checked, and the report says so.

## 4. The typo suspicion is a case where the memory can be right and the judgement still wrong

The plain LLM said it did not know SRR390278, recalled SRR390728 (well known as the SRA Toolkit example) and suggested a typo. Querying ENA for SRR390728 showed that everything it recalled (human, RNA-seq, paired, 7,178,576 spots) was correct, but SRR390278 is a real Drosophila ChIP-seq run (it is also in the nf-core/fetchngs test ID list). Had the user followed this suggestion, they would most likely have downloaded entirely different data. The team queries the accession and first shows whether it exists and what its metadata is, so this kind of detour does not arise.

## 5. New findings on the team side

- This was the first run in which the team's code reached the real APIs, and the GDC, NCBI E-utilities, GEO FTP, ENA, ENCODE and Zenodo clients worked as written from the documentation (queries took 0.86\~2.59 s).
- For P3 the benchmark script filtered only on `file_format=bed`, so it also downloaded the non-IDR peaks (pseudoreplicated, ranked, etc.), 12 files and 95.87 MB in total. To match the request, `output_type` should also have been set, so that only 4 files (about 15.7 MB) were downloaded. In real use, choosing this argument is the encode-discovery agent's job.
- Log-tail masking mistook a file name longer than 40 characters (`Poster-Efficiently-starting-institutional-RDM.pdf`) for a token and hid it as `***REDACTED***`. This did not affect the download, but the `security.redact` criterion needs to be narrowed.

## Limitations

- Five requests and a single run, and the requests were chosen by the party that built the team (Claude).
- The plain LLM answers are a single response from the same model; other models or repeated responses may differ.
- The team called the MCP tool functions directly with fixed arguments; the step in which Claude picks subagents and arguments during a conversation, and Slurm submission, were not tested.
- The review PASS and `confirm=True` were inserted by the script. In real use, the peer-reviewer verdict and the user's approval take their place.
- Controlled-data downloads (GDC token, EGA, dbGaP) and ArrayExpress, CELLxGENE, cBioPortal and PRIDE were not included in this comparison.
