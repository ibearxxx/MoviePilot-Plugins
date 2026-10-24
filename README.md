# MoviePilot 插件仓库

MoviePilot 第三方插件仓库。

## 插件列表

| 插件 ID | 名称 | 版本 | 说明 |
|---|---|---|---|
| `AutoCheckIn` | 站点自动签到 | 1.0.0 | 多站点每日签到：纯 HTTP 优先，需要过盾/点按钮时用 Browserless 或本地浏览器 |

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
├── tests/v3/autocheckin/    插件自测（结构 / 配置迁移）
└── plugins.v3/
    └── autocheckin/         站点自动签到源码
        ├── __init__.py      插件主类
        ├── base.py          共用层（会话 / 代理 / BrowserQL / Cookie 解析）
        └── sites/           只放注册表和站点，一站点一模块
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
