# 踩坑与解决记录（PITFALLS）

> 本文档记录「Garmin Connect → 华为运动健康」FIT 迁移全过程中踩过的全部坑、排查思路与最终解决方案。
> 内容基于真实大规模迁移项目验证整理（跨跑步/划船机/徒步/登山/游泳/追踪等多种运动类型，含 8 轮静态结构对比 + 21 个变体 A/B 测试）。
> 每个坑尽量按「现象 → 排查 → 根因 → 解决 → 验证」叙述，方便后续遇到类似问题时直接复用。

---

## 目录

| # | 坑 | 一句话根因 | 严重度 |
|---|----|-----------|--------|
| 1 | [修改 FIT 后必须重算 CRC](#坑1修改-fit-后必须重算-crc) | Garmin 官方 nibble 查表算法，改任何字节都需重算全文件 CRC | 🔴 必须 |
| 2 | [压缩时间戳记录头的解析陷阱](#坑2压缩时间戳记录头解析) | 记录头 bit 位不同编码，lmt 位置随之变化 | 🔴 必须 |
| 3 | [fitparse 的 num_laps 字段绑定陷阱](#坑3fitparse-的-num_laps-字段绑定) | 库把 session.num_laps 绑在旧 Profile 的 f26，新标准是 f41 | 🟠 研究误导 |
| 4 | [华为只接受 6 大类运动类型](#坑4华为只接受-6-大类运动类型) | 佳明独有编码（划船/徒步/登山）无法导入 | 🔴 必须 |
| 5 | [泳池游泳需要 pool_length 字段](#坑5泳池游泳需要-pool_length-字段) | 字段缺失/为 0 时导入被拒 | 🟠 视数据 |
| 6 | [力量/游泳/划船一律拒绝 + 部分跑步 code=400](#坑6力量游泳划船一律拒绝--部分跑步-code400) | 华为服务器端类型限制 + 解析 bug，静态结构无法复现 | 🔴 核心 |
| 7 | [华为仅接受 2014 后记录，无细节记录 App 不可见](#坑7华为仅接受-2014-后记录) | 华为机制，与文件无关 | 🟡 注意 |
| 8 | [必须 OAuth 授权，无法纯脚本模拟登录](#坑8必须-oauth-授权) | 涉及账号/短信/扫码验证，无法代替用户 | 🟠 设计约束 |
| 9 | [华为 token 有效期 1 小时](#坑9华为-token-有效期-1-小时) | 超时需重新登录授权 | 🟡 注意 |
| 10 | [上传接口与请求头细节](#坑10上传接口与请求头细节) | 逆向 protocol：dataImport + x-client-id + apiBase 计算 | 🔴 必须 |
| 11 | [导入错误码含义](#坑11导入错误码含义) | 6000xxx 业务码 vs code=400 HTTP 层 | 🟡 排障 |
| 12 | [Windows GBK 编码崩溃](#坑12windows-gbk-编码崩溃) | 终端重定向 stdout 时 U+2717 打印报错 | 🟢 小坑 |
| 13 | [Garmin 中国站导出要点](#坑13garmin-中国站导出要点) | is_cn 登录、SSO 中国域、下载返回 ZIP 包裹 | 🟡 注意 |

---

## 坑1：修改 FIT 后必须重算 CRC

**现象**：改完 sport 编码的 FIT 文件导入华为后「文件解析失败」，或部分第三方工具拒读。

**根因**：FIT 文件末尾 2 字节是 CRC-16，**覆盖从文件头到数据区末尾的全部字节**。任何字节改动（哪怕 1 个 enum 值）都会让末尾 CRC 失效。此外文件头为 14 字节时（FIT 2.0+），头部还有第二个 CRC 覆盖前 12 字节。

**解决**：使用 Garmin FIT SDK 官方算法——**16 项 nibble 查表法**（非标准 CRC-16/CCITT 多项式查表），初值 `0x0000`：

```python
CRC_TABLE = (0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
             0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400)

def _crc16(data, crc=0x0000):
    for byte in data:
        tmp = CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ CRC_TABLE[byte & 0xF]
        tmp = CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ CRC_TABLE[(byte >> 4) & 0xF]
    return crc
```

重写文件时：先重算头部 CRC（仅 FIT2.0 的 14 字节头），再对 `header+body` 整体重算末尾 CRC。

**验证**：`fit_sport_modifier.py verify` 应全部 `CRC匹配`；`selftest` 模式可验证「重写不改数据」——不修改任何字段直接 rebuild，输出与原始数据区逐字节一致且 CRC 正确。

---

## 坑2：压缩时间戳记录头解析

**现象**：自己手写 FIT 解析器读 Garmin 文件时，把 record 头当成普通字节解析，出现「local message type 不存在」「记录长度不足」等错误。

**根因**：FIT 记录头第一个字节的 **bit7/bit6 两位**编码不同消息类型，且 lmt（local message type）所在位随之变化：

| bit7 | bit6 | 类型 | lmt 位置 | 备注 |
|------|------|------|----------|------|
| 0 | 0 | 普通数据消息 | bit0-3 | 开发者数据标志在 bit5 |
| 0 | 1 | 定义消息（def） | bit0-3 | 开发者数据计数在 bit5 |
| 1 | - | 压缩时间戳数据消息 | **bit5-6** | 时间戳由相邻记录差值推算 |

**解决**：按 `hdr & 0x80`（压缩时间戳）→ `hdr & 0x40`（定义）→ 普通数据的顺序分支解析：

```python
if hdr & 0x80:       # 压缩时间戳: lmt 在 bit5-6
    lmt = (hdr >> 5) & 0x03
elif hdr & 0x40:     # 定义消息: lmt 在 bit0-3
    lmt = hdr & 0x0F
else:                # 普通数据: lmt 在 bit0-3
    lmt = hdr & 0x0F
```

另外定义消息还需解析：`arch`（字节序）、`gmsg`（全局消息类型，2 字节，按 arch 决定大小端）、`field_count`，每个字段定义占 3 字节 `(field_num, size, base_type)`。

**验证**：全部 Garmin 原始文件可解析、CRC 全通过。

---

## 坑3：fitparse 的 num_laps 字段绑定

**现象**：用 `fitparse` 读取 session 消息的 `num_laps`，发现全是 `0xFFFFFFFF`（无效值），导致「每圈数量」统计完全不可信。

**根因**：fitparse 的旧 Profile 把 `session.num_laps` 绑定在 **field 26**，而标准新版 FIT Profile 中 `num_laps` 是 **field 41**。Garmin 中国站导出的文件使用 field 41，两者对不上 → 库读到的全是未初始化的 `0xFFFFFFFF`。

**解决**：
1. 研究阶段不要盲信库的字段名绑定——**直接按字段号读原始字节**（`session` 消息 gmsg=18，`num_laps` = field 41）；
2. 通用做法：`parse_data()` 按「定义消息里的字段定义表」逐字段切字节，拿到的是 `{field_num: raw_bytes}`，再按官方 Profile 文档查号。

**验证**：对比 fitparse 与手写解析器对同一文件的 num_laps 输出，手写解析器数值合理（= 实际 lap 消息条数）。

---

## 坑4：华为只接受 6 大类运动类型

**现象**：把 Garmin 原始的划船机、徒步、登山 FIT 直接传华为导入，返回错误或导入后 App 里看不到。

**根因**：华为运动健康网页导入接口（`healthrunninggroup/v1/dataImport`）只识别 **6 大类** sport 编码：跑步(1)、骑行(2)、游泳(5)、步行(11)、铁三(18)、力量训练(10)。佳明独有的 sport 编码（如划船机 15、登山 16、徒步 17）华为不认识。

**解决**：用 `fit_sport_modifier.py` 改写 `session` 消息（gmsg 18）的字段 5/6（sport/sub_sport）与 `sport` 消息（gmsg 12）的字段 0/1，把不兼容类型映射到 6 大类之一：

| 原编码 | 原运动 | 新编码 | 备注 |
|--------|--------|--------|------|
| 15-14 / 4-16 | 划船机 | 11-0 步行 | 华为拒力量训练兜底，最终统一改步行 |
| 17-0 | 徒步 | 11-0 步行 | |
| 16-0 | 登山 | 11-0 步行 | |
| 5-17 / 5-18 | 游泳 | 11-0 步行 | 华为拒游泳兜底，见坑 5/6 |
| 0-0 / 0-51 | 其他/追踪 | 11-0 步行 | |
| 1-45 | Indoor Running | 1-1 跑步机 | |

**验证**：转换后文件全部通过 CRC + fitparse 交叉校验，sport 分布全部落在华为 6 大类。

---

## 坑5：泳池游泳需要 pool_length 字段

**现象**：把泳池游泳 FIT（sport=5, sub_sport=17 lap_swimming）改成「游泳 5-0」后导入，被拒 `60002102`。

**根因**：华为对游泳类型要求 session 消息携带 `pool_length` 字段（field 44，单位 0.01m，25 米池 = 原始值 2500）。字段缺失或值为 0 时判定文件无效。

**解决**：`modify_fit` 对目标仍为游泳的文件补 pool_length：字段缺失则**在定义消息末尾追加字段定义** `(44, 2, uint16)` 并逐条数据消息补值；已有但为 0 则修正；合法值（如 2500）保持不动。注意：**给字段定义表追加字段**必须同时修改该 lmt 的所有数据消息字节布局，并重算 CRC。

**教训**：虽然最终验证发现华为对游泳类型的限制无法靠补 pool_length 绕开（见坑 6），但「字段缺失→定义级追加」这一处理方式本身是正确的，可用于任何自定义字段的补全。

---

## 坑6：力量/游泳/划船一律拒绝 + 部分跑步 code=400

> 本项目**最难、耗时最长**的坑，也是最终解决方案的由来。

**现象**：首批导入失败 25 条 = 网络错误 1 条（重试即成功）+ **稳定失败 24 条**：
- 6 条跑步 → HTTP `code=400`
- 14 条划船机（已改成力量训练 10-20）→ `60002102`
- 4 条泳池游泳（已改成游泳 5-0）→ `60002102`

### 排查：8 轮静态结构对比（全部排除）

对失败文件与成功文件做了 8 轮逐字节级对比，全部**无差异**：
1. gmsg 消息类型分布（def/data 数量与顺序）
2. 字段定义表逐项对比
3. session/lap 时间线自洽性（start/elapsed/timer 差值）
4. 每圈距离与总距离一致性
5. timer 事件序列（event 消息 gmsg 21）
6. `num_laps` 字段值（曾怀疑 f41=0xFFFFFFFF 是原因——全量扫描 192 个文件该字段全部无效，排除）
7. 压缩时间戳连续性
8. 文件头/CRC/字节序

**结论**：失败文件的静态结构完全合法、与成功文件无异，问题在华为服务器端。

### 定位：21 个变体 A/B 测试（一次登录批量测）

对 5 个稳定失败的跑步文件生成变体（`tools/make_variants.py`）：

| 变体 | 做法 | 结果 |
|------|------|------|
| V2 | 合并最后一圈残段圈，num_laps 减 1 | 全部失败 |
| V3 | 删除全部 event 消息 | 全部失败 |
| V4 | V2 + V3 组合 | 全部失败 |
| **V5** | **改写为步行(11-0)** | **全部成功** |

对划船机做 T4 实验（改步行）→ 成功；对游泳改步行 → 成功。

### 根因结论

- 华为 FIT 导入对**力量训练(10)、游泳(5)、划船(15)** 类型一律拒绝（6000xxx 业务码），即使文件结构完全合法；
- 对**部分跑步文件**在「跑步」类型下的解析存在**服务器端 bug**（HTTP code=400），静态结构无法复现，客户端不可控。

### 解决：统一改步行(11-0) 兜底

**把稳定失败文件全部改写为步行(11-0) 是唯一验证通过的绕行方案**：
- 划船机、游泳、部分跑步 → 步行
- 原始轨迹点、距离、心率、时长等数据 100% 保留（逐类抽查验证）
- 代价：华为 App 中这些记录的运动类型显示为「步行」

**验证**：24 个改步行文件 + 1 个遗漏，最终**全部记录导入成功**。

---

## 坑7：华为仅接受 2014 后记录

**现象**：极早期（如 2013 年）的运动记录导入后在 App 中不可见。

**根因**：华为机制，导入接口只认 `2014-01-01 之后` 的记录（其数据平台的时间下限）。

**另外**：无/极少运动细节（轨迹、心率、步频等 record 数据）的记录会被判「无效」，导入接口可能返回成功但 App 内不显示。这是华为的判定机制，不是文件问题，无法在客户端绕过。

---

## 坑8：必须 OAuth 授权

**现象**：尝试用纯 requests 脚本模拟华为导入接口，返回 401/403。

**根因**：华为导入页是 Vue SPA（`h5hosting.dbankcdn.com/.../oauth-callback.html`），上传前必须完成 OAuth 授权（账号密码 / 短信验证码 / 扫码），token 存在页面 `sessionStorage.accessToken`。**无法也不需要代替用户完成登录**。

**解决**：设计为**半自动模式**——有头浏览器打开导入页 → 自动触发 OAuth 跳转 → 用户在浏览器里手动登录 → 脚本轮询 `sessionStorage.getItem('accessToken')`，拿到 token 后接管上传：

```python
# 轮询等待登录（最多 15 分钟）
while time.time() < deadline:
    token = page.evaluate("() => sessionStorage.getItem('accessToken')")
    if token:
        break
```

---

## 坑9：华为 token 有效期 1 小时

**现象**：登录成功后等很久再上传，突然全部 401。

**根因**：页面逻辑 `improtExpireTime` 规定 accessToken **1 小时过期**。

**解决**：超过 1 小时重新运行脚本再授权一次。全量文件（数百条）约需 1-2 分钟传完，一次登录足够。若担心超时，用 `--batch` 调大每批数量减少总时长。

---

## 坑10：上传接口与请求头细节

**逆向结论**（依据官方导入页 `importData.js` 反编译）：

| 环节 | 详情 |
|------|------|
| 导入页 | `h5hosting.dbankcdn.com/cch5/healthkit/data-import/pages/oauth-callback.html#/` |
| OAuth 授权 | `oauth-login.cloud.huawei.com/oauth2/v3/authorize`，`client_id=106533743`，scope 含 healthkit 写入权限 |
| 换取 token | POST `health-api.cloud.huawei.com/commonAbility/userAccessToken/obtain`，body `{authorizationCode, appId}` |
| 上传接口 | `<apiBase>/healthrunninggroup/v1/dataImport` |
| apiBase 计算 | 由 `sessionStorage.site` 决定：中国区 `1` → `viteApiBaseUrl_CN`（`hihealthbase-drcn.things.dbankcloud.cn`） |
| 请求体 | `multipart/form-data`：`file`（FIT 二进制）+ `fileType=fit` |
| 请求头 | `Authorization: Bearer <token>` + `x-client-id: 106533743` |
| 成功判定 | HTTP 200 且 `json.error.code === 0` |

**关键实现技巧**：上传请求**在页面内用 `page.evaluate` 执行 fetch**（不是 Python requests），这样自动携带登录 cookie / Origin / 同源上下文，与页面真实行为一致，**规避跨域与风控**：

```javascript
const fd = new FormData();
fd.append('file', new File([bytes], name));
fd.append('fileType', 'fit');
await fetch(url, {
  method: 'POST',
  headers: { 'Authorization': 'Bearer ' + token, 'x-client-id': '...' },
  body: fd,
});
```

---

## 坑11：导入错误码含义

| 错误 | 含义 | 处置 |
|------|------|------|
| `60002001` | FIT 解析失败 | 检查文件是否被错误修改 / 换类型重试 |
| `60002101` | FIT 文件无效 | 同上 |
| `60002102` | 运动类型不支持 | **改步行(11-0) 兜底** |
| `60002301` | FIT 解析类错误 | 同上 |
| HTTP `400`（部分跑步） | 服务器端解析 bug，静态结构无法复现 | **改步行(11-0) 兜底** |
| HTTP `200` 但 `error.code != 0` | 业务层拒绝 | 按 error.code 查上表 |

**结论**：凡遇到上述任何错误，最快的路径是**直接改步行重试**（已验证唯一可行兜底）。

---

## 坑12：Windows GBK 编码崩溃

**现象**：脚本在 Windows 终端直接运行时正常；一旦 `python script.py > log.txt` 重定向 stdout，就在打印 `✗`（U+2717）时抛 `UnicodeEncodeError`。

**根因**：重定向后 Python 用 `locale` 默认编码（Windows 是 GBK）写文件，U+2717 等字符不在 GBK 码表内。

**解决**：打印用纯 ASCII 标记（`OK` / `FAIL`）替代 `✓` / `✗`；或显式 `sys.stdout.reconfigure(encoding='utf-8')`。

---

## 坑13：Garmin 中国站导出要点

- **登录**：`garminconnect.Garmin(user, pass, is_cn=True)` 走 `sso.garmin.cn`（中国站），URL 是 `connect.garmin.cn`；备用纯 requests 实现见 `garmin_login.py`（解析 `_csrf` + `response_url`）。
- **下载返回 ZIP 包裹**：`download_activity()` 返回的 ORIGINAL 数据是 ZIP（魔数 `PK`），内部含一个 `.fit`，需解包（见 `garmin_export.py: save_activity_data`）。
- **限流**：触发 `GarminConnectTooManyRequestsError` 需等待 10s 重试。
- **断点续传**：`--start 偏移` 从指定条数继续，避免中途失败全量重来。

---

## 附：修改 FIT 后的通用验证清单

任何修改后，务必按序验证：

1. **CRC**：文件末尾 CRC 与头部 CRC（FIT2.0）均正确
2. **可解析**：能被自研解析器完整重读，无「未知 lmt / 长度不足」
3. **交叉校验**：fitparse 可独立读取（如安装）
4. **无损**：`selftest` 模式——不改字段直接 rebuild，数据区与原始逐字节一致
5. **类型落在白名单**：`scan` 确认所有 sport/sub_sport 都在华为 6 大类
6. **抽样人工核对**：改类型后时间/距离/心率/轨迹与原始文件一致（逐类抽查）

---

*文档维护：每次踩坑解决后同步追加，历史记录只增不改。*
