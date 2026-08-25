# -*- coding: utf-8 -*-
"""
FIT 文件 sport/sub-sport 批量修改工具（Garmin → 华为 Health 兼容）
用法:
    python fit_sport_modifier.py scan <fit目录>            # 扫描编码分布
    python fit_sport_modifier.py modify <fit目录> <输出目录>  # 批量修改
    python fit_sport_modifier.py verify <fit目录>            # 校验文件合法
"""
import os
import sys
import struct
import shutil
import csv
from pathlib import Path
from collections import Counter

# ---------- FIT 二进制解析 ----------

# 官方 FIT 枚举（来自 Garmin FIT SDK Profile，fitparse 生成）——仅用于展示
SPORT = {0: 'generic', 1: 'running', 2: 'cycling', 4: 'fitness_equipment', 5: 'swimming',
         10: 'training', 11: 'walking', 12: 'cross_country_skiing', 13: 'alpine_skiing',
         15: 'rowing', 16: 'mountaineering', 17: 'hiking', 18: 'multisport', 28: 'other'}
SUB_SPORT = {0: 'generic', 1: 'treadmill', 2: 'street', 3: 'trail', 4: 'track', 5: 'indoor',
             6: 'indoor_cycling', 14: 'indoor_rowing', 16: 'rower', 17: 'lap_swimming',
             18: 'open_water', 20: 'strength_training'}


# Garmin FIT SDK 官方 CRC 表（16 项 nibble 查表法）
CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)


def _crc16(data, crc=0x0000):
    """Garmin FIT 官方 CRC 算法（nibble 查表，初值 0）"""
    for byte in data:
        tmp = CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ CRC_TABLE[byte & 0xF]
        tmp = CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ CRC_TABLE[(byte >> 4) & 0xF]
    return crc


