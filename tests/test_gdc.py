import json

import httpx
import pytest
import respx

from dataops_mcp import gdc, http
from dataops_mcp.util import read_tsv


def file_hit(fid, size, case, access="open", sample_type="Primary Tumor"):
    return {
        "file_id": fid,
        "file_name": f"{fid}.bam",
        "file_size": size,
        "md5sum": "0" * 32,
        "state": "released",
        "access": access,
        "data_format": "BAM",
        "data_type": "Aligned Reads",
        "data_category": "Sequencing Reads",
        "experimental_strategy": "WGS",
        "analysis": {"workflow_type": "BWA with Mark Duplicates and BQSR"},
        "cases": [{"case_id": case, "submitter_id": f"TCGA-{case}", "project": {"project_id": "TCGA-BRCA"},
                   "samples": [{"submitter_id": f"TCGA-{case}-01A", "sample_type": sample_type}]}],
    }


def test_build_filters_maps_logical_names_per_endpoint():
    f = gdc.build_filters("files", {"project": "TCGA-BRCA", "access": ["open"], "data_type": None})
    assert f == {"op": "and", "content": [
        {"op": "in", "content": {"field": "cases.project.project_id", "value": ["TCGA-BRCA"]}},
        {"op": "in", "content": {"field": "access", "value": ["open"]}},
    ]}
    c = gdc.build_filters("cases", {"project": "TCGA-BRCA", "experimental_strategy": "WGS"})
    fields = [x["content"]["field"] for x in c["content"]]
    assert fields == ["project.project_id", "files.experimental_strategy"]
    assert gdc.build_filters("files", {}) is None
    with pytest.raises(ValueError):
        gdc.build_filters("files", {"bogus": "x"})


@respx.mock
def test_search_files_paginates_summarises_and_writes_manifest(cfg, db, client):
    pages = [
        {"data": {"hits": [file_hit("f1", 100, "c1"), file_hit("f2", 200, "c1", sample_type="Blood Derived Normal")],
                  "pagination": {"total": 3}}},
        {"data": {"hits": [file_hit("f3", 300, "c2")], "pagination": {"total": 3}}},
    ]
    bodies = []

    def responder(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=pages[len(bodies) - 1])

    respx.post(f"{gdc.GDC_API}/files").mock(side_effect=responder)
    out = gdc.search_files(cfg, db, client, project="TCGA-BRCA", experimental_strategy="WGS",
                           data_format="BAM", access="open", page_size=2)

    s = out["summary"]
    assert s["n_files"] == 3 and s["api_total"] == 3 and not s["truncated"]
    assert s["total_bytes"] == 600 and s["n_cases"] == 2
    assert s["by_access"] == {"open": 3}
    assert out["access"] == "open"
    # filters and pagination went into the request body
    assert bodies[0]["from"] == 0 and bodies[1]["from"] == 2
    fields = [c["content"]["field"] for c in bodies[0]["filters"]["content"]]
    assert "cases.project.project_id" in fields and "experimental_strategy" in fields
    # provenance: API call log
    assert len(out["api_queries"]) == 2 and out["api_queries"][0]["method"] == "POST"
    # gdc-client manifest format
    rows = read_tsv(out["manifest_path"])
    assert list(rows[0].keys()) == ["id", "filename", "md5", "size", "state"]
    assert [r["id"] for r in rows] == ["f1", "f2", "f3"]
    saved = db.get_query(out["query_id"])
    assert saved["summary"]["total_bytes"] == 600 and saved["manifest_path"] == out["manifest_path"]


@respx.mock
def test_search_files_controlled_and_zero_hits(cfg, db, client):
    respx.post(f"{gdc.GDC_API}/files").mock(return_value=httpx.Response(
        200, json={"data": {"hits": [file_hit("f1", 5, "c1", access="controlled")], "pagination": {"total": 1}}}))
    out = gdc.search_files(cfg, db, client, project="TCGA-BRCA")
    assert out["access"] == "controlled"

    respx.post(f"{gdc.GDC_API}/files").mock(return_value=httpx.Response(
        200, json={"data": {"hits": [], "pagination": {"total": 0}}}))
    out = gdc.search_files(cfg, db, client, project="TCGA-XXXX")
    assert out["summary"]["n_files"] == 0 and out["manifest_path"] is None and "hint" in out


@respx.mock
def test_get_field_values_uses_facets(cfg, db, client):
    route = respx.get(f"{gdc.GDC_API}/files").mock(return_value=httpx.Response(200, json={
        "data": {"hits": [], "aggregations": {"experimental_strategy": {"buckets": [
            {"key": "WXS", "doc_count": 10}, {"key": "WGS", "doc_count": 50}]}}}}))
    out = gdc.get_field_values(cfg, db, client, field="experimental_strategy",
                               filters_criteria={"project": "TCGA-BRCA"})
    assert out["values"][0] == {"value": "WGS", "count": 50}
    params = route.calls[0].request.url.params
    assert params["facets"] == "experimental_strategy" and params["size"] == "0"
    assert json.loads(params["filters"])["content"][0]["content"]["field"] == "cases.project.project_id"


