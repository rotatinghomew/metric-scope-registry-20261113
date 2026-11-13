# metric_scope_registry

登记请求、应用调用和用户日活三个粒度的事实，并在同一份**版本化指标定义**上执行明细查询与汇总查询。指标定义一次写入、可持久保存版本；明细和汇总走同一套编译计划与可合并中间状态，分母不能在查询时被悄悄换掉。

## 命令

```bash
python3 -m pip install -r requirements.txt
python3 -m compileall -q metric_scope_registry
python3 -m unittest discover -s tests -v

# 事实核对（原有能力，保持不变）
python3 -m metric_scope_registry inspect --input fixtures/catalog.json --output /tmp/metric-facts.json

# 指标引擎：编译、规划、缓存、回放
python3 -m metric_scope_registry.engine --input <JSON> --state <目录> --output <JSON>

# 端到端演练：按天 -> 跨天 -> 修订 -> 旧版本回放
bash scripts/demo.sh
```

输入输出均为 UTF-8 JSON，原子写盘；格式冲突、口径错误或缺失关键字段以非零状态退出，并在输出的 `rejected` 中给出稳定错误码。未定义值用 JSON `null`，不用零替代。

## 口径三要素与表达式

每个指标节点编译后都带有可追溯的三要素：

* **实体粒度（grain）**：`request` / `call` / `user_day`（去重后为 `user`）；
* **时间锚点（time anchor）**：所在事实表的 `day` 字段，窗口过滤只作用于锚点；
* **有效人群（population）**：全部过滤谓词及其作用表，比例指标的分母人群必须包含分子人群。

表达式是声明式 JSON，支持的 `op`：

| op | 含义 | 中间状态族 | 跨分区合并方式 |
| --- | --- | --- | --- |
| `count` / `sum` | 基础计数、求和 | `scalar` | 可加 |
| `distinct_count` | 按实体字段去重 | `hash_set` | 集合并集，跨日用户不会重复相加 |
| `quantile` | 分位数（最近秩） | `sample` | 保留原始样本后重算，不做分位数相加 |
| `ratio` | 比例，只存分子/分母两个状态 | `ratio` | 分量分别合并，禁止分子分母跨粒度 |
| `ref` | 引用其他指标，可带 `filters` 与 `bridge` | 继承被引用指标 | 同口径 |

状态族不兼容时合并被拒绝（`state_incompatible`）；缺分区或缺中间状态时拒绝汇总（`partition_missing`），而不是输出零。

## 连接与桥接

* `calls -> requests` 是有外键背书的**多对一**连接，可沿连接取 `firmware` 等维度；
* 反方向 `requests -> calls` 是一对多扇出。一个请求可关联多个应用，未显式声明 `bridge` 的跨粒度引用在编译期拒绝（`undeclared_join`）；
* `bridge.rule` 只有两种：
  * `multicast`：计数落到父实体键集合，同一请求关联多少条调用都只计一次；
  * `pick`：按 `order_by` + 主键确定性地选择一条，禁止“随便选一条”。
* 请求成功按 `requests.success`（最终请求结果）计算，应用成功按 `calls.success`（调用结果）计算。二者都是合法指标，但比例的分子分母必须同粒度同锚点；没有显式桥接时混算会被拒绝（`ratio_grain_mismatch`）。

## 版本、缓存与回放

* 指标定义按 `name + version` 不可变登记；同名同版本出现不同表达式拒绝（`duplicate_definition`），同一查询闭包混用版本拒绝（`version_mixing`）。
* 事实按分区内容寻址：requests/user_days 按天，calls 按 `(day, app)`。修订一条调用只改变该分区摘要。
* 缓存键绑定指标版本、表达式摘要、查询形状（分组、窗口、分位参数）和分区摘要。修订单个应用调用时，依赖该分区的结果重算，请求/用户等无关结果继续命中旧缓存。
* 分区快照与缓存条目不可变，`replay` 通过历史运行记录里的分区摘要重放旧版本结果，即使目录已经装载了修订后的数据。

## 输出结构

```jsonc
{
  "run_id": "...",
  "replay_of": null,
  "values": [],       // 每个指标的分组结果、状态族与可合并中间状态
  "plans": [],        // 编译期计划：粒度、锚点、维度连接路径、桥接
  "lineage": {},      // 每个指标的口径追溯：粒度/锚点/人群/依赖/表达式摘要
  "cache_effects": [],// hit / stored / recomputed / bypassed 及分区版本
  "rejected": []      // 稳定错误码与原因；不可比较维度在这里说明，不填零
}
```

一个查询可以同时请求不同粒度的指标并按 `firmware`、`app` 分组。无法在某维度上定义的指标标记为 `incomparable` 并在 `rejected` 中说明原因，而不是输出零。

## 目录

`schema.py` 登记表、字段与连接；`compiler.py` 做口径编译与执行前校验；`states.py` 定义可合并中间状态；`executor.py` 按计划装载分区并累积状态；`store.py` 负责内容寻址快照、定义版本库、缓存与运行记录；`engine.py` 编排全流程；`fixtures/` 含版本化定义、分阶段查询与各类非法定义样例；`tests/` 覆盖状态合并、编译拒绝与端到端缓存/回放。
