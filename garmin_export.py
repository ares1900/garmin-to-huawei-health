# -*- coding: utf-8 -*-
"""
Garmin Connect 中国站 (connect.garmin.cn) 运动批量导出脚本
用法:
    export GARMIN_USER="用户名" GARMIN_PASS="密码"
    python garmin_export.py list                  # 列出所有运动（分页）
    python garmin_export.py download              # 导出全部运动原始文件(FIT)
    python garmin_export.py download --limit 5    # 只导出前 5 条（试运行）
    python garmin_export.py download --start 500  # 从第 500 条开始（断点续传）
输出目录: ./fit/  清单文件: ./export_manifest.csv
"""
import io
import os
import re
import sys
import time
import csv
import zipfile
from datetime import datetime
from pathlib import Path

from garminconnect import Garmin
from garminconnect import GarminConnectTooManyRequestsError

OUT_DIR = Path(__file__).parent / "fit"
MANIFEST = Path(__file__).parent / "export_manifest.csv"


def sanitize(name: str) -> str:
    """清理文件名中的非法字符"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip()
    return name or "activity"


def is_valid_fit(path: Path) -> bool:
    """检查是否为合法 FIT 文件（第 9-12 字节为 .FIT 魔数）"""
    try:
        with open(path, "rb") as f:
            return f.read(12)[8:12] == b".FIT"
    except Exception:
        return False


def save_activity_data(out_file: Path, data: bytes) -> None:
    """处理下载数据：若是 zip 则解出内部 .fit 文件，否则原样保存。返回实际写入的文件"""
    if data[:2] == b"PK":  # zip 魔数
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            fits = [n for n in names if n.lower().endswith(".fit")]
            if fits:
                zf.read(fits[0])  # 触发读取
                out_file.write_bytes(zf.read(fits[0]))
            else:
                # 没有 fit，把 zip 内所有文件解出
                for n in names:
                    (out_file.parent / f"{out_file.stem}__{Path(n).name}").write_bytes(zf.read(n))
    else:
        out_file.write_bytes(data)


def parse_args(argv):
    mode = argv[0] if argv else "list"
    limit = None
    start = 0
    i = 1
    while i < len(argv):
        if argv[i] == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1]); i += 2
        elif argv[i] == "--start" and i + 1 < len(argv):
            start = int(argv[i + 1]); i += 2
        else:
            i += 1
    return mode, limit, start


def fetch_all(g, start_offset=0, max_items=None):
    """分页获取全部运动，返回 (list, total)。库返回普通 list，分页到短页为止。"""
    activities = []
    offset = start_offset
    page_size = 20
    while True:
        batch = g.get_activities(start=offset, limit=page_size)
        if isinstance(batch, dict):
            items = batch.get("activities", [])
        else:
            items = batch or []
        if not items:
            break
        activities.extend(items)
        offset += len(items)
        print(f"  已拉取 {offset} 条...")
        if len(items) < page_size:
            break  # 最后一页
        if max_items is not None and len(activities) >= max_items:
            break
        time.sleep(0.3)
    if max_items is not None:
        activities = activities[:max_items]
    print(f"共获取运动: {len(activities)} 条")
    return activities, len(activities)


def make_filename(act) -> str:
    aid = act.get("activityId", "")
    t = act.get("startTimeLocal", "")[:16].replace(":", "").replace("-", "").replace("T", "_")
    name = sanitize(act.get("activityName", ""))
    return f"{t}_{name}_{aid}"


def download_all(g, start_offset=0, max_items=None):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    activities, total = fetch_all(g, start_offset=start_offset, max_items=max_items)

    manifest_rows = []
    ok, fail = 0, 0
    for idx, act in enumerate(activities, 1):
        aid = act.get("activityId")
        base = make_filename(act)
        out_file = OUT_DIR / f"{base}.fit"
        if out_file.exists() and is_valid_fit(out_file):
            print(f"  [{idx}/{len(activities)}] 已存在且为合法 FIT，跳过: {out_file.name}")
            manifest_rows.append((aid, act.get("activityName", ""), act.get("startTimeLocal", ""), out_file.name, "skipped"))
            ok += 1
            continue
        for attempt in range(4):
            try:
                data = g.download_activity(str(aid), dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL)
                save_activity_data(out_file, data)
                print(f"  [{idx}/{len(activities)}] OK: {out_file.name} ({len(data)} 字节)")
                manifest_rows.append((aid, act.get("activityName", ""), act.get("startTimeLocal", ""), out_file.name, "ok"))
                ok += 1
                break
            except GarminConnectTooManyRequestsError:
                print(f"  [{idx}] 触发限流，等待 10s 重试...")
                time.sleep(10)
            except Exception as e:
                print(f"  [{idx}] 失败({type(e).__name__}): {e}")
                if attempt == 3:
                    manifest_rows.append((aid, act.get("activityName", ""), act.get("startTimeLocal", ""), str(aid), f"fail:{type(e).__name__}"))
                    fail += 1
                else:
                    time.sleep(2)
        time.sleep(0.5)

    with open(MANIFEST, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["activity_id", "activity_name", "start_time_local", "file_name", "status"])
        w.writerows(manifest_rows)
    print(f"\n完成: 成功 {ok}, 失败 {fail}, 清单已写入 {MANIFEST}")
    return ok, fail


def main():
    username = os.environ.get("GARMIN_USER")
    password = os.environ.get("GARMIN_PASS")
    if not username or not password:
        print("请先设置环境变量 GARMIN_USER 和 GARMIN_PASS")
        sys.exit(1)
    mode, limit, start = parse_args(sys.argv[1:])

    print("正在登录...")
    g = Garmin(username, password, is_cn=True)
    g.login()
    print("登录成功!")

    if mode == "list":
        acts, total = fetch_all(g, start_offset=start, max_items=limit)
        for a in acts[:limit] if limit else acts:
            print(f"  {a.get('startTimeLocal','')} | {a.get('activityName','')} | id={a.get('activityId')} | {a.get('activityType',{}).get('typeKey','')}")
    else:
        download_all(g, start_offset=start, max_items=limit)


if __name__ == "__main__":
    main()
