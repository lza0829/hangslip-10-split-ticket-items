"""拆号另立联动与不变量测试（ADR-001 架构二）。

运行：backend/.venv/bin/python -m pytest backend/tests -q
（需 httpx；TestClient 基于线程池，可真实驱动 BEGIN IMMEDIATE 锁竞争）
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "t.db"

    # config 在导入时固化 DB_PATH，用 monkeypatch 直接改模块变量。
    from app import config
    monkeypatch.setattr(config, "DB_PATH", Path(db_file))
    from app import db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", Path(db_file))

    # 每个测试一个全新库：显式建表并准备在用门店（app.main 的 seed 仅首次导入执行一次）。
    db_mod.init_db()
    conn = db_mod.connect()
    conn.execute(
        "INSERT INTO stores(code, name, rack_count, active) VALUES ('店-河西','河西干洗',3,1)"
    )
    conn.commit()
    conn.close()

    from app.services import gate as gate_mod
    monkeypatch.setattr(gate_mod, "connect", db_mod.connect)
    from app.services import tickets as svc
    monkeypatch.setattr(svc, "connect", db_mod.connect)
    from app.routers import dashboard as dash
    monkeypatch.setattr(dash, "connect", db_mod.connect)

    from app.main import app

    with TestClient(app) as c:
        yield c, db_mod


def _open(client, no, items, status=None):
    body = {
        "ticket_no": no,
        "store_code": "店-河西",
        "pickup_code": f"码-{no}",
        "customer_name": "测试客",
        "items": items,
    }
    r = client.post("/api/挂牌", json=body)
    assert r.status_code == 200, r.text
    if status:
        r = client.post(f"/api/挂牌/{no}/推进", json={"to_status": status})
        assert r.status_code == 200, r.text
    return r.json()


def _total_qty(db_mod):
    conn = db_mod.connect()
    try:
        return conn.execute("SELECT COALESCE(SUM(qty),0) AS s FROM ticket_items").fetchone()["s"]
    finally:
        conn.close()


def _watermark(db_mod):
    conn = db_mod.connect()
    try:
        return conn.execute("SELECT value FROM watermarks WHERE key='在途票'").fetchone()["value"]
    finally:
        conn.close()


def _in_flight_count(db_mod):
    conn = db_mod.connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM hang_tickets WHERE status NOT IN ('已取','丢失隔离','已拆')"
        ).fetchone()["c"]
    finally:
        conn.close()


def test_split_partial_keeps_source_and_inherits_status(client):
    c, db = client
    _open(c, "挂-T1", [{"garment": "衬衣", "qty": 3}, {"garment": "大衣", "qty": 2, "remark": "防水"}])
    c.post("/api/挂牌/挂-T1/推进", json={"to_status": "水洗"})
    before_total = _total_qty(db)
    wm_before = _watermark(db)

    r = c.post(
        "/api/挂牌/拆号",
        json={
            "source_ticket_no": "挂-T1",
            "new_ticket_no": "挂-T1-A",
            "items": [{"garment": "衬衣", "qty": 2}],
        },
    )
    assert r.status_code == 200, r.text
    receipt = r.json()["拆票回执"]

    # 起始态继承原票现行态
    assert receipt["新票起始态"] == "水洗"
    assert r.json()["新票"]["status"] == "水洗"
    assert receipt["原票关为已拆"] is False
    assert receipt["原票残留件数"] == 3

    src = c.get("/api/挂牌/挂-T1").json()
    new = c.get("/api/挂牌/挂-T1-A").json()
    assert src["status"] == "水洗"  # 原票残留，继续沿原工序
    # 衣物件键排他：衬衣 1 件留原票、2 件归新票，无共用
    assert {(i["garment"], i["remark"], i["qty"]) for i in src["items"]} == {
        ("衬衣", "", 1),
        ("大衣", "防水", 2),
    }
    assert {(i["garment"], i["remark"], i["qty"]) for i in new["items"]} == {("衬衣", "", 2)}

    # 件数守恒
    assert _total_qty(db) == before_total
    # 残留场景水位 +1，且与全量重算值一致
    assert _watermark(db) == wm_before + 1 == _in_flight_count(db)

    # 事件：新票首事件 from 空、to 继承态；原票记拆号动作
    assert new["events"][-1]["to_status"] == "水洗"
    assert new["events"][-1]["from_status"] is None
    assert any("拆号另立 挂-T1-A" in e["note"] for e in src["events"])

    # 回执可查
    logs = c.get("/api/拆票回执").json()
    assert logs[0]["source_ticket_no"] == "挂-T1"
    assert logs[0]["new_ticket_no"] == "挂-T1-A"
    assert logs[0]["split_qty"] == 2


def test_split_full_closes_source_as_split_watermark_net_zero(client):
    c, db = client
    _open(c, "挂-T2", [{"garment": "西装", "qty": 4}])
    c.post("/api/挂牌/挂-T2/推进", json={"to_status": "熨烫"})
    before_total = _total_qty(db)
    wm_before = _watermark(db)

    r = c.post(
        "/api/挂牌/拆号",
        json={
            "source_ticket_no": "挂-T2",
            "new_ticket_no": "挂-T2-B",
            "items": [{"garment": "西装", "qty": 4}],
        },
    )
    assert r.status_code == 200, r.text
    receipt = r.json()["拆票回执"]
    assert receipt["原票关为已拆"] is True
    assert receipt["原票残留件数"] == 0

    src = c.get("/api/挂牌/挂-T2").json()
    new = c.get("/api/挂牌/挂-T2-B").json()
    assert src["status"] == "已拆"
    assert src["closed_at"] is not None
    assert new["status"] == "熨烫"  # 继承
    assert src["items"] == []       # 原票无残留衣物件
    assert _total_qty(db) == before_total

    # 原票出在途、新票入在途 → 水位净 0，且与重算值一致（无双加）
    assert _watermark(db) == wm_before == _in_flight_count(db)
    # 已拆票不可再推进
    r2 = c.post("/api/挂牌/挂-T2/推进", json={"to_status": "待取"})
    assert r2.status_code == 409


def test_over_split_rejected_no_negative_qty_and_no_half_split(client):
    c, db = client
    _open(c, "挂-T3", [{"garment": "衬衣", "qty": 2}])
    wm_before = _watermark(db)
    total_before = _total_qty(db)

    r = c.post(
        "/api/挂牌/拆号",
        json={
            "source_ticket_no": "挂-T3",
            "new_ticket_no": "挂-T3-X",
            "items": [{"garment": "衬衣", "qty": 5}],
        },
    )
    assert r.status_code == 409
    # 整事务回滚：无新票、无新衣物件行、水位与件数不变
    assert c.get("/api/挂牌/挂-T3-X").status_code == 404
    assert _total_qty(db) == total_before
    assert _watermark(db) == wm_before
    conn = db.connect()
    try:
        qty = conn.execute(
            "SELECT qty FROM ticket_items WHERE ticket_no='挂-T3'"
        ).fetchone()["qty"]
    finally:
        conn.close()
    assert qty == 2


def test_concurrent_splits_serialized_no_negative_no_double_count(client):
    c, db = client
    _open(c, "挂-T4", [{"garment": "衬衣", "qty": 6}])
    total_before = _total_qty(db)

    def one(suffix):
        # 每个线程独立 TestClient 共享同一 sqlite 文件，真实驱动写锁竞争。
        from fastapi.testclient import TestClient
        from app.main import app
        local = TestClient(app)
        resp = local.post(
            "/api/挂牌/拆号",
            json={
                "source_ticket_no": "挂-T4",
                "new_ticket_no": f"挂-T4-{suffix}",
                "items": [{"garment": "衬衣", "qty": 4}],
            },
        )
        return resp.status_code


    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(one, ["P", "Q"]))
    assert sorted(codes) == [200, 409]  # 仅一笔成功，超拆笔被串行化后拦截

    # 无负件数；件数守恒；水位 = 重算值（无双加）
    conn = db.connect()
    try:
        min_qty = conn.execute("SELECT MIN(qty) AS m FROM ticket_items").fetchone()["m"]
        rows = conn.execute(
            "SELECT ticket_no, SUM(qty) AS s FROM ticket_items GROUP BY ticket_no"
        ).fetchall()
    finally:
        conn.close()
    assert min_qty >= 0
    # 胜者非 P 即 Q（写锁串行化，谁先拿到 BEGIN IMMEDIATE 谁赢）
    winner = next(t for t in ("挂-T4-P", "挂-T4-Q") if t in {r["ticket_no"] for r in rows})
    assert {r["ticket_no"]: r["s"] for r in rows} == {"挂-T4": 2, winner: 4}
    assert _total_qty(db) == total_before
    assert _watermark(db) == _in_flight_count(db)


def test_concurrent_full_split_and_partial_split(client):
    c, db = client
    _open(c, "挂-T5", [{"garment": "裙", "qty": 3}])

    def call(suffix, qty):
        from fastapi.testclient import TestClient
        from app.main import app
        local = TestClient(app)
        return local.post(
            "/api/挂牌/拆号",
            json={
                "source_ticket_no": "挂-T5",
                "new_ticket_no": f"挂-T5-{suffix}",
                "items": [{"garment": "裙", "qty": qty}],
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(call, "F", 3)  # 全拆 → 原票关已拆
        f2 = pool.submit(call, "H", 1)  # 后到者要么成功（先到的是 H），要么 409
        codes = sorted([f1.result(), f2.result()])
    assert codes == [200, 409]

    conn = db.connect()
    try:
        statuses = dict(
            (r["ticket_no"], r["status"])
            for r in conn.execute("SELECT ticket_no, status FROM hang_tickets").fetchall()
        )
    finally:
        conn.close()
    # 原票要么已拆（全拆成功），要么仍是已收（部分拆成功留 2 件）；不存在双挂
    assert statuses["挂-T5"] in ("已拆", "已收")
    assert _watermark(db) == _in_flight_count(db)


def test_split_guard_conditions(client):
    c, _ = client
    _open(c, "挂-T6", [{"garment": "衬衣", "qty": 2}])
    c.post("/api/挂牌/挂-T6/推进", json={"to_status": "待取"})
    c.post(
        "/api/挂牌/挂-T6/隔离",
        json={"kind": "超期隔离", "reason": "逾承诺日"},
    )
    # 隔离态不可拆
    r = c.post(
        "/api/挂牌/拆号",
        json={
            "source_ticket_no": "挂-T6",
            "new_ticket_no": "挂-T6-Z",
            "items": [{"garment": "衬衣", "qty": 1}],
        },
    )
    assert r.status_code == 409

    # 新票号撞号
    r2 = c.post(
        "/api/挂牌/拆号",
        json={
            "source_ticket_no": "挂-T6",
            "new_ticket_no": "挂-T6",
            "items": [{"garment": "衬衣", "qty": 1}],
        },
    )
    assert r2.status_code in (400, 409)

    # 拆不存在的衣物件
    r3 = c.post(
        "/api/挂牌/拆号",
        json={
            "source_ticket_no": "挂-T6",
            "new_ticket_no": "挂-T6-W",
            "items": [{"garment": "貂皮大衣", "qty": 1}],
        },
    )
    assert r3.status_code in (400, 409)


def test_same_item_key_cannot_be_shared_after_split(client):
    c, db = client
    _open(c, "挂-T7", [{"garment": "衬衣", "qty": 5, "remark": "白"}])
    r = c.post(
        "/api/挂牌/拆号",
        json={
            "source_ticket_no": "挂-T7",
            "new_ticket_no": "挂-T7-A",
            "items": [{"garment": "衬衣", "qty": 2, "remark": "白"}],
        },
    )
    assert r.status_code == 200, r.text
    conn = db.connect()
    try:
        # 同一 (票号, 衣物, 备注) 键各自只出现一行，且两票各持各的归属，不共用行
        owners = conn.execute(
            "SELECT ticket_no, qty FROM ticket_items WHERE garment='衬衣' AND remark='白' ORDER BY ticket_no"
        ).fetchall()
    finally:
        conn.close()
    assert [(o["ticket_no"], o["qty"]) for o in owners] == [
        ("挂-T7", 3),
        ("挂-T7-A", 2),
    ]
