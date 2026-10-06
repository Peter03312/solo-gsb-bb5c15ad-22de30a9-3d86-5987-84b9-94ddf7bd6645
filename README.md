# 冷链运输温度事件核验服务

基于 **Python + FastAPI + SQLite** 的冷链温度核验服务。客户端按设备提交温度读数，
服务自动核验并维护**超温区间**；支持迟到补报、按事件号更正与撤销，并在每次写操作后
重算受影响区间，返回当前全量区间及本次新增 / 变更 / 失效的区间差异。

**离线网关**可把同一设备的多条读数延迟、成批上报：服务接收设备号、批次号和有序读数
列表，在同一事务内整批校验、整批写入，逐条返回**新建 / 已存在**状态，并给出提交后的
全量超温区间与**整批一次性**区间差异；同批次号同内容重试永远返回首次回执。

此外支持**读数争议工作流**：对现有读数按争议号提出“更正采样时间与温度”或“撤销”，
申请时冻结读数内容与**修订号**；待审争议可批准或驳回，批准时在同一事务内核对修订号，
读数若已被直接操作或其他争议改变，争议转为**过期**且不再改读数。

还支持**按采样时间生效的温度上限策略**：客户端按设备提交策略号、阈值与生效时间，
每条读数适用其采样时间所处策略的上限（生效时间不晚于该读数的最近一条策略），
未覆盖时间沿用全局上限；策略写入后在同一事务内按每条读数所处策略重算超温区间
（含历史读数，回溯生效时间立即生效），同策略号同内容重放幂等、异内容冲突，
同设备同一生效时间只允许一条策略。

并支持**冷链交接温控凭证**：交接时按设备号、交接单号与起止时间（时间窗
左闭右开）生成凭证，服务按当时未撤销读数逐条冻结事件号、采样时间、温度、
该时刻适用的策略号与上限（无策略时用全局上限）及是否严格超限，并汇总读数数、
超限数与最高温度（空窗为 `null`）；凭证生成与证据存档在**同一事务**完成，
同设备同单号、等价时区表示的同一时间窗重试原样返回首次凭证，异窗冲突；
凭证一经存档，后续补报、更正、撤销或策略变化都不再改动它。

## 超温区间判定规则

- 同一设备的**有效**（未撤销）读数按 **(采样时间, 事件号)** 升序排列；
- 温度 **严格大于** 上限的读数视为“超限”（等于上限不算超限）；
  上限按读数采样时间所处的**温度上限策略**确定，无策略覆盖时用全局 `TEMP_LIMIT`；
- **连续 ≥ 2 条**超限读数构成一个超温区间；
  - **起点**：首个超限读数；**终点**：区间之后首个不超限读数；
  - 若超限一直延续到最后一条读数，区间未结束，终点字段为 `null`；
- 区间以**起点读数的事件号**为唯一标识；
- 迟到、更正、撤销读数后会重算受影响区间：
  - 起点不变、内容变化 → 计入 `changed`；
  - 起点改变 → 旧区间计入 `invalidated`，新区间计入 `added`。

## 快速开始

### 方式一：Docker Compose（推荐，一键完成依赖安装与数据库初始化）

```bash
docker compose up --build
```

### 方式二：Docker

```bash
docker build -t coldchain-service .
docker run -d -p 8000:8000 -e TEMP_LIMIT=8.0 -v coldchain-data:/data coldchain-service
```

### 方式三：本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

数据库文件会在服务启动时自动初始化（建表，幂等），无需手工执行任何脚本。

## 配置

| 环境变量     | 默认值                            | 说明                                   |
| ------------ | --------------------------------- | -------------------------------------- |
| `TEMP_LIMIT` | `8.0`                             | 全局默认超温上限（°C）：未被任何温度上限策略覆盖的读数按它判定，严格大于视为超限 |
| `DB_PATH`    | 本地 `./coldchain.db`，容器 `/data/coldchain.db` | SQLite 数据库文件路径 |

Compose 中通过 `environment` 修改，例如 `TEMP_LIMIT: "2.0"`。

## 访问地址

| 地址                            | 说明                       |
| ------------------------------- | -------------------------- |
| http://localhost:8000           | 服务信息                   |
| http://localhost:8000/docs      | Swagger 交互式 API 文档    |
| http://localhost:8000/health    | 健康检查                   |
| 端口                            | **8000**（宿主机:容器）    |

## API 一览