@respx.mock
def test_get_field_values_mapping_lists_fields(cfg, db, client):
    respx.get(f"{gdc.GDC_API}/files/_mapping").mock(return_value=httpx.Response(200, json={
        "_mapping": {}, "fields": ["access", "data_type", "cases.project.project_id"], "defaults": []}))
    out = gdc.get_field_values(cfg, db, client, search="project")
    assert out["fields"] == ["cases.project.project_id"]


@respx.mock
def test_search_cases(cfg, db, client):
    respx.post(f"{gdc.GDC_API}/cases").mock(return_value=httpx.Response(200, json={"data": {
        "hits": [{"case_id": "c1", "submitter_id": "TCGA-A", "project": {"project_id": "TCGA-BRCA"},
                  "primary_site": "Breast", "disease_type": "Ductal and Lobular Neoplasms"}],
        "pagination": {"total": 1}}}))
    out = gdc.search_cases(cfg, db, client, project="TCGA-BRCA", experimental_strategy="WGS")
    assert out["summary"]["n_cases"] == 1 and out["summary"]["by_primary_site"] == {"Breast": 1}


@respx.mock
def test_http_retries_on_503(client):
    route = respx.get("https://example.org/x").mock(side_effect=[httpx.Response(503), httpx.Response(200, json={})])
    log = http.ApiLog()
    resp = http.request(client, log, "GET", "https://example.org/x", retries=2, sleep=lambda s: None)
    assert resp.status_code == 200 and route.call_count == 2 and len(log.calls) == 2


@respx.mock
def test_http_raises_on_client_error(client):
    respx.get("https://example.org/x").mock(return_value=httpx.Response(400, text="bad filter"))
    with pytest.raises(http.ApiError, match="400"):
        http.request(client, http.ApiLog(), "GET", "https://example.org/x", retries=2, sleep=lambda s: None)


# ---------------------------------------------------------------- case (patient) filter
UUID1 = "6e7d5ec6-a469-467c-b748-237353c23416"
UUID2 = "55262fcb-1b01-4480-b322-36570430c917"


def test_build_filters_case_uses_barcode_or_uuid_field():
    f = gdc.build_filters("files", {"case": ["TCGA-A2-A04P", "TCGA-A8-A08B"]})
    assert f["content"][0]["content"] == {"field": "cases.submitter_id", "value": ["TCGA-A2-A04P", "TCGA-A8-A08B"]}
    f = gdc.build_filters("files", {"case": [UUID1]})
    assert f["content"][0]["content"]["field"] == "cases.case_id"
    c = gdc.build_filters("cases", {"case": ["TCGA-A2-A04P"]})
    assert c["content"][0]["content"]["field"] == "submitter_id"
    c = gdc.build_filters("cases", {"case": [UUID1]})
    assert c["content"][0]["content"]["field"] == "case_id"
    with pytest.raises(ValueError, match="mixes"):
        gdc.build_filters("files", {"case": ["TCGA-A2-A04P", UUID1]})


def test_load_case_file_plain_list(cfg, tmp_path):
    p = tmp_path / "cases.txt"
    p.write_text("tcga_barcode\n# comment\nTCGA-A2-A04P\n\nTCGA-A8-A08B\nTCGA-A2-A04P\n", encoding="utf-8")
    ids, src = gdc.load_case_file(cfg, p)
    assert ids == ["TCGA-A2-A04P", "TCGA-A8-A08B"]  # header, comment, blank line and duplicate removed
    assert src["n_ids"] == 2 and len(src["sha256"]) == 64 and src["column"] is None


def test_load_case_file_table_column(cfg, tmp_path):
    p = tmp_path / "overlap.tsv"
    p.write_text("tcga_barcode\tgroup\nTCGA-A2-A04P\tnew\n\tnew\nTCGA-A8-A08B\toverlap\n", encoding="utf-8")
    ids, _ = gdc.load_case_file(cfg, p, "tcga_barcode")
    assert ids == ["TCGA-A2-A04P", "TCGA-A8-A08B"]
    c = tmp_path / "list.csv"
    c.write_text("id,group\nTCGA-A2-A04P,new\n", encoding="utf-8")
    assert gdc.load_case_file(cfg, c, "id")[0] == ["TCGA-A2-A04P"]
    with pytest.raises(ValueError, match="Available columns"):
        gdc.load_case_file(cfg, p, "barcode")
    with pytest.raises(ValueError, match="case_column"):
        gdc.load_case_file(cfg, p)  # has tabs but no column name given


