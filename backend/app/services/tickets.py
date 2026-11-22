from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException

from app.db import FORWARD, SPLITTABLE, connect, transaction
from app.services.gate import ensure_day_row, is_day_sealed, refresh_in_flight, today_str


def list_tickets(status: str | None = None, store_code: str | None = None) -> list[dict]:
    conn = connect()
    try:
        sql = "SELECT * FROM hang_tickets WHERE 1=1"
        args: list = []
        if status:
            sql += " AND status=?"
            args.append(status)
        if store_code:
            sql += " AND store_code=?"
            args.append(store_code)
        sql += " ORDER BY id DESC"
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        for r in rows:
            r["items"] = [
                dict(i)
                for i in conn.execute(
                    "SELECT * FROM ticket_items WHERE ticket_no=? ORDER BY id",
                    (r["ticket_no"],),
                ).fetchall()
            ]
        return rows
    finally:
        conn.close()


def get_ticket(ticket_no: str) -> dict:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "挂牌不存在")
        data = dict(row)
        data["items"] = [
            dict(i)
            for i in conn.execute(
                "SELECT * FROM ticket_items WHERE ticket_no=? ORDER BY id",
                (ticket_no,),
            ).fetchall()
        ]
        data["events"] = [
            dict(e)
            for e in conn.execute(
                "SELECT * FROM process_events WHERE ticket_no=? ORDER BY id",
                (ticket_no,),
            ).fetchall()
        ]
        return data
    finally:
        conn.close()


