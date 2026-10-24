"""re0 —— 整站 Cloudflare 托管，签到入口藏在右上角头像展开的侧栏里。

实测：
  * 纯 HTTP 直接请求首页 → 403 + Cloudflare 挑战页（Just a moment...），此路不通
  * 登录后在首页点开右上角头像，侧栏第一项是「每日签到」，点完当场弹 toast

本文件自带整条点击流（Browserless BrowserQL 真实交互）：过盾 → （需要时登录）→
点头像 → 点「每日签到」→ 点签到按钮。这条流程只有 re0 用，所以不单独拆模块。

为什么非 BrowserQL 不可：REST 端点没有 ``solve``，过不了 Cloudflare；
``type`` / ``click`` / ``waitForFunction`` 这些也只有 BQL 有。
登录用 ``type`` 真实键盘输入（受控组件只认键盘事件），提交前用 ``waitForFunction``
等 Turnstile token 落进隐藏域——token 没出来就提交等于白发一次登录。

默认按钮文字是「签到」，可用配置项 ``checkin_type`` 覆盖成站点里真实的按钮文案
（该站也可能有「普通签到 / 赌狗签到」这种分叉）。
"""
from __future__ import annotations

import json
import re

from ..base import parse_cookies

# ---- 判定用：账号密码输入框还在不在（还在=停在登录页） ----
JS_LOGIN_CHECK = (
    "(() => { return document.querySelector('input[type=password]') ? 'login' : 'ok'; })()"
)

# ---- 标记登录表单里的账号/密码/提交按钮（只打 id，不点击） ----
JS_MARK_LOGIN = (
    "(() => {"
    "  const vis = (e) => e && e.offsetParent !== null;"
    "  const q = (sels) => { for (const s of sels) { const e = document.querySelector(s);"
    "    if (vis(e)) return e; } return null; };"
    "  const u = q(__USER_SELECTORS__), p = q(__PASS_SELECTORS__);"
    "  if (!u || !p) return '';"
    "  u.setAttribute('id', 'ac-user'); p.setAttribute('id', 'ac-pass');"
    "  const btn = document.querySelector('form button[type=submit]')"
    "    || document.querySelector('button[type=submit]')"
    "    || [...document.querySelectorAll('button, a, [role=button], input[type=submit]')]"
    "         .filter(vis)"
    "         .find((b) => /登录|登入|Sign in|Log in|登陆|提交/i"
    "                       .test(((b.innerText || b.value || '') + '').trim()));"
    "  if (btn) btn.setAttribute('id', 'ac-submit');"
    "  return btn ? 'ready' : 'ready-no-submit';"
    "})()"
)

# ---- Turnstile 闸门：没有 turnstile 直接放行；有就等 token 落进隐藏域 ----
JS_TURNSTILE_READY = (
    "(() => {"
    "  const e = document.querySelector('input[name=cf-turnstile-response]');"
    "  if (!e) return true;"
    "  return ((e.value || '') + '').length > 40;"
    "})()"
)

# ---- 读出钩子记下的内容（拿不到不报错，返回空数组） ----
JS_TOAST_READ = (
    "(() => JSON.stringify((window.__acToasts || []).slice(-10)))()"
)

# ---- 按文字标记元素（只打 id，不点击；返回命中的文字） ----
JS_MARK_TEMPLATE = (
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
    "  node.setAttribute('id', TARGET.id);"
    "  if (TARGET.click) {"
    "    const box = node.closest('button, a, [role=button], [role=menuitem], li') || node;"
    "    box.click();"
    "  }"
    "  return ((node.value || node.textContent || '') + '').replace(/\\s+/g, ' ').trim();"
    "})"
)

