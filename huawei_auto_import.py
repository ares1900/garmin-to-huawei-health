# -*- coding: utf-8 -*-
"""
华为运动健康网页版 自动导入脚本（半自动：登录需用户手动）
=============================================================
流程:
  1. 有头浏览器打开华为导入页
  2. 自动触发 OAuth 登录跳转 → 用户在浏览器里手动登录华为账号
  3. 脚本检测登录成功（sessionStorage 出现 accessToken）
  4. 自动读取上传 URL，逐批上传 fit_huawei/ 下全部 FIT 文件
  5. 逐文件记录成功/失败与错误码，输出导入报告 CSV

用法:
    python huawei_auto_import.py                # 全量导入（默认 fit_huawei/）
    python huawei_auto_import.py <fit目录>       # 指定目录
    python huawei_auto_import.py --batch 20     # 每批 20 个（默认 10）

前置步骤:
    先用 fit_sport_modifier.py modify 生成华为可识别的 FIT 文件到 fit_huawei/，
    再运行本脚本导入（详见 README）。
"""
import base64
import csv
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

# 导入页（Vue SPA，hash 路由）
IMPORT_URL = ("https://h5hosting.dbankcdn.com/cch5/healthkit/data-import/"
              "pages/oauth-callback.html#/")
FIT_DIR = Path(__file__).parent / "fit_huawei"
CLIENT_ID = "106533743"
X_CLIENT_ID = "106533743"

# 候选 API Base（按 sessionStorage.site 优先级 + 兜底）
SITE_BASE_MAP = {
    "1": None,  # 从配置读取 viteApiBaseUrl_CN
    "5": None,
    "7": None,
}


def get_api_base(cfg, site):
    """按页面逻辑计算上传接口的 base URL"""
    nrc = cfg["nrcUrl"]
    if not site:
        return cfg["manage"]["host"]
    m = {"1": nrc.get("viteApiBaseUrl_CN"),
         "5": nrc.get("viteApiBaseUrl_DRA"),
         "7": nrc.get("viteApiBaseUrl_DRE")}
    return m.get(site) or nrc.get("viteApiBaseUrl")


def find_login_button(page):
    """尝试定位并点击登录按钮（Vue 页面，多候选选择器）"""
    cands = [
        "button.import_btn",
        ".button_operation",
        "div.button_import",
        ".import-btn",
        "text=登录账号",
        "text=登录",
    ]
    for sel in cands:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                return el
        except Exception:
            continue
    return None


UPLOAD_JS = """
async (args) => {
  const [url, token, files] = args;   // files: [[b64, name], ...]
  const out = [];
  for (const [b64, name] of files) {
    try {
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const fd = new FormData();
      fd.append('file', new File([bytes], name));
      fd.append('fileType', 'fit');
      const resp = await fetch(url, {
        method: 'POST',
        headers: {
          'Authorization': 'Bearer ' + token,
          'x-client-id': '__CLIENT_ID__',
        },
        body: fd,
      });
      const json = await resp.json().catch(() => null);
      out.push({ name, http: resp.status, json });
    } catch (e) {
      out.push({ name, http: -1, json: String(e) });
    }
  }
  return out;
}
"""