class FitReader:
    """读取并解析 FIT 文件，支持重写输出"""

    def __init__(self, data: bytes):
        self.data = data
        self.header_size = data[0]
        if self.header_size not in (12, 14):
            raise ValueError(f"不支持的文件头大小: {self.header_size}")
        self.data_size = struct.unpack("<I", data[4:8])[0]
        self.records = []          # ('def', lmt, dict) 或 ('data', lmt, raw_bytes)
        self.definitions = {}      # lmt -> dict
        self.parse()
        self.crc_ok = self.check_crc()

    @staticmethod
    def lmt_of(hdr):
        """解析记录头得到 local message type。
        FIT 记录头 bit7-6 两位编码: 0b10=压缩时间戳, 0b01=定义, 0b00=普通数据"""
        if hdr & 0x80:       # 压缩时间戳: lmt 在 bit5-6
            return (hdr >> 5) & 0x03
        return hdr & 0x0F    # 定义/普通数据: lmt 在 bit0-3

    def parse(self):
        body = self.data[self.header_size:self.header_size + self.data_size]
        i, n = 0, len(body)
        while i < n:
            hdr = body[i]; i += 1
            if hdr & 0x80:
                # 压缩时间戳数据消息（无开发者数据）
                lmt = (hdr >> 5) & 0x03
                d = self.definitions.get(lmt)
                if d is None:
                    raise ValueError(f"未知的 local message type: {lmt}")
                size = sum(f[1] for f in d['fields'])
                raw = body[i:i+size]
                if len(raw) != size:
                    raise ValueError("数据记录长度不足，文件可能损坏")
                self.records.append(('data', hdr, raw, d))  # 记录生效定义引用
                i += size
            elif hdr & 0x40:
                # 定义消息 (bit6)：reserved + arch + gmsg(2) + nfields + 字段定义
                arch = body[i+1]
                gmsg = struct.unpack("<H" if arch == 0 else ">H", body[i+2:i+4])[0]
                field_count = body[i+4]
                j = i + 5
                fields = []
                for _ in range(field_count):
                    fields.append((body[j], body[j+1], body[j+2]))  # (num, size, basetype)
                    j += 3
                dev = []
                if hdr & 0x20:
                    dev_count = body[j]; j += 1
                    for _ in range(dev_count):
                        dev.append((body[j], body[j+1], body[j+2]))
                        j += 3
                lmt = hdr & 0x0F
                d = {'arch': arch, 'gmsg': gmsg, 'fields': fields, 'dev': dev}
                self.definitions[lmt] = d
                self.records.append(('def', hdr, d))   # 保留原始头字节
                i = j
            else:
                # 普通数据消息，开发者数据标志在 bit5
                lmt = hdr & 0x0F
                d = self.definitions.get(lmt)
                if d is None:
                    raise ValueError(f"未知的 local message type: {lmt}")
                size = sum(f[1] for f in d['fields'])
                if hdr & 0x20:
                    size += sum(x[1] for x in d['dev'])
                raw = body[i:i+size]
                if len(raw) != size:
                    raise ValueError("数据记录长度不足，文件可能损坏")
                self.records.append(('data', hdr, raw, d))  # 记录生效定义引用
                i += size

    def check_crc(self):
        """校验文件 CRC（返回 (文件CRC是否匹配, 头部CRC是否匹配)）
        文件 CRC 覆盖 [0:header_size+data_size]（含头部 CRC 字段，不含末尾 2 字节）"""
        stored = struct.unpack("<H", self.data[-2:])[0]
        calc = _crc16(self.data[0:self.header_size + self.data_size])
        if self.header_size == 14:
            hdr_crc = struct.unpack("<H", self.data[12:14])[0]
            hdr_ok = (hdr_crc == _crc16(self.data[0:12]))
        else:
            hdr_ok = True
        return (stored == calc, hdr_ok)

    def rebuild(self) -> bytes:
        """按当前 records/definitions 重写整个文件，重算 CRC"""
        header_size = self.header_size
        body = bytearray()
        for rec in self.records:
            kind, hdr, payload = rec[0], rec[1], rec[2]
            if kind == 'def':
                body.append(hdr)
                body.append(0x00)  # reserved 字节
                arch = payload['arch']
                body.append(arch)
                body += struct.pack("<H" if arch == 0 else ">H", payload['gmsg'])
                body.append(len(payload['fields']))
                for fnum, fsize, fbt in payload['fields']:
                    body += bytes((fnum, fsize, fbt))
                if payload['dev']:
                    body.append(len(payload['dev']))
                    for dnum, dsize, dindex in payload['dev']:
                        body += bytes((dnum, dsize, dindex))
            else:
                body.append(hdr)
                body += payload

        # 头部：保留原字节但更新 data_size
        out = bytearray()
        out += self.data[0:4]
        out += struct.pack("<I", len(body))
        out += self.data[8:header_size]
        if header_size == 14:
            hcrc = _crc16(bytes(out[0:12]))
            out[12:14] = struct.pack("<H", hcrc)
        out += body
        fcrc = _crc16(bytes(out))
        out += struct.pack("<H", fcrc)
        return bytes(out)

    def parse_data(self, d, raw):
        """把数据消息解析为 {field_num: raw_bytes}"""
        vals = {}
        off = 0
        for fnum, fsize, _ in d['fields']:
            vals[fnum] = raw[off:off+fsize]
            off += fsize
        return vals

    def build_data(self, d, vals, orig_size):
        """根据字段值重新构建数据消息字节（按定义字段顺序）"""
        out = bytearray(orig_size)
        off = 0
        for fnum, fsize, _ in d['fields']:
            if fnum in vals:
                v = vals[fnum]
                if len(v) > fsize:
                    v = v[:fsize]
                elif len(v) < fsize:
                    v = v + b'\x00' * (fsize - len(v))
                out[off:off+fsize] = v
            off += fsize
        return bytes(out)


# ---------- 扫描 ----------

def scan_dir(fit_dir: Path):
    c = Counter()
    samples = {}
    errors = []
    for f in sorted(fit_dir.glob("*.fit")):
        try:
            fr = FitReader(f.read_bytes())
            if not fr.crc_ok[0]:
                errors.append((f.name, "CRC不匹配"))
                continue
            sport, subsport = get_sport_subsport(fr)
            key = f"{sport}|{subsport}"
            c[key] += 1
            samples.setdefault(key, []).append(f.name)
        except Exception as e:
            errors.append((f.name, f"{type(e).__name__}: {e}"))
    return c, samples, errors


# FIT 消息字段编号（sport 相关）
SPORT_MSG = {5: 'sport', 6: 'sub_sport'}   # session 消息 (gmsg 18)
SPORT_MSG12 = {0: 'sport', 1: 'sub_sport'}  # sport 消息 (gmsg 12)


