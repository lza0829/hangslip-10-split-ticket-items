import { Fragment, useEffect, useState } from "react";
import { api } from "../api";

const NEXT = {
  已收: ["水洗", "烘干", "熨烫", "待取"],
  水洗: ["烘干", "熨烫", "待取"],
  烘干: ["熨烫", "待取"],
  熨烫: ["待取"],
};
const HIDDEN = ["已取", "已拆", "超期隔离", "丢失隔离"];

export default function Process() {
  const [rows, setRows] = useState([]);
  const [msg, setMsg] = useState("");
  const [ok, setOk] = useState(false);
  const [splitFor, setSplitFor] = useState(null);
  const [newNo, setNewNo] = useState("");
  const [splitQtys, setSplitQtys] = useState({});
  const [receipts, setReceipts] = useState([]);

  async function load() {
    const [all, logs] = await Promise.all([
      api.挂牌列表(),
      api.拆票回执(),
    ]);
    setRows(all.filter((r) => !HIDDEN.includes(r.status)));
    setReceipts(logs);
  }

  useEffect(() => {
    load().catch((e) => {
      setMsg(e.message);
      setOk(false);
    });
  }, []);

  function flash(text, good = false) {
    setMsg(text);
    setOk(good);
  }

  async function go(ticket_no, to_status) {
    try {
      await api.推进(ticket_no, { to_status, operator: "工序员" });
      flash(`${ticket_no} → ${to_status}`, true);
      await load();
    } catch (e) {
      flash(e.message);
    }
  }

  function openSplit(ticket_no) {
    setSplitFor(ticket_no);
    setNewNo("");
    setSplitQtys({});
    setMsg("");
  }

  async function submitSplit(row) {
    const items = row.items
      .map((it) => ({
        garment: it.garment,
        remark: it.remark || "",
        qty: Number(splitQtys[it.id] || 0),
      }))
      .filter((it) => it.qty > 0);
    if (!newNo.trim()) {
      flash("请填写新票号");
      return;
    }
    if (items.length === 0) {
      flash("请至少填写一件拆出数");
      return;
    }
    try {
      const res = await api.拆号({
        source_ticket_no: row.ticket_no,
        new_ticket_no: newNo.trim(),
        operator: "工序员",
        items,
      });
      const r = res["拆票回执"];
      flash(
        `拆号回执：${r.source_ticket_no} → ${r.new_ticket_no}，` +
          `新票起始态「${r["新票起始态"]}」（继承原票现行态），拆出 ${r.split_qty} 件，` +
          (r["原票关为已拆"]
            ? "原票无残留已关为已拆，水位净 0"
            : `原票残留 ${r["原票残留件数"]} 件，水位 ${r["拆后在途水位"]}`),
        true
      );
      setSplitFor(null);
      await load();
    } catch (e) {
      flash(e.message);
    }
  }

  return (
    <section className="panel">
      <h3>工序轨</h3>
      <p className={`msg ${ok ? "ok" : ""}`}>{msg}</p>
      <table>
        <thead>
          <tr>
            <th>票号</th>
            <th>门店</th>
            <th>顾客</th>
            <th>现行</th>
            <th>衣物件</th>
            <th>承诺日</th>
            <th>推进 / 拆号</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <Fragment key={r.ticket_no}>
              <tr key={r.ticket_no}>
                <td>{r.ticket_no}</td>
                <td>{r.store_code}</td>
                <td>{r.customer_name}</td>
                <td>
                  <span className={`badge ${r.status}`}>{r.status}</span>
                </td>
                <td>
                  {r.items
                    .map((it) => `${it.garment}×${it.qty}${it.remark ? `（${it.remark}）` : ""}`)
                    .join("，")}
                </td>
                <td>{r.due_date}</td>
                <td>
                  <div className="row">
                    {(NEXT[r.status] || []).map((s) => (
                      <button
                        key={s}
                        type="button"
                        className="ghost"
                        onClick={() => go(r.ticket_no, s)}
                      >
                        {s}
                      </button>
                    ))}
                    <button type="button" className="ghost" onClick={() => openSplit(r.ticket_no)}>
                      拆号另立
                    </button>
                  </div>
                </td>
              </tr>
              {splitFor === r.ticket_no && (
                <tr key={`${r.ticket_no}-split-panel`}>
                  <td colSpan={7}>
                    <div className="split-box">
                      <p className="msg ok">
                        新票起始态将继承原票现行态「{r.status}」，拆后两票不共用衣物件键
                      </p>
                      <table>
                        <thead>
                          <tr>
                            <th>衣物件</th>
                            <th>现存件数</th>
                            <th>拆出件数</th>
                          </tr>
                        </thead>
                        <tbody>
                          {r.items.map((it) => (
                            <tr key={it.id}>
                              <td>
                                {it.garment}
                                {it.remark ? `（${it.remark}）` : ""}
                              </td>
                              <td>{it.qty}</td>
                              <td>
                                <input
                                  type="number"
                                  min="0"
                                  max={it.qty}
                                  value={splitQtys[it.id] ?? ""}
                                  placeholder="0"
                                  onChange={(e) =>
                                    setSplitQtys({
                                      ...splitQtys,
                                      [it.id]: e.target.value,
                                    })
                                  }
                                />
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      <div className="row">
                        <input
                          placeholder="新票号"
                          value={newNo}
                          onChange={(e) => setNewNo(e.target.value)}
                        />
                        <button type="button" onClick={() => submitSplit(r)}>
                          确认拆号（起始态：{r.status}）
                        </button>
                        <button type="button" className="ghost" onClick={() => setSplitFor(null)}>
                          取消
                        </button>
                      </div>
                    </div>
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
      <h3 style={{ marginTop: 24 }}>拆票回执</h3>
      <table>
        <thead>
          <tr>
            <th>时间</th>
            <th>原票</th>
            <th>新票</th>
            <th>拆出件数</th>
            <th>明细</th>
            <th>操作人</th>
          </tr>
        </thead>
        <tbody>
          {receipts.length === 0 && (
            <tr>
              <td colSpan={6}>暂无拆号记录</td>
            </tr>
          )}
          {receipts.map((rc) => (
            <tr key={rc.id}>
              <td>{rc.created_at}</td>
              <td>{rc.source_ticket_no}</td>
              <td>{rc.new_ticket_no}</td>
              <td>{rc.split_qty}</td>
              <td>{rc.detail}</td>
              <td>{rc.operator}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