# ---- 头像：先按 CSS 选择器标记（re0 的头像是无文字的 span.avatar），再退回文字 ----
JS_MARK_AVATAR_TEMPLATE = (
    "(TARGET => {"
    "  for (const sel of TARGET.selectors) {"
    "    const el = document.querySelector(sel);"
    "    if (el && el.offsetParent !== null) {"
    "      el.setAttribute('id', TARGET.id);"
    "      if (TARGET.click) {"
    "        const box = el.closest('button, a, [role=button], li, [role=menuitem]') || el;"
    "        box.click();"
    "      }"
    "      return 'css:' + sel; }"
    "  }"
    "  const re = new RegExp(TARGET.pattern);"
    "  const nodes = [...document.querySelectorAll("
    "'button, a, [role=button], [role=menuitem], li, span, div, img')];"
    "  const hits = nodes.filter((e) => {"
    "    const t = ((e.value || e.alt || e.textContent || '') + '').replace(/\\s+/g, ' ').trim();"
    "    return t && t.length <= 12 && re.test(t) && e.offsetParent !== null;"
    "  });"
    "  if (!hits.length) return '';"
    "  hits.sort((a, b) => (a.textContent || '').length - (b.textContent || '').length);"
    "  hits[0].setAttribute('id', TARGET.id);"
    "  if (TARGET.click) {"
    "    const box = hits[0].closest('button, a, [role=button]') || hits[0];"
    "    box.click();"
    "  }"
    "  return ((hits[0].textContent || '') + '').trim().slice(0, 20);"
    "})"
)

# ---- 打字自检：只回报长度，绝不回报内容 ----
JS_TYPED_CHECK = (
    "(() => {"
    "  const u = document.querySelector('#ac-user'), p = document.querySelector('#ac-pass');"
    "  return JSON.stringify({u: u ? (u.value || '').length : -1,"
    "                         p: p ? (p.value || '').length : -1});"
    "})()"
)

# ---- toast 钩子：MutationObserver 记账，2~3 秒的浮层也不漏 ----
# 为什么不用"点完立刻读"：toast 存活时间短、渲染时机不定，
# 读早了没出现、读晚了已消失。挂在 DOM 上就不受时机影响。
JS_TOAST_HOOK = (
    "(() => {"
    "  if (window.__acToastHooked) return 'already';"
    "  window.__acToastHooked = 1;"
    "  window.__acToasts = [];"
    # 只认"结果型"提示：抽屉里的「每日签到 / 赌狗签到」这类导航文字不算
    "  const KEY = /签到成功|签到失败|已经签到|已签到|明天再来|获得\\s*\\d+\\s*积分"
    "|积分\\s*[+＋]\\s*\\d+|已领取|领取成功|重复签到|签到过期/;"
    "  const push = (raw) => {"
    "    const t = ((raw || '') + '').replace(/\\s+/g, ' ').trim();"
    "    if (!t || t.length > 80 || !KEY.test(t)) return;"
    "    if (window.__acToasts.includes(t)) return;"
    "    if (window.__acToasts.length >= 30) return;"
    "    window.__acToasts.push(t);"
    "  };"
    "  const scan = (node) => { if (node && node.nodeType === 1) push(node.innerText || node.textContent); };"
    "  new MutationObserver((muts) => {"
    "    for (const m of muts) {"
    "      if (m.type === 'characterData') { scan(m.target.parentElement); continue; }"
    "      m.addedNodes.forEach(scan);"
    "    }"
    "  }).observe(document.body || document.documentElement,"
    "             {childList: true, subtree: true, characterData: true});"
    "  return 'hooked';"
    "})()"
)

# ---- 读取页面正文（限长） ----
JS_READ_TEXT = ("(() => { return ((document.body && document.body.innerText) || '')"
                ".slice(0, 2500); })()")


def _js_call(wrapper: str, arg: dict) -> str:
    """把模板变成自执行调用：``(TARGET => {...})({...})``。"""
    return f"({wrapper})({json.dumps(arg, ensure_ascii=False)})"


def _pick_script(pattern: str, marker_id: str = "ac-target", click: bool = True) -> str:
    """按文字找到元素并打上 id；``click=False`` 时只标记，点击交给 BQL 原生 click。"""
    return _js_call(JS_MARK_TEMPLATE, {"pattern": pattern, "id": marker_id, "click": click})


