# 标签坐标联合读取（label-coordinate joint reading）

不同工厂交来的检测矩阵，轴的排列和坐标顺序常常不同：一份按设备×批次放值，
另一份按批次×设备放值；同一设备编号的数据还可能分散在几块矩阵里。
`archindex.labelread` 把这些只读 NumPy 矩阵当成**一套按真实标签对齐的资料**
使用，而不是关系行。它只整理存储与访问：**不做插值、重采样、统计汇总或
物理计算**，也不把矩阵转成关系表或合并成一份大数组。

## 1. 描述文件格式

描述文件是一个 UTF-8 JSON，路径相对该描述文件所在目录解析。完整样例见
[`labelread_descriptor.example.json`](labelread_descriptor.example.json)。

```json
{
  "format": "archindex-label-read-1",
  "axes": [
    {"name": "device", "type": "text",    "set": "union"},
    {"name": "batch",  "type": "integer", "set": "intersection"}
  ],
  "sources": [
    {
      "source_id": "jia",
      "coordinates": {
        "device": {"path": "jia_device.npy"},
        "batch":  {"values": [20, 10]}
      }
    }
  ],
  "variables": [
    {
      "name": "value",
      "source_id": "jia",
      "path": "jia_value.npy",
      "mask_path": "jia_value_valid.npy",
      "dimensions": ["device", "batch"]
    }
  ]
}
```

### 轴（`axes`，全局有序，1–4 个）

- `name`：全局唯一轴名；顺序就是逻辑网格的轴顺序，输出时**最后一轴变化最快**。
- `type`：
  - `integer`：有符号 64 位整数（`int64`），**不允许 bool**；
  - `text`：字符串，逐 Unicode 码点原样比较，不裁空格、不做大小写折叠。
- `set`：该轴可见标签的集合策略，`union` 或 `intersection`。
- 不同轴可以出现同名标签（例如两个轴都有标签 `1`），轴身份由 `axis` 名携带，
  绝不会因为标签字面相同而串轴。

### 来源（`sources`）

- `source_id`：全局唯一字符串。
- `coordinates`：只列**该来源实际用到的轴**，每个轴给一个一维坐标向量：
  - `{"path": "x.npy"}}` 读只读 `.npy` 向量；
  - `{"values": [...]}` 直接内联（等价的一维向量）。
- 向量顺序可以杂乱；同一来源在同一轴上**重复标签直接拒绝**，
  不会取第一项掩盖冲突。
- 一个来源没用某轴时，它对该轴的集合运算**没有任何贡献**，既不是空集合，
  也不参与交集。

### 变量（`variables`，可多条同名）

每个条目是“某来源里的某变量”的一块物理存储：

- `name`：变量名；同名条目的所有来源必须具有**同一组逻辑维度**
  （集合相等），但物理顺序可任意排列。
- `source_id` + `path`：只读 `.npy` 数组，支持 `int64` 与 `float64`，
  大小端均可；拒绝 object 数组、bool/32 位等其他类型。
- `dimensions`：按**该数组物理维度次序**写轴名，名字不能重复；
  数组每个维度的长度必须逐项等于该来源对应坐标向量的长度，否则拒绝。
- `mask_path`：可选，同形 bool 数组；`false` 表示该格掩码无效。
- 同名变量的存储类型必须一致（不能一块 int64、一块 float64）。

变量可以只用部分全局轴。例如校准值只声明 `["device"]`，读取它与
设备×批次的检测值时，校准值沿 `batch` **按声明广播**。广播来自变量自身的
维度声明；来源缺少一个本该有的维度会在校验阶段报错，不会被偷偷广播。

## 2. 可见标签与网格

- 每个轴的可见标签只在**实际含该轴的来源**之间求 union/intersection；
- 整数按数值排序、文本按码点排序；`intersection` 为空时正常返回**空网格**；
- 坐标集合相同但局部顺序不同，不改变任何结果。

读取请求：

```json
{
  "variables": ["value", "calibration"],
  "selection": {"device": ["D3", "D1"], "batch": [30, 10]},
  "mode": "strict"
}
```

- 未指定的轴取完整可见集合；显式列表**按请求顺序**输出；
- 拒绝重复标签和不在可见集合里的标签（在发出第一格之前拒绝）；
- 输出网格按全局轴顺序排列，最后一轴变化最快；变量投影顺序、来源物理轴
  顺序、来源登记顺序都不能把数值换到别的格子。

## 3. 每格三态：value / masked / absent

一个来源只有在**该变量全部维度的坐标都能定位**时才提供这个格；不能把两个
各有一半坐标的来源拼成一个从未存在的原值。因此 union 网格里出现空洞是正常
情况，而且必须与掩码无效分开：

| 状态 | 含义 |
| --- | --- |
| `absent` | 没有任何来源能定位该变量在此格的完整坐标 |
| `masked` | 有来源定位，但全部掩码为 `false`；底层位模式**不读作业务值** |
| `value` | 至少一个来源定位且掩码有效（无掩码视为有效） |

