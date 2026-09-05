# 渠道日报表群机器人 — 部署与运行指南

> 状态：**2026-09-03 已验证上线**，首次日报（2026-09-02 数据）已成功推送「渠道日报表群」。
> 目标：每日定时把渠道销售日报推送到群里。
> 数据链路：钉钉AI表格「9月电商渠道日报表」7 张渠道明细表 + 月目标表 → 聚合 → 群机器人推送。

---

## 一、两个脚本怎么选

| 脚本 | 用途 |
|------|------|
| **`run_today.py`** ✅ 当前在用 | 适配已探测的真实表结构：7 张「{渠道}渠道数据{月}」明细表 + 「渠道销售目标达成率{月}」月目标表，输出销售额/推广费/ROI/环比/月累计/达成率完整日报 |
| `main.py` + `channel_report.py` | 通用版（单表 + 字段自动识别），备用方案，字段方案保持原样未改 |

跨月注意：`run_today.py` 顶部 `MONTH = 9` 改成当月数字，且 Base 的月度表后缀也要对应（如 10 月为「天猫渠道数据10」）。

---

## 二、已探明的关键信息（踩坑记录）

1. **operatorId 必须用 unionId，不是 userId**：
   - userId `014341566058939427`（王城）→ Notable API 报 `paramError-operatorId`
   - 正确值：unionId `x6sfcPCiiJqIDWuQva8GfFQiEiE`（已填入 config.json）
   - 查法：OAPI `topapi/v2/user/get?userid=...` 返回的 `unionid` 字段
2. **日期字段是毫秒时间戳**（CST 午夜），脚本已按 +8 时区解析。
3. **表结构**：渠道明细表 = 店铺/负责人/销售额/推广费/ROI/日期；月目标表 = 店铺/负责人/月目标/渠道（按渠道汇总，猫超 1160万/直播 2531万/天猫 630万 等 28 家店）。
4. **销售额为字符串形态**（如 "11002.41"），空行（只有店铺+日期无销售额）跳过。
5. Windows 控制台中文乱码：`PYTHONIOENCODING=utf-8`，不影响推送内容。

---

## 三、本地运行（Windows + Miniconda）

已安装 Miniconda3（Python 3.14.7，`%USERPROFILE%\miniconda3\python.exe`），纯标准库无需装包：

```bash
cd outputs/channel-daily-robot
"%USERPROFILE%\miniconda3\python.exe" run_today.py --dry              # 干跑看效果
"%USERPROFILE%\miniconda3\python.exe" run_today.py --date 2026-09-02  # 指定日期推送
"%USERPROFILE%\miniconda3\python.exe" run_today.py                    # 默认推送昨日
```

本地定时（Windows 计划任务，任务计划程序 → 创建基本任务 → 每日 09:00）：
```
程序: C:\Users\<用户>\miniconda3\python.exe
参数: run_today.py
起始于: <channel-daily-robot 目录绝对路径>
```

---

## 四、ECS 部署（迁移目标）

```bash
scp -r channel-daily-robot/ root@<ECS公网IP>:/opt/
ssh root@<ECS公网IP>
cd /opt/channel-daily-robot

# 定时任务：每日 09:00 推送昨日日报
crontab -e
0 9 * * * cd /opt/channel-daily-robot && /usr/bin/python3 run_today.py >> logs/cron.log 2>&1
```

config.json 已含全部凭据（AppKey/AppSecret/unionId/webhook），拷贝即用；更安全的做法是改用环境变量 `DINGTALK_APP_SECRET`。

---

## 五、日常运维

| 现象 | 排查 |
|------|------|
| 群里没收到 | 本地看运行输出；ECS 看 logs/cron.log |
| 某渠道显示 -- | 该渠道表当天未登记数据（如私域 9/2 无数据） |
| token 失效报错 | AppSecret 是否被重置；重新复制到 config.json |
| 跨月无数据 | MONTH 变量 + 表名后缀是否切到新月份 |
| paramError-operatorId | 检查 config.json 的 operatorId 是否为 unionId（不是 userId） |

---

## 六、安全提醒

1. `config.json` 含 AppSecret 和 webhook：不要提交到公开仓库、不要截图外发。
2. webhook 泄露等于任何人可向群里发消息。
3. 凭据均为公司侧新授权，不复用旧备份明文（交接包安全原则）。

---

## 附：表清单速查（Base「9月电商渠道日报表」，2026-09-03 探测）

| 表名 | sheetId | 用途 |
|------|---------|------|
| 天猫渠道数据9 | 6AJ3wpa | 明细 |
| 京东渠道数据9 | WCaC6Yx | 明细 |
| 拼多多渠道数据9 | gYQlqjn | 明细 |
| 猫超渠道数据9 | s2wLsNP | 明细 |
| 即时零售渠道数据9 | opo5gl8 | 明细 |
| 直播渠道数据9 | uRjZ8u5 | 明细 |
| 私域渠道数据9 | ViSjPlk | 明细 |
| 渠道销售目标达成率9 | SlMvcWa | 月目标 |
| 各渠道每日总销售 9 | HWNsPi9 | 只有日期列（未用） |

---

---

## 附二：文件说明

```
channel-daily-robot/
|- run_today.py        当前在用：适配真实表结构的完整日报脚本
|- main.py             备用：通用版主入口（字段自动识别方案，未改动）
|- dingtalk_client.py  钉钉API客户端（token/notable/webhook/群消息）
|- channel_report.py   通用版字段识别+聚合+markdown渲染
|- discover_tables.py  表结构发现工具
|- config.json         已填真实凭据（勿外传）
|- config.example.json 配置模板
|- requirements.txt    无第三方依赖（纯标准库）
|- logs/               运行日志
```
