"""不安装就验证 AutoCheckIn：规范符合性 + 配置解析 + 代理策略。

在 MoviePilot 容器里跑：
    docker cp plugins.v3/autocheckin moviepilot-v3:/tmp/AutoCheckIn
    docker cp tests/v3/autocheckin/test_autocheckin.py moviepilot-v3:/tmp/verify.py
    docker exec moviepilot-v3 python /tmp/verify.py
"""
import logging
import sys
import types

# ---- 伪造 MoviePilot 运行时 ----
sys.modules.setdefault("app", types.ModuleType("app"))
sys.modules.setdefault("app.plugins", types.ModuleType("app.plugins"))
sys.modules["app.plugins"]._PluginBase = type("_PluginBase", (), {})
sys.modules.setdefault("app.sdk", types.ModuleType("app.sdk"))
sys.modules.setdefault("app.sdk.logging", types.ModuleType("app.sdk.logging"))
sys.modules["app.sdk.logging"].logger = logging.getLogger("t")

# 伪造 app.schemas.types.EventType（真实环境里由 MoviePilot 提供）
_schemas = types.ModuleType("app.schemas")
_types = types.ModuleType("app.schemas.types")


class _EventType:
    PluginAction = "plugin_action"


class _NotificationType:
    Plugin = "plugin"


_types.EventType = _EventType
_types.NotificationType = _NotificationType
_schemas.types = _types
sys.modules["app.schemas"] = _schemas
sys.modules["app.schemas.types"] = _types
sys.modules["app"].schemas = _schemas

sys.path.insert(0, "/tmp")
import importlib

mod = importlib.import_module("AutoCheckIn")
Plugin = mod.AutoCheckIn
SITES = mod.SITES
SignContext = mod.SignContext

fails = []


def check(name, condition, detail=""):
    print(("  ✅ " if condition else "  ❌ ") + name + (f"  → {detail}" if detail and not condition else ""))
    if not condition:
        fails.append(name)


print("=== 1. 插件规范（类属性 / 钩子）")
for attr in ("plugin_name", "plugin_desc", "plugin_icon", "plugin_version",
             "plugin_author", "author_url", "plugin_config_prefix", "plugin_order", "auth_level"):
    check(f"有 {attr}", hasattr(Plugin, attr), getattr(Plugin, attr, ""))

import inspect
check("get_command 是 staticmethod", isinstance(inspect.getattr_static(Plugin, "get_command"), staticmethod))
for hook in ("init_plugin", "get_state", "get_form", "get_page", "get_service", "get_api", "stop_service"):
    check(f"实现 {hook}", callable(getattr(Plugin, hook, None)))

print("\n=== 2. 表单：model 不得含点号（MoviePilot 不支持点号路径）")
inst = Plugin.__new__(Plugin)
form, defaults = Plugin.get_form(inst)
check("get_form 返回 (list, dict)", isinstance(form, list) and isinstance(defaults, dict))

models = []


def walk(node):
    if isinstance(node, dict):
        props = node.get("props")
        if isinstance(props, dict) and isinstance(props.get("model"), str):
            models.append(props["model"])
        for value in node.values():
            walk(value)
    elif isinstance(node, list):
        for item in node:
            walk(item)


walk(form)
dotted = [m for m in models if "." in m]
check(f"共 {len(models)} 个 model，无一个是点号路径", not dotted, f"点号 model: {dotted}")
check("含 4 个站点的启用开关", all(f"{s.key}_enabled" in models for s in SITES))
_pw_sites = [s for s in SITES if getattr(s, "auth", "") != "cookie"]
_cookie_sites = [s for s in SITES if getattr(s, "auth", "") == "cookie"]
check("凭据字段齐全（Cookie 站只要 Cookie，其余要账号密码）",
      all(f"{s.key}_username" in models and f"{s.key}_password" in models for s in _pw_sites)
      and all(f"{s.key}_cookie" in models for s in _cookie_sites)
      and not any(f"{s.key}_username" in models for s in _cookie_sites),
      f"账号密码站={[s.key for s in _pw_sites]} cookie 站={[s.key for s in _cookie_sites]}")
check("含每站 use_proxy 开关", all(f"{s.key}_use_proxy" in models for s in SITES))
check("不再暴露每站 use_browser（引擎由站点决定）",
      not any(f"{s.key}_use_browser" in models for s in SITES))