| 方法   | 路径                                    | 说明                                       |
| ------ | --------------------------------------- | ------------------------------------------ |
| POST   | `/devices/{device_id}/readings`         | 提交读数（同号同内容幂等，同号异内容 409） |
| POST   | `/devices/{device_id}/reading-batches`  | 离线网关成批上报（整批成功 / 整批拒绝，详见下节） |
| PUT    | `/devices/{device_id}/readings/{event_no}` | 按事件号更正采样时间与温度              |
| DELETE | `/devices/{device_id}/readings/{event_no}` | 按事件号撤销读数（重复撤销幂等）        |
| GET    | `/devices/{device_id}/intervals`        | 查询当前全部超温区间                       |
| GET    | `/devices/{device_id}/readings`         | 查询全部读数（含已撤销），按规则排序       |
| POST   | `/devices/{device_id}/disputes`         | 提出争议（冻结读数与修订号）               |
| POST   | `/devices/{device_id}/disputes/{dispute_no}/decisions` | 批准 / 驳回待审争议     |
| GET    | `/devices/{device_id}/disputes`         | 查询设备全部争议                           |
| GET    | `/devices/{device_id}/disputes/{dispute_no}` | 查询单个争议（未知争议号 404）        |
| POST   | `/devices/{device_id}/limit-policies`   | 提交温度上限策略（同号同内容幂等，异内容 409） |
| GET    | `/devices/{device_id}/limit-policies`   | 查询设备全部温度上限策略（按生效时间排序）   |
| POST   | `/devices/{device_id}/handover-vouchers` | 生成 / 重放冷链交接温控凭证（同窗幂等，异窗 409） |
| GET    | `/devices/{device_id}/handover-vouchers` | 查询设备全部交接凭证（按生成时间排序）       |
| GET    | `/devices/{device_id}/handover-vouchers/{handover_no}` | 按交接单号查询凭证（未知单号 404） |
| GET    | `/health`                               | 健康检查                                   |

### 请求字段

- `event_no`：设备内唯一事件号（字母 / 数字 / `.` `_` `-`，≤ 64 字符）；
- `sampled_at`：ISO 8601 采样时间（如 `2026-10-01T08:00:00Z`，无时区按 UTC 处理）；
- `temperature`：温度（°C），须为有限数值且在 `[-273.15, 1000]` 内。

### 统一响应结构（写操作与区间查询）

```json
{
  "device_id": "truck-001",
  "limit": 8.0,
  "intervals": [ /* 当前全部有效超温区间 */ ],
  "changes": {
    "added":       [ /* 本次新增区间 */ ],
    "changed":     [ /* 起点不变但内容变化的区间（变更后版本） */ ],
    "invalidated": [ /* 本次失效区间（变更前版本） */ ]
  }
}
```

区间对象：`start_event_no` / `start_sampled_at` / `start_temperature` /
`end_event_no` / `end_sampled_at` / `end_temperature`（未结束为 `null`）/
`reading_count`（连续超限条数）/ `max_temperature`（区间峰值）。

## 调用示例

```bash
BASE=http://localhost:8000/devices/truck-001

# 1. 提交读数：首条超限，尚不构成区间
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E1","sampled_at":"2026-10-01T08:00:00Z","temperature":9.0}'

# 2. 第二条连续超限 → 形成区间（未结束，终点为 null），changes.added 出现 E1 区间
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E2","sampled_at":"2026-10-01T08:10:00Z","temperature":9.5}'

# 3. 首次不超限 → 区间闭合，终点为 E3，计入 changes.changed
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E3","sampled_at":"2026-10-01T08:20:00Z","temperature":6.0}'

# 4. 幂等重放：与 E3 内容完全一致 → 200 且 changes 全空
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E3","sampled_at":"2026-10-01T08:20:00Z","temperature":6.0}'

# 5. 同号异内容 → 409 Conflict
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E3","sampled_at":"2026-10-01T08:20:00Z","temperature":7.0}'

# 6. 更正：把 E2 改为不超限 → 原区间失效（changes.invalidated）
curl -X PUT $BASE/readings/E2 -H 'Content-Type: application/json' \
  -d '{"sampled_at":"2026-10-01T08:10:00Z","temperature":5.0}'

# 7. 撤销读数
curl -X DELETE $BASE/readings/E1

# 8. 查询当前区间与全部读数
curl $BASE/intervals
curl $BASE/readings
```

迟到补报示例：若已有 `E2(08:10, 9.0)`、`E3(08:20, 9.5)` 构成起点为 `E2` 的区间，
补交一条更早的超限读数 `E1(08:00, 8.5)` 后，响应中 `changes.invalidated` 含旧区间
`E2`、`changes.added` 含新区间 `E1`（起点改变 → 旧失效、新新增）。

## 离线网关成批上报

### 请求 `POST /devices/{device_id}/reading-batches`

```json
{
  "batch_no": "GW-20261001-001",
  "readings": [
    {"event_no": "E1", "sampled_at": "2026-10-01T08:00:00Z", "temperature": 9.0},
    {"event_no": "E2", "sampled_at": "2026-10-01T08:10:00Z", "temperature": 9.5},
    {"event_no": "E3", "sampled_at": "2026-10-01T08:20:00Z", "temperature": 6.0}
  ]
}
```