有效（未被掩码）的 `float64` 若是 `NaN` 或无穷，读到该格时报 `CellDataError`，
指明来源、变量、逻辑坐标和物理下标——它不会被混同为缺测。未请求的格子不会被
提前扫描，因此坏浮点只在真正读到时才失败。掩码下的 NaN 不报错（状态为
`masked`）。

## 4. 多来源重叠：strict 与 priority

同一变量同一逻辑坐标出现多个有效值时：

- `strict`（默认）：值必须**完全相等**。整数全程精确；浮点不设容差，
  `+0.0` 与 `-0.0` 视为相等，`int64 1` 与 `float64 1.0` 类型不同即冲突。
  相等时保留全部来源证据；不等时报冲突，给出逻辑坐标、双方原始位置和值。
- `priority`：按请求里**唯一**的来源优先级取第一个**有效值**；
  掩码无效的高优先级来源会被跳过；与描述文件里来源登记先后无关。
  必须列出承载所请求变量的全部来源。

两种策略都不拿 masked 的底层数比较，也绝不求平均。

## 5. 输出与来路（provenance）

每格 JSONL 含带轴身份的坐标、每个所选变量的状态/值，以及每份证据的
`source_id`、变量文件、按**该数组物理维度次序**排列的下标和物理轴名。
广播后多个格引用同一原位置是正常现象。64 位整数以 JSON 整数输出，
**不经过 float**。流末尾的 summary 给出格数与每变量三态计数；命令失败时
退出码非零且**不打印** `status:ok` 标记。

值冲突或坏浮点可能发生在流的中途：此前已写出的格子仍是合法 JSONL
（部分流），但**部分流不算成功结果**——没有 summary、退出码非零。

## 6. Python 接口

```python
from archindex import load_descriptor

with load_descriptor("examples/labelread_descriptor.example.json") as store:
    store.inspect()                       # 轴/可见标签/来源/变量，不读载荷
    store.visible_labels("batch")         # 排序后的可见标签
    for cell in store.iter_cells(
            ["value", "calibration"],
            selection={"device": ["D3", "D1"]},
            mode="priority",
            priority=["yi", "jia", "bing"]):
        ...
    for batch in store.iter_batches(["value"], batch_size=1000):
        ...                               # 有界分批
    cells, summary = store.read_cells(["value"])   # 小请求的便捷物化
```

## 7. 终端命令

```bash
python3 -I archindex/__main__.py label-inspect examples/labelread_descriptor.example.json

python3 -I archindex/__main__.py label-read \
    examples/labelread_descriptor.example.json \
    --request examples/data/handcalc_request.example.json
```

## 8. 资源模型（为什么能读 >1 GiB 矩阵）

- 所有数组以**只读 mmap** 打开（object 数组无法 mmap，在校验时即拒绝），
  从不 `np.load` 整体复制，也不构建全局 `meshgrid`、转置副本或 `tolist`。
- 常驻内存的是**坐标向量和“标签→局部位置”字典**，规模随各轴**标签数**线性
  增长，记所有来源坐标向量长度之和为 L、轴数为 A（≤4），则这部分约
  O(L·A) 个条目；它与数组单元总数（各矩阵 shape 之积）是完全不同的量级，
  不随矩阵变大而膨胀。
- 一次读取只触及请求格对应的标量位置；未请求的变量不会被读取数据；
  不生成任何完整对齐副本。

`examples/demo_labelread_big.py` 建立两块各 512 MiB（合计 **1.000 GiB**）的
8192×8192 float64 矩阵，只从尾部不相邻标签取一个 4×4 矩形（16 格）。
实测（结果写入 `examples/data/big_access_report.json`）：

```text
matrix_bytes_total:          1 073 742 080 (1.000 GiB)
grid_cells_read:             16
scalar_reads:                32  (16 格 × 2 来源)
touched_4kib_pages:          8   (约 32 KiB，占载荷 0.003%)
peak_rss:                    约 36.5 MiB
elapsed:                     约 0.005 s
```

若整体装载或枚举全局笛卡尔积，峰值内存/触页量会接近 1 GiB；实测证明读取
只触及支撑这 32 个标量的 8 个文件页，且每个值都与独立重算结果逐一核对。

## 9. 可复跑演练

```bash
python3 -I examples/build_labelread_examples.py   # 生成手算例子的 .npy 与描述
python3 -I examples/demo_labelread.py              # 手算表逐格核对 + 请求校验
python3 -I examples/demo_labelread_big.py          # 1 GiB 内存/访问证据（首次较慢）
python3 -I run_tests.py                             # 含本模块全部 unittest
```

演练与测试覆盖：手算九格三态与物理来路、校准广播、来源/轴/坐标重排不变性、
union/intersection、空轴交集、显式乱序选格、半坐标来源不可拼格、strict 冲突、
priority 跳过无效高优先级来源、重复标签与坏 shape/坏类型诊断、超过 2^53 的
精确整数、大小端存储、有效 NaN 与掩码下 NaN 的区别、三轴且变量维度不同的
非二维特例，以及 1 GiB 矩阵的有界内存访问。