def test_load_case_file_reports_bad_lines_without_content(cfg, tmp_path):
    secret_like = "eyJhbGciOi" + "x" * 300
    p = tmp_path / "cases.txt"
    p.write_text(f"TCGA-A2-A04P\n{secret_like}\nhas space\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        gdc.load_case_file(cfg, p)
    msg = str(exc.value)
    assert "[2, 3]" in msg and secret_like not in msg and "has space" not in msg


def test_load_case_file_refuses_credential_paths(cfg, tmp_path):
    token = cfg.path("gdc_token_path")
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("TCGA-A2-A04P\n")
    named = tmp_path / "my_token_list.txt"
    named.write_text("TCGA-A2-A04P\n")
    ssh = tmp_path / ".ssh" / "ids.txt"
    ssh.parent.mkdir()
    ssh.write_text("TCGA-A2-A04P\n")
    for p in (token, named, ssh):
        with pytest.raises(ValueError, match="credential"):
            gdc.load_case_file(cfg, p)


def test_collect_cases_merges_and_validates(cfg, tmp_path):
    p = tmp_path / "cases.txt"
    p.write_text("TCGA-A8-A08B\nTCGA-B6-A0I6\n")
    ids, src = gdc.collect_cases(cfg, "TCGA-A2-A04P, TCGA-A8-A08B", str(p))
    assert ids == ["TCGA-A2-A04P", "TCGA-A8-A08B", "TCGA-B6-A0I6"]
    assert src["direct"] == 2 and src["file"]["n_ids"] == 2
    assert gdc.collect_cases(cfg) == ([], {})
    with pytest.raises(ValueError, match="case_file"):
        gdc.collect_cases(cfg, case_column="tcga_barcode")
    with pytest.raises(ValueError, match="not in case ID format"):
        gdc.collect_cases(cfg, ["TCGA-A2-A04P", "bad;id"])


@respx.mock
def test_search_files_with_case_file_reports_unmatched_and_compacts_output(cfg, db, client, tmp_path):
    wanted = [f"TCGA-XX-{i:04d}" for i in range(60)]
    p = tmp_path / "batch01.txt"
    p.write_text("\n".join(wanted) + "\n")
    bodies = []

    def responder(request):
        bodies.append(json.loads(request.content))
        hits = [file_hit("f1", 100, "XX-0000"), file_hit("f2", 200, "XX-0000", sample_type="Blood Derived Normal"),
                file_hit("f3", 300, "XX-0001")]
        return httpx.Response(200, json={"data": {"hits": hits, "pagination": {"total": 3}}})

    respx.post(f"{gdc.GDC_API}/files").mock(side_effect=responder)
    out = gdc.search_files(cfg, db, client, project="TCGA-BRCA", experimental_strategy="WGS",
                           data_format="BAM", case_file=str(p))

    # the request body contains all 60 cases
    case_filter = [c["content"] for c in bodies[0]["filters"]["content"] if c["content"]["field"] == "cases.submitter_id"]
    assert case_filter and case_filter[0]["value"] == wanted
    cf = out["summary"]["case_filter"]
    assert cf["requested"] == 60 and cf["matched"] == 2 and cf["unmatched_n"] == 58
    assert len(cf["unmatched"]) == gdc.COMPACT_OVER and "TCGA-XX-0000" not in cf["unmatched"]
    assert cf["source"]["file"]["n_ids"] == 60 and "case_warning" in out
    # tool output is shortened; the stored record keeps the full list
    shown = out["api_queries"][0]["json"]["filters"]["content"]
    assert any(isinstance(c["content"]["value"], str) and "total 60" in c["content"]["value"] for c in shown)
    assert "total 60" in out["filters_json"]
    saved = db.get_query(out["query_id"])
    assert saved["params"]["criteria"]["case"] == wanted
    assert saved["params"]["case_source"]["file"]["sha256"] == cf["source"]["file"]["sha256"]
    assert any(c["content"]["value"] == wanted for c in saved["api_calls"][0]["json"]["filters"]["content"]
               if c["content"]["field"] == "cases.submitter_id")


@respx.mock
def test_search_cases_with_uuid_list(cfg, db, client):
    route = respx.post(f"{gdc.GDC_API}/cases").mock(return_value=httpx.Response(200, json={"data": {
        "hits": [{"case_id": UUID1, "submitter_id": "TCGA-3C-AAAU", "project": {"project_id": "TCGA-BRCA"},
                  "primary_site": "Breast", "disease_type": "Ductal and Lobular Neoplasms"}],
        "pagination": {"total": 1}}}))
    out = gdc.search_cases(cfg, db, client, case=[UUID1, UUID2], experimental_strategy="WGS")
    body = json.loads(route.calls[0].request.content)
    fields = {c["content"]["field"] for c in body["filters"]["content"]}
    assert fields == {"case_id", "files.experimental_strategy"}
    cf = out["summary"]["case_filter"]
    assert cf["matched"] == 1 and cf["unmatched"] == [UUID2] and "case_warning" in out
