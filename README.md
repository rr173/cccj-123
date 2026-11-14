# archindex — 只读归档索引器与流式表格接入

一个**只读、不落盘解压**的 TAR / ZIP 成员目录索引器，并提供一次性 CSV → SQLite
表格接入。索引过程中成员内容仅以有界分块（默认 1 MiB）流式通过哈希器，绝不写入
磁盘；表格接入同样直接读取归档成员数据流，不生成临时 CSV，也不一次性装载整个
成员或全部业务行。

## 功能

- 支持 ZIP（含 JAR/APK 类）与 TAR（plain / gzip / bzip2 / xz）。
- 每个成员记录：
  - 规范逻辑路径（POSIX、根相对、`normpath` 归一）
  - 目录层级深度
  - 压缩前长度 `size` 与压缩后长度 `compressed_size`（流格式不可得时为 `None`）
  - 包内偏移 `offset` / `data_offset`（可取得时）
  - CRC-32 与 SHA-256 双摘要
- 同一逻辑路径重复出现：保留出现次序（`occurrence`），全部标记 `ambiguous`，
  并进入 `IndexResult.ambiguous_names`。
- 查询接口：前缀查询 `find_by_prefix`、索引差异 `diff_indices`、
  单成员安全导出 `export_member`（导出前重新流式复核 size / CRC-32 / SHA-256）。

## 流式 CSV → SQLite 接入

`ingest_sources` 根据字段契约和接入清单，把多个归档中的 CSV 成员清洗到同一个
SQLite 数据库。业务表、隔离表和来源摘要在同一数据库内创建；目标路径已存在时
直接拒绝，数据库先写到同目录临时文件，只有全部来源耗尽、摘要复核和关闭均成功
后才原子改名发布。

### 数据契约与接入清单

字段契约定义目标表、列类型、空值、默认值、取值限制和业务主键：

- 类型：`text`、`integer`、`decimal`、`timestamp`。
- 空字符串是唯一缺失值标记；默认值只用于缺失值，不能掩盖非法值。
- `integer` 限制为可选符号 + 十进制数字，并检查有符号 64 位范围。
- `decimal` 必须声明 `precision` / `scale`，全程使用 `Decimal`，拒绝浮点输入、
  科学计数法和超精度值；SQLite 中以接受时的十进制文本无损保存。
- `timestamp` 必须带时区，转换为 UTC，固定六位微秒；无时区值直接隔离。
- 数值列可配置闭区间 `min` / `max`，text 列可配置 `allowed` 集合。
- 主键由一列或多列组成，主键列必须 `nullable=false`；重复转换后键的后来有效行
  进入隔离表，不使用覆盖写入。

接入清单按顺序声明 `archive`、`member`、`occurrence`、`encoding`、`delimiter`
和源列到目标列映射：

- 成员名使用索引中的规范名称；同名成员有多个 occurrence 时必须显式选择。
- 编码仅允许 `utf-8`（自动去除文件头 BOM）或 `gb18030`。
- 分隔符仅允许逗号 `,` 或制表符 `\t`。
- 目录、符号链接和硬链接不能作为数据源。
- 表头重名、映射到不存在的源列、两个源列映射到同一目标列、缺少必需目标列以及
  未明确允许的多余源列，均在接收业务行前终止。
- 不做隐式小写化、模糊匹配或按位置猜测列。

CSV 解析支持引号中的分隔符、双引号转义和跨物理行逻辑记录。每个业务行保存
`source_id`、规范成员名、occurrence、从 1 开始的逻辑记录序号以及起止物理行号。
字段数量不符、类型/范围/允许值错误和重复业务键进入 `quarantine`，保留原始
`raw_cells` 与具体 `reason`；引号错误、解码错误、损坏/截断归档和来源结构错误
会终止整次接入，且不发布目标数据库。

### Python 接口

```python
from archindex import ingest_sources

outcome = ingest_sources(
    "contract.json",
    "manifest.json",
    "/path/to/new.sqlite",
    diagnostic_path="/path/to/optional-diagnostic.json",
)
print(outcome.accepted, outcome.quarantined)
```

