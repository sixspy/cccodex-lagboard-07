# 源到达SLA

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 源到达SLA |
| 目录 | `06-lagboard` |
| 领域建议 | 数据采集、爬虫与 RPA |
| 接口 | http://127.0.0.1:10817 |
| 库 | `data/app.db` |
| 账号 | 管理 admin / 123456；只读 viewer / view123 |

## 鉴权解冻口径（甲路径，已钉死）

详见 `docs/auth-zero-write-ledger.md`（鉴权零写说明专册）。要点：

- 解冻 `POST /api/catchup/{source}` 与采集 `POST /api/arrivals` 在**服务端**强制角色：仅「管理员」可写；「只读」令牌一律 `403`，正式侧零写入（不改 `output_mode`、不写 `catchups`），只追加 `auth_audit` 审计。
- 不新开鉴权台、不另设口令：复用 `/api/login` 会话与既有 `users`；鉴权表 `auth_audit`/`auth_ledger` 同落本仓 `data/app.db`。
- 解冻全程占鉴权锁；只读与管理并发打同一源时，后交一路留 `contended=1` 的「鉴权解冻争用」痕。
- 前端隐藏写按钮仅为体验，最终裁决在服务端。权限五处（权限说明页、审计列表、源目录、补采队列、水位卡片）经 `/api/auth-policy` 等接口同口径读数。

## 为何能出多题

源时钟 vs 摄入时钟、分级处罚、降级可读不可写、补齐解冻

## 避开

别做成纯监控大盘；须有写路径与状态机

## 互斥切口候选

- 超SLA冻结 vs 降级只读
- 补齐整源解冻 vs 按业务日窗口解冻
- 只认源时钟 vs 双时钟取严

## 启动（一键）

`powershell
cd projects/06-lagboard
docker compose up -d --build
`

浏览器：http://127.0.0.1:10817/  
健康检查：GET http://127.0.0.1:10817/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10603
python -m pytest -q
`