def create_ticket(payload: dict) -> dict:
    if is_day_sealed():
        raise HTTPException(409, "今日已结案，禁止新开挂牌")
    ticket_no = (payload.get("ticket_no") or "").strip()
    store_code = (payload.get("store_code") or "").strip()
    pickup_code = (payload.get("pickup_code") or "").strip()
    customer_name = (payload.get("customer_name") or "").strip()
    due_date = (payload.get("due_date") or "").strip() or today_str()
    note = (payload.get("note") or "").strip()
    items = payload.get("items") or []
    if not ticket_no or not store_code or not pickup_code or not customer_name:
        raise HTTPException(400, "票号、门店、暗码、顾客名必填")
    if not items:
        raise HTTPException(400, "至少一件衣物")

    now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
    with transaction() as conn:
        ensure_day_row(conn)
        store = conn.execute(
            "SELECT * FROM stores WHERE code=? AND active=1", (store_code,)
        ).fetchone()
        if not store:
            raise HTTPException(400, "门店不可用")
        exists = conn.execute(
            "SELECT 1 FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if exists:
            raise HTTPException(409, "票号已存在")
        conn.execute(
            """INSERT INTO hang_tickets
               (ticket_no, store_code, pickup_code, customer_name, status, opened_at, due_date, note)
               VALUES (?,?,?,?, '已收',?,?,?)""",
            (ticket_no, store_code, pickup_code, customer_name, now, due_date, note),
        )
        for it in items:
            garment = (it.get("garment") or "").strip()
            qty = int(it.get("qty") or 0)
            if not garment or qty <= 0:
                raise HTTPException(400, "衣物件不合法")
            conn.execute(
                "INSERT INTO ticket_items(ticket_no, garment, qty, remark) VALUES (?,?,?,?)",
                (ticket_no, garment, qty, (it.get("remark") or "").strip()),
            )
        conn.execute(
            """INSERT INTO process_events(ticket_no, from_status, to_status, operator, occurred_at, note)
               VALUES (?,?,?,?,?,?)""",
            (ticket_no, None, "已收", payload.get("operator") or "店员", now, "收衣开票"),
        )
        refresh_in_flight(conn)
    return get_ticket(ticket_no)


def advance(ticket_no: str, to_status: str, operator: str = "店员", note: str = "") -> dict:
    if is_day_sealed():
        raise HTTPException(409, "今日已结案，禁止改工序")
    to_status = (to_status or "").strip()
    with transaction() as conn:
        ensure_day_row(conn)
        row = conn.execute(
            "SELECT * FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "挂牌不存在")
        cur = row["status"]
        if cur in ("已取", "已拆", "超期隔离", "丢失隔离"):
            raise HTTPException(409, f"当前态「{cur}」不可推进工序")
        allowed = FORWARD.get(cur, ())
        if to_status not in allowed:
            raise HTTPException(409, f"不可从「{cur}」到「{to_status}」")
        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        conn.execute(
            "UPDATE hang_tickets SET status=? WHERE ticket_no=?",
            (to_status, ticket_no),
        )
        conn.execute(
            """INSERT INTO process_events(ticket_no, from_status, to_status, operator, occurred_at, note)
               VALUES (?,?,?,?,?,?)""",
            (ticket_no, cur, to_status, operator or "店员", now, note or ""),
        )
        refresh_in_flight(conn)
    return get_ticket(ticket_no)


def pickup(ticket_no: str, pickup_code: str, operator: str = "店员") -> dict:
    if is_day_sealed():
        raise HTTPException(409, "今日已结案，禁止取衣核销")
    with transaction() as conn:
        ensure_day_row(conn)
        row = conn.execute(
            "SELECT * FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "挂牌不存在")
        if row["status"] != "待取":
            raise HTTPException(409, f"仅「待取」可核销，当前「{row['status']}」")
        if (pickup_code or "").strip() != row["pickup_code"]:
            raise HTTPException(403, "取衣暗码不符")
        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        conn.execute(
            "UPDATE hang_tickets SET status='已取', closed_at=? WHERE ticket_no=?",
            (now, ticket_no),
        )
        conn.execute(
            """INSERT INTO pickups(ticket_no, pickup_code, picked_at, operator)
               VALUES (?,?,?,?)""",
            (ticket_no, row["pickup_code"], now, operator or "店员"),
        )
        conn.execute(
            """INSERT INTO process_events(ticket_no, from_status, to_status, operator, occurred_at, note)
               VALUES (?,?,?,?,?,?)""",
            (ticket_no, "待取", "已取", operator or "店员", now, "取衣核销"),
        )
        refresh_in_flight(conn)
    return get_ticket(ticket_no)


def split_ticket(payload: dict) -> dict:
    """一票多件拆号另立（ADR-001 架构二：新票继承原票现行态）。

    全部联动（衣物件核减/迁移、挂牌、事件、拆票回执、水位）在同一
    BEGIN IMMEDIATE 事务内完成：并发拆号被写锁串行化，负件数在事务内
    拦截，水位全量重放，杜绝半截拆号与双加。
    """
    if is_day_sealed():
        raise HTTPException(409, "今日已结案，禁止拆号另立")
    source_no = (payload.get("source_ticket_no") or "").strip()
    new_no = (payload.get("new_ticket_no") or "").strip()
    operator = (payload.get("operator") or "店员").strip() or "店员"
    note = (payload.get("note") or "").strip()
    req_items = payload.get("items") or []
    if not source_no or not new_no:
        raise HTTPException(400, "原票号与新票号必填")
    if source_no == new_no:
        raise HTTPException(400, "新票号不得与原票号相同")
    if not req_items:
        raise HTTPException(400, "至少拆出一件衣物")

    # 归一化拆出明细，禁止同一 (衣物, 备注) 键在请求内重复，避免归属歧义。
    splits: list[tuple[str, int, str]] = []
    seen: set[tuple[str, str]] = set()
    total_split = 0
    for it in req_items:
        garment = (it.get("garment") or "").strip()
        qty = int(it.get("qty") or 0)
        remark = (it.get("remark") or "").strip()
        if not garment or qty <= 0:
            raise HTTPException(400, "拆出衣物件不合法")
        key = (garment, remark)
        if key in seen:
            raise HTTPException(400, f"拆出明细重复：{garment}（{remark or '无备注'}）")
        seen.add(key)
        splits.append((garment, qty, remark))
        total_split += qty

    now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
    with transaction() as conn:
        ensure_day_row(conn)
        src = conn.execute(
            "SELECT * FROM hang_tickets WHERE ticket_no=?", (source_no,)
        ).fetchone()
        if not src:
            raise HTTPException(404, "原挂牌不存在")
        if src["status"] not in SPLITTABLE:
            raise HTTPException(409, f"原票当前态「{src['status']}」不可拆号另立")
        clash = conn.execute(
            "SELECT 1 FROM hang_tickets WHERE ticket_no=?", (new_no,)
        ).fetchone()
        if clash:
            raise HTTPException(409, "新票号已存在")

        src_rows = conn.execute(
            "SELECT * FROM ticket_items WHERE ticket_no=? ORDER BY id",
            (source_no,),
        ).fetchall()
        src_index = {(r["garment"], r["remark"]): r for r in src_rows}
        for garment, qty, remark in splits:
            row = src_index.get((garment, remark))
            if row is None:
                raise HTTPException(400, f"原票无此衣物件：{garment}（{remark or '无备注'}）")
            # 同事务内先校验后核减：并发拆号串行化后，超拆在此处必被拦截。
            if row["qty"] < qty:
                raise HTTPException(
                    409,
                    f"拆出数超过原票件数：{garment}（{remark or '无备注'}）"
                    f"现存 {row['qty']} 件，申请拆出 {qty} 件",
                )

        inherited_status = src["status"]

        # 1) 衣物件键排他迁移：原票核减（清零即删行），新票插入；拆后任一件只挂一张票。
        for garment, qty, remark in splits:
            row = src_index[(garment, remark)]
            remain = row["qty"] - qty
            if remain == 0:
                conn.execute(
                    "DELETE FROM ticket_items WHERE id=?", (row["id"],)
                )
            else:
                conn.execute(
                    "UPDATE ticket_items SET qty=? WHERE id=?", (remain, row["id"])
                )
            conn.execute(
                "INSERT INTO ticket_items(ticket_no, garment, qty, remark) VALUES (?,?,?,?)",
                (new_no, garment, qty, remark),
            )

        # 2) 新票挂牌：起始态继承原票现行态（架构二唯一口径）。
        conn.execute(
            """INSERT INTO hang_tickets
               (ticket_no, store_code, pickup_code, customer_name, status, opened_at, due_date, note)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                new_no,
                src["store_code"],
                src["pickup_code"],
                src["customer_name"],
                inherited_status,
                now,
                src["due_date"],
                f"拆号另立自 {source_no}；{note}".strip("；"),
            ),
        )

        # 3) 原票：有残留则保持现行态；无残留则关为「已拆」终态。
        remain_total = conn.execute(
            "SELECT COALESCE(SUM(qty),0) AS s FROM ticket_items WHERE ticket_no=?",
            (source_no,),
        ).fetchone()["s"]
        if remain_total == 0:
            conn.execute(
                "UPDATE hang_tickets SET status='已拆', closed_at=? WHERE ticket_no=?",
                (now, source_no),
            )
            src_event_to = "已拆"
        else:
            src_event_to = inherited_status

        # 4) 事件流：原票记拆号动作；新票记继承起始事件，轨页可解释其出生工序。
        detail_text = "，".join(f"{g}×{q}" + (f"（{r}）" if r else "") for g, q, r in splits)
        conn.execute(
            """INSERT INTO process_events(ticket_no, from_status, to_status, operator, occurred_at, note)
               VALUES (?,?,?,?,?,?)""",
            (
                source_no,
                inherited_status,
                src_event_to,
                operator,
                now,
                f"拆号另立 {new_no}，拆出 {total_split} 件：{detail_text}",
            ),
        )
        conn.execute(
            """INSERT INTO process_events(ticket_no, from_status, to_status, operator, occurred_at, note)
               VALUES (?,?,?,?,?,?)""",
            (
                new_no,
                None,
                inherited_status,
                operator,
                now,
                f"拆号另立自 {source_no}，起始态继承「{inherited_status}」；拆入 {total_split} 件",
            ),
        )

        # 5) 拆票回执：原票/新票互证。
        conn.execute(
            """INSERT INTO split_receipts
               (source_ticket_no, new_ticket_no, split_qty, operator, created_at, detail)
               VALUES (?,?,?,?,?,?)""",
            (source_no, new_no, total_split, operator, now, detail_text),
        )

        # 6) 水位同事务全量重算：原票关已拆则净 0，原票残留则 +1，可重放无双加。
        after_watermark = refresh_in_flight(conn)

    return {
        "拆票回执": {
            "source_ticket_no": source_no,
            "new_ticket_no": new_no,
            "split_qty": total_split,
            "operator": operator,
            "created_at": now,
            "detail": detail_text,
            "原票残留件数": remain_total,
            "原票关为已拆": remain_total == 0,
            "新票起始态": inherited_status,
            "拆后在途水位": after_watermark,
        },
        "原票": get_ticket(source_no),
        "新票": get_ticket(new_no),
    }


def list_split_receipts() -> list[dict]:
    conn = connect()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM split_receipts ORDER BY id DESC"
            ).fetchall()
        ]
    finally:
        conn.close()


def quarantine(ticket_no: str, kind: str, reason: str, operator: str = "店员") -> dict:
    kind = (kind or "").strip()
    if kind not in ("超期隔离", "丢失隔离"):
        raise HTTPException(400, "隔离种类仅超期隔离或丢失隔离")
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM hang_tickets WHERE ticket_no=?", (ticket_no,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "挂牌不存在")
        if row["status"] in ("已取", "已拆", "丢失隔离"):
            raise HTTPException(409, f"当前态「{row['status']}」不可隔离")
        if kind == "超期隔离" and row["status"] not in ("待取", "超期隔离"):
            raise HTTPException(409, "超期隔离仅从待取进入")
        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        prev = row["status"]
        conn.execute(
            "UPDATE hang_tickets SET status=? WHERE ticket_no=?",
            (kind, ticket_no),
        )
        conn.execute(
            """INSERT INTO quarantines(ticket_no, kind, reason, created_at, resolved)
               VALUES (?,?,?,?,0)""",
            (ticket_no, kind, reason or kind, now),
        )
        conn.execute(
            """INSERT INTO process_events(ticket_no, from_status, to_status, operator, occurred_at, note)
               VALUES (?,?,?,?,?,?)""",
            (ticket_no, prev, kind, operator or "店员", now, reason or ""),
        )
        refresh_in_flight(conn)
    return get_ticket(ticket_no)
