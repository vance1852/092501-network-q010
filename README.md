# 城市地下管网安全监测与应急调度服务

本项目为城市供水、排水和燃气管网提供离线后台服务，保存管段、传感器读数、泄漏告警、巡检工单、维修审批和应急资源分配。系统使用确定性的风险评分帮助值班人员优先处理高风险管段，账号按角色授予读取、处置和审批权限，状态变化写入 SQLite 审计表。

## 目录

- `src/urban_network/`：管网领域服务、风险计算、权限、SQLite 存储、班组路线批次与离线回执合并（`routes.py`）和 JSON API；
- `src/power_dispatch/`：应急泵站资源分配使用的计划与容量计算组件；
- `src/plant_science/`：传感器校准与统计分析组件；
- `tests/`：领域规则、存储事务和 API 测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m urban_network.acceptance --workspace .
```

验收命令会创建演示管段、导入传感器读数、计算泄漏风险、生成巡检工单并输出 JSON。它不访问外部网络，也不要求常驻的数据库、队列或其他服务。

## HTTP API

```bash
PYTHONPATH=src python3 -m urban_network.api --database network.sqlite3 --host 127.0.0.1 --port 8080
```

`GET /health` 返回服务状态，其余接口使用 JSON 和 `Authorization: Bearer <token>` 会话，支持管段登记、读数上报、风险查询、工单创建和应急资源分配。

### 班组路线与离线回执

片区主管（`supervisor` / `network-supervisor`，角色 engineer）把多条巡检工单组成路线批次，锁定工单顺序和预计时窗；队员（operator）在地下空间离线作业，恢复连接后按设备序号集中回传。

- `POST /routes`：创建路线批次（draft），body 含 `route_id`、`district`、`device_id` 和 `stops`（工单顺序与 `planned_start`/`planned_end` 时窗）。
- `POST /routes/{route_id}/lock`：锁定为新版本（自动派单 open→assigned）；对已锁定路线再次调用并提供新的 `stops` 会产生下一版本，历史快照完整保留。
- `GET /routes/{route_id}`、`GET /routes/{route_id}/versions`、`GET /routes/{route_id}/versions/{version}`：查询当前路线与每个版本的快照。
- `POST /routes/{route_id}/receipts`：离线回执集中回传，body 含 `device_id` 与 `receipts`（每条含设备内序号 `seq`、事件类型 `checkin`/`finding`/`completion`、缺陷和材料消耗）。同 `(device_id, seq)` 同内容为幂等重放；缺号、同序号异文、同工单互斥结论、回执路线版本不匹配都会登记为冲突（`GET /routes/{route_id}/conflicts`），绝不静默覆盖原始回执。
- `POST /conflicts/{conflict_id}/resolutions`：主管裁决。结论冲突需选择获胜序号 `winning_seq`；缺号/版本不匹配需 `waive`；重复异文需 `keep_stored`。
- `POST /receipt-batches/{batch_id}/confirm`：冲突全部裁决后确认批次，单事务驱动工单状态（签到→in_progress、完工→completed/blocked）、登记缺陷并扣减材料库存。任何一条失败整批回滚，批次标记为 `failed` 且保留原因，补货或处理后可重试；确认接口本身幂等。
- `GET /routes/{route_id}/batches`、`GET /routes/{route_id}/receipts`：批次与回执追溯；路线锁定、版本变更、冲突登记/裁决、工单改判和材料消耗均写入 `audit_events`。

