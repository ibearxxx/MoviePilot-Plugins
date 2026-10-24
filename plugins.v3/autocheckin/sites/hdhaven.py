"""栖影 —— Next.js 站点，走「固定出口 IP + 注入 Cookie + 本地浏览器」。

站点现状（2026-10-20 实测）：
  * 未登录访问 /me → ``307`` 跳 ``/login?next=%2Fme``
  * 登录页本身不设防，但**登录接口强制人机验证**：
    ``POST /api/auth/login`` 不带令牌直接回 ``{"statusCode":400,"message":"请先完成人机验证"}``，
    且它的 Turnstile 是按需渲染的，云端 Browserless 靠 ``solve`` 只能碰运气（单次成功率约三成，
    而且即使登上，点签到按钮也不产生任何请求）。**这条路已经废弃并删除。**
  * 签到入口：/me 页面上「立即签到」和「赌狗签到」两个按钮（同属 .points-checkin-actions）

现在的做法：**用你自己浏览器的会话 Cookie，走和登录时相同的出口 IP（NAS 代理）**，
在宿主自带的 CloakBrowser 里注入 Cookie 后直接点按钮。

  * Cookie 在插件配置的「栖影 Cookie」里，由用户自己登录后复制；
  * 出口 IP 必须和拿 Cookie 时一致，否则站点会把会话判为「网络环境变化」并要求重新验证；
  * 站点判定会话老化或换 IP 后，Cookie 会失效，插件会明确报「Cookie 失效」，
    需要重新登录复制一次——这是这条路的固有成本，不是 bug。

浏览器用完必须关掉（run_local_flow 里有 try/finally），否则会漏一个 Chromium 在后台。
"""
from __future__ import annotations

import json
import re
import time

from ..base import parse_cookies

_POINT_PATTERNS = (r"可用积分[：:\s]*(\d+)", r"我的积分[：:\s]*(\d+)",
                   r"积分[：:\s]*(\d+)", r"(\d+)\s*积分")


def _extract_points(text: str) -> int | None:
    """从页面文字里抠出积分数字，抠不到返回 None。"""
    for pattern in _POINT_PATTERNS:
        match = re.search(pattern, str(text or ""))
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                continue
    return None


JS_CLICK_BY_TEXT = (
    "(TARGET => {"
    "  const re = new RegExp(TARGET.pattern);"
    "  const nodes = [...document.querySelectorAll("
    "'button, a, [role=button], [role=menuitem], [role=tab], li, span, div')];"
    "  const hits = nodes.filter((e) => {"
    "    const t = ((e.value || e.textContent || '') + '').replace(/\\s+/g, ' ').trim();"
    "    return t && t.length <= 20 && re.test(t) && e.offsetParent !== null;"
    "  });"
    "  if (!hits.length) return '';"
    "  hits.sort((a, b) => (a.textContent || '').length - (b.textContent || '').length);"
    "  const node = hits[0];"
    "  const box = node.closest('button, a, [role=button]') || node;"
    "  box.click();"
    "  return ((node.value || node.textContent || '') + '').replace(/\\s+/g, ' ').trim();"
    "})"
)


JS_CLICK_AVATAR = (
    "(TARGET => {"
    "  for (const sel of TARGET.selectors || []) {"
    "    const el = document.querySelector(sel);"
    "    if (el && el.offsetParent !== null) {"
    "      const box = el.closest('button, a, [role=button], li, [role=menuitem]') || el;"
    "      box.click();"
    "      return 'css:' + sel;"
    "    }"
    "  }"
    "  const re = new RegExp(TARGET.pattern);"
    "  const nodes = [...document.querySelectorAll("
    "'button, a, [role=button], [role=menuitem], li, span, div, img')];"
    "  const hits = nodes.filter((e) => {"
    "    const t = ((e.alt || e.textContent || '') + '').replace(/\\s+/g, ' ').trim();"
    "    return t && t.length <= 12 && re.test(t) && e.offsetParent !== null;"
    "  });"
    "  if (!hits.length) return '';"
    "  hits.sort((a, b) => (a.textContent || '').length - (b.textContent || '').length);"
    "  const box = hits[0].closest('button, a, [role=button]') || hits[0];"
    "  box.click();"
    "  return ((hits[0].textContent || '') + '').trim().slice(0, 20);"
    "})"
)


def _click_by_text(page, pattern: str) -> str:
    try:
        return page.evaluate(f"({JS_CLICK_BY_TEXT})({json.dumps({'pattern': pattern}, ensure_ascii=False)})") or ""
    except Exception:
        return ""


