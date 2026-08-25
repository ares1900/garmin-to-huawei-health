# Garmin Connect → 华为运动健康 FIT 迁移工具

把 Garmin Connect（中国站）的全部运动记录导出为 FIT，批量转换运动类型编码以兼容**华为运动健康**网页导入，并半自动完成批量上传。

> ⚠️ 个人数据安全：本仓库**不包含任何运动数据**。运行后产生的 `fit*/` 目录、`*.fit` 文件、日志、导入报告均在 `.gitignore` 中排除，切勿强制提交。

## 为什么需要这个工具

华为运动健康的 FIT 导入接口**只接受 6 大类运动**：跑步、骑行、游泳、步行、铁三、力量训练。佳明独有的编码（划船机、徒步、登山等）以及部分运动类型**无法直接导入**，会返回业务错误码或 HTTP 400。本工具用二进制级解析/重写 FIT 文件的 sport/sub-sport 编码，把不兼容类型安全改写为华为可接受的类型（详见 [docs/PITFALLS.md](docs/PITFALLS.md)）。

## 三步骤工作流

```
┌─────────────┐   ┌──────────────────┐   ┌──────────────────┐
│ ① 导出 Garmin │ → │ ② 转换运动类型编码 │ → │ ③ 华为半自动导入   │
│ garmin_export │   │ fit_sport_modifier │   │ huawei_auto_import │
└─────────────┘   └──────────────────┘   └──────────────────┘
      fit/*.fit        fit_huawei/*.fit       导入成功报告 CSV
```

| 步骤 | 脚本 | 作用 |
|------|------|------|
| ① | [garmin_export.py](garmin_export.py) | 登录 Garmin Connect 中国站，分页拉取运动列表，批量下载原始 FIT |
| ② | [fit_sport_modifier.py](fit_sport_modifier.py) | 二进制级解析 FIT，改写 sport/sub-sport 编码，重建 CRC（核心工具） |
| ③ | [huawei_auto_import.py](huawei_auto_import.py) | 有头浏览器打开华为导入页 → 用户手动 OAuth 登录 → 自动批量上传 → 输出报告 |

另有实验工具 `tools/make_variants.py`（A/B 变体生成）与 `tools/make_final_fix.py`（失败文件批量改步行兜底），对应踩坑定位过程中的实验方法。

## 快速开始

```bash
pip install -r requirements.txt
# Playwright 需安装浏览器内核
playwright install chromium
```

### ① 导出 Garmin 数据

```bash
export GARMIN_USER="你的邮箱"
export GARMIN_PASS="你的密码"
python garmin_export.py list                  # 先查看运动列表
python garmin_export.py download              # 批量导出全部运动（输出到 fit/）
python garmin_export.py download --limit 5    # 试运行前 5 条
```

> 凭据仅通过环境变量传入，脚本内不落盘。导出失败自动重试 4 次，支持断点续传 `--start 偏移`。

### ② 转换运动类型编码

```bash
python fit_sport_modifier.py scan fit         # 扫描编码分布
python fit_sport_modifier.py modify fit fit_huawei   # 按 TARGET_MAP 批量转换
python fit_sport_modifier.py verify fit_huawei        # 校验输出合法（CRC + 可解析）
```

映射规则集中在 `fit_sport_modifier.py` 顶部的 `TARGET_MAP`，可自行调整。

### ③ 导入华为运动健康

```bash
python huawei_auto_import.py fit_huawei       # 半自动导入
```

运行后弹出浏览器 → 自动跳转华为 OAuth 登录页 → **你在浏览器里手动登录/授权** → 脚本检测到 token 后自动逐批上传并输出 `huawei_import_report.csv`。

> 设计为「用户手动登录 + 脚本自动上传」：华为 OAuth 涉及账号密码/短信验证码/扫码，脚本不代劳也无法代劳。token 有效期 1 小时。

## 已知限制（华为机制）

- **运动类型**：划船机 / 游泳 / 力量训练 / 部分跑步文件无法按原类型导入，只能改写为「步行(11-0)」兜底（详见 PITFALLS 坑 4/6）
- **时间**：仅接受 2014-01-01 之后的记录
- **数据**：无/极少运动细节（轨迹、心率、步频）的记录会被判「无效」，App 内不可见——这是华为的机制，不是文件问题

## 踩坑与解决

**这是本项目最重要的部分**：全部 13 个踩坑记录（FIT 二进制格式陷阱、华为导入协议逆向、类型限制根因定位、Windows 编码崩溃等）与解决方法见 **[docs/PITFALLS.md](docs/PITFALLS.md)**，其中包含完整的「8 轮静态结构对比 + 21 个变体 A/B 测试」根因定位过程。

## 免责声明

- 本仓库的华为导入协议分析（OAuth 流程、上传接口）**仅用于个人数据迁移与学习研究**，请遵守华为运动健康服务条款，请勿用于批量爬取或滥用
- 修改 FIT 编码可能改变运动类型展示，轨迹/距离/心率/时长等原始数据不受影响
- 工具按现状（AS-IS）提供，不构成任何保证；数据迁移前请自行备份原始文件

## License

[MIT](LICENSE)
