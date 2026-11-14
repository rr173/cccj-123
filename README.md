# archindex — 只读归档包索引器

一个**只读、不落盘解压**的 TAR / ZIP 成员目录索引器。索引过程中成员内容仅以
有界分块（默认 1 MiB）流式通过哈希器，绝不写入磁盘；仅在显式导出单个成员时，
先重新校验全部摘要再原子落盘。

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
CRC 篡改 / 截断流、摘要变更后导出拒绝、单成员导出往返验证与目录包含防护。

## 模块布局

```
archindex/
  errors.py    # 异常层次
  pathsan.py   # 纯逻辑路径归一化与链接目标安全解析（不碰真实文件系统）
  hashing.py   # 有界流式摘要 / 流式拷贝
  records.py   # Limits / MemberRecord / IndexResult
  indexer.py   # ZIP/TAR 索引、查询、差异、验证导出
tests/         # 合成归档构造器 + unittest 用例
```