def _line_with(text: str, pattern: str) -> str:
    """返回第一段命中 pattern 的文字（用来把站点原话摘出来当结果）。"""
    try:
        rx = re.compile(pattern)
    except re.error:
        return ""
    for line in str(text or "").splitlines() or [str(text or "")]:
        if rx.search(line):
            return line.strip()[:80]
    return ""


def _focus_lines(text: str, word: str = "签到", limit: int = 400) -> str:
    """只保留含有关键词的句子。

    页面正文里"获得""成功"这类词到处都是，整段拿去匹配会误报签到成功
    （曾经就这样把"没签到"报成了成功）。只认提到签到的那几行。
    """
    lines = []
    for raw in str(text or "").replace("|", "\n").splitlines():
        line = raw.strip()
        if line and len(line) <= 200 and word in line:
            lines.append(line)
    return " ".join(lines)[:limit]


def _menu_ready_script(patterns: list[str]) -> str:
    """等到菜单项出现再标记：抽屉是异步渲染的，早一步就点空。"""
    pats = json.dumps(list(patterns), ensure_ascii=False)
    return ("() => { const pats = " + pats + ";"
            " const nodes = [...document.querySelectorAll("
            "'button, a, [role=button], [role=menuitem], li')];"
            " return nodes.some((e) => e.offsetParent !== null"
            "   && pats.some((p) => ((e.innerText || '').trim()) === p)); }")


def _avatar_script(pattern: str, selectors: list[str], marker_id: str = "ac-avatar",
                   click: bool = True) -> str:
    return _js_call(JS_MARK_AVATAR_TEMPLATE,
                    {"pattern": pattern, "selectors": list(selectors),
                     "id": marker_id, "click": click})


def _login_fill_script(conf: dict) -> str:
    """返回「标记登录表单」的 JS（只打标记，填值交给 type）。"""
    del conf
    return (JS_MARK_LOGIN
            .replace("__USER_SELECTORS__",
                     json.dumps(["input[name=username]", "input[type=email]",
                                 "input[name=email]", "input[type=text]"]))
            .replace("__PASS_SELECTORS__",
                     json.dumps(["input[name=password]", "input[type=password]"])))