check("不再暴露每站专用代理", not any(f"{s.key}_proxy" in models for s in SITES))
check("hdhaven 有签到类型下拉", "hdhaven_checkin_type" in models)
check("默认值里没有嵌套 sites 字典", "sites" not in defaults)
check("默认值含扁平站点键", "wn4k_enabled" in defaults and "re0_checkin_type" in defaults)

print("\n=== 3. 配置解析：扁平键 / 旧点号键 / 旧嵌套 三路兼容")
make = lambda cfg: Plugin._collect_sites(inst, cfg, {})  # noqa: E731

sites = make({"wn4k_enabled": True, "wn4k_username": "u1", "wn4k_password": "p1",
              "wn4k_use_proxy": False, "wn4k_use_browser": False})
check("扁平键能读到启用与账号", sites["wn4k"]["enabled"] is True and sites["wn4k"]["username"] == "u1")

sites = make({"sites.wn4k.enabled": True, "sites.wn4k.username": "u2", "sites.wn4k.password": "p2"})
check("旧版点号键能迁移过来（用户已填的内容不丢）",
      sites["wn4k"]["enabled"] is True and sites["wn4k"]["username"] == "u2")

nested = Plugin._collect_sites(inst, {}, {"jying": {"enabled": True, "username": "u3"}})
check("旧版嵌套字典也能读", nested["jying"]["enabled"] is True and nested["jying"]["username"] == "u3")

check("未配置时按站点默认决定代理",
      make({})["hdhaven"]["use_proxy"] is True and make({})["wn4k"]["use_proxy"] is False)
check("扁平键优先于旧点号键",
      make({"wn4k_enabled": False, "sites.wn4k.enabled": True})["wn4k"]["enabled"] is False)

print("\n=== 4. 其他钩子返回结构")
cmds = Plugin.get_command()
check("get_command 返回 list", isinstance(cmds, list) and bool(cmds))
check("命令字段齐备", all(k in cmds[0] for k in ("cmd", "event", "desc", "category", "data")))
apis = Plugin.get_api(inst)
check("get_api 返回 list", isinstance(apis, list) and bool(apis))
check("API 字段齐备", all(k in apis[0] for k in ("path", "endpoint", "methods", "summary", "description")))

print("\n=== 5. 代理上下文")
base_kw = dict(timeout=30, browserless_url="https://production-sfo.browserless.io",
               browserless_token="T", logger=logging.getLogger("t"),
               default_proxy="http://127.0.0.1:7890")
check("开后走代理就用全局默认代理",
      SignContext({"use_proxy": True}, **base_kw).proxy == "http://127.0.0.1:7890")
check("关闭代理则直连", SignContext({"use_proxy": False}, **base_kw)._proxies() is None)
check("浏览器引擎由站点决定，不从配置读",
      SignContext({"use_browser": False}, default_use_browser=True, **base_kw).use_browser is True)
check("BrowserQL 端点按部署形态拼接",
      SignContext({}, **base_kw)._bql_endpoint().endswith("/stealth/bql?token=T&stealth=true"))

print("\n=== 6. 站点称呼与默认策略")
names = {s.key: s.name for s in SITES}
check("站点称呼统一", names == {"wn4k": "蜗牛4K", "jying": "聚影",
                                "hdhaven": "栖影", "re0": "re0"}, str(names))
expect = {"wn4k": (False, False), "jying": (False, False), "hdhaven": (True, True), "re0": (True, True)}
for site in SITES:
    px, br = expect[site.key]
    check(f"{site.key}: 代理={px} 浏览器={br}",
          bool(getattr(site, "needs_proxy", False)) == px
          and bool(getattr(site, "needs_browser", False)) == br)

print("\n=== 7. 旧配置自动迁移（点号键 → 扁平键）")
saved = {}


class _Fake(Plugin):
    def update_config(self, cfg):
        saved.update(cfg)

fake = _Fake.__new__(_Fake)
fake.update_config = lambda cfg: saved.update(cfg)
Plugin.init_plugin(fake, {"enabled": True,
                          "sites.wn4k.enabled": True,
                          "sites.wn4k.username": "u9",
                          "sites.wn4k.password": "p9"})
check("迁移后运行时读到了账号", fake._sites["wn4k"]["username"] == "u9")
check("写回了扁平键", saved.get("wn4k_username") == "u9" and saved.get("wn4k_enabled") is True,
      f"saved={ {k: v for k, v in saved.items() if k.startswith('wn4k')} }")

print("\n=== 8. 通知汇总：有重试只发一条，且用最终结果")