也可以直接传入已解析的契约 dict 和 manifest list，便于在应用内构造。数据库中
至少包含：

| 表 | 内容 |
| --- | --- |
| 契约声明的业务表 | 清洗后的业务值和逐行来源定位，业务主键有唯一约束 |
| `quarantine` | 原始单元格 JSON、物理行号、来源和隔离原因 |
| `source_summary` | 每来源逻辑记录数、接收数、隔离数、编码/分隔符和状态 |
| `ingestion_meta` | schema 版本与 UTC 创建时间 |

### 终端入口

```bash
# 隔离模式下可直接运行入口脚本；普通环境也可用 python -m archindex
python3 -I archindex/__main__.py ingest \
  --contract examples/contract.json \
  --manifest manifest.json \
  --output new.sqlite \
  --diagnostic failure.json

python3 -I archindex/__main__.py index package.zip
```

## 关系表组装嵌套交付文件

`assemble_nested` 把一个**已有 SQLite 数据库**按一份**组装方案**导出为
UTF-8 JSONL：每个根记录一行一个完整 JSON 对象，子记录按归属挂进父对象的
指定数组。下游拿到的是原始标量加正确归属的子数组，而不是另一份平面关系库；
同一根的两个箱和三条独立备注各归各的数组，不会像 JOIN 那样相乘成六个根。

### 组装方案

方案是一棵有限深度的节点树（见 `examples/assembly_plan.example.json`）：

```json
{
  "nodes": [
    {"name": "order", "table": "handover_order",
     "identity": ["order_id"],
     "scalars": [{"column": "order_no", "alias": "orderNo"}],
     "order_by": ["order_no"]},
    {"name": "box", "table": "box",
     "identity": ["box_id"],
     "parent": "order", "array": "boxes", "foreign_key": ["order_id"],
     "scalars": [{"column": "box_no", "alias": "boxNo"}],
     "order_by": ["sort_key"]}
  ]
}
```

- `name` 唯一标识节点；同一张表可以在不同节点扮演不同角色。
- `identity`：一至多个整数/文本列，在该节点**全表范围**内非空且唯一。
  分量按值和存储类型判等（TEXT `'01'` 与 INTEGER `1` 不同），不裁空格、
  不拼接分隔符充当复合键。
- `scalars`：输出标量列及别名；只输出声明过的列，身份列默认不输出，
  需要时在标量映射里明确选择（例如接入库里的 `source_id`/`member` 等
  来源定位列可以按方案保留）。
- `order_by`：可选、可为空列表。给出时按这些列字典序升序（NULL 在非空
  之前，文本按 Unicode 码点），并列时按本节点身份打破；为零个排序列时
  完全由身份决定次序。排序分量只允许整数、文本或 NULL，且同一配置列的
  非空值必须是同一种类型。输出不依赖插入顺序或 rowid。
- 非根节点另有 `parent`（父节点名）、`array`（挂在父对象里的数组名）、
  `foreign_key`（指向父身份的列，数目与父身份一致、逐分量存储类型相容）。
  恰好一个节点没有 `parent`（根）。
- 配置只携带标识符：表列名可以包含空格或双引号，一律作为真实标识符
  安全引用（双引号加倍转义），不存在注入 SQL 表达式的途径。未知父节点、
  多根、循环、不存在的表列、标量别名与子数组名冲突都在读取数据前拒绝。

### 归属与值规则

- 每个非根行必须有完整非空的父引用，并精确对应一个父行。任一参与节点
  出现重复身份、（部分）空父引用或孤儿，整个请求在写出第一份业务对象前
  拒绝，报错指出方案节点与问题行身份；不静默丢行、不把孤儿提到根层、
  不随便选择重复父行。空根表且所有参与子表也为空时输出空文件；根为空
  但还有子行时仍会查出孤儿。
- 每行子资料只出现在自己直接父对象的指定数组里；一个父节点的多个子
  数组分别组织，空分支明确为 `[]`，父对象不因某分支为空而被删除；
  身份不同但载荷完全一样的行各保留一次。
