# 本目录由演示脚本生成，可随时删除后重建

- `handcalc/`、`handcalc_desc.json`：
  `python3 -I examples/build_labelread_examples.py`
- `big/`（两块各 512 MiB、合计 1.000 GiB 的 8192×8192 float64 矩阵）与
  `big_access_report.json`：
  `python3 -I examples/demo_labelread_big.py`（首次运行会自动建矩阵，约 10 秒）

所有 `.npy` 均为只读 mmap 访问，脚本不会把它们整体读入内存。
