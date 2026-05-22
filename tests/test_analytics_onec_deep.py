"""Deep tests for src/tools/analytics_local.py + src/tools/onec.py."""
import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


# ============================================================
# DuckDB (analytics_local)
# ============================================================

def test_duckdb_query_returns_fix_hint_when_lib_missing():
    """If duckdb isn't installed, _connect returns a structured fix_hint."""
    from src.tools import analytics_local
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "duckdb":
            raise ImportError("not installed")
        return real_import(name, *a, **kw)

    with patch.object(builtins, "__import__", side_effect=fake_import):
        out = analytics_local.duckdb_query("SELECT 1")
    if not out.get("ok"):
        assert "fix_hint" in out
        assert "duckdb" in out["fix_hint"]


def test_duckdb_query_simple_select(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    from src.tools import analytics_local
    monkeypatch.setattr(analytics_local, "DB_PATH", tmp_path / "d.duckdb")
    out = analytics_local.duckdb_query("SELECT 42 AS answer")
    assert out["ok"] is True
    assert out["data"]["row_count"] == 1
    assert out["data"]["rows"][0]["answer"] == 42


def test_duckdb_query_max_rows_truncates(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    from src.tools import analytics_local
    monkeypatch.setattr(analytics_local, "DB_PATH", tmp_path / "d.duckdb")
    out = analytics_local.duckdb_query("SELECT * FROM range(20)", max_rows=5)
    assert out["data"]["row_count"] == 20
    assert len(out["data"]["rows"]) == 5
    assert out["_meta"]["truncated"] is True


def test_duckdb_query_syntax_error_returns_error_dict(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    from src.tools import analytics_local
    monkeypatch.setattr(analytics_local, "DB_PATH", tmp_path / "d.duckdb")
    out = analytics_local.duckdb_query("SELECT FROM where bad")
    assert out["ok"] is False


def test_duckdb_import_csv_creates_table(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    from src.tools import analytics_local
    monkeypatch.setattr(analytics_local, "DB_PATH", tmp_path / "d.duckdb")
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    out = analytics_local.duckdb_import_csv("mytab", str(csv_path))
    assert out["ok"] is True
    assert out["data"]["row_count"] == 2


def test_duckdb_import_csv_replace_overwrites(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    from src.tools import analytics_local
    monkeypatch.setattr(analytics_local, "DB_PATH", tmp_path / "d.duckdb")
    csv1 = tmp_path / "in1.csv"
    csv1.write_text("a\n1\n2\n", encoding="utf-8")
    csv2 = tmp_path / "in2.csv"
    csv2.write_text("a\n10\n20\n30\n", encoding="utf-8")
    analytics_local.duckdb_import_csv("t", str(csv1))
    out = analytics_local.duckdb_import_csv("t", str(csv2), replace=True)
    assert out["data"]["row_count"] == 3


def test_duckdb_list_tables_after_import(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    from src.tools import analytics_local
    monkeypatch.setattr(analytics_local, "DB_PATH", tmp_path / "d.duckdb")
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("a,b\n1,2\n", encoding="utf-8")
    analytics_local.duckdb_import_csv("t1", str(csv_path))
    out = analytics_local.duckdb_list_tables()
    assert out["ok"] is True
    assert any(t["name"] == "t1" for t in out["data"]["tables"])


def test_duckdb_drop_table(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    from src.tools import analytics_local
    monkeypatch.setattr(analytics_local, "DB_PATH", tmp_path / "d.duckdb")
    csv = tmp_path / "in.csv"
    csv.write_text("a\n1\n", encoding="utf-8")
    analytics_local.duckdb_import_csv("t1", str(csv))
    analytics_local.duckdb_drop_table("t1")
    tables = analytics_local.duckdb_list_tables()["data"]["tables"]
    assert not any(t["name"] == "t1" for t in tables)


def test_duckdb_export_parquet(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    from src.tools import analytics_local
    monkeypatch.setattr(analytics_local, "DB_PATH", tmp_path / "d.duckdb")
    csv = tmp_path / "in.csv"
    csv.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    analytics_local.duckdb_import_csv("t", str(csv))
    out_pq = tmp_path / "out.parquet"
    out = analytics_local.duckdb_export_parquet("t", str(out_pq))
    assert out["ok"] is True
    assert out_pq.exists()
    assert out["data"]["bytes"] > 0


def test_duckdb_query_inline_read_csv_auto(tmp_path, monkeypatch):
    """`SELECT FROM read_csv_auto(...)` should work directly without
    explicit import."""
    pytest.importorskip("duckdb")
    from src.tools import analytics_local
    monkeypatch.setattr(analytics_local, "DB_PATH", tmp_path / "d.duckdb")
    csv = tmp_path / "in.csv"
    csv.write_text("x,y\n1,A\n2,B\n", encoding="utf-8")
    out = analytics_local.duckdb_query(
        f"SELECT * FROM read_csv_auto('{csv.as_posix()}') WHERE x > 1"
    )
    assert out["ok"] is True
    assert out["data"]["row_count"] == 1
    assert out["data"]["rows"][0]["y"] == "B"


# ============================================================
# 1С OData (onec)
# ============================================================

def test_onec_odata_get_includes_basic_auth():
    from src.tools import onec
    captured = {}

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.headers)
        m = MagicMock()
        m.read.return_value = b'{"value":[]}'
        m.status = 200
        m.headers = {}
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        onec._odata_get("https://1c.example.ru/buh3/odata/standard.odata",
                        "user1", "pass1", "Catalog_Контрагенты")
    auth = captured["headers"]["Authorization"]
    assert auth.startswith("Basic ")
    assert base64.b64decode(auth[6:]).decode() == "user1:pass1"


def test_onec_odata_get_forces_json_format():
    from src.tools import onec
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req if isinstance(req, str) else req.full_url
        m = MagicMock()
        m.read.return_value = b'{"value":[]}'
        m.status = 200
        m.headers = {}
        m.__enter__ = lambda s: s
        m.__exit__ = lambda s, *a: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        onec._odata_get("https://x/odata", "u", "p", "Catalog_X")
    assert "%24format=json" in captured["url"] or "$format=json" in captured["url"]


def test_onec_odata_query_passes_filter_and_top():
    from src.tools import onec
    captured = {}

    def fake_get(base, login, password, path, query=None, timeout=60):
        captured["query"] = query
        return {"ok": True, "data": {"value": []}}

    with patch.object(onec, "_odata_get", side_effect=fake_get):
        onec.onec_odata_query("base", "u", "p", "Catalog_X",
                              filter_="Description eq 'A'", top=50, skip=100)
    assert captured["query"]["$filter"] == "Description eq 'A'"
    assert captured["query"]["$top"] == 50
    assert captured["query"]["$skip"] == 100


def test_onec_odata_query_select_field():
    from src.tools import onec
    captured = {}

    def fake_get(base, login, password, path, query=None, timeout=60):
        captured["query"] = query
        return {"ok": True}

    with patch.object(onec, "_odata_get", side_effect=fake_get):
        onec.onec_odata_query("base", "u", "p", "Catalog_X",
                              select="Description,Code")
    assert captured["query"]["$select"] == "Description,Code"


def test_onec_contractors_builds_substring_filter():
    from src.tools import onec
    captured = {}

    def fake_get(base, login, password, path, query=None, timeout=60):
        captured["path"] = path
        captured["query"] = query
        return {"ok": True}

    with patch.object(onec, "_odata_get", side_effect=fake_get):
        onec.onec_contractors("base", "u", "p", name_like="Ромашка")
    assert captured["path"] == "Catalog_Контрагенты"
    assert "substringof('Ромашка', Description)" in captured["query"]["$filter"]


def test_onec_contractors_no_filter_when_no_name():
    from src.tools import onec
    captured = {}

    def fake_get(base, login, password, path, query=None, timeout=60):
        captured["query"] = query
        return {"ok": True}

    with patch.object(onec, "_odata_get", side_effect=fake_get):
        onec.onec_contractors("base", "u", "p")
    assert "$filter" not in captured["query"]


def test_onec_products_pagination():
    from src.tools import onec
    captured = {}

    def fake_get(base, login, password, path, query=None, timeout=60):
        captured["query"] = query
        return {"ok": True}

    with patch.object(onec, "_odata_get", side_effect=fake_get):
        onec.onec_products("base", "u", "p", top=500, skip=1000)
    assert captured["query"]["$top"] == 500
    assert captured["query"]["$skip"] == 1000


def test_onec_documents_date_filter():
    from src.tools import onec
    captured = {}

    def fake_get(base, login, password, path, query=None, timeout=60):
        captured["query"] = query
        return {"ok": True}

    with patch.object(onec, "_odata_get", side_effect=fake_get):
        onec.onec_documents("base", "u", "p",
                            "Document_РеализацияТоваровУслуг",
                            date_from="2026-05-01T00:00:00")
    assert "Date ge datetime'2026-05-01T00:00:00'" in captured["query"]["$filter"]


def test_onec_money_balance_endpoint():
    from src.tools import onec
    captured = {}

    def fake_get(base, login, password, path, query=None, timeout=60):
        captured["path"] = path
        captured["query"] = query
        return {"ok": True}

    with patch.object(onec, "_odata_get", side_effect=fake_get):
        onec.onec_money_balance("base", "u", "p")
    assert captured["path"] == "AccumulationRegister_ДенежныеСредстваБалансе"


def test_onec_money_balance_with_date_filter():
    from src.tools import onec
    captured = {}

    def fake_get(base, login, password, path, query=None, timeout=60):
        captured["query"] = query
        return {"ok": True}

    with patch.object(onec, "_odata_get", side_effect=fake_get):
        onec.onec_money_balance("base", "u", "p", date_iso="2026-05-01T00:00:00")
    assert "Period le datetime'2026-05-01T00:00:00'" in captured["query"]["$filter"]


def test_onec_odata_get_handles_401():
    from src.tools import onec
    from urllib.error import HTTPError
    fake = MagicMock()
    fake.read.return_value = b'<html>Unauthorized</html>'
    with patch("urllib.request.urlopen",
               side_effect=HTTPError("u", 401, "Unauthorized", {}, fake)):
        out = onec._odata_get("https://1c.example.com/odata", "u", "p", "Catalog_X")
    assert out["ok"] is False
    assert out["_meta"]["http_status"] == 401


def test_onec_odata_get_handles_404():
    from src.tools import onec
    from urllib.error import HTTPError
    fake = MagicMock()
    fake.read.return_value = b'not found'
    with patch("urllib.request.urlopen",
               side_effect=HTTPError("u", 404, "Not Found", {}, fake)):
        out = onec._odata_get("https://1c.example.com/odata", "u", "p", "Catalog_X")
    assert out["ok"] is False
    assert out["_meta"]["http_status"] == 404


def test_onec_query_default_top_limit_100():
    from src.tools import onec
    captured = {}

    def fake_get(base, login, password, path, query=None, timeout=60):
        captured["query"] = query
        return {"ok": True}

    with patch.object(onec, "_odata_get", side_effect=fake_get):
        onec.onec_odata_query("b", "u", "p", "Catalog_X")
    assert captured["query"]["$top"] == 100


def test_onec_query_default_skip_zero():
    from src.tools import onec
    captured = {}

    def fake_get(base, login, password, path, query=None, timeout=60):
        captured["query"] = query
        return {"ok": True}

    with patch.object(onec, "_odata_get", side_effect=fake_get):
        onec.onec_odata_query("b", "u", "p", "Catalog_X")
    assert captured["query"]["$skip"] == 0