- `batch_no`：设备内唯一次批号（规则同事件号，字母 / 数字开头，≤ 64 字符）；
- `readings`：**非空**有序读数列表，逐条沿用现有事件号、采样时间、温度校验；
  批内事件号必须唯一，重复即整批拒绝（`422`）。

### 响应（首次成功 `201 Created`）

```json
{
  "device_id": "truck-001",
  "batch_no": "GW-20261001-001",
  "limit": 8.0,
  "results": [
    {"event_no": "E1", "sampled_at": "2026-10-01T08:00:00.000000Z", "temperature": 9.0, "status": "created"},
    {"event_no": "E2", "sampled_at": "2026-10-01T08:10:00.000000Z", "temperature": 9.5, "status": "created"},
    {"event_no": "E3", "sampled_at": "2026-10-01T08:20:00.000000Z", "temperature": 6.0, "status": "created"}
  ],
  "intervals": [ /* 提交后的全量超温区间（结构同前） */ ],
  "changes":   { "added": [], "changed": [], "invalidated": [] },
  "created_at": "2026-10-05T21:06:30.505316Z"
}
```

- `results[].status`：`created`（本次新建）或 `existed`（库中已有同号同内容，
  仅标记已存在，不覆盖、修订号不变）；顺序与请求列表一致；
- `intervals`：成功批次的新读数按现有 **(采样时间, 事件号)** 排序规则纳入后的
  **全量**区间；
- `changes`：**整批一次性**差异，只比较“批次提交前”与“批次提交后”，不按条拆分，
  因此不会出现单条逐个写入时的中间态（例如一批内区间只会 `added` 一次，
  不会先 `added` 再 `changed`）。

### 整批拒绝（原子事务）

出现以下任一情况时**整批拒绝**，原有读数、修订号与区间保持不变，且失败批次不留痕
（同批次号修正后可重新首次提交）：

| 情况 | 状态码 |
| ---- | ------ |
| 批内事件号重复；任一读数的时间 / 温度 / 事件号非法；读数列表为空 | `422` |
| 任一读数与库中**同号异内容**（`detail` 会列出冲突事件号） | `409` |

校验全部通过后才执行写入；批次读数与批次回执在**同一事务**提交。

### 重试与冲突

- **同批次号 + 规范化后的有序内容相同**（时间支持等价时区表示）：返回 `200` 与
  **首次回执**（含首次的逐条状态、当时的全量区间与差异、`created_at`），
  不重复写入、不重算；**即使该批读数之后被 `PUT` / `DELETE` 或争议决定修改 / 撤销，
  首次回执也原样返回**。
- **同批次号 + 异内容**（任一读数的时间 / 温度不同，或仅列表顺序不同）：`409`。
  有序列表顺序是批次内容的一部分。
- 批次号按设备隔离，不同设备可复用同一次批号。

### 批次接口示例

```bash
BASE=http://localhost:8000/devices/truck-001

# 1. 网关成批上报：3 条读数，E1、E2 连续超限，E3 闭合区间（整批一次性 added E1）
curl -X POST $BASE/reading-batches -H 'Content-Type: application/json' -d '{
  "batch_no":"GW-001",
  "readings":[
    {"event_no":"E1","sampled_at":"2026-10-01T08:00:00Z","temperature":9.0},
    {"event_no":"E2","sampled_at":"2026-10-01T08:10:00Z","temperature":9.5},
    {"event_no":"E3","sampled_at":"2026-10-01T08:20:00Z","temperature":6.0}
  ]}'

# 2. 网关重试（同批次号、等价时区的同内容）→ 200，返回与首次完全一致的回执
curl -X POST $BASE/reading-batches -H 'Content-Type: application/json' -d '{
  "batch_no":"GW-001",
  "readings":[
    {"event_no":"E1","sampled_at":"2026-10-01T16:00:00+08:00","temperature":9.0},
    {"event_no":"E2","sampled_at":"2026-10-01T16:10:00+08:00","temperature":9.5},
    {"event_no":"E3","sampled_at":"2026-10-01T16:20:00+08:00","temperature":6.0}
  ]}'

# 3. 第二批含一条已存在读数：E3 同号同内容 → existed；E4 新建 → created
curl -X POST $BASE/reading-batches -H 'Content-Type: application/json' -d '{
  "batch_no":"GW-002",
  "readings":[
    {"event_no":"E3","sampled_at":"2026-10-01T08:20:00Z","temperature":6.0},
    {"event_no":"E4","sampled_at":"2026-10-01T08:30:00Z","temperature":5.5}
  ]}'

# 4. 批内重复事件号 → 422 整批拒绝
curl -X POST $BASE/reading-batches -H 'Content-Type: application/json' -d '{
  "batch_no":"GW-003",
  "readings":[
    {"event_no":"E9","sampled_at":"2026-10-01T09:00:00Z","temperature":9.0},
    {"event_no":"E9","sampled_at":"2026-10-01T09:10:00Z","temperature":9.2}
  ]}'

# 5. 与库中同号异内容（E3 温度不同）→ 409 整批拒绝，E5 不会部分写入，GW-004 可修正后重用
curl -X POST $BASE/reading-batches -H 'Content-Type: application/json' -d '{
  "batch_no":"GW-004",
  "readings":[
    {"event_no":"E5","sampled_at":"2026-10-01T09:00:00Z","temperature":9.0},
    {"event_no":"E3","sampled_at":"2026-10-01T08:20:00Z","temperature":7.0}
  ]}'

# 6. 同批次号异内容（仅顺序不同也算）→ 409
curl -X POST $BASE/reading-batches -H 'Content-Type: application/json' -d '{
  "batch_no":"GW-001",
  "readings":[
    {"event_no":"E2","sampled_at":"2026-10-01T08:10:00Z","temperature":9.5},
    {"event_no":"E1","sampled_at":"2026-10-01T08:00:00Z","temperature":9.0},
    {"event_no":"E3","sampled_at":"2026-10-01T08:20:00Z","temperature":6.0}
  ]}'
```