- 标量值支持 SQLite 的 NULL、INTEGER、TEXT 和有限 REAL，按对应 JSON
  类型原样表达：64 位整数不经过浮点转换，TEXT 保存的精确金额仍输出
  字符串。BLOB、NaN/无穷或其他不支持值会被拒绝，并定位到节点、行身份
  和列。中文、引号、反斜线与真实换行正确转义，一个巨大根对象在文件中
  仍只有一个物理行。

### 一致读取、资源构成与失败清理

- 源库以只读方式打开，全部读取发生在同一次读事务里：WAL 模式下导出
  进行到第一根后另一连接修改后续根和子行，当前文件仍对应原读取视图，
  下一次导出才看到新值。源库不会被创建索引或表，已有归档目录、清洗
  接入、来源摘要与隔离资料不会被改写。
- 访问结构建立在**调用者指定目录**下的独立临时 SQLite 里：每个节点
  一张按 `(父序号, 排序列, 身份)` 排序的访问表（SQLite 磁盘排序，暂存
  也落在该目录），每个父节点的孩子连续存放，不需要为每个父节点重扫
  子表。写出时每层一个前向游标递归下钻，不对任何根、父的孩子或全部
  输出做整体组装。内存构成：只读源连接缓存、临时库缓存
  （`cache_size_kb`，默认 4096）、每层游标的批次（`batch_size`，默认
  1000 行）、排序暂存（磁盘）与写出缓冲（`write_buffer_bytes`，默认
  256 KiB）；单行投影载荷上限 `max_row_payload_bytes`（默认 8 MiB）。
- 目标已存在则拒绝覆盖；目标不得与源库或其同一文件别名重合。输出先
  写到同目录临时文件，成功后才原子改名，失败时不留下最终 JSONL；临时
  目录只清理本次运行创建的内容。不要求持久作业状态或断点续办。

### Python 与终端入口

```python
from archindex import assemble_nested

result = assemble_nested(
    "plan.json", "existing.sqlite", "out.jsonl",
    temp_dir="/fast/scratch",        # 可选，默认输出目录
    cache_size_kb=4096, batch_size=1000,
    max_row_payload_bytes=8 * 1024 * 1024)
print(result.roots, result.node_rows)
```

```bash
python3 -I archindex/__main__.py assemble \
  --database existing.sqlite \
  --plan examples/assembly_plan.example.json \
  --output out.jsonl \
  --temp-dir /fast/scratch
```

### 可复跑演练与压力验证

```bash
python3 -I examples/demo_assembly.py                 # 小型演练 + 小样本压力
python3 -I examples/demo_assembly.py \
  --stress-rows 300000 --stress-mib 280              # 完整压力规模
```

演练覆盖：交接单/箱/明细/签收备注四节点组装并与独立列出的期望逐对象
核对；一个四层方案；孤儿、重复身份、空父引用、错误存储类型、配置名
冲突与不可写位置各不留最终文件；WAL 并发快照与连接释放。压力演练在
子进程中运行终端命令：单个根下挂三十万条直接孩子、总文本载荷超过
256 MiB，校验根数、各层行数、首尾孩子并用增量校验器证明文件可流式
解析，记录耗时、峰值 RSS（要求 ≤ 96 MiB）、临时磁盘量与访问方法；
压力规模由参数控制，普通回归使用小样本。


### 可复现演示与内存验证

```bash
python3 -I examples/demo_ingestion.py
```

演示会现场合成 ZIP 与 gzip TAR，覆盖混合归档格式、不同列名/列顺序、重复路径的
第二次 occurrence、GB18030 中文、跨时区时间、精确 decimal、多行引号记录、主键
冲突、隔离原因和物理行号；随后生成 220,000 行、约 201 MiB 解压大小且含长字段的
输入，并用 `tracemalloc` 打印实际峰值，证明清洗阶段没有一次性读入全部业务行。

小型契约、清单和核对查询见：

- `examples/contract.json`
- `examples/manifest.example.json`
- `examples/verify_queries.sql`
- `examples/demo_ingestion.py`