def get_sport_subsport(fr):
    """从第一个 session 消息读取 sport/sub_sport（session 字段 5/6）"""
    for rec in fr.records:
        if rec[0] == 'data':
            _, hdr, payload, d = rec
            if d['gmsg'] == 18:  # session
                vals = fr.parse_data(d, payload)
                if 5 in vals and 6 in vals:
                    sport = vals[5][0] if vals[5] else 255
                    sub = vals[6][0] if vals[6] else 255
                    return sport, sub
    return 255, 255


def dump_structure(fr):
    """列出各消息类型出现的字段（用于确认字段编号）"""
    from collections import OrderedDict
    seen = OrderedDict()
    for rec in fr.records:
        if rec[0] == 'data':
            _, hdr, payload, d = rec
            lmt = FitReader.lmt_of(hdr)
            g = d['gmsg']
            if g in (12, 18, 19, 20):
                key = (g, lmt)
                if key not in seen:
                    seen[key] = [f[0] for f in d['fields']]
    return seen


def sport_name(s, ss):
    sn = SPORT.get(s, f"sport{s}")
    ss2 = SUB_SPORT.get(ss, f"sub{ss}")
    return f"{s}-{ss} {sn}/{ss2}"


# ---------- 批量修改 ----------

# 目标映射：(原始 sport, sub_sport) -> (新 sport, sub_sport)
# 来源编码 = 实际扫描 Garmin 文件得到的值（非佳明网页的 typeKey）
# 华为 FIT 导入验证结论（2026-08-25）：
#   力量训练(10)/游泳(5)/划船(15) 均无法导入（6000xxx / code=400），
#   统一改为步行(11,0) 是唯一验证通过的绕行方案。
TARGET_MAP = {
    (15, 14): (11, 0),   # 划船机 rower/indoor_rowing → 步行（原力量训练 10-20 被华为拒）
    (4, 16):  (11, 0),   # 划船机（其他编码形式）→ 步行（原力量训练 10-20 被华为拒）
    (17, 0):  (11, 0),   # 徒步 hiking → 步行
    (16, 0):  (11, 0),   # 登山 mountaineering → 步行
    (5, 17):  (11, 0),   # 泳池游泳 lap_swimming → 步行（原游泳 5-0 被华为拒 60002102）
    (5, 18):  (11, 0),   # 开放水域 open_water → 步行（原游泳 5-1 被华为拒 60002102）
    (0, 51):  (11, 0),   # 追星活动 generic/sub51 → 步行
    (0, 0):   (11, 0),   # 其他 generic/generic → 步行（通用兜底）
    (1, 45):  (1, 1),    # Indoor Running（自定义 sub45）→ 跑步机
}
SWIM_SPORT = 5            # sport=5 为游泳，仅当目标仍为游泳时才需补泳池长度字段
POOL_LENGTH_DEFAULT = 25  # 泳池长度默认 25 米（米为单位，写字段原始值需 *100）
POOL_LENGTH_RAW = POOL_LENGTH_DEFAULT * 100   # session.pool_length scale=100，25m=原始值2500


def build_data_message(d, raw, overrides, add_fields, add_vals):
    """按修改后的字段表重建一条数据消息。
    overrides: 覆盖已有字段的新值
    add_fields: 新增字段定义（num, size, basetype），追加在定义末尾
    add_vals:   新增字段的值 {fnum: raw_bytes}
    开发者数据字段字节原样保留在末尾。"""
    orig_vals = {}
    off = 0
    for fnum, fsize, _ in d['fields']:
        orig_vals[fnum] = raw[off:off + fsize]
        off += fsize
    dev_bytes = raw[off:]                      # 开发者数据字段（如有）
    fields = list(d['fields']) + list(add_fields)
    out = bytearray()
    for fnum, fsize, _ in fields:
        if fnum in overrides:
            v = overrides[fnum]
        elif fnum in add_vals:
            v = add_vals[fnum]
        elif fnum in orig_vals:
            v = orig_vals[fnum]
        else:
            v = b'\xff' * fsize                # 理论不可达，无效值兜底
        if len(v) > fsize:
            v = v[:fsize]
        elif len(v) < fsize:
            v = v + b'\x00' * (fsize - len(v))
        out += v
    return bytes(out) + dev_bytes


