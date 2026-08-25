# -*- coding: utf-8 -*-
"""
为"导入失败"的 FIT 文件生成"改步行(11-0)"重试版本
（根因验证结论：华为 FIT 导入对力量/游泳/划船类型及部分跑步文件解析失败，
  统一改为步行是唯一验证通过的绕行方案，详见 PITFALLS.md）

用法:
    python tools/make_final_fix.py <src_dir> <out_dir> [--ids id1,id2,... | --ids-file ids.txt]
    # 不给 --ids 时处理 src_dir 下全部文件（等价于 fit_sport_modifier modify 的指定映射）

示例:
    python tools/make_final_fix.py fit_huawei fit_huawei_fixed --ids 12345678,87654321
"""
import sys
from pathlib import Path

# 使 tools/ 下的脚本可直接 import 仓库根的 fit_sport_modifier
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fit_sport_modifier import FitReader, modify_fit, get_sport_subsport

TARGET = (11, 0)  # 步行


def parse_ids(raw_ids, ids_file):
    """解析 --ids 逗号分隔或 --ids-file 逐行读取的活动 ID 列表"""
    ids = []
    if raw_ids:
        ids = [x.strip() for x in raw_ids.split(",") if x.strip()]
    if ids_file:
        with open(ids_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    ids.append(line.split()[0])
    return ids


def main():
    argv = sys.argv[1:]
    src_dir = Path(argv[0]) if argv else Path("fit_huawei")
    out_dir = Path(argv[1]) if len(argv) > 1 else Path("fit_huawei_fixed")
    raw_ids = ids_file = None
    i = 2
    while i < len(argv):
        if argv[i] == "--ids" and i + 1 < len(argv):
            raw_ids = argv[i + 1]
            i += 2
        elif argv[i] == "--ids-file" and i + 1 < len(argv):
            ids_file = argv[i + 1]
            i += 2
        else:
            i += 1
    ids = parse_ids(raw_ids, ids_file)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(src_dir.glob("*.fit"))
    if ids:
        files = [f for f in files if any(x in f.name for x in ids)]
    if not files:
        print(f"目录 {src_dir} 下没有匹配的 .fit 文件")
        sys.exit(1)
    print(f"输入: {src_dir}（匹配 {len(files)} 个文件）\n输出: {out_dir}")

    n = 0
    for f in files:
        fr = FitReader(f.read_bytes())
        orig_sport, orig_sub = get_sport_subsport(fr)
        new_raw = modify_fit(fr, TARGET, need_pool=False)
        if new_raw is None:
            print(f"  [跳过] {f.name}: 无法识别运动类型")
            continue
        # 校验输出可解析 + CRC
        fr2 = FitReader(new_raw)
        if not fr2.crc_ok[0]:
            print(f"  [失败] {f.name}: 输出 CRC 校验失败")
            continue
        (out_dir / f.name).write_bytes(new_raw)
        print(f"  [OK] {f.name}  {orig_sport}-{orig_sub} → 11-0 步行")
        n += 1
    print(f"\n共生成 {n} 个文件 → {out_dir}")


if __name__ == "__main__":
    main()