class _Harness(Plugin):
    """只测调度与汇总逻辑：站点执行、一次性任务、消息发送全部打桩。"""

    def __init__(self):
        self._sites = {s.key: {"enabled": True} for s in SITES}
        self._notify = True
        self._retry = 1
        self.sent = []
        self.jobs = []
        self.plan = {}
        self._round = None

    def _run_site(self, site_key):
        seq = self.plan.get(site_key) or [(True, "默认成功")]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def _schedule_once(self, job_id, func, name, delay_seconds=0, func_kwargs=None):
        self.jobs.append({"job_id": job_id, "func": func, "kwargs": dict(func_kwargs or {})})
        return True

    def _upsert_record(self, *args, **kwargs):
        return {}

    def post_message(self, **kwargs):
        self.sent.append(kwargs)


def _run_jobs(h, only=None):
    """把排上的一次性任务跑掉（模拟宿主调度器到点回调）；only 指定只跑哪一个。"""
    pending, h.jobs = list(h.jobs), []
    for job in pending:
        if only and job["job_id"] != only:
            h.jobs.append(job)      # 没点到的留给下一次
            continue
        job["func"](**job["kwargs"])


# 8.1 全部一次成功 → 只有一条汇总
h = _Harness()
h.checkin_all()
check("全部成功时只发 1 条消息", len(h.sent) == 1, f"实际 {len(h.sent)} 条")
check("标题是 成功 4/4", h.sent and h.sent[0]["title"].endswith("成功 4/4"),
      str(h.sent[0]["title"] if h.sent else None))

# 8.2 首轮失败 + 重试成功 → 不发「重试成功」消息，只在最后发一条，正文是最终结果
h = _Harness()
h.plan = {"wn4k": [(False, "登录请求失败（网络）"), (True, "获得 10 积分")]}
h.checkin_all()
check("有重试时主流程结束先不发消息", len(h.sent) == 0, f"实际 {len(h.sent)} 条")
check("排了 1 个重试任务", len(h.jobs) == 1 and h.jobs[0]["job_id"] == "retry_wn4k",
      str([j["job_id"] for j in h.jobs]))
check("重试任务带轮次号", bool(h.jobs[0]["kwargs"].get("round_id")))
_run_jobs(h)
check("重试跑完只发 1 条", len(h.sent) == 1, f"实际 {len(h.sent)} 条")
_body = h.sent[0]["text"] if h.sent else ""
check("正文里该站是最终的成功结果", "蜗牛4K: ✅ 获得 10 积分" in _body, _body)
check("正文不再出现首轮失败", "登录请求失败" not in _body, _body)
check("标题按最终结果统计 成功 4/4", h.sent and h.sent[0]["title"].endswith("成功 4/4"),
      str(h.sent[0]["title"] if h.sent else None))

# 8.3 重试也失败（重试预算用完）→ 依然只发一条，正文是最后的失败原因
h = _Harness()
h.plan = {"wn4k": [(False, "登录请求失败（网络）"), (False, "登录请求失败（网络）")]}
h.checkin_all()
_run_jobs(h)
check("重试也失败时只发 1 条", len(h.sent) == 1, f"实际 {len(h.sent)} 条")
check("标题统计 成功 3/4", h.sent and h.sent[0]["title"].endswith("成功 3/4"),
      str(h.sent[0]["title"] if h.sent else None))
check("正文是该站失败原因", "蜗牛4K: ❌" in (h.sent[0]["text"] if h.sent else ""),
      h.sent[0]["text"] if h.sent else "")
check("正文不含「已安排重试」这种过程话术",
      "重试" not in (h.sent[0]["text"] if h.sent else ""),
      h.sent[0]["text"] if h.sent else "")

# 8.4 两个站都要重试 → 先回来的那个不发，最后一个回来才发，且只发一条
h = _Harness()
h.plan = {"wn4k": [(False, "网络失败"), (True, "获得 10 积分")],
          "jying": [(False, "登录请求失败（网络）"), (True, "累计签到 138 天")]}
h.checkin_all()
check("两站失败时排了 2 个重试", len(h.jobs) == 2, str([j["job_id"] for j in h.jobs]))
_run_jobs(h, only="retry_wn4k")
check("只回来一个重试时仍不发消息", len(h.sent) == 0, f"实际 {len(h.sent)} 条")
_run_jobs(h)
check("最后一个重试回来才发，且只发 1 条", len(h.sent) == 1, f"实际 {len(h.sent)} 条")
_body = h.sent[0]["text"] if h.sent else ""
check("两个站都是最终的成功结果",
      "蜗牛4K: ✅ 获得 10 积分" in _body and "聚影: ✅ 累计签到 138 天" in _body, _body)
