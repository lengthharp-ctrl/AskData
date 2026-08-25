"""API 集成测试：示例数据 → 问答 → 表格与图表。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_sample_then_ask():
    resp = client.post("/api/sample")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["row_count"] > 0
    assert any(item["name"] == "订单日期" for item in payload["schema"])

    resp = client.post(
        "/api/ask",
        json={"session_id": payload["session_id"], "question": "按月统计销售额，画个趋势图"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["sandbox"]["ok"] is True
    assert data["results"], "应返回表格结果"
    assert data["charts"], "应返回图表"
    assert "pandas" in data["code"] or "df" in data["code"]

    chart = data["charts"][0]
    resp = client.get(chart["url"])
    assert resp.status_code == 200


def test_ask_blocks_dangerous_generated_code():
    """极端情况下模型返回危险代码，沙箱必须拦截而不是执行。"""
    from unittest.mock import patch

    with patch("app.main.generate_code", return_value="import os\nos.system('whoami')"):
        resp = client.post(
            "/api/ask",
            json={"session_id": _new_session(), "question": "随便问问"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["sandbox"]["ok"] is False
    assert data["sandbox"]["kind"] == "blocked"


def test_upload_csv():
    import io

    content = "城市,销售额\n北京,100\n上海,200\n"
    resp = client.post(
        "/api/upload",
        files={"file": ("demo.csv", io.BytesIO(content.encode("utf-8")), "text/csv")},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["row_count"] == 2
    assert payload["session_id"]


def _new_session() -> str:
    return client.post("/api/sample").json()["session_id"]