def build_query(with_login: bool, needs_solve: bool, menu_patterns: list[str],
                with_button: bool = True, cookie_mode: bool = False) -> str:
    """拼出 BrowserQL mutation。

    变量必须**全部**被用到（GraphQL 硬性要求），步骤名不能重复。
    顺序：打开目标页 → 过盾 → （登录）→ 点头像 → 点菜单 → 点按钮 → 读页面。
    """
    variables = ["$targetUrl:String!", "$pageWait:Float!", "$evalTimeout:Float!",
                 "$settleWait:Float!", "$menuWait:Float!", "$avatar:String!",
                 "$toastHook:String!", "$toastRead:String!", "$clickTimeout:Float!"]
    steps = []
    if cookie_mode:
        # 有 Cookie 就直接注进浏览器会话，跳过登录表单（省掉 Turnstile 那一关）
        variables.append("$cookieList:[CookieInput]!")
        steps.append('setCk:cookies(cookies:$cookieList){time}')
    steps += [
        'targetGoto:goto(url:$targetUrl,waitUntil:domContentLoaded){status}',
        'targetWait:waitForTimeout(time:$pageWait){time}',
        # 先把 toast 记账器挂上，后面每个动作产生的浮层都不会漏
        'hook:evaluate(content:$toastHook,timeout:$evalTimeout){value}',
    ]
    if needs_solve:
        variables += ["$solveTimeout:Float!", "$solveTimeout2:Float!"]
        steps += [
            'shield:solve(timeout:$solveTimeout){found solved time token}',
            'shieldWait:waitForTimeout(time:$settleWait){time}',
            # 第一次没过就再解一次：Cloudflare 有相当比例是第一次超时、第二次几秒就过
            'shield2:solve(timeout:$solveTimeout2){found solved time}',
            'shieldWait2:waitForTimeout(time:$settleWait){time}',
        ]
    if cookie_mode:
        variables += ["$turnstileFn:String!", "$turnstileWait:Float!", "$verifyJs:String!"]
        steps += [
            # hdhaven 换了网络环境会先甩到 /verify-session：确认 token 到位后点「继续访问」
            'verifyGate:waitForFunction(fn:$turnstileFn,timeout:$turnstileWait,'
            'pollingType:mutation){time}',
            'verifyWarm:waitForTimeout(time:5000){time}',
            'verifyMark:evaluate(content:$verifyJs,timeout:$evalTimeout){value}',
            'verifyClick:click(selector:"#ac-verify",timeout:$clickTimeout){selector time}',
            'verifyWait:waitForTimeout(time:$settleWait){time}',
            # 按钮有时要等 token 落定后才渲染，再给一次机会
            'verifyMark2:evaluate(content:$verifyJs,timeout:$evalTimeout){value}',
            'verifyClick2:click(selector:"#ac-verify",timeout:$clickTimeout){selector time}',
            'verifyWait2:waitForTimeout(time:$settleWait){time}',
            'verifyRevisit:goto(url:$targetUrl,waitUntil:domContentLoaded){status}',
            'verifyRevisitWait:waitForTimeout(time:$pageWait){time}',
        ]
    if with_login:
        variables += ["$loginMark:String!", "$typedCheck:String!", "$loginCheck:String!",
                      "$turnstileFn:String!", "$username:String!", "$password:String!",
                      "$turnstileWait:Float!", "$loginWait:Float!"]
        steps += [
            'loginMark:evaluate(content:$loginMark,timeout:$evalTimeout){value}',
            # 真实键盘输入：React / Next.js 受控组件只认键盘事件
            'typeUser:type(selector:"#ac-user",text:$username,delay:40,timeout:$clickTimeout){text}',
            'typePass:type(selector:"#ac-pass",text:$password,delay:40,timeout:$clickTimeout){text}',
            'typedCheck:evaluate(content:$typedCheck,timeout:$evalTimeout){value}',
            'typeWait:waitForTimeout(time:600){time}',
            # 表单里嵌的 Turnstile 要在**填完表之后**再解一次，
            # 否则用的是页面加载时那个已经过期的 token（hdhaven 就栽在这）
            *(['loginshield:solve(timeout:$solveTimeout){found solved time}',
               'loginshieldWait:waitForTimeout(time:1500){time}'] if needs_solve else []),
            # Turnstile 闸门：token 没落进隐藏域就提交，等于白发一次登录
            'turnstile:waitForFunction(fn:$turnstileFn,timeout:$turnstileWait,'
'pollingType:mutation){time}',
            'clickSubmit:click(selector:"#ac-submit",timeout:$clickTimeout){selector time}',
            'loginSettle:waitForTimeout(time:$loginWait){time}',
            'loginCheck:evaluate(content:$loginCheck,timeout:$evalTimeout){value}',
            # 登录成功后回到目标页（登录页往往不是签到页）
            'revisit:goto(url:$targetUrl,waitUntil:domContentLoaded){status}',
            'revisitWait:waitForTimeout(time:$pageWait){time}',
            'hook2:evaluate(content:$toastHook,timeout:$evalTimeout){value}',
        ]
    steps += [
        'avatar:evaluate(content:$avatar,timeout:$evalTimeout){value}',
        'avatarWait:waitForTimeout(time:$menuWait){time}',
    ]
    # 菜单/按钮：只标记不点，点击交给 BQL 原生 click（真实鼠标事件）
    # 实测 re0 的菜单项用 JS .click() 完全没反应，原生点击才能弹「签到成功」
    if menu_patterns:
        # 变量必须真的被用到：没有菜单时声明它会让整条 mutation 被 GraphQL 拒掉
        variables.append("$menuReadyFn:String!")
        # 先等抽屉把菜单项渲染出来，否则标记和点击都会落空
        steps += [
            'menuReady:waitForFunction(fn:$menuReadyFn,timeout:$menuWait,'
            'pollingType:mutation){time}',
        ]
    for i, _pattern in enumerate(menu_patterns, start=1):
        var = f"$menu{i}"
        variables.append(f"{var}:String!")
        steps += [
            f'menu{i}:evaluate(content:{var},timeout:$evalTimeout){{value}}',
            f'menu{i}Click:click(selector:"#ac-menu{i}",timeout:$clickTimeout){{time}}',
            f'menu{i}Wait:waitForTimeout(time:1200){{time}}',
            f'menu{i}Text:evaluate(content:$textJs,timeout:$evalTimeout){{value}}',
        ]
    if with_button:
        variables.append("$button:String!")
        steps += [
            'button:evaluate(content:$button,timeout:$evalTimeout){value}',
            'buttonClick:click(selector:"#ac-btn",timeout:$clickTimeout){time}',
            'buttonWait:waitForTimeout(time:1500){time}',
            'buttonText:evaluate(content:$textJs,timeout:$evalTimeout){value}',
        ]
    steps += [
        # toast（签到成功 / 你已经签到过了）只显示两三秒，先抢一次再慢慢读
        'toastWait:waitForTimeout(time:900){time}',
        'textEarly:evaluate(content:$textJs,timeout:$evalTimeout){value}',
        'settle:waitForTimeout(time:$settleWait){time}',
        'text:evaluate(content:$textJs,timeout:$evalTimeout){value}',
        # 钩子记账的浮层：比"抓某一刻的正文"可靠得多
        'toasts:evaluate(content:$toastRead,timeout:$evalTimeout){value}',
        'page:html{html}',
    ]
    variables.append("$textJs:String!")
    return "mutation CheckIn(%s) {\n  %s\n}" % (" ".join(variables), "\n  ".join(steps))


