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
多行引号、物理行号、原子发布、诊断文件和 220,000 行有界内存验证。

## 模块布局

```text
archindex/
  errors.py      # 异常层次（索引、契约、清单、来源流错误）
  pathsan.py     # 纯逻辑路径归一化与链接目标安全解析（不碰真实文件系统）
  hashing.py     # 有界流式摘要 / 流式拷贝
  records.py     # Limits / MemberRecord / IndexResult
  indexer.py     # ZIP/TAR 索引、查询、差异、验证导出
  csvstream.py   # BOM、严格解码、逻辑 CSV 记录与物理行号
  contracts.py   # 字段契约、严格类型转换、decimal/timestamp 规则
  manifest.py    # 接入清单校验
  streams.py     # 归档成员的流式打开与完整读取/摘要复核
  ingestion.py   # 多来源清洗、隔离、SQLite 原子发布
  __main__.py    # 终端 index / ingest 入口
tests/           # 合成归档构造器 + unittest 用例
examples/        # 契约、清单样例、查询和干净环境可复现演示
```
