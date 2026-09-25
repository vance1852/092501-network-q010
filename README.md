# 城市地下管网安全监测与应急调度服务

本项目为城市供水、排水和燃气管网提供离线后台服务，保存管段、传感器读数、泄漏告警、巡检工单、维修审批和应急资源分配。系统使用确定性的风险评分帮助值班人员优先处理高风险管段，账号按角色授予读取、处置和审批权限，状态变化写入 SQLite 审计表。

## 目录

- `src/urban_network/`：管网领域服务、风险计算、权限、SQLite 存储和 JSON API；
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

## 班组路线与离线回执

- `POST /routes` 创建路线首个版本，`POST /routes/{id}/revisions` 修订出新版本并把旧版本置为 `superseded`；路线锁定工单顺序与预计时窗，同一工单同一时间只能属于一条活动路线，已有回执的版本不可再修订。
- `POST /receipt-batches` 由队员设备在无网络恢复后集中回传，按设备生成的序号幂等合并：同号同内容视为重放，同号异内容登记 `duplicate_sequence` 冲突并保留双方内容，缺号登记 `gap` 冲突（后续补号自动闭合），同一工单出现相反完工结论登记 `work_order_conclusion` 冲突，均不静默覆盖。
- `GET /receipt-conflicts` 与 `POST /receipt-conflicts/{id}/resolve` 供主管查询和裁决：缺号可豁免，重复号可保留已有或新进内容，结论冲突选定最终结论。
- `POST /receipt-batches/{id}/confirm` 在冲突全部裁决后于单事务内驱动工单状态、登记缺陷并扣减资源库存，任一失败整批回滚且批次可重试；`GET /routes/{id}/versions`、`GET /receipt-batches/{id}` 与审计接口完整追溯路线版本、冲突和人工裁决。
- 角色上新增 `supervisor`（路线编排、回执确认、冲突裁决），`operator`/`engineer` 可上传回执。
