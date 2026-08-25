# -*- coding: utf-8 -*-
"""
为指定的"导入失败"跑步 FIT 文件生成变体测试文件（A/B 定位根因，见 PITFALLS.md）
变体策略：
  V2_merge_last_lap   最后一圈（残段圈）并入前一圈，session.num_laps 同步减 1
  V3_del_events       删除全部 event 消息
  V4_merge+del_events 以上两者组合

用法:
    python tools/make_variants.py <src_dir> <out_dir> --ids id1,id2,... [--ctrl-ok id] [--ctrl-fail id]

示例:
    python tools/make_variants.py fit_huawei fit_variants --ids 12345678,87654321 --ctrl-ok 55555555 --ctrl-fail 66666666
"""
import sys
import struct
from pathlib import Path

# 使 tools/ 下的脚本可直接 import 仓库根的 fit_sport_modifier
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fit_sport_modifier import FitReader

# lap 消息 (gmsg 19) 字段
LAP_ELAPSED = 2      # total_elapsed_time uint32 scale 1000 (s)
LAP_TIMER = 3        # total_timer_time   uint32 scale 1000 (s)
LAP_DIST = 4         # total_distance     uint32 scale 100   (m)
# session 消息 (gmsg 18) 字段
SES_NUM_LAPS = 41    # uint16


def _u32(data, off, size):
    """按字段大小读取 uint，支持 1/2/4 字节"""
    if size == 1:
        return struct.unpack("<B", data[off:off + 1])[0]
    if size == 2:
        return struct.unpack("<H", data[off:off + 2])[0]
    if size == 4:
        return struct.unpack("<I", data[off:off + 4])[0]
    raise ValueError(f"unsupported uint size {size}")


def _pack_uint(val, size):
    if size == 1:
        return struct.pack("<B", val & 0xFF)
    if size == 2:
        return struct.pack("<H", val & 0xFFFF)
    if size == 4:
        return struct.pack("<I", val & 0xFFFFFFFF)
    raise ValueError(f"unsupported uint size {size}")


def _lap_vals(fr, d, raw):
    """从 lap 消息提取 (elapsed, timer, dist) 原始整数值"""
    vals = fr.parse_data(d, raw)
    e = t = dd = 0
    for fnum, fsize, _ in d['fields']:
        if fnum == LAP_ELAPSED:
            e = _u32(vals[fnum], 0, fsize)
        elif fnum == LAP_TIMER:
            t = _u32(vals[fnum], 0, fsize)
        elif fnum == LAP_DIST:
            dd = _u32(vals[fnum], 0, fsize)
    return e, t, dd


def merge_last_lap(fr):
    """把最后一个 lap 并入前一个 lap，session.num_laps 减 1。
    若 lap 不足 2 个返回 False（无法合并）。"""
    lap_datas = []  # (record_index, hdr, raw, d)
    for i, rec in enumerate(fr.records):
        if rec[0] == 'data' and rec[3]['gmsg'] == 19:
            lap_datas.append((i, rec[1], rec[3], rec[2]))   # (index, hdr, d, raw)
    if len(lap_datas) < 2:
        return False

    # 找出属于 lap 的 lmt 集合（用于过滤）
    lap_lmts = {FitReader.lmt_of(hdr) for _, hdr, _, _ in lap_datas}

    # 倒数第二个 lap 吸收最后一圈
    last_idx, last_hdr, last_d, last_raw = lap_datas[-1]
    prev_idx, prev_hdr, prev_d, prev_raw = lap_datas[-2]

    pe, pt, pd = _lap_vals(fr, prev_d, prev_raw)
    le, lt, ld = _lap_vals(fr, last_d, last_raw)
    ne, nt, nd = pe + le, pt + lt, pd + ld

    # 重建倒数第二个 lap
    vals = fr.parse_data(prev_d, prev_raw)
    for fnum, fsize, _ in prev_d['fields']:
        if fnum == LAP_ELAPSED:
            vals[fnum] = _pack_uint(ne, fsize)
        elif fnum == LAP_TIMER:
            vals[fnum] = _pack_uint(nt, fsize)
        elif fnum == LAP_DIST:
            vals[fnum] = _pack_uint(nd, fsize)
    prev_raw_new = fr.build_data(prev_d, vals, len(prev_raw))

    # 重建 records：替换 prev，删除 last
    new_records = []
    prev_replaced = False
    for i, rec in enumerate(fr.records):
        if rec[0] == 'data':
            _, hdr, raw, d = rec
            if d['gmsg'] == 19:
                if FitReader.lmt_of(hdr) not in lap_lmts:
                    new_records.append(rec)
                    continue
                if i == prev_idx and not prev_replaced:
                    new_records.append(('data', prev_hdr, prev_raw_new, prev_d))
                    prev_replaced = True
                elif i == last_idx:
                    continue      # 删除最后一圈
                else:
                    new_records.append(rec)
            else:
                new_records.append(rec)
        else:
            new_records.append(rec)
    fr.records = new_records

    # session.num_laps 减 1
    for i, rec in enumerate(fr.records):
        if rec[0] == 'data' and rec[3]['gmsg'] == 18:
            d = rec[3]
            raw = rec[2]
            vals = fr.parse_data(d, raw)
            if SES_NUM_LAPS in vals:
                fsize = dict((f[0], f[1]) for f in d['fields'])[SES_NUM_LAPS]
                cur = _u32(vals[SES_NUM_LAPS], 0, fsize)
                if cur > 0:
                    vals[SES_NUM_LAPS] = _pack_uint(cur - 1, fsize)
                    new_raw = fr.build_data(d, vals, len(raw))
                    fr.records[i] = ('data', rec[1], new_raw, d)
    return True


