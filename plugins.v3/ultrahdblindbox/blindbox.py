"""UltraHD 幸运盲盒 —— 站点接口层（唯一跟站点说话的地方）。

协议是从站点真实页面上抠出来的（2026-10-29 实测；NexusPHP 上游到 1.10.3 都没有盲盒
相关实现，这是 UltraHD 自己加的模块）：

    读状态   GET  /                                         → 盲盒面板直接渲染在首页 HTML 里
    抽一次   POST /plugins/nexusphp-blindbox/api.php?action=draw
             body: {"is_free": true|false}
             回包: {success, prize:{name,value,extra_message?},
                    new_bonus, remaining_draws, free_remaining, daily_limit}
    查历史   GET  /plugins/nexusphp-blindbox/api.php?action=history
             回包: {success, history:[{prize_name,prize_value,is_free,created_at}]}

关键点：**没有单独的「状态接口」**。当前魔力值、每次消耗、今日剩余、免费次数这四个数
是服务端渲染进首页 HTML 的（`#userBonus` / `#drawCost` / `今日剩余: <span>N</span>` /
`#freeRemaining`），所以 `state()` 解析首页，不额外打 API。

本文件只放「怎么跟站点说话」，插件主逻辑（调度、同日防重、记录、通知）在
``__init__.py`` 里，两者只靠这份契约耦合：

    Client.state()          -> BoxState(free_left, total_left, magic, cost)
    Client.draw(free: bool) -> DrawResult(ok, reward, magic_after, raw)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# 站点定价兜底值：首页 `#drawCost` 读不到时用它，真实值以页面为准。
COST_PER_DRAW = 3000

BOX_API = "/plugins/nexusphp-blindbox/api.php"

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


class BlindBoxError(Exception):
    """接口层可预期的失败：Cookie 失效、接口拒绝、回包看不懂。

    插件主流程把它当成「确定失败」——不重试（抽奖是付费动作，重试等于重复扣魔力值）。
    """


@dataclass
class BoxState:
    """抽之前读到的盒子状态。"""

    free_left: int = 0             # 今日还剩几次免费
    total_left: int = 0            # 今日还剩几次可抽（含免费）
    magic: Optional[float] = None  # 当前魔力值，读不到给 None
    cost: int = 0                  # 单次付费消耗的魔力值，读不到按 COST_PER_DRAW


@dataclass
class DrawResult:
    """一次抽取的结果。"""

    ok: bool = False
    reward: str = ""               # 站点原样的奖励文案，例如「10 魔力值」「15 GB上传量」
    magic_after: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ 解析（纯函数）
def _first(html: str, pattern: str) -> Optional[str]:
    """按正则取第一个捕获组，取不到返回 None。"""
    match = re.search(pattern, str(html or ""))
    return match.group(1).strip() if match else None


def _to_int(raw: Optional[str]) -> Optional[int]:
    """把「1,234」「3000.0」这类文本转成 int，转不动返回 None。

    先按数字解析再取整，**不能直接删非数字字符**：`#drawCost` 渲染成「3,000.0」
    时删字符会得到 30000，单价一次性膨胀十倍，汇总里的「消耗 N 魔力值」跟着错。
    """
    if raw is None:
        return None
    text = str(raw).replace(",", "").replace("，", "").strip()
    try:
        return int(float(text))
    except (TypeError, ValueError):
        digits = re.sub(r"[^0-9]", "", text)
        return int(digits) if digits else None


def _to_float(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).replace(",", "").strip()
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def parse_state(html: str, site_name: str = "") -> BoxState:
    """从首页 HTML 里读盲盒面板的四个数。

    面板是服务端渲染的；读不到就说明 Cookie 失效或站点改版，直接抛错，
    不要「猜一个默认值」继续抽——那是拿用户的魔力值赌。
    """
    text = str(html or "")
    free_raw = _first(text, r"id=['\"]freeRemaining['\"][^>]*>\s*([0-9]+)")
    total_raw = _first(text, r"今日剩余:\s*<span>\s*([0-9]+)")
    cost_raw = _first(text, r"id=['\"]drawCost['\"][^>]*>\s*([0-9,.]+)")
    magic_raw = _first(text, r"id=['\"]userBonus['\"][^>]*>\s*([0-9,.]+)")
    if free_raw is None and total_raw is None:
        hint = f"（站点：{site_name}）" if site_name else ""
        raise BlindBoxError(f"首页里没有找到盲盒面板{hint}：Cookie 可能已失效，或站点改了页面结构")
    return BoxState(
        free_left=_to_int(free_raw) or 0,
        total_left=_to_int(total_raw) or 0,
        magic=_to_float(magic_raw),
        cost=_to_int(cost_raw) or 0,
    )


def parse_draw(data: Any) -> DrawResult:
    """解析 `action=draw` 的回包。

    成功：``{success: true, prize: {name, value}, new_bonus, remaining_draws, ...}``
    失败：``{success: false, message: "..."}``
    """
    if not isinstance(data, dict):
        return DrawResult(ok=False, reward=f"回包看不懂：{str(data)[:80]}")
    if not data.get("success"):
        message = str(data.get("message") or data.get("msg") or "站点没有说明原因")
        return DrawResult(ok=False, reward=message, raw=data)

    raw_prize = data.get("prize")
    prize: Dict[str, Any] = raw_prize if isinstance(raw_prize, dict) else {}
    value = str(prize.get("value") or "").strip()
    name = str(prize.get("name") or "").strip()
    extra = str(prize.get("extra_message") or "").strip()
    reward = value or name or "未知奖励"
    if extra and extra not in reward:
        reward = f"{reward}（{extra}）"
    return DrawResult(ok=True, reward=reward,
                      magic_after=_to_float(data.get("new_bonus")), raw=data)


# ------------------------------------------------------------------ HTTP 客户端
class Client:
    """UltraHD 盲盒的 HTTP 客户端。

    凭据全部来自 MoviePilot 的「站点管理」（site 表），这里不存也不写回任何 Cookie。
    """

    def __init__(self, site: Any, timeout: int = 30, proxy: str = "",
                 logger: Any = None) -> None:
        self.site = site
        self.name = str(getattr(site, "name", "") or "")
        self.url = str(getattr(site, "url", "") or "").rstrip("/")
        self.timeout = max(5, int(timeout or 30))
        self.proxy = str(proxy or "").strip()
        self.logger = logger

    # ------------------------------------------------------------------ 基础
    def _log(self, level: str, message: str) -> None:
        if not self.logger:
            return
        try:
            getattr(self.logger, level)(f"{self.name} {message}")
        except Exception:
            pass

    def _session(self):
        """建一个带站点 Cookie / UA 的会话。

        只读站点管理里的值，不写回：Cookie 失效时由用户在「站点管理」里更新。
        """
        import requests  # 宿主自带，站点模块不做额外依赖

        cookie = str(getattr(self.site, "cookie", "") or "").strip()
        if not cookie:
            raise BlindBoxError(f"站点「{self.name}」在站点管理里没有 Cookie，请先配置")
        session = requests.Session()
        session.headers.update({
            "User-Agent": str(getattr(self.site, "ua", "") or DEFAULT_UA),
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": f"{self.url}/",
        })
        session.cookies.update(_parse_cookies(cookie))
        if self.proxy:
            session.proxies.update({"http": self.proxy, "https": self.proxy})
        return session

    # ---------------------------------------------------------------- 状态
    def state(self) -> BoxState:
        """读盒子状态：还剩几次免费、今天还能抽几次、当前魔力值、单次消耗。

        没有独立的状态接口 —— 盲盒面板是服务端渲染在首页上的，所以解析首页 HTML。
        """
        session = self._session()
        try:
            resp = session.get(f"{self.url}/", timeout=self.timeout,
                               headers={"Accept": "text/html,application/xhtml+xml"})
        except Exception as err:
            raise BlindBoxError(f"打开首页失败：{type(err).__name__}: {str(err)[:120]}")
        if resp.status_code in (401, 403):
            raise BlindBoxError(f"Cookie 失效或被拒（HTTP {resp.status_code}），"
                                f"请到「站点管理」更新「{self.name}」的 Cookie")
        if resp.status_code >= 400:
            raise BlindBoxError(f"打开首页失败：HTTP {resp.status_code}")
        state = parse_state(resp.text, site_name=self.name)
        self._log("info", f"状态：免费 {state.free_left} / 今日剩余 {state.total_left} 次"
                          f" / 魔力值 {state.magic} / 每次 {state.cost}")
        return state

    # ---------------------------------------------------------------- 抽取
    def draw(self, free: bool) -> DrawResult:
        """抽一次。``free=True`` 走免费额度，``False`` 走付费（消耗魔力值）。"""
        session = self._session()
        url = f"{self.url}{BOX_API}?action=draw"
        try:
            resp = session.post(url, json={"is_free": bool(free)}, timeout=self.timeout,
                                headers={"Content-Type": "application/json",
                                         "Accept": "*/*",
                                         "X-Requested-With": "XMLHttpRequest",
                                         "Origin": self.url})
        except Exception as err:
            raise BlindBoxError(f"抽奖请求失败：{type(err).__name__}: {str(err)[:120]}")
        if resp.status_code in (401, 403):
            raise BlindBoxError(f"Cookie 失效或被拒（HTTP {resp.status_code}），"
                                f"请到「站点管理」更新「{self.name}」的 Cookie")
        if resp.status_code >= 400:
            raise BlindBoxError(f"抽奖请求失败：HTTP {resp.status_code}：{resp.text[:120]}")
        try:
            data = resp.json()
        except Exception:
            raise BlindBoxError(f"抽奖回包不是 JSON：{resp.text[:120]}")
        self._log("info", f"draw(is_free={str(bool(free)).lower()}) -> "
                          f"{json.dumps(data, ensure_ascii=False)[:240]}")
        return parse_draw(data)


def _parse_cookies(raw: str) -> Dict[str, str]:
    """把站点管理里的 Cookie 串解析成字典。"""
    jar: Dict[str, str] = {}
    for part in str(raw or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            jar[key] = value.strip()
    return jar