## 读数争议工作流

### 数据模型：修订号

每条读数带单调递增的**修订号** `revision`：提交时为 `1`，此后每次更正 / 撤销
（无论来自直接的 `PUT` / `DELETE`，还是争议被**批准执行**）都 `+1`。
修订号是争议能否执行的唯一判据，与读数当前内容无关。

### 状态机

```
                POST /disputes（冻结 sampled_at/temperature/revision）
   (不存在) ────────────────────────────────►  pending
                                                  │
                          decision=reject         │         decision=approve
                        ┌─────────────────────────┴─────────────────────────┐
                        ▼                                                   ▼
                    rejected                        revision 仍匹配 → approved（执行更正/撤销）
            （不改读数）                            revision 已变   → expired（不改读数，保留原因）
```

- 争议**只能进入一个终态**；
- 对终态争议重放**同一决定**：返回首次结果（含首次执行结果与区间差异），不重复执行；
- 对终态争议发起**反向决定**：`409 Conflict`；
- 批准过期争议后状态为 `expired`（终态），对它重放 `approve` 返回首次过期结果，
  再发 `reject` 属于反向决定，`409`。

### 申请争议 `POST /devices/{device_id}/disputes`

请求体：

| 字段              | 说明                                                        |
| ----------------- | ----------------------------------------------------------- |
| `dispute_no`      | 设备内唯一争议号（规则同事件号，≤ 64 字符）                 |
| `target_event_no` | 被争议读数的事件号，**必须存在且未撤销**（否则 404 / 409）  |
| `action`          | `correct`（更正采样时间与温度）或 `revoke`（撤销）          |
| `reason`          | **非空**原因（1–1000 字符，不能全为空白）                   |
| `sampled_at`      | `action=correct` 时**必填**；`revoke` 时必须缺省            |
| `temperature`     | `action=correct` 时**必填**（同温度校验）；`revoke` 时缺省  |

- 首次成功：`201 Created`，响应中 `dispute.status = "pending"`，并带回冻结依据
  `frozen_sampled_at` / `frozen_temperature` / `frozen_revision`；
- **同号同内容**重试（时间支持等价时区表示）：`200` 且原样返回首次申请，
  不重复冻结；
- **同号异内容**（动作、目标读数、原因、目标时间 / 温度任一不同）：`409`；
- 同一读数可同时存在多个待审争议，先被批准的会改变修订号，使后批准者过期。

### 作出决定 `POST .../disputes/{dispute_no}/decisions`

请求体：`{"decision": "approve" | "reject", "reason": "可选说明"}`。

- **驳回**：读数与区间均不变，`status = "rejected"`，
  `execution.outcome = "rejected"`、`applied = false`、`changes` 三个列表为空；
- **批准且仍匹配冻结修订号**：在**同一事务**内执行更正 / 撤销（走与直接接口相同的
  现有规则），`status = "approved"`、`execution.outcome = "applied"`、
  `applied = true`，并返回 `execution.intervals`（决定时点全量区间）与
  `execution.changes`（**当次**区间差异：`added` / `changed` / `invalidated`）；
- **批准时已不匹配**（读数被直接更正 / 撤销，或被其他争议改变）：
  `status = "expired"`、`execution.outcome = "expired"`、`applied = false`、
  `changes` 为空，`expiry_reason` 记录修订号变化（如 `1 → 2`）；
  **即使读数当前内容恰好与申请目标相同，也判定过期，不再写入。**

### 查询争议

- `GET /devices/{device_id}/disputes` → `{"device_id", "disputes":[...]}`，
  按创建时间、争议号排序；