def run_click_flow(ctx, conf: dict, url: str, button_text: str,
                   already: str, success: str, site_name: str = "",
                   needs_solve: bool = True) -> tuple[bool, str]:
    """用 Browserless BrowserQL 走一遍签到流程，返回 (ok, msg)。"""
    if not ctx.use_browser:
        return False, "该站点需要浏览器引擎，但运行时没有启用（程序内部问题）"
    if not ctx.has_browser():
        return False, "未配置 Browserless 地址或 Token"

    # 有 Cookie 就注入会话、跳过登录；否则才用账号密码真实登录
    cookie = str(conf.get("cookie") or "").strip()
    cookie_mode = bool(cookie)
    with_login = bool(conf.get("username") and conf.get("password")) and not cookie_mode
    solve_enabled = bool(conf.get("use_solve", needs_solve))
    menus = [str(p) for p in (conf.get("menu_patterns") or ["每日签到", "签到"])]
    avatar_pattern = str(conf.get("avatar_pattern") or "头像|账户|账号|我的|个人中心")
    avatar_selectors = conf.get("avatar_selectors") or [
        "span[class*=avatar]", "[class*=avatar]", "img[class*=avatar]",
        "[class*=userActions] button", "[class*=TopBar] button",
    ]
    with_button = bool(button_text)

    query = build_query(with_login, solve_enabled, menus, with_button,
                        cookie_mode=cookie_mode)
    variables = {
        "targetUrl": url,
        "pageWait": float(conf.get("page_wait") or 2000),
        "evalTimeout": float(conf.get("action_timeout") or 15000),
        "settleWait": float(conf.get("settle_wait") or 2500),
        "menuWait": float(conf.get("menu_wait") or 4000),
        "avatar": _avatar_script(avatar_pattern, list(avatar_selectors)),
        "textJs": JS_READ_TEXT,
        "toastHook": JS_TOAST_HOOK,
        "toastRead": JS_TOAST_READ,
        "clickTimeout": float(conf.get("click_timeout") or 5000),
        **({"menuReadyFn": _menu_ready_script(menus)} if menus else {}),
    }
    if cookie_mode:
        variables["cookieList"] = parse_cookies(cookie, conf.get("cookie_domain") or "")
        variables["turnstileFn"] = JS_TURNSTILE_READY
        variables["turnstileWait"] = float(conf.get("turnstile_timeout") or 20000)
        variables["verifyJs"] = _pick_script(
            str(conf.get("verify_button") or "继续访问"), "ac-verify")
        variables["clickTimeout"] = float(conf.get("click_timeout") or 5000)
    if with_button:
        variables["button"] = _pick_script(button_text, "ac-btn", click=False)
    for i, pattern in enumerate(menus, start=1):
        # click=False：只打 id，真正的点击由 BQL 原生 click 完成
        variables[f"menu{i}"] = _pick_script(pattern, f"ac-menu{i}", click=False)
    if with_login:
        variables["loginMark"] = _login_fill_script(conf)
        variables["typedCheck"] = JS_TYPED_CHECK
        variables["loginCheck"] = JS_LOGIN_CHECK
        variables["turnstileFn"] = JS_TURNSTILE_READY
        variables["loginWait"] = float(conf.get("login_wait") or 6000)
        variables["turnstileWait"] = float(conf.get("turnstile_timeout") or 20000)
        variables["username"] = str(conf.get("username") or "")
        variables["password"] = str(conf.get("password") or "")
    if solve_enabled:
        variables["solveTimeout"] = float(conf.get("solve_timeout") or 30000)
        variables["solveTimeout2"] = float(conf.get("solve_timeout2") or 20000)

    if ctx.logger:
        ctx.logger.info(f"{site_name} BrowserQL："
                        f"{'Cookie 注入' if cookie_mode else ('登录' if with_login else '免登录')}，"
                        f"solve={'开' if solve_enabled else '关'}，真实输入=开")

    data = ctx.bql(query, variables, timeout=max(200, ctx.timeout + 200))
    if data is None:
        # 说清到底是网络/鉴权/额度还是流程超时——别再一律甩给"地址 / Token / 额度"
        return False, (getattr(ctx, "last_error", "") or "Browserless 请求失败（详见日志）")

    def value(name: str) -> str:
        return str(((data.get(name) or {}).get("value")) or "")


    errors = data.get("_errors") or []
    shield = data.get("shield") or {}
    shield2 = data.get("shield2") or {}
    if shield2.get("solved"):
        shield = shield2
    html = str((data.get("page") or {}).get("html") or "")

    if ctx.logger:
        if cookie_mode:
            ctx.logger.info(f"{site_name} 报错={errors[:2]}")
            ctx.logger.info(f"{site_name} Cookie 注入={bool(data.get('setCk'))} "
                            f"验证页按钮={(value('verifyJs') or value('verifyJs2') or '未出现')}"
                            f" 点击={'成功' if (data.get('verifyClick') or {}).get('selector') else '未点'}"
                            f"/{'成功' if (data.get('verifyClick2') or {}).get('selector') else '未点'}")
        if solve_enabled:
            ctx.logger.info(f"{site_name} solve：found={shield.get('found')} "
                            f"solved={shield.get('solved')} time={shield.get('time')}")
        if with_login:
            ctx.logger.info(f"{site_name} 登录：标记={value('loginMark')!r} "
                            f"输入框长度={value('typedCheck') or '?'} "
                            f"提交={'是' if (data.get('clickSubmit') or {}).get('selector') else '否'} "
                            f"状态={value('loginCheck')!r}")

    login_state = value("loginCheck")
    if with_login and login_state == "login":
        detail = f"；GraphQL 报错 {errors[:2]}" if errors else ""
        return False, f"登录失败（提交后仍在登录页：账号密码错、或人机验证没过）{detail}"

    menu_hit = next((value(f"menu{i}") for i in range(1, len(menus) + 1)
                     if value(f"menu{i}")), "")
    button_hit = value("button")
    steps = [f"头像={value('avatar') or '未触达'}",
             f"入口={menu_hit or '未找到'}",
             f"按钮={button_hit or '未找到'}"]

    if shield and solve_enabled and shield.get("found") and not shield.get("solved"):
        return False, f"Cloudflare 挑战未解开（{'；'.join(steps)}）"
    if html and ("just a moment" in html.lower() or "cf-challenge" in html.lower()):
        return False, f"Cloudflare 挑战未通过（{'；'.join(steps)}）"

    # 钩子记账的浮层是"最权威"的证据：它记下了页面出现过的每一句提示
    toasts: list[str] = []
    try:
        toasts = json.loads(value("toasts") or "[]") or []
    except (ValueError, TypeError):
        toasts = []
    toast_text = " ".join(str(t) for t in toasts if t)

    # 再把各时点的页面快照合起来，作为浮层没抓到时（如整页跳转）的兜底
    snapshots = [value("textEarly")]
    snapshots += [value(f"menu{i}Text") for i in range(1, len(menus) + 1)]
    if with_button:
        snapshots.append(value("buttonText"))
    page_text = " ".join(s for s in snapshots if s).strip()
    snapshots.append(toast_text)
    snapshots.append(value("text"))
    text = " ".join(s for s in snapshots if s).strip()
    # 只拿"提到签到的那几行"做判定，避免整页里的「获得」之类误报成功
    focused = _focus_lines(page_text) or page_text[:400]
    if ctx.logger:
        ctx.logger.info(f"{site_name} 捕获到的提示：" +
                        (" | ".join(toasts[-3:]) if toasts else "无"))
        snippet = " ".join(str(text or "").split())[:220]
        ctx.logger.info(f"{site_name} 页面可见文字：{snippet or '（空）'}")

    # 浮层优先：它是站点当场给的答复，原样当结果，不再二次加工
    if toast_text:
        hit = next((t for t in reversed(toasts) if ctx.match_any(t, already)), "")
        if hit:
            # 站点常把「签到失败」当标题、"你已经签到过了"当正文，标题留着会和状态列打架
            return True, re.sub(r"^\s*签到失败\s*[，,：:]?\s*", "", str(hit)).strip()
        hit = next((t for t in reversed(toasts) if ctx.match_any(t, success)), "")
        if hit:
            return True, str(hit)
        # 有提示、但既不是「已签到」也不在成功关键词里——**不能报成功**。
        # 以前这里直接 return True, ""，于是站点弹「签到失败」也会被记成 ✅ 且空白。
        return False, f"站点提示：{toasts[-1]}"

    if ctx.match_any(focused, already):
        # 点了二级按钮后页面翻成「已签到」= 这次签上的
        if button_hit:
            return True, _line_with(focused, already)
        return True, _line_with(focused, already) or "今日已领取"
    if ctx.match_any(focused, success):
        return True, _line_with(focused, success)
    if not (menu_hit or button_hit):
        detail = f"；GraphQL 报错 {errors[:2]}" if errors else ""
        return False, f"没找到签到入口（{'；'.join(steps)}）{detail}"
    return False, f"点了「{button_hit or menu_hit}」但没读到结果（{'；'.join(steps)}）"