def del_events(fr):
    """删除所有 event 消息（gmsg 21）的 def 与 data 记录"""
    new_records = []
    for rec in fr.records:
        if rec[0] == 'def' and rec[2]['gmsg'] == 21:
            continue
        if rec[0] == 'data' and rec[3]['gmsg'] == 21:
            continue
        new_records.append(rec)
    fr.records = new_records


def main():
    argv = sys.argv[1:]
    if len(argv) < 1:
        print(__doc__)
        sys.exit(1)
    src_dir = Path(argv[0])
    out_dir = Path(argv[1]) if len(argv) > 1 else Path("fit_variants")
    ids = []
    ctrl_ok = ctrl_fail = None
    i = 2
    while i < len(argv):
        if argv[i] == "--ids" and i + 1 < len(argv):
            ids = [x.strip() for x in argv[i + 1].split(",") if x.strip()]
            i += 2
        elif argv[i] == "--ctrl-ok" and i + 1 < len(argv):
            ctrl_ok = argv[i + 1]
            i += 2
        elif argv[i] == "--ctrl-fail" and i + 1 < len(argv):
            ctrl_fail = argv[i + 1]
            i += 2
        else:
            i += 1
    if not ids:
        print("请用 --ids 指定要生成变体的活动 ID 列表")
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)
    files = [f for f in sorted(src_dir.glob("*.fit"))
             if any(x in f.name for x in ids)]
    if not files:
        print(f"目录 {src_dir} 下没有匹配的 .fit 文件")
        sys.exit(1)

    n = 0
    for f in files:
        fid = [x for x in ids if x in f.name][0]
        raw = f.read_bytes()

        # V2: 合并最后一圈
        fr = FitReader(raw)
        if merge_last_lap(fr):
            out = fr.rebuild()
            FitReader(out)   # 验证可解析 + CRC
            (out_dir / f"V2_{fid}.fit").write_bytes(out)
            n += 1
            print(f"  V2 {fid}: 合并残段圈 OK")
        else:
            print(f"  V2 {fid}: 仅 1 圈，跳过")

        # V3: 删除 event
        fr = FitReader(raw)
        del_events(fr)
        out = fr.rebuild()
        FitReader(out)
        (out_dir / f"V3_{fid}.fit").write_bytes(out)
        n += 1
        print(f"  V3 {fid}: 删 event OK")

        # V4: 合并 + 删 event
        fr = FitReader(raw)
        merge_last_lap(fr)
        del_events(fr)
        out = fr.rebuild()
        FitReader(out)
        (out_dir / f"V4_{fid}.fit").write_bytes(out)
        n += 1
        print(f"  V4 {fid}: 合并+删event OK")

    # 可选对照文件（成功/失败样本各一，用于 A/B 对比）
    for ctrl_id, tag in ((ctrl_ok, "CTRL_成功"), (ctrl_fail, "CTRL_失败")):
        if ctrl_id:
            for f in src_dir.glob("*.fit"):
                if ctrl_id in f.name:
                    (out_dir / f"{tag}_{ctrl_id}.fit").write_bytes(f.read_bytes())
                    n += 1
                    break
    print(f"\n共生成 {n} 个文件 → {out_dir}")


if __name__ == "__main__":
    main()