- `GET /devices/{device_id}/disputes/{dispute_no}` → 单个争议；未知争议号 `404`。

争议对象包含三部分信息：

1. **冻结依据**：`target_event_no` / `action` / `reason` /
   `target_sampled_at` / `target_temperature`（申请内容）+
   `frozen_sampled_at` / `frozen_temperature` / `frozen_revision`（申请时快照）；
2. **终态**：`status`（`pending` / `approved` / `rejected` / `expired`）、
   `decided_at`、`decided_reason`、`expiry_reason`；
3. **执行结果**：`execution`（待审时为 `null`），结构见上。

### 争议接口示例

```bash
BASE=http://localhost:8000/devices/truck-001

# 1. 提交两条超限读数，形成起点 E1 的区间
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E1","sampled_at":"2026-10-01T08:00:00Z","temperature":9.0}'
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E2","sampled_at":"2026-10-01T08:10:00Z","temperature":9.5}'

# 2. 提出更正争议：申请把 E2 改为 5.0（冻结到 revision=1）
curl -X POST $BASE/disputes -H 'Content-Type: application/json' -d '{
  "dispute_no":"D1",
  "target_event_no":"E2",
  "action":"correct",
  "reason":"sensor calibration drift; recalibrated probe",
  "sampled_at":"2026-10-01T08:10:00Z",
  "temperature":5.0
}'

# 3. 同号同内容重放 → 200，返回原申请（等价时区也算同内容）
curl -X POST $BASE/disputes -H 'Content-Type: application/json' -d '{
  "dispute_no":"D1","target_event_no":"E2","action":"correct",
  "reason":"sensor calibration drift; recalibrated probe",
  "sampled_at":"2026-10-01T16:10:00+08:00","temperature":5.0}'

# 4a. 批准（读数未变）：执行更正，E1 区间失效，execution.changes.invalidated 含 E1
curl -X POST $BASE/disputes/D1/decisions -H 'Content-Type: application/json' \
  -d '{"decision":"approve","reason":"evidence confirmed"}'

# 4b. 或者驳回：读数与区间均不变
curl -X POST $BASE/disputes/D1/decisions -H 'Content-Type: application/json' \
  -d '{"decision":"reject","reason":"calibration log valid"}'

# 5. 过期场景：争议悬而未决时读数被直接操作（修订号变化），再批准 → expired
curl -X PUT $BASE/readings/E2 -H 'Content-Type: application/json' \
  -d '{"sampled_at":"2026-10-01T08:10:00Z","temperature":5.0}'
curl -X POST $BASE/disputes/D1/decisions -H 'Content-Type: application/json' \
  -d '{"decision":"approve"}'
# → status="expired", applied=false, expiry_reason 说明 revision 1 → 2

# 6. 撤销型争议（不得带 sampled_at / temperature）
curl -X POST $BASE/disputes -H 'Content-Type: application/json' -d '{
  "dispute_no":"D2","target_event_no":"E1","action":"revoke",
  "reason":"duplicate packet from gateway retry"}'
curl -X POST $BASE/disputes/D2/decisions -H 'Content-Type: application/json' \
  -d '{"decision":"approve"}'

# 7. 查询
curl $BASE/disputes                 # 全部争议
curl $BASE/disputes/D1              # 单个争议（含冻结依据、终态、执行结果）
```

### 兼容性

- 原有读数与区间接口的**请求、响应与幂等语义完全不变**（仅内部新增 `revision` 列，
  不在原读数 / 区间响应中暴露）；
- 新增批次表 `reading_batches` 与原表、争议表相互独立，批次提交只复用既有读数写入
  与区间规则，不改变单条读数 / 争议接口的行为；
- 新增策略表 `limit_policies` 与既有各表相互独立；未配置任何策略时，所有接口的
  区间判定与之前完全一致（统一使用全局 `TEMP_LIMIT`）；
- 新增凭证表 `handover_vouchers` 只在凭证接口写入，且仅插入新行、从不更新 / 删除；
  凭证生成不改动任何读数 / 区间 / 策略 / 批次 / 争议数据，既有接口的请求、响应与
  幂等语义完全不变；已存凭证为生成时刻快照，后续任何数据变化都不会改写它；
- 升级旧版本数据库时自动幂等迁移：旧读数补列 `revision = 1`，新表自动创建，
  无需手工脚本，现有 `uvicorn app.main:app` 启动方式保持可用。

## 按采样时间生效的温度上限策略

### 规则

- 策略按设备隔离，客户端提交 **策略号 + 阈值 + 生效时间**；
- 每条读数适用**生效时间不晚于其采样时间的最近一条策略**的上限
  （`effective_from <= sampled_at`，生效时间等于采样时间即生效）；