def _click_avatar(page, pattern: str, selectors: list[str]) -> str:
    try:
        arg = {"pattern": pattern, "selectors": list(selectors or [])}
        return page.evaluate(f"({JS_CLICK_AVATAR})({json.dumps(arg, ensure_ascii=False)})") or ""
    except Exception:
        return ""


def _points_note(before, after) -> str:
    """结果列的积分写法：签到前 → 签到后。"""
    if before is not None and after is not None:
        return f"积分：{before} → {after}"
    if after is not None:
        return f"积分：{after}"
    if before is not None:
        return f"积分：{before} → 未读到"
    return ""


def run_local_flow(ctx, conf: dict, url: str, button_text: str,
                   already: str, success: str, site_name: str = "",
                   menus: list[str] | None = None) -> tuple[bool, str]:
    """用本地 CloakBrowser 走一遍签到流程，返回 (ok, msg)。"""
    try:
        from app.sdk.browser import launch_browser_context
    except Exception:
        return False, "宿主不支持本地浏览器（app.sdk.browser 不可用）"

    proxy = (str(getattr(ctx, "proxy", "") or "").strip()
             or str(conf.get("proxy") or "").strip())
    kwargs: dict = {"headless": True}
    if proxy:
        kwargs["proxy"] = {"server": proxy}

    browser = None
    api_hits: list[tuple[str, str, int, str]] = []
    try:
        browser = launch_browser_context(**kwargs)
        cookie = str(conf.get("cookie") or "").strip()
        if cookie:
            try:
                browser.add_cookies(parse_cookies(cookie, conf.get("cookie_domain") or ""))
            except Exception as err:
                if ctx.logger:
                    ctx.logger.warning(f"{site_name} Cookie 注入失败：{err}")
        page = browser.new_page()
        page.set_default_timeout(float(conf.get("action_timeout") or 20000))

        def on_response(resp):
            try:
                if "/api/" not in resp.url:
                    return
                body = ""
                if "json" in (resp.headers.get("content-type") or ""):
                    try:
                        body = resp.text()[:400]
                    except Exception:
                        body = ""
                api_hits.append((resp.request.method, resp.url.split("//")[-1][:120],
                                 resp.status, body))
            except Exception:
                pass

        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(float(conf.get("page_wait") or 3))
        points_before = _extract_points(page.inner_text("body") or "")

        avatar_pattern = str(conf.get("avatar_pattern") or "头像|账户|账号|我的|个人中心")
        avatar_selectors = conf.get("avatar_selectors") or [
            "span[class*=avatar]", "[class*=avatar]", "img[class*=avatar]",
            "[class*=userActions] button", "[class*=TopBar] button",
        ]
        steps: list[str] = []

        avatar_hit = _click_avatar(page, avatar_pattern, avatar_selectors)
        steps.append(f"头像={avatar_hit or '未触达'}")
        time.sleep(2.5)

        menu_hit = ""
        for pattern in (menus or ["每日签到", "签到"]):
            menu_hit = _click_by_text(page, pattern)
            if menu_hit:
                break
        steps.append(f"入口={menu_hit or '未找到'}")
        time.sleep(2.5)

        button_hit = ""
        if button_text:
            button_hit = _click_by_text(page, button_text)
            steps.append(f"按钮={button_hit or '未找到'}")

        # toast 只活两三秒，先抢一次
        time.sleep(1.2)
        text_early = page.inner_text("body") or ""
        # 站点点完不会自己刷新积分：重新拉一次页面，才读得到服务端的新值
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            time.sleep(float(conf.get("page_wait") or 3))
        except Exception as err:
            if ctx.logger:
                ctx.logger.warning(f"{site_name} 签到后刷新页面失败：{err}")
        text_late = page.inner_text("body") or ""
        points_after = _extract_points(text_late) or _extract_points(text_early)
    except Exception as err:
        return False, f"本地浏览器异常：{type(err).__name__}: {str(err)[:120]}"
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

    if ctx.logger:
        ctx.logger.info(f"{site_name} 本地浏览器：{ctx.route_note()}；{'；'.join(steps)}")
        api_note = "; ".join(f"{m} {u} -> {s}" for m, u, s, _b in api_hits[-6:])
        if api_note:
            ctx.logger.info(f"{site_name} 接口：{api_note}")
        else:
            ctx.logger.info(f"{site_name} 接口：本轮没抓到 /api/ 请求")
        # 签到接口的响应体是判定依据之一，原样留一条
        for _m, _u, _s, _b in api_hits:
            if "checkin" in _u and _b:
                ctx.logger.info(f"{site_name} 签到响应：{_s} {_b[:200]}")
        if points_before is not None or points_after is not None:
            ctx.logger.info(f"{site_name} 积分：{points_before} → {points_after}")
        snippet = " ".join(str(text_early).split())[:200]
        ctx.logger.info(f"{site_name} 页面可见文字：{snippet or '（空）'}")

    api_text = " ".join(b for _m, _u, _s, b in api_hits if b)
    # 判定只认**可见文字**：SPA 的隐藏菜单/注释/JS chunk 里到处是「已签到」字样，
    # 把整页 HTML 拼进来等于给自己埋误报。
    text = f"{text_early} {text_late} {api_text}"

    # hdhaven 换了网络环境会把会话作废、要求重新验证
    # （"暂时无法验证登录状态…你的登录会话已保留" 是它最常抛的一句，漏掉就会被误报成「没找到签到入口」）
    if ("确认是你本人" in text or "verify-session" in text or "网络环境发生了变化" in text
            or "暂时无法验证登录状态" in text):
        return False, ("需要重新验证（出口 IP 变了或 Cookie 过期）：请用 NAS 代理登录一次站点、"
                       "重新复制 Cookie 到插件里")

    # 积分变化是最硬的证据：站点点完只写"今日已领取"，看不出到底动没动
    moved = (points_before is not None and points_after is not None
             and points_after != points_before)
    note = _points_note(points_before, points_after)
    tail = f"，{note}" if note else ""
    if moved:
        return True, f"今日已领取{tail}"

    if ctx.match_any(text, already):
        # 只有「点了签到按钮」才算这次签上的；单纯点菜单只是跳转，不能算成功
        return True, f"今日已领取{tail}"
    if ctx.match_any(text, success):
        return True, f"今日已领取{tail}"
    if not (menu_hit or button_hit):
        return False, f"没找到签到入口（{'；'.join(steps)}）"
    return False, f"点了「{button_hit or menu_hit}」但没读到结果（{'；'.join(steps)}）"


