# 实体键桥

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 实体键桥 |
| 目录 | `07-keyremap` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10919 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

一对多/多对一、生效窗、旧键只读、重映重放

## 避开

别做成 Feed 冲突归并；正式区不是 staging 换皮

## 互斥切口候选

- 冲突拒收整批 vs 进冲突区后重放
- 废弃旧键只读保留 vs 重写正式键
- 生效窗原子切换 vs 双窗按事件时裁

## 启动（一键）

`powershell
cd projects/07-keyremap
docker compose up -d --build
`

浏览器：http://127.0.0.1:10919/  
健康检查：GET http://127.0.0.1:10919/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10604
python -m pytest -q
`

## 挂版证据与推版口径（formal_version_pin）

推版（m1 → m2）不得悄悄改写已正式实体行的挂版。挂版证据持久落在
`entity_map_freeze(formal_row_id, entity_key, freeze_version, freeze_ts)`，
与界面展示无关；正确性口径按库级 `pin_policy` 二选一，建库定型后不可翻转：

| 口径 | 取值 | 推 m2 时旧正式行 | 同事务留痕 |
|------|------|------------------|-----------|
| 甲·冻结挂版（默认） | `PIN_POLICY=freeze` | 挂版保持 m1，仅新重映用 m2 | `freeze_drift_reject` 逐行拒漂；挂版表带 UPDATE/DELETE 不可变触发器 |
| 乙·追随挂版 | `PIN_POLICY=follow` | 可改挂 m2 | `remap_generation` 逐行追加审计，失败则挂版与审计一并回滚 |

单库只贯彻一种口径：`freeze` 库不写 generation 审计，`follow` 库不写拒漂，
对账卡对混用直接判失衡。所有挂版/映册写经 `BEGIN IMMEDIATE` 显式锁，
抢锁失败写 `write_contention` 后返回 409，禁止无留痕争用。

接口（均需登录 Bearer）：

- `POST /api/map-versions/push` 推新版映册（同事务完成推版与拒漂/改挂审计）
- `GET  /api/formal/pins` 挂版冻结核证（只读，正式列版号与冻结证据并排）
- `GET  /api/audit` 审计流水（generation / drift_reject / contention）
- `GET  /api/reconciliation` 版级对账卡（balanced 与逐项 check）
- `GET  /api/inbound` 入站列表（formal / conflict）

夹具与验证：

- `python fixtures/build_fixtures.py` 生成 `fixtures/fixture_freeze.db`（旧行仍挂 m1）
  与 `fixtures/fixture_follow.db`（旧行挂 m2、generation 可查）
- `python scripts/e2e_verify.py freeze|follow` 单库端到端核证
- `python -m pytest -q` 含并发抢写争用、原子回滚、不可变触发器共 9 项

