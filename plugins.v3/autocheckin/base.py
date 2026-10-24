"""站点模块公用上下文：带 Cookie/代理的 HTTP 会话 + BrowserQL 调用。

站点自己能决定的两件事：
  * ``use_proxy``  —— 该站是否走代理（地址统一取插件全局的默认代理）
  * 是否用浏览器   —— 由站点适配器自己声明（needs_browser），用户不用手选
连 Browserless 服务本身时先直连，失败再自动改用默认代理重试。
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def parse_cookies(raw: str, domain: str = "") -> list[dict]:
    """把 ``a=1; b=2`` 解析成 BrowserQL 的 ``[CookieInput]``。

    浏览器相关的两个站点（re0 云端点击流、栖影本地兜底）都要用它，
    所以放在共用层，站点模块里不再各留一份。
    """
    items: list[dict] = []
    for part in str(raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if not name:
            continue
        item = {"name": name, "value": value, "path": "/"}
        if domain:
            item["domain"] = domain
        items.append(item)
    return items


class SignContext:
    """给站点模块用的小工具。"""

    def __init__(self, conf: Mapping[str, Any], timeout: int = 30,
                 browserless_url: str = "", browserless_token: str = "",
                 logger: Any = None, default_proxy: str = "",
                 default_use_browser: bool = False) -> None:
        self.conf = dict(conf or {})
        self.timeout = timeout
        self.logger = logger
        self.browserless_url = (browserless_url or "").rstrip("/")
        self.browserless_token = browserless_token or ""
        self.default_proxy = (default_proxy or "").strip()

        # 站点开关决定是否走代理，地址统一用全局默认代理
        self.use_proxy = bool(self.conf.get("use_proxy"))
        self.proxy = self.default_proxy if self.use_proxy else ""

        # 是否用浏览器引擎由站点模块决定，不做成表单开关
        self.use_browser = bool(default_use_browser)

        # stealth=true 过 Cloudflare 更稳
        self.browser_stealth = bool(self.conf.get("browser_stealth", True))

        self._session = None
        # 上一次 bql() 失败的真实原因，调用方直接透出
        self.last_error = ""

    # ------------------------------------------------------------------ HTTP
    @property
    def cookie(self) -> str:
        return str(self.conf.get("cookie") or "").strip()

    def route_note(self) -> str:
        """给日志/结果用的路由描述。"""
        return f"代理 {self.proxy}" if self.proxy else "直连"

    def _proxies(self) -> Optional[dict]:
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def session(self):
        """惰性创建 requests 会话，带上 UA、Cookie 和代理。"""
        if self._session is None:
            import requests

            session = requests.Session()
            session.headers.update({
                "User-Agent": DEFAULT_UA,
                "Accept": "text/html,application/json,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            proxies = self._proxies()
            if proxies:
                session.proxies.update(proxies)
            if self.cookie:
                session.headers["Cookie"] = self.cookie
            self._session = session
        return self._session

    def _call(self, method: str, url: str, **kwargs):
        try:
            return self.session().request(method, url, timeout=self.timeout, **kwargs)
        except Exception as err:  # 网络异常一律返回 None，由站点模块决定怎么报
            if self.logger:
                self.logger.warning(f"请求失败 {url}（{self.route_note()}）: {err}")
            return None

    def get(self, url: str, **kwargs):
        return self._call("GET", url, allow_redirects=True, **kwargs)

    def post(self, url: str, data=None, json_body=None, headers=None, **kwargs):
        return self._call("POST", url, data=data, json=json_body, headers=headers,
                          allow_redirects=True, **kwargs)

    def page(self, url: str) -> str:
        response = self.get(url)
        return response.text if response is not None and response.status_code == 200 else ""

    def has_browser(self) -> bool:
        return bool(self.browserless_url)

    def _bql_endpoint(self) -> str:
        """BrowserQL 端点：云端是 /stealth/bql，自建可直接填到 /bql 或 /stealth。"""
        base = self.browserless_url.rstrip("/")
        params = []
        if self.browserless_token:
            params.append(f"token={self.browserless_token}")
        # stealth=true 让云端开启反检测，过 Cloudflare 效果明显好于默认
        if self.browser_stealth:
            params.append("stealth=true")
        query = ("?" + "&".join(params)) if params else ""
        if base.endswith("/bql"):
            return f"{base}{query}"
        if base.endswith("/stealth"):
            return f"{base}/bql{query}"
        return f"{base}/stealth/bql{query}"

    def bql(self, query: str, variables: dict, timeout: int = 240,
            operation: str = "CheckIn") -> Optional[dict]:
        """调用 Browserless BrowserQL（GraphQL），返回响应里的 data 字典。

        BrowserQL 是过 Cloudflare / 点按钮的正路：``solve`` 能在 stealth 浏览器里
        等挑战自己跑完并解掉，而 REST 的 /content 只能干等。参考 ptsitesignin 的实现。

        返回 None 表示请求失败（网络 / 鉴权 / 额度），调用方按失败处理；
        GraphQL 层的部分错误放在返回值里的 ``_errors``。
        """
        if not self.use_browser:
            if self.logger:
                self.logger.warning("该站点未开启 Browserless")
            return None
        if not self.browserless_url:
            if self.logger:
                self.logger.warning("未配置 Browserless 地址")
            return None

        import requests

        endpoint = self._bql_endpoint()
        payload = {"query": query, "operationName": operation, "variables": variables}

        # 连 Browserless 服务本身：先直连，网络不通再用默认代理重试一次；
        # 没有配默认代理就明确报错，别让调用方以为是用量或鉴权问题。
        self.last_error = ""
        resp = None
        try:
            resp = requests.post(endpoint, json=payload, timeout=timeout)
        except Exception as err:
            proxies = self._proxies()
            if not proxies:
                reason = ("该站点「走代理」=关，按设置直连" if not self.use_proxy
                          else "未配置默认代理")
                if self.logger:
                    self.logger.warning(f"Browserless 直连失败（{err}）；{reason}")
                self.last_error = f"连不上 Browserless（{reason}）：{err}"
                return None
            if self.logger:
                self.logger.info("Browserless 直连失败，改用默认代理重试")
            try:
                resp = requests.post(endpoint, json=payload, timeout=timeout, proxies=proxies)
            except Exception as err2:
                if self.logger:
                    self.logger.warning(f"Browserless 走代理仍失败：{err2}")
                self.last_error = f"连不上 Browserless（直连和代理都失败）：{err2}"
                return None

        if resp.status_code in (401, 403):
            if self.logger:
                self.logger.warning(f"Browserless 鉴权失败（HTTP {resp.status_code}），检查 Token")
            self.last_error = f"鉴权失败（HTTP {resp.status_code}），检查 Token"
            return None
        if resp.status_code == 429:
            if self.logger:
                self.logger.warning("Browserless 额度不足或请求过频（HTTP 429）")
            self.last_error = "额度不足或请求过频（HTTP 429）"
            return None
        if resp.status_code == 408:
            # Browserless 对单条 mutation 有总时长预算（约 120 秒），超了就是这个码。
            # 以前这里报的是"地址/Token/额度"，把人往完全错误的方向带。
            if self.logger:
                self.logger.warning("Browserless BQL 408：整条 mutation 超时——流程太长，不是 token/额度问题")
            self.last_error = "整条流程超过 Browserless 的时长预算（约 120 秒），需要精简步骤"
            return None
        if resp.status_code != 200:
            if self.logger:
                self.logger.warning(
                    f"Browserless BQL 返回 {resp.status_code}：{(resp.text or '')[:300]}")
            self.last_error = f"BQL 返回 HTTP {resp.status_code}：{(resp.text or '')[:120]}"
            return None

        try:
            body = resp.json()
        except Exception:
            if self.logger:
                self.logger.warning(f"Browserless BQL 响应不是 JSON：{(resp.text or '')[:200]}")
            return None

        errors = body.get("errors") or []
        data = body.get("data")
        if not isinstance(data, dict):
            data = {}
        if errors:
            messages = [str((e or {}).get("message") or "")[:200] for e in errors][:3]
            data["_errors"] = messages
            if not data:
                if self.logger:
                    self.logger.warning(f"Browserless BQL 整体失败：{messages}")
                return None
            if self.logger:
                self.logger.info(f"Browserless BQL 部分步骤报错（继续按页面判定）：{messages}")
        return data


    # ---------------------------------------------------------------- 工具
    @staticmethod
    def match_any(text: str, patterns: str) -> Optional[str]:
        """``patterns`` 用 ``|`` 分隔，命中任一即返回该模式。"""
        import re

        for pattern in [p for p in (patterns or "").split("|") if p.strip()]:
            found = re.search(pattern, text or "")
            if found:
                return found.group(0)
        return None