- 策略生效时间**之前**未被任何策略覆盖的读数，沿用全局 `TEMP_LIMIT`；
- 同一设备**同一生效时间只允许一条策略**（UTC 归一化后判定，等价时区算同一时间）；
- 策略写入后在**同一事务**内按每条读数所处策略重算该设备全部超温区间，
  响应返回当前全量区间与相对变更（`added` / `changed` / `invalidated`）；
  生效时间可回溯到历史读数之前 / 之间，历史读数立即按新策略重判；
- 策略生效后，读数提交 / 更正 / 撤销、批次上报、争议批准执行的区间判定
  一律使用各读数所处策略的上限。

### 提交策略 `POST /devices/{device_id}/limit-policies`

```json
{
  "policy_no": "P1",
  "effective_from": "2026-10-01T00:00:00Z",
  "temp_limit": 5.0
}
```

- `policy_no`：设备内唯一策略号（规则同事件号，≤ 64 字符）；
- `effective_from`：ISO 8601 生效时间（采样时间口径，无时区按 UTC 处理）；
- `temp_limit`：温度上限（°C），校验规则同读数温度（有限数值，`[-273.15, 1000]`）。

### 响应（首次成功 `201 Created`）

```json
{
  "device_id": "truck-001",
  "limit": 8.0,
  "policy": {
    "policy_no": "P1",
    "effective_from": "2026-10-01T00:00:00.000000Z",
    "temp_limit": 5.0,
    "created_at": "2026-10-06T02:34:30.061523Z"
  },
  "intervals": [ /* 策略写入后的全量超温区间（结构同前） */ ],
  "changes":   { "added": [], "changed": [], "invalidated": [] }
}
```

- `limit` 为全局默认上限（未被策略覆盖的时间沿用该值），`policy` 为本次写入的策略；
- **同策略号 + 同内容**重放（时间支持等价时区表示）：`200` 且原样返回**首次回执**
  （含首次的区间与差异），不重复写入、不重算——即使读数或其他策略随后发生变化；
- **同策略号 + 异内容**（生效时间或阈值任一不同）：`409`；
- **同设备同一生效时间**已被其他策略占用：`409`（`detail` 指明占用该时间的策略号）；
- **未知设备**（从未上报过读数）：`404`；**非法阈值 / 策略号 / 生效时间**：`422`；
- 冲突 / 校验失败的提交不留痕，修正后同一策略号可重新首次提交。

### 查询策略 `GET /devices/{device_id}/limit-policies`

返回 `{"device_id", "limit", "policies": [...]}`，策略按生效时间、策略号排序。

### 策略接口示例

```bash
BASE=http://localhost:8000/devices/truck-001

# 1. 未知设备提交策略 → 404
curl -X POST $BASE/limit-policies -H 'Content-Type: application/json' \
  -d '{"policy_no":"P1","effective_from":"2026-10-01T00:00:00Z","temp_limit":5.0}'

# 2. 先上报两条历史读数（全局上限 8.0 下 7.0 / 7.5 均不超限，无区间）
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E1","sampled_at":"2026-10-01T08:00:00Z","temperature":7.0}'
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E2","sampled_at":"2026-10-01T08:10:00Z","temperature":7.5}'

# 3. 回溯生效的策略：上限 5.0 自 00:00 起 → 历史读数重判，
#    E1、E2 连续超限，201 响应中 changes.added 出现起点 E1 的区间
curl -X POST $BASE/limit-policies -H 'Content-Type: application/json' \
  -d '{"policy_no":"P1","effective_from":"2026-10-01T00:00:00Z","temp_limit":5.0}'

# 4. 同号同内容重放（等价时区）→ 200，返回与首次完全一致的回执
curl -X POST $BASE/limit-policies -H 'Content-Type: application/json' \
  -d '{"policy_no":"P1","effective_from":"2026-10-01T08:00:00+08:00","temp_limit":5.0}'

# 5. 同号异内容 → 409；同生效时间换策略号 → 409
curl -X POST $BASE/limit-policies -H 'Content-Type: application/json' \
  -d '{"policy_no":"P1","effective_from":"2026-10-01T00:00:00Z","temp_limit":6.0}'
curl -X POST $BASE/limit-policies -H 'Content-Type: application/json' \
  -d '{"policy_no":"P2","effective_from":"2026-10-01T00:00:00Z","temp_limit":6.0}'

# 6. 第二段策略：09:00 起上限抬到 10.0
curl -X POST $BASE/limit-policies -H 'Content-Type: application/json' \
  -d '{"policy_no":"P2","effective_from":"2026-10-01T09:00:00Z","temp_limit":10.0}'

# 7. 新读数按所处策略判定：09:10 适用 P2（上限 10.0），9.0 不超限 → E1 区间闭合
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E3","sampled_at":"2026-10-01T09:10:00Z","temperature":9.0}'

# 8. 查询设备全部策略（按生效时间排序）
curl $BASE/limit-policies
```

## 冷链交接温控凭证

### 规则