def _field_offset(d, fnum):
    """计算字段 fnum 在数据消息中的字节偏移（按定义字段顺序）"""
    off = 0
    for n, s, _ in d['fields']:
        if n == fnum:
            return off
        off += s
    return None


def modify_fit(fr, target, need_pool):
    """按目标 sport/sub_sport 重建 FIT 记录。返回新文件字节；无需修改时返回 None。
    改动对象：session 消息(字段 5/6)与 sport 消息(字段 0/1)。
    泳姿向 session 补 pool_length 字段(44)：缺失则追加（默认 25m），已有但为 0 则修正，合法值保持不动。"""
    orig_sport, orig_sub = get_sport_subsport(fr)
    if orig_sport == 255:
        return None                            # 无法识别运动类型，保持原样
    t_sport, t_sub = (orig_sport, orig_sub) if target is None else target

    # 定义级：仅在字段缺失时新增（sport/sub_sport/pool_length）
    defs = {id(rec[2]): rec[2] for rec in fr.records if rec[0] == 'def'}
    add_fields = {}    # id(d) -> [(fnum, size, basetype), ...]
    add_vals = {}      # id(d) -> {fnum: raw_bytes}
    for did, d in defs.items():
        fnums = [f[0] for f in d['fields']]
        if d['gmsg'] == 18:                    # session
            if target is not None:
                if 5 not in fnums:
                    add_fields.setdefault(did, []).append((5, 1, 0x00))     # sport enum
                    add_vals.setdefault(did, {})[5] = bytes([t_sport])
                if 6 not in fnums:
                    add_fields.setdefault(did, []).append((6, 1, 0x00))     # sub_sport enum
                    add_vals.setdefault(did, {})[6] = bytes([t_sub])
            if need_pool and 44 not in fnums:
                add_fields.setdefault(did, []).append((44, 2, 0x84))        # uint16
                add_vals.setdefault(did, {})[44] = struct.pack("<H", POOL_LENGTH_RAW)
        elif d['gmsg'] == 12 and target is not None:   # sport 消息
            if 0 not in fnums:
                add_fields.setdefault(did, []).append((0, 1, 0x00))
                add_vals.setdefault(did, {})[0] = bytes([t_sport])
            if 1 not in fnums:
                add_fields.setdefault(did, []).append((1, 1, 0x00))
                add_vals.setdefault(did, {})[1] = bytes([t_sub])

    # 记录级重建：逐条计算覆盖值（已有字段改值/补 pool_length=0）
    changed = False
    new_records = []
    for rec in fr.records:
        if rec[0] == 'def':
            d = rec[2]
            did = id(d)
            if did in add_fields:
                changed = True
                new_records.append(('def', rec[1],
                                    {'arch': d['arch'], 'gmsg': d['gmsg'],
                                     'fields': list(d['fields']) + list(add_fields[did]),
                                     'dev': d['dev']}))
            else:
                new_records.append(rec)
        else:
            _, hdr, raw2, d = rec
            did = id(d)
            fnums = [f[0] for f in d['fields']]
            ov = {}
            if d['gmsg'] == 18:
                if target is not None:
                    ov[5] = bytes([t_sport])
                    ov[6] = bytes([t_sub])
                if need_pool and 44 in fnums:
                    off = _field_offset(d, 44)
                    cur = struct.unpack("<H", raw2[off:off + 2])[0]
                    if cur == 0:               # 仅修正为 0 的无效长度
                        ov[44] = struct.pack("<H", POOL_LENGTH_RAW)
            elif d['gmsg'] == 12 and target is not None:
                ov[0] = bytes([t_sport])
                ov[1] = bytes([t_sub])
            if ov or did in add_fields:
                changed = True
                new_raw = build_data_message(d, raw2, ov, add_fields.get(did, []), add_vals.get(did, {}))
                new_records.append(('data', hdr, new_raw, d))
            else:
                new_records.append(rec)

    if not changed:
        return None                            # 无需修改
    fr.records = new_records
    return fr.rebuild()