def main():
    fit_dir = FIT_DIR
    batch_size = 10
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    for i, a in enumerate(sys.argv[1:]):
        if a == "--batch" and i + 1 < len(sys.argv[1:]):
            batch_size = int(sys.argv[1:][i + 1])
    if args:
        fit_dir = Path(args[0])

    files = sorted(fit_dir.glob("*.fit"))
    if not files:
        print(f"目录 {fit_dir} 下没有 .fit 文件")
        sys.exit(1)
    print(f"待导入文件: {len(files)} 个（每批 {batch_size}）")
    print(f"来源目录: {fit_dir}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # 有头浏览器，供用户登录
        ctx = browser.new_context()
        page = ctx.new_page()

        # 1. 打开导入页
        print("\n[1/4] 打开华为导入页…")
        page.goto(IMPORT_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)

        cfg = page.evaluate("() => window.SYS_GLOBAL_CONFIG")
        if not cfg:
            print("  未读到页面配置，直接构造 OAuth 地址")
        nrc = cfg["nrcUrl"] if cfg else {}

        # 2. 检查是否已登录
        token = page.evaluate("() => sessionStorage.getItem('accessToken')")
        if token:
            print("  检测到已登录（复用登录态）")
        else:
            # 构造 OAuth 授权 URL（等价于点登录按钮的 handleLogIn）
            auth_url = (nrc["authUrl"] + CLIENT_ID + "&redirect_uri="
                        + nrc["redirectUrl"] + "&scope=" + nrc["scopes"])
            print("  未登录，跳转华为 OAuth 授权页…")
            page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
            print("\n  ================================================")
            print("  请在浏览器窗口中完成华为账号登录/授权")
            print("  （账号密码 / 短信验证码 / 扫码均可）")
            print("  脚本会在登录后自动继续，无需其他操作")
            print("  ================================================")

            # 轮询等待登录成功（最多 15 分钟）
            deadline = time.time() + 900
            token = None
            while time.time() < deadline:
                time.sleep(2)
                try:
                    token = page.evaluate(
                        "() => sessionStorage.getItem('accessToken')")
                    if token:
                        break
                    # 若已回到导入页但仍无 token（授权页被关闭等），重新触发
                    if "oauth-callback.html" in page.url:
                        print("  已回到导入页但未取到 token，稍等…")
                except Exception:
                    continue
            if not token:
                print("  等待登录超时，请重新运行脚本")
                browser.close()
                sys.exit(2)

        print("  登录成功！token 已获取")

        # 3. 计算上传 URL
        site = page.evaluate("() => sessionStorage.getItem('site')")
        api_base = get_api_base(cfg, site)
        upload_url = api_base + "/healthrunninggroup/v1/dataImport"
        print(f"[2/4] 上传接口: {upload_url}")
        print(f"      site={site}")

        # 4. 逐批上传
        print(f"[3/4] 开始上传 {len(files)} 个文件…")
        results = []
        js = UPLOAD_JS.replace("__CLIENT_ID__", X_CLIENT_ID)
        for i in range(0, len(files), batch_size):
            batch = files[i:i + batch_size]
            payloads = [
                [base64.b64encode(f.read_bytes()).decode("ascii"), f.name]
                for f in batch
            ]
            try:
                r = page.evaluate(js, [upload_url, token, payloads])
            except Exception as e:
                print(f"  批次异常: {e}")
                for f in batch:
                    results.append([f.name, "脚本异常", str(e)])
                break
            for item in r:
                name = item["name"]
                http = item["http"]
                j = item["json"]
                if isinstance(j, dict):
                    err = j.get("error") or {}
                    code = err.get("code") if isinstance(err, dict) else None
                    ok = (http == 200 and err and err.get("code") == 0)
                    results.append([name, "成功" if ok else "失败",
                                    f"http={http} code={code} msg={err.get('msg') if isinstance(err, dict) else ''}"])
                else:
                    results.append([name, "失败", f"http={http} resp={j}"])
                status = results[-1][1]
                mark = "OK  " if status == "成功" else "FAIL"
                print(f"  {mark} {name}: {results[-1][2]}")
            time.sleep(0.5)  # 批次间隔，避免触发风控

        # 5. 输出报告
        ok_n = sum(1 for r in results if r[1] == "成功")
        fail_n = len(results) - ok_n
        print(f"\n[4/4] 完成: 成功 {ok_n}, 失败 {fail_n}")
        report = Path(__file__).parent / "huawei_import_report.csv"
        with open(report, "w", newline="", encoding="utf-8-sig") as fp:
            w = csv.writer(fp)
            w.writerow(["文件名", "结果", "详情"])
            w.writerows(results)
        print(f"报告已写入: {report}")

        browser.close()


if __name__ == "__main__":
    main()