class Re0:
    key = "re0"
    name = "re0"
    url = "https://re0.me"
    needs_browser = True
    needs_proxy = True            # 必须走代理才能访问
    engine = "cloud"              # Browserless BrowserQL

    DEFAULT_ALREADY = "已签到|今日已签到|already|明天再来|已打卡|已领取|今日已领"
    DEFAULT_SUCCESS = "签到成功|成功签到|签到完成|领取成功"

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
    extra_defaults = {"checkin_type": "normal"}

    # 点右上角头像（无文字的 span.avatar）展开下拉：「每日签到」只是跳到账户页，
    # 账户页上才是真正的签到按钮 —— 所以是「菜单 → 按钮」两级点击。
    MENUS = {"normal": "每日签到", "normal2": "每日签到", "gamble": "赌狗签到"}

    def signin(self, conf: dict, ctx) -> tuple[bool, str]:
        already = str(conf.get("already") or self.DEFAULT_ALREADY)
        success = str(conf.get("success") or self.DEFAULT_SUCCESS)
        kind = str(conf.get("checkin_type") or "normal")
        menu = self.MENUS.get(kind, "每日签到")
        # 首页右上角头像 → 侧栏「每日签到」，点完当场弹 toast，页面不跳转。
        url = f"{self.url}/"

        conf = dict(conf)
        conf.setdefault("menu_patterns", [menu])
        conf.setdefault("avatar_pattern", "头像|账户|账号|我的")

        return run_click_flow(ctx, conf, url, "", already, success, site_name=self.name,
                              needs_solve=True)