# ---------- 主入口 ----------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if mode == "scan":
        fit_dir = Path(sys.argv[2] if len(sys.argv) > 2 else "fit")
        c, samples, errors = scan_dir(fit_dir)
        print(f"扫描目录: {fit_dir}")
        print(f"编码分布:")
        for k, v in sorted(c.items()):
            s, ss = map(int, k.split("|"))
            print(f"  {v:4d} 条  {sport_name(s, ss)}")
            if samples[k]:
                print(f"        示例: {samples[k][0]}")
        if errors:
            print(f"\n解析失败 {len(errors)} 个文件:")
            for name, err in errors:
                print(f"  {name}: {err}")
        return
    if mode == "modify":
        fit_dir = Path(sys.argv[2] if len(sys.argv) > 2 else "fit")
        out_dir = Path(sys.argv[3] if len(sys.argv) > 3 else "fit/华为导入目录")
        out_dir.mkdir(parents=True, exist_ok=True)
        files = sorted(fit_dir.glob("*.fit"))
        changed = unchanged = failed = 0
        print(f"输入: {fit_dir}（{len(files)} 个文件）\n输出: {out_dir}\n")
        for f in files:
            try:
                raw = f.read_bytes()
                fr = FitReader(raw)
                if not fr.crc_ok[0]:
                    failed += 1
                    print(f"  [失败] {f.name}: 原始 CRC 校验失败")
                    continue
                orig_sport, orig_sub = get_sport_subsport(fr)
                target = TARGET_MAP.get((orig_sport, orig_sub))
                need_pool = (target is not None and target[0] == SWIM_SPORT)
                new_raw = modify_fit(fr, target, need_pool)
                if new_raw is None:
                    new_raw = raw
                    unchanged += 1
                    tag = "未改动"
                else:
                    # 修改后必须能再次解析且 CRC 正确，否则不算成功
                    fr2 = FitReader(new_raw)
                    if not fr2.crc_ok[0]:
                        failed += 1
                        print(f"  [失败] {f.name}: 修改后 CRC 校验失败")
                        continue
                    changed += 1
                    tag = (f"已修改 {sport_name(orig_sport, orig_sub)}"
                           f" → {sport_name(target[0], target[1])}"
                           + (" +pool_length" if need_pool else ""))
                (out_dir / f.name).write_bytes(new_raw)
                print(f"  [OK] {f.name}: {tag}")
            except Exception as e:
                failed += 1
                print(f"  [失败] {f.name}: {type(e).__name__}: {e}")
        print(f"\n完成: 修改 {changed}, 未改动 {unchanged}, 失败 {failed}")
        print(f"输出目录: {out_dir}")
        return
    if mode == "verify":
        fit_dir = Path(sys.argv[2] if len(sys.argv) > 2 else "fit")
        n_ok = 0
        for f in sorted(fit_dir.glob("*.fit")):
            try:
                fr = FitReader(f.read_bytes())
                if fr.crc_ok[0]:
                    n_ok += 1
                else:
                    print(f"  CRC失败: {f.name}")
            except Exception as e:
                print(f"  解析失败: {f.name}: {e}")
        print(f"合法文件: {n_ok}/{len(list(fit_dir.glob('*.fit')))}")
        return
    if mode == "selftest":
        # 不修改直接重写，验证重写后文件与原始文件数据一致且 CRC 正确
        fit_dir = Path(sys.argv[2] if len(sys.argv) > 2 else "fit")
        import tempfile
        n = 0
        for f in sorted(fit_dir.glob("*.fit"))[:5]:
            raw = f.read_bytes()
            fr = FitReader(raw)
            out = fr.rebuild()
            fr2 = FitReader(out)
            ok_crc = fr2.crc_ok[0]
            same = (fr2.data[fr2.header_size:fr2.header_size+fr2.data_size] ==
                    fr.data[fr.header_size:fr.header_size+fr.data_size])
            print(f"  {f.name}: 重写CRC={ok_crc} 数据一致={same}")
            n += 1
        print(f"已测 {n} 个文件")
        return
    if mode == "struct":
        f = Path(sys.argv[2])
        fr = FitReader(f.read_bytes())
        print(f"文件: {f.name}, CRC匹配: {fr.crc_ok}")
        for g, lmt in dump_structure(fr).items():
            fields = dump_structure(fr)[g]
            print(f"  gmsg={g[0]} lmt={g[1]} 字段: {fields}")


if __name__ == "__main__":
    main()
