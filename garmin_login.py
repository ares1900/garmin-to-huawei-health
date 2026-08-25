# -*- coding: utf-8 -*-
"""
Garmin Connect (中国站 connect.garmin.cn) 登录 + 运动列表测试脚本
用法:
    export GARMIN_USER="用户名" GARMIN_PASS="密码"
    python garmin_login.py            # 仅列出最近 5 条运动
    python garmin_login.py count      # 统计运动总数
"""
import os
import re
import sys
import json
import time
from urllib.parse import urlencode
import requests

SSO_BASE = "https://sso.garmin.cn"
CONNECT_BASE = "https://connect.garmin.cn"


def build_login_url():
    """构造 SSO 登录页 URL（带参数）"""
    params = {
        "service": f"{CONNECT_BASE}/modern/",
        "webhost": "olaxpw-connect00.garmin.cn",
        "source": f"{CONNECT_BASE}/signin/",
        "redirectAfterAccountLoginUrl": f"{CONNECT_BASE}/modern/",
        "redirectAfterAccountCreationUrl": f"{CONNECT_BASE}/signin/",
        "gauthHost": f"{SSO_BASE}/sso/",
        "rememberMeShown": "true",
        "rememberMeChecked": "false",
        "id": "gauth-widget",
        "embedWidget": "false",
        "clientId": "GarminConnect",
        "consumerKey": "GarminConnect",
        "redirectAfterLoginUrl": f"{CONNECT_BASE}/modern/",
        "displayNameShown": "false",
    }
    return f"{SSO_BASE}/sso/signin?" + urlencode(params)


def parse_csrf(html):
    m = re.search(r'name="_csrf"\s+value="([^"]+)"', html)
    if not m:
        m = re.search(r'value="([^"]+)"[^>]*name="_csrf"', html)
    return m.group(1) if m else None


def login(username, password):
    """登录 Garmin 中国站，返回已认证的 requests.Session"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0 Safari/537.36"),
    })
    login_url = build_login_url()

    # 1. 获取登录页，拿到 _csrf 与初始 cookie
    r = s.get(login_url, timeout=20)
    r.raise_for_status()
    csrf = parse_csrf(r.text)
    if not csrf:
        # 尝试从 JSON 中找
        m = re.search(r'"_csrf"\s*:\s*"([^"]+)"', r.text)
        csrf = m.group(1) if m else None
    if not csrf:
        raise RuntimeError("无法从登录页解析 _csrf 令牌")
    print(f"[1/4] 已获取登录页，_csrf 令牌已解析")

    # 2. POST 提交凭据
    data = {
        "username": username,
        "password": password,
        "embed": "false",
        "_csrf": csrf,
    }
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": login_url,
    }
    r = s.post(f"{SSO_BASE}/sso/signin", data=data, headers=headers, timeout=20)
    if r.status_code not in (200, 302):
        print(f"  登录 POST 返回 {r.status_code}")
        print(r.text[:500])
        raise RuntimeError("登录请求失败")

    # 3. 从响应中解析 ticket 的 response_url
    response_url = None
    try:
        js = r.json()
        response_url = js.get("response_url")
    except Exception:
        pass
    if not response_url:
        m = re.search(r'var response_url\s*=\s*"([^"]+)"', r.text)
        if m:
            response_url = m.group(1).replace("\\/", "/")
    if not response_url:
        print("  --- 登录响应原文（前 1500 字符）---")
        print(r.text[:1500])
        print("  --- 状态码:", r.status_code, "---")
        raise RuntimeError("未在登录响应中找到 response_url（可能是密码错误或需要验证码）")

    # 4. 访问 response_url，建立 connect.garmin.cn 会话
    s.get(response_url, timeout=20)
    print(f"[2/4] 登录成功，ticket 已兑换")

    # 验证会话是否真的可用
    check = s.get(f"{CONNECT_BASE}/modern/proxy/activity-service/userprofile/settings", timeout=20)
    print(f"[3/4] 用户资料接口返回 {check.status_code}")
    if check.status_code != 200:
        raise RuntimeError("登录后会话校验失败")
    print(f"[4/4] 会话已认证通过")
    return s


def get_activities(session, start_date="2000-01-01", end_date="2099-12-31", limit=100, offset=0):
    """获取运动列表（分页），返回 (activities, total_count)"""
    url = (f"{CONNECT_BASE}/modern/proxy/activity-service/activity/search/activities"
           f"?startDate={start_date}&endDate={end_date}&limit={limit}&offset={offset}")
    r = session.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    total = data.get("totalFound", 0)
    return data.get("activities", []), total


def main():
    username = os.environ.get("GARMIN_USER")
    password = os.environ.get("GARMIN_PASS")
    if not username or not password:
        print("请先设置环境变量 GARMIN_USER 和 GARMIN_PASS")
        sys.exit(1)

    s = login(username, password)

    mode = sys.argv[1] if len(sys.argv) > 1 else "list"
    if mode == "count":
        acts, total = get_activities(s, limit=1, offset=0)
        print(f"运动总数: {total}")
    else:
        acts, total = get_activities(s, limit=5, offset=0)
        print(f"运动总数: {total}，最近 {len(acts)} 条:")
        for a in acts:
            t = a.get("startTimeLocal", "")
            name = a.get("activityName", "")
            aid = a.get("activityId")
            typ = a.get("activityType", {}).get("typeKey", "")
            print(f"  [{t}] {name} | type={typ} | id={aid}")


if __name__ == "__main__":
    main()
