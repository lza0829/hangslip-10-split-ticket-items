# 衣票墙（hangslip）

收衣贴号 → 工序推进 → 取衣核销关账 → 超期/丢失隔离互证。

- 前端 http://127.0.0.1:6200
- API http://127.0.0.1:11200

## 拆号另立（一票多件）

口径见 `docs/adr-001-拆号起始态.md`：全局唯一采用「继承原票现行态」。

- `POST /api/挂牌/拆号`：同事务完成衣物件核减迁移、新票挂牌（起始态＝原票现行态）、
  原票无残留则关为「已拆」、双方事件、拆票回执与在途水位全量重算。
- `GET /api/拆票回执`：拆号流水。
- 不变量：件数守恒；拆后两票不共用衣物件键；水位可重放不双加；
  并发拆号由 `BEGIN IMMEDIATE` 串行化，超拆返 409，不产生负件数。
- 仅覆盖拆票起始态；并票吞并不在范围内。

本地开发：

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 11200

cd ..\frontend
npm install
npm run dev
```