check("标题 成功 4/4", h.sent and h.sent[0]["title"].endswith("成功 4/4"),
      str(h.sent[0]["title"] if h.sent else ""))

# 8.5 停用插件后不再补发上一轮的汇总
h = _Harness()
h.plan = {"wn4k": [(False, "网络失败")]}
h.checkin_all()
Plugin._cancel_once = lambda self, job_id: None
Plugin.stop_service(h)
_run_jobs(h)
check("停用后不发补汇总", len(h.sent) == 0, f"实际 {len(h.sent)} 条")

print("\n=== 9. 栖影只走 Cookie：云端 Browserless 那条路已删除")
from AutoCheckIn.sites import hdhaven as hdv

check("栖影声明了 auth=cookie", getattr(hdv.Hdhaven, "auth", "") == "cookie")
check("云端那套函数已删除",
      not any(hasattr(hdv, n) for n in
              ("run_cloud_flow", "_cloud_once", "build_query", "_mark_button_script")),
      "还有残留")
check("云端那套常量已删除",
      not any(hasattr(hdv, n) for n in
              ("JS_MARK_LOGIN", "JS_TOKEN_READY", "JS_REACT_READY", "JS_STATE")))

# 表单：栖影那一块不该再出现账号/密码输入框
_models = []
def _walk(node):
    if isinstance(node, dict):
        props = node.get("props")
        if isinstance(props, dict) and isinstance(props.get("model"), str):
            _models.append(props["model"])
        for v in node.values():
            _walk(v)
    elif isinstance(node, list):
        for it in node:
            _walk(it)


_walk(Plugin.get_form(Plugin.__new__(Plugin))[0])
check("表单里栖影没有账号字段", "hdhaven_username" not in _models, str(_models))
check("表单里栖影没有密码字段", "hdhaven_password" not in _models)
check("表单里栖影有 Cookie 字段", "hdhaven_cookie" in _models)
check("其它站点仍有账号密码",
      "wn4k_username" in _models and "jying_password" in _models)


class _SiteCtx:
    """只喂 signin 需要的字段。"""
    cookie = ""
    use_browser = True

    def __init__(self):
        self.called = []

    class _L:
        def info(self, *a, **k):
            pass

        warning = info


def _hd_signin(conf, ctx):
    return hdv.Hdhaven().signin(conf, ctx)


ok, msg = _hd_signin({}, _SiteCtx())
check("没有 Cookie → 直接失败", ok is False, msg)
check("失败原因指名要 Cookie", "Cookie" in msg, msg)

# 有 Cookie 时应该调用 run_local_flow（打桩，不真开浏览器）
_orig = hdv.run_local_flow
try:
    hdv.run_local_flow = lambda *a, **k: (True, "累计签到 138 天")
    _ctx = _SiteCtx()
    _ctx.cookie = "sid=abc"
    ok, msg = _hd_signin({"cookie": "sid=abc"}, _ctx)
    check("有 Cookie → 走本地浏览器流程并透传结果", ok is True and "138" in msg, msg)

    hdv.run_local_flow = lambda *a, **k: (False, "点了「立即签到」但页面没变化")
    ok, msg = _hd_signin({"cookie": "sid=abc"}, _ctx)
    check("本地流程失败 → 如实报失败", ok is False and "没变化" in msg, msg)

    hdv.run_local_flow = lambda *a, **k: (True, "赌狗签到: 已领取")
    ok, msg = _hd_signin({"cookie": "sid=abc", "checkin_type": "gamble"}, _ctx)
    check("签到类型 gamble → 结果透传", ok is True and "赌狗" in msg, msg)
finally:
    hdv.run_local_flow = _orig

# 结果列的积分写法：签到前 → 签到后
_pn = hdv._points_note
check("积分前后都有 → 「积分：X → Y」", _pn(127, 132) == "积分：127 → 132", _pn(127, 132))
check("只读到签到后 → 「积分：Y」", _pn(None, 132) == "积分：132", _pn(None, 132))
check("签到后没读到 → 明说未读到", _pn(127, None) == "积分：127 → 未读到", _pn(127, None))
check("都没读到 → 不出积分段", _pn(None, None) == "", repr(_pn(None, None)))

print("\n" + ("全部通过" if not fails else f"失败项：{fails}"))
sys.exit(1 if fails else 0)
