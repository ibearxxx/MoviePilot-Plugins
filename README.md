# MoviePilot 插件仓库

MoviePilot 第三方插件仓库。

## 插件列表

| 插件 ID | 名称 | 版本 | 说明 |
|---|---|---|---|
| `AutoCheckIn` | 站点自动签到 | 1.0.0 | 多站点每日签到：纯 HTTP 优先，需要过盾/点按钮时用 Browserless 或本地浏览器 |
| `UltraHDBlindBox` | UltraHD幸运盲盒 | 1.0.0 | 每天自动抽 UltraHD 幸运盲盒：默认只抽免费的那 1 次，可配置 1–30 次 |

## 怎么用

MoviePilot → 设置 → 插件 → 插件市场地址，添加本仓库地址：

```
https://github.com/ibearxxx/MoviePilot-Plugins
```

然后在「插件市场」里搜索「站点自动签到」并安装。

## 目录结构

```
.
├── package.v3.json          插件索引（V3）
├── icons/                   插件图标
├── tests/v3/                插件自测（结构 / 配置迁移 / 抽取规则）
│   ├── autocheckin/
│   └── ultrahdblindbox/
└── plugins.v3/
    ├── autocheckin/         站点自动签到源码
    │   ├── __init__.py      插件主类
    │   ├── base.py          共用层（会话 / 代理 / BrowserQL / Cookie 解析）
    │   └── sites/           只放注册表和站点，一站点一模块
    └── ultrahdblindbox/     UltraHD 幸运盲盒源码
        ├── __init__.py      插件主类（调度 / 同日防重 / 记录 / 通知）
        └── blindbox.py      站点接口层（唯一跟站点说话的地方）
```

## 站点自动签到

| 站点 | 引擎 | 登录 | 代理 |
|---|---|---|---|
| re0 | 云端 Browserless | 账号密码（浏览器内真实输入） | 需要 |
| 栖影 | 本地 CloakBrowser | Cookie（走代理 + 浏览器内已登录会话） | 需要 |
| 聚影 | HTTP | 账号密码 | 不需要 |
| 蜗牛4K | HTTP | 账号密码 | 不需要 |

> 说明：Browserless 可用云端（`https://production-sfo.browserless.io` + token），
> 也可以用本机自建容器。

## UltraHD幸运盲盒

每天自动抽 UltraHD 站的「幸运盲盒」。站点规则：一天最多 30 次，第一次免费，之后每次消耗 3000 魔力值。

| 项 | 默认 | 说明 |
|---|---|---|
| 启用插件 | 关 | 需要手动打开 |
| 发送通知 | 开 | 跑完发一条汇总 |
| 站点 | UltraHD | MoviePilot「站点管理」添加站点 |
| 每日抽取次数 | 1 | 范围 1–30；默认只抽免费的那 1 次 |
| 执行周期 | 每天 08:30 | cron，可自己改 |

抽取规则：

1. **同一天不重复抽**——当天计划次数跑满就跳过，cron 抖动或手动再点一次都不会多抽。
2. **失败不自动重试**——抽奖是付费动作，重试等于重复扣魔力值。
3. **中途失败即停**——第 N 次失败就收工，不会把剩余次数抽完再报。

免费额度当天已被手动用掉时：次数设成 1 就跳过不抽；设成 N（>1）则按 N-1 次付费跑，把免费那格让出来。