## 安全模型

索引阶段拒绝：

- 绝对路径（`/etc/passwd`、`C:\...`）
- 路径穿越（`../`、`..\`、归一化后逃逸虚拟根的路径）
- 指向包外的符号/硬链接（绝对目标或逃逸根的相对目标）
- 设备/FIFO 等特殊成员（可通过 `Limits(reject_special_files=False)` 放宽）

受限资源（命中即抛 `LimitExceededError`）：

| 限制 | 默认值 | 含义 |
| --- | --- | --- |
| `max_members` | 100000 | 成员总数 |
| `max_depth` | 100 | 目录嵌套深度 |
| `max_member_size` | 4 GiB | 单成员解压上限（流式强制，不依赖头部声明） |
| `max_total_size` | 16 GiB | 解压总大小 |
| `max_compression_ratio` | 1000 | 压缩比炸弹阈值 |
| `max_member_compressed_size` | 1 GiB | 单成员存储大小 |

损坏目录 / CRC 不符 / 截断流统一抛 `CorruptArchiveError`。

## 用法

```python
from archindex import index_archive, find_by_prefix, diff_indices, export_member, Limits

idx = index_archive("pkg.tar.gz", Limits(max_compression_ratio=500))

for m in find_by_prefix(idx, "src/lib"):
    print(m.name, m.depth, m.size, m.compressed_size, m.sha256)

# 重复路径：必须显式指定 occurrence
idx.get("MANIFEST", occurrence=1)

# 导出：重新打开归档、流式复核摘要，通过后才原子改名落盘
path = export_member(idx, "src/lib/x.py", "/tmp/out")

# 两个索引的成员差异（按逻辑路径，逐 occurrence 比较）
diff = diff_indices(index_archive("a.zip"), index_archive("b.zip"))
```

## 测试

环境无第三方依赖，测试基于标准库 `unittest`，所有归档都在临时目录中现场合成：

```bash
python3 -I run_tests.py
```

覆盖：重复路径与歧义标记、压缩炸弹（数量/深度/单成员/总量/压缩比）、
符号/硬链接与包外链接拒绝、GBK/UTF-8 编码文件名、损坏 central directory /
CRC 篡改 / 截断流、摘要变更后导出拒绝、单成员导出往返验证与目录包含防护；
以及完整 CSV 接入、契约/清单错误、结构错误与可隔离坏行分类、跨来源主键冲突、
多行引号、物理行号、原子发布、诊断文件和 220,000 行有界内存验证；
以及嵌套组装的期望对象逐对象核对、四层方案、复合身份与分隔符、
TEXT `'01'` 与 INTEGER `1` 区分、排序并列/NULL 排序/插入顺序无关、
孤儿/重复身份/空引用/错误存储类型/配置冲突拒绝且不留最终文件、
WAL 并发快照、连接释放、接入库只读不被改写和单巨根有界内存冒烟。

## 模块布局

```text
archindex/
  errors.py      # 异常层次（索引、契约、清单、来源流、组装错误）
  pathsan.py     # 纯逻辑路径归一化与链接目标安全解析（不碰真实文件系统）
  hashing.py     # 有界流式摘要 / 流式拷贝
  records.py     # Limits / MemberRecord / IndexResult
  indexer.py     # ZIP/TAR 索引、查询、差异、验证导出
  csvstream.py   # BOM、严格解码、逻辑 CSV 记录与物理行号
  contracts.py   # 字段契约、严格类型转换、decimal/timestamp 规则
  manifest.py    # 接入清单校验
  streams.py     # 归档成员的流式打开与完整读取/摘要复核
  ingestion.py   # 多来源清洗、隔离、SQLite 原子发布
  assembly.py    # 关系表 → 嵌套 JSONL 组装（方案校验、一致读取、流式写出）
  __main__.py    # 终端 index / ingest / assemble 入口
tests/           # 合成归档构造器 + unittest 用例
examples/        # 契约、清单、组装方案样例、查询和干净环境可复现演示
```