- 交接时提交 **设备号 + 交接单号 + 起止时间**，采样时间窗为 **左闭右开**
  `[window_start, window_end)`（含起点、不含终点）；
- 凭证冻结窗内**当时未撤销**读数，按 **(采样时间, 事件号)** 升序逐条记录：
  - `event_no` / `sampled_at` / `temperature`：生成凭证时读数的事件号、时间、温度；
  - `policy_no`：该时刻适用的策略号；**无策略覆盖时为 `null`**；
  - `temp_limit`：该时刻适用的上限（策略上限；无策略时为全局 `TEMP_LIMIT`）；
  - `exceeds_limit`：温度是否**严格大于**该时刻适用上限（等于不算超限）；
- 同时汇总 `reading_count`（读数数）、`exceedance_count`（超限数）、
  `max_temperature`（窗内最高温度，**空窗为 `null`**）；
- **凭证生成与证据存档在同一事务完成**：要么同时可见，要么都不留痕；
- **同设备同单号 + 等价时区表示的同一时间窗**重试：`200` 并**原样返回首次凭证**；
  **同单号 + 异窗**（起点或终点时刻不同）：`409`；
- 凭证只反映**生成时刻**的数据快照：之后的读数补报、更正、撤销、争议批准
  或策略新增 / 变化**都不会改动已存凭证**；需要按当前数据出证请使用**新交接单号**；
- 起止时间非法或结束不晚于开始（`window_end <= window_start`）→ `422`；
  **空窗（窗内无有效读数）允许生成**，此时读数列表为空、最高温度为 `null`；
  设备是否曾上报读数不影响出证。
- 交接单号按设备隔离，不同设备可复用同一交接单号。

### 生成凭证 `POST /devices/{device_id}/handover-vouchers`

请求体：

```json
{
  "handover_no": "HO-20261001-001",
  "window_start": "2026-10-01T08:00:00Z",
  "window_end":   "2026-10-01T09:00:00Z"
}
```

- `handover_no`：设备内唯一交接单号（规则同事件号，字母 / 数字开头，≤ 64 字符）；
- `window_start` / `window_end`：ISO 8601 时间（无时区按 UTC 处理），
  终点必须**严格晚于**起点。

响应（首次成功 `201 Created`，同窗重放 `200 OK`）：

```json
{
  "voucher": {
    "device_id": "truck-001",
    "handover_no": "HO-20261001-001",
    "window_start": "2026-10-01T08:00:00.000000Z",
    "window_end":   "2026-10-01T09:00:00.000000Z",
    "limit": 8.0,
    "readings": [
      {"event_no": "E1", "sampled_at": "2026-10-01T08:00:00.000000Z",
       "temperature": 9.0, "policy_no": null, "temp_limit": 8.0,
       "exceeds_limit": true},
      {"event_no": "E2", "sampled_at": "2026-10-01T08:10:00.000000Z",
       "temperature": 7.0, "policy_no": "P1", "temp_limit": 5.0,
       "exceeds_limit": true}
    ],
    "reading_count": 2,
    "exceedance_count": 2,
    "max_temperature": 9.0,
    "created_at": "2026-10-06T03:10:00.123456Z"
  }
}
```

空窗凭证：`readings` 为 `[]`、`reading_count = 0`、`exceedance_count = 0`、
`max_temperature = null`。

### 查询凭证

- `GET /devices/{device_id}/handover-vouchers/{handover_no}`
  → `{"voucher": {...}}`，未知交接单号 `404`；
- `GET /devices/{device_id}/handover-vouchers`
  → `{"device_id", "vouchers":[...]}`，按生成时间、交接单号排序。

### 凭证接口示例

