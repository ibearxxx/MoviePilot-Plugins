# 站点自动签到（AutoCheckIn）— MoviePilot V3 插件

给普通站点做每日自动签到：一个站点一个模块，能用纯 HTTP 就不开浏览器。

## 支持站点

| 站点 | 代理 | 引擎 | 登录方式 | 状态 |
|---|---|---|---|---|
| re0 | 必须 | 云端 Browserless（含 solve） | 账号 + 密码（浏览器内真实输入） | ✅ 实测签到成功 |
| 栖影 | 必须 | 本地 CloakBrowser | **仅 Cookie**（注入已登录会话，无云端登录） | ⚠️ 需自备 Cookie |
| 聚影 | 不用 | 纯 HTTP | 账号 + 密码（自动登录换 token） | ✅ 实测签到成功 |
| 蜗牛4K | 不用 | 纯 HTTP | 账号 + 密码（自动登录） | ✅ 实测签到成功 |

引擎由站点自己决定，用户不用选；只有「是否通过代理」这一个网络开关交给用户。

## 配置

全局：启用 / 通知 / 立即运行一次、cron、失败重试次数、HTTP 超时、
Browserless 地址与 Token、默认代理。

每个站点：是否通过代理、签到类型（re0/栖影 普通/赌狗，聚影 普通/幸运）；
账号密码（re0、聚影、蜗牛4K）或 Cookie（栖影，必填）。

连 Browserless 服务本身时**先直连，失败再自动改用默认代理重试**；两者都不通会明确报
「请配置代理」。

## 判定与结果列

状态列只有两种：`签到成功` / `签到失败`。结果列放站点当场给出的内容：

| 站点 | 结果列示例 |
|---|---|
| re0 | `签到成功，获得 10 积分` |
| 栖影 | `今日已领取，积分：95 → 100` |
| 聚影 | `累计签到 100 天，+5积分` |
| 蜗牛4K | `获得 10 积分` |

站点没给出可用内容时结果列留空（页面显示 `—`）。

## 手动触发

- 远程命令：`/autocheckin`
- HTTP API：`POST /api/v1/plugin/AutoCheckIn/autocheckin/run`

## 目录结构

```
autocheckin/
├── __init__.py          插件主类：表单、详情页、命令、API、定时服务、通知汇总
├── base.py              唯一共用层：SignContext（HTTP 会话 / 代理 / BrowserQL）+ parse_cookies
├── sites/               只放 __init__.py（注册表）和各个站点，一个站点一个文件
│   ├── __init__.py      站点注册表 SITES
│   ├── wn4k.py          蜗牛4K —— 纯 HTTP
│   ├── jying.py         聚影 —— 纯 HTTP
│   ├── hdhaven.py       栖影 —— 仅 Cookie 注入 + 本地浏览器点击
│   └── re0.py           re0 —— Browserless 点击流（过盾 → 登录 → 点头像 → 点菜单 → 点按钮）
└── README.md
```

新增站点：在 `sites/` 放一个模块，定义 `key / name / url / needs_proxy / needs_browser /
engine / signin(conf, ctx)`，然后在 `sites/__init__.py` 的 `SITES` 里登记即可，表单和详情页会自动多一项。
`engine` 只影响日志里标的引擎名：本地浏览器写 `"local"`，云端 Browserless 写 `"cloud"`，纯 HTTP 不用写。
新站点如果也要「过盾 + 点按钮」，直接把同目录某个站的流程复制进自己的文件改选择器，
不要去改别人的文件。

## 已知限制

- **栖影只走 Cookie**：站点把会话绑在网络环境上，出口 IP 变了或会话老化就会要求重新验证，
  这时插件会报「需要重新验证（出口 IP 变了或 Cookie 过期）」，需要重新登录复制一次 Cookie。
- 栖影取 Cookie 与插件执行时**必须同一个出口 IP**：用浏览器走 NAS 代理
  （`http://127.0.0.1:7890` 这种你就填你自己的代理地址）登录后再复制 Cookie。