class Hdhaven:
    key = "hdhaven"
    name = "栖影"
    url = "https://hdhaven.com"
    needs_browser = True          # 必须用浏览器点按钮
    needs_proxy = True            # 必须走代理才能访问
    engine = "local"              # 用宿主自带的 CloakBrowser，不走云端

    DEFAULT_ALREADY = "已签到|今日已签到|今日已领取|已经签到|已领取|明天再来|已打卡"
    DEFAULT_SUCCESS = "签到成功|领取成功|签到完成"

    extra_config = {
        "checkin_type": {
            "component": "VSelect",
            "props": {
                "model": "unused",
                "label": "签到类型",
                "hint": "当天二选一，不可更改",
                "items": [
                    {"title": "普通签到", "value": "normal"},
                    {"title": "赌狗签到", "value": "gamble"},
                ],
                "clearable": False,
            },
        },
    }
    # 只认 Cookie：表单据此隐藏「账号/密码」两个输入框
    auth = "cookie"
    extra_defaults = {"checkin_type": "normal"}

    def signin(self, conf: dict, ctx) -> tuple[bool, str]:
        """注入 Cookie → 本地浏览器打开 /me → 点签到按钮 → 读结果。

        **只走这一条路**：没有 Cookie 就直接报错，不猜、不倒退回云端。
        """
        already = str(conf.get("already") or self.DEFAULT_ALREADY)
        success = str(conf.get("success") or self.DEFAULT_SUCCESS)
        kind = str(conf.get("checkin_type") or "normal")
        # /me 上两个按钮分别是「立即签到」和「赌狗签到」
        button = "赌狗签到" if kind == "gamble" else "立即签到"
        me = f"{self.url}/me"

        conf = dict(conf)
        cookie = str(conf.get("cookie") or "").strip()
        if not cookie:
            return False, ("栖影只能靠 Cookie 登录：请在插件里填「栖影 Cookie」"
                           "（用浏览器走插件里配置的代理登录后再复制）")
        # 站点把会话绑在网络环境上：出口 IP 和拿 Cookie 时不一致，会被要求重新人机验证
        conf.setdefault("cookie_domain", ".hdhaven.com")

        ok, msg = run_local_flow(ctx, conf, me, button, already, success,
                                 site_name=self.name,
                                 menus=["每日签到", "签到"])
        return (True, msg) if ok else (False, msg)