```bash
BASE=http://localhost:8000/devices/truck-001

# 0. 准备数据（也可已有读数 / 策略）：E1、E2 处于交接时间窗内
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E1","sampled_at":"2026-10-01T08:00:00Z","temperature":9.0}'
curl -X POST $BASE/readings -H 'Content-Type: application/json' \
  -d '{"event_no":"E2","sampled_at":"2026-10-01T08:10:00Z","temperature":7.0}'
# 08:05 起策略上限 5.0：E1 用全局上限 8.0，E2 用策略上限 5.0
curl -X POST $BASE/limit-policies -H 'Content-Type: application/json' \
  -d '{"policy_no":"P1","effective_from":"2026-10-01T08:05:00Z","temp_limit":5.0}'

# 1. 交接出证：时间窗 [08:00, 09:00)，左闭右开；首次 201
curl -X POST $BASE/handover-vouchers -H 'Content-Type: application/json' -d '{
  "handover_no":"HO-001",
  "window_start":"2026-10-01T08:00:00Z",
  "window_end":"2026-10-01T09:00:00Z"}'

# 2. 等价时区表示的同一时间窗重试 → 200，返回与首次完全一致的凭证
curl -X POST $BASE/handover-vouchers -H 'Content-Type: application/json' -d '{
  "handover_no":"HO-001",
  "window_start":"2026-10-01T16:00:00+08:00",
  "window_end":"2026-10-01T17:00:00+08:00"}'

# 3. 同单号异窗（终点不同）→ 409
curl -X POST $BASE/handover-vouchers -H 'Content-Type: application/json' -d '{
  "handover_no":"HO-001",
  "window_start":"2026-10-01T08:00:00Z",
  "window_end":"2026-10-01T08:30:00Z"}'

# 4. 出证后数据变化（补报 / 更正 / 撤销 / 新策略）不影响已存凭证：
#    按单号查询仍返回首次凭证；需要当前证据请用新单号
curl -X PUT    $BASE/readings/E2 -H 'Content-Type: application/json' \
  -d '{"sampled_at":"2026-10-01T08:10:00Z","temperature":1.0}'
curl -X DELETE $BASE/readings/E1
curl $BASE/handover-vouchers/HO-001     # 仍为首次冻结内容
curl -X POST $BASE/handover-vouchers -H 'Content-Type: application/json' -d '{
  "handover_no":"HO-002",
  "window_start":"2026-10-01T08:00:00Z",
  "window_end":"2026-10-01T09:00:00Z"}'

# 5. 空窗也可出证（readings=[]、max_temperature=null）
curl -X POST $BASE/handover-vouchers -H 'Content-Type: application/json' -d '{
  "handover_no":"HO-EMPTY",
  "window_start":"2026-10-02T00:00:00Z",
  "window_end":"2026-10-02T01:00:00Z"}'

# 6. 非法时间窗 → 422（结束不晚于开始同样拒绝）
curl -X POST $BASE/handover-vouchers -H 'Content-Type: application/json' -d '{
  "handover_no":"HO-BAD",
  "window_start":"2026-10-01T09:00:00Z",
  "window_end":"2026-10-01T09:00:00Z"}'

# 7. 查询
curl $BASE/handover-vouchers             # 全部凭证（按生成时间排序）
curl $BASE/handover-vouchers/HO-001      # 按交接单号查询（未知单号 404）
```

## 错误码

| 状态码 | 场景                                                                 |
| ------ | -------------------------------------------------------------------- |
| 200    | 成功（含幂等重放、重复撤销、争议同内容重放 / 终态同决定重试、批次同内容重试、策略同号同内容重放、交接凭证同窗重试 / 查询） |
| 201    | 争议首次申请成功；批次首次提交成功；策略首次写入成功；交接凭证首次生成成功 |
| 404    | 更正 / 撤销的事件号不存在；争议目标读数不存在；查询 / 决定未知争议号；向未知设备（无任何读数）提交策略；查询未知交接单号 |
| 409    | 同事件号提交不同内容；更正已撤销读数；争议目标已撤销；同争议号异内容；对终态争议发反向决定；同批次号异内容；批次中任一读数与库中同号异内容（整批拒绝）；同策略号异内容；同设备同一生效时间的策略重复；同交接单号请求不同时间窗 |
| 422    | 无效时间格式、无效温度（非数值 / 无穷 / 超范围）、非法事件号；争议原因缺失 / 空白；`correct` 缺目标值或 `revoke` 带目标值；批内事件号重复、批次读数列表为空、非法批次号；非法策略号 / 生效时间 / 策略阈值；非法交接单号 / 时间格式、时间窗结束不晚于开始 |

错误响应体均含 `detail` 字段说明原因。

## 运行测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

## 项目结构

```
app/
  main.py       # FastAPI 路由、事务内重算区间、批次提交、争议工作流、温度上限策略、交接温控凭证、错误处理
  intervals.py  # 区间计算（单上限 / 按策略逐读数上限）、适用策略查找与差异比对（纯函数）
  models.py     # 请求/响应模型与输入校验（含批次、争议、策略、交接凭证模型）
  db.py         # SQLite 连接、建表（读数 / 争议 / 批次 / 策略 / 交接凭证）与旧库 revision 增量迁移
  config.py     # 环境变量配置
  util.py       # 时间规范化（UTC、固定宽度，保证可排序）
tests/
  test_api.py              # 原读数 / 区间接口测试（语义不变）
  test_batches.py          # 离线网关成批上报测试（整批事务、逐条状态、一次性差异、重试回执）
  test_disputes.py         # 争议工作流测试（冻结、批准 / 驳回、过期、幂等、迁移）
  test_policies.py         # 温度上限策略测试（回溯重算、多策略时序、幂等、冲突、错误、隔离）
  test_handover_vouchers.py # 交接温控凭证测试（窗口边界、逐条冻结、汇总、幂等 / 冲突、出证后不变性、校验、查询、隔离）
Dockerfile / docker-compose.yml / requirements*.txt
```
