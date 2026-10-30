"""不安装就能验证 UltraHDBlindBox：规范符合性 + 表单 + 抽取规则 + 通知格式。

在 MoviePilot 容器里跑：
    docker cp plugins.v3/ultrahdblindbox moviepilot-v3:/tmp/UltraHDBlindBox
    docker cp tests/v3/ultrahdblindbox/test_ultrahdblindbox.py moviepilot-v3:/tmp/verify_bb.py
    docker exec moviepilot-v3 python /tmp/verify_bb.py
"""
import importlib
import inspect
import logging
import sys
import types

# ---------------------------------------------------------------- 伪造宿主
_STORE = {}
_SCHEDULED = []
_MESSAGES = []


class _PluginBase:
    """最小插件基类：只实现本插件真正用到的那几个方法。"""

    def __init__(self, *args, **kwargs):
        pass

    def get_data(self, key, default=None):
        return _STORE.get(key, default)

    def save_data(self, key, value):
        _STORE[key] = value

    def update_config(self, config):
        self._saved_config = dict(config or {})

    def post_message(self, **kwargs):
        _MESSAGES.append(kwargs)


sys.modules.setdefault("app", types.ModuleType("app"))
sys.modules.setdefault("app.plugins", types.ModuleType("app.plugins"))
sys.modules["app.plugins"]._PluginBase = _PluginBase

sys.modules.setdefault("app.sdk", types.ModuleType("app.sdk"))
sys.modules.setdefault("app.sdk.logging", types.ModuleType("app.sdk.logging"))
sys.modules["app.sdk.logging"].logger = logging.getLogger("t")

# 宿主调度器桩：记录注册了什么，用来断言「失败不自动重试」
_sched = types.ModuleType("app.sdk.scheduler")


def _add_once(plugin_id, job_id, func, name, delay_seconds=0, func_kwargs=None):
    _SCHEDULED.append((plugin_id, job_id, name, delay_seconds, func_kwargs))


def _remove_once(plugin_id, job_id):
    return None


_sched.add_plugin_once_job = _add_once
_sched.remove_plugin_once_job = _remove_once
sys.modules["app.sdk.scheduler"] = _sched

_events = types.ModuleType("app.sdk.events")


class _EventManager:
    def register(self, event_type):
        def wrapper(func):
            return func
        return wrapper


_events.eventmanager = _EventManager()
_events.Event = object
sys.modules["app.sdk.events"] = _events

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

# 站点管理桩：两个有 Cookie 的站点 + 一个没有 Cookie 的
_site_db = types.ModuleType("app.db")
_site_oper_mod = types.ModuleType("app.db.oper")
_site_mod = types.ModuleType("app.db.oper.site")


class _FakeSite:
    def __init__(self, sid, name, domain, url, cookie):
        self.id, self.name, self.domain, self.url, self.cookie = sid, name, domain, url, cookie


_FAKE_SITES = [
    _FakeSite(21, "ultrahd", "ultrahd.net", "https://ultrahd.net/", "a=1; b=2"),
    _FakeSite(22, "馒头", "m-team.cc", "https://kp.m-team.cc/", "c=3"),
    _FakeSite(30, "无cookie站", "nocookie.example", "https://nocookie.example/", None),
]


class _FakeSiteOper:
    def list_order_by_pri(self):
        return list(_FAKE_SITES)

    def get(self, sid):
        for site in _FAKE_SITES:
            if int(site.id) == int(sid):
                return site
        return None


_site_mod.SiteOper = _FakeSiteOper
_site_oper_mod.site = _site_mod
_site_db.oper = _site_oper_mod
sys.modules["app.db"] = _site_db
sys.modules["app.db.oper"] = _site_oper_mod
sys.modules["app.db.oper.site"] = _site_mod
sys.modules["app"].sdk = sys.modules["app.sdk"]
sys.modules["app"].db = _site_db

sys.path.insert(0, "/tmp")
mod = importlib.import_module("UltraHDBlindBox")
Plugin = mod.UltraHDBlindBox
blindbox = importlib.import_module("UltraHDBlindBox.blindbox")

fails = []


def check(name: str, condition: object, detail: object = "") -> None:
    print(("  ✅ " if condition else "  ❌ ") + name
          + (f"  → {detail}" if detail and not condition else ""))
    if not condition:
        fails.append(name)


def reset_state():
    _STORE.clear()
    _SCHEDULED.clear()
    _MESSAGES.clear()


def make_plugin(**config):
    plugin = Plugin.__new__(Plugin)
    Plugin.init_plugin(plugin, config)
    return plugin


def D(reward, magic_after=None, ok=True):
    return blindbox.DrawResult(ok=ok, reward=reward, magic_after=magic_after)


def attach(plugin, state, draws, site=None):
    """给插件挂一个假站点 + 假接口层客户端。"""

    class FakeClient:
        def __init__(self):
            self.calls = []
            self._draws = list(draws)

        def state(self):
            return state

        def draw(self, free):
            self.calls.append(free)
            item = self._draws.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    client = FakeClient()
    plugin._load_site = lambda: site or _FAKE_SITES[0]
    plugin._make_client = lambda _site: client
    return client


# ---------------------------------------------------------------- 1. 规范
print("=== 1. 插件规范（类属性 / 钩子）")
for attr in ("plugin_name", "plugin_desc", "plugin_icon", "plugin_version", "plugin_author",
             "author_url", "plugin_config_prefix", "plugin_order", "auth_level"):
    check(f"有 {attr}", hasattr(Plugin, attr), getattr(Plugin, attr, ""))
check("插件名是 UltraHD幸运盲盒", Plugin.plugin_name == "UltraHD幸运盲盒", Plugin.plugin_name)
check("get_command 是 staticmethod",
      isinstance(inspect.getattr_static(Plugin, "get_command"), staticmethod))
for hook in ("init_plugin", "get_state", "get_form", "get_page", "get_service", "get_api",
             "stop_service"):
    check(f"实现 {hook}", callable(getattr(Plugin, hook, None)))

# ---------------------------------------------------------------- 2. 表单
print("\n=== 2. 表单：model 不得含点号，站点下拉来自站点管理")
reset_state()
inst = Plugin.__new__(Plugin)
form, defaults = Plugin.get_form(inst)
check("get_form 返回 (list, dict)", isinstance(form, list) and isinstance(defaults, dict))

models, selects = [], []


def walk(node):
    if isinstance(node, dict):
        props = node.get("props")
        if isinstance(props, dict):
            if isinstance(props.get("model"), str):
                models.append(props["model"])
            if node.get("component") == "VSelect":
                selects.append(props)
        for value in node.values():
            walk(value)
    elif isinstance(node, list):
        for item in node:
            walk(item)


walk(form)
dotted = [m for m in models if "." in m]
check(f"共 {len(models)} 个 model，无一个是点号路径", not dotted, f"点号 model: {dotted}")


def find_component(node, name):
    """收集某种组件的 props（用来断言用了宿主自定义控件）。"""
    found = []
    if isinstance(node, dict):
        if node.get("component") == name:
            found.append(node.get("props") or {})
        for value in node.values():
            found += find_component(value, name)
    elif isinstance(node, list):
        for item in node:
            found += find_component(item, name)
    return found


cron_fields = find_component(form, "VCronField")
check("执行周期用宿主的 VCronField 控件",
      len(cron_fields) == 1 and cron_fields[0].get("model") == "cron", cron_fields)
check("不再用 VTextField 承载 cron",
      not any((c or {}).get("model") == "cron" for c in find_component(form, "VTextField")))
check("自定义控件不塞 Vuetify 的 density / variant / hint",
      bool(cron_fields) and not ({"density", "variant", "hint"} & set(cron_fields[0])),
      cron_fields[:1])
for model in ("enabled", "notify", "site_id", "daily", "cron", "onlyonce"):
    check(f"表单含 {model}", model in models)

site_select = next((s for s in selects if s.get("model") == "site_id"), None)
check("有站点下拉", site_select is not None)
items = (site_select or {}).get("items") or []
check("站点下拉只列 UltraHD（功能相关站点）",
      [i["title"] for i in items] == ["ultrahd"], [i["title"] for i in items])
check("下拉值用站点 id", [i["value"] for i in items] == [21], items)

# 站点管理里没有 UltraHD → 下拉为空 + 一条提示
_keep = list(_FAKE_SITES)
try:
    _FAKE_SITES.clear()
    _FAKE_SITES.extend([_FakeSite(22, "馒头", "m-team.cc", "https://kp.m-team.cc/", "c=3")])
    form2, defaults2 = Plugin.get_form(Plugin.__new__(Plugin))
    _selects2 = []

    def _walk2(node):
        if isinstance(node, dict):
            if node.get("component") == "VSelect" and (node.get("props") or {}).get("model") == "site_id":
                _selects2.append(node.get("props"))
            for v in node.values():
                _walk2(v)
        elif isinstance(node, list):
            for i in node:
                _walk2(i)

    _walk2(form2)
    _items2 = (_selects2[0].get("items") if _selects2 else []) or []
    check("没有 UltraHD 时下拉为空", _items2 == [], _items2)
    check("没有 UltraHD 时默认站点为 0", defaults2.get("site_id") == 0, defaults2.get("site_id"))
    _alerts2 = []

    def _walk_alert(node):
        if isinstance(node, dict):
            if node.get("component") == "VAlert":
                _alerts2.append(node)
            for v in node.values():
                _walk_alert(v)
        elif isinstance(node, list):
            for i in node:
                _walk_alert(i)

    _walk_alert(form2)
    _text2 = str(_alerts2)
    check("没有 UltraHD 时给出提示",
          "请先到站点管理中添加站点并配置 Cookie" in _text2, _text2)

    # 有 UltraHD 站点但没填 Cookie → 同样不进下拉
    _FAKE_SITES.clear()
    _FAKE_SITES.extend([_FakeSite(21, "ultrahd", "ultrahd.net", "https://ultrahd.net/", None)])
    check("UltraHD 没 Cookie 时也不进下拉", Plugin._list_sites() == [], Plugin._list_sites())
finally:
    _FAKE_SITES.clear()
    _FAKE_SITES.extend(_keep)

check("默认 cron 是每天 08:30", defaults.get("cron") == "30 8 * * *", defaults.get("cron"))
check("默认每日次数 1", defaults.get("daily") == 1, defaults.get("daily"))
check("默认站点是 ultrahd", defaults.get("site_id") == 21, defaults.get("site_id"))
check("默认发送通知开", defaults.get("notify") is True)
check("默认不启用", defaults.get("enabled") is False)

# ---------------------------------------------------------------- 3. 配置解析
print("\n=== 3. 配置解析")
reset_state()
p = make_plugin(enabled=True, notify=False, site_id=22, daily="7", cron="0 9 * * *")
check("读到启用/站点/次数", (p._enabled, p._site_id, p._daily) == (True, 22, 7))
check("cron 按用户设置", p._cron == "0 9 * * *", p._cron)
check("通知开关读得到", p._notify is False)

p = make_plugin(daily=99)
check("次数上限钳到 30", p._daily == 30, p._daily)
p = make_plugin(daily=0)
check("次数下限钳到 1", p._daily == 1, p._daily)
p = make_plugin()
check("空配置 → 默认 cron 08:30", p._cron == "30 8 * * *", p._cron)
check("空配置 → 默认次数 1", p._daily == 1, p._daily)
check("未启用时 get_state 为假", Plugin.get_state(p) is False)

# ---------------------------------------------------------------- 4. 计划次数
print("\n=== 4. 计划抽取次数（免费已用掉时按 N-1 跑）")
p = make_plugin(daily=1)
check("N=1 免费还在 → 抽 1 次",
      Plugin._planned_draws(p, blindbox.BoxState(free_left=1, total_left=30)) == 1)
check("N=1 免费已用 → 0 次（跳过）",
      Plugin._planned_draws(p, blindbox.BoxState(free_left=0, total_left=29)) == 0)
p = make_plugin(daily=5)
check("N=5 免费还在 → 5 次",
      Plugin._planned_draws(p, blindbox.BoxState(free_left=1, total_left=30)) == 5)
check("N=5 免费已用 → 4 次（让出免费那格）",
      Plugin._planned_draws(p, blindbox.BoxState(free_left=0, total_left=29)) == 4)

# ---------------------------------------------------------------- 5. 正常抽取
print("\n=== 5. 正常抽取：1 次免费 + 3 次付费")
reset_state()
p = make_plugin(enabled=True, notify=True, site_id=21, daily=4)
client = attach(p, blindbox.BoxState(free_left=1, total_left=30, magic=None),
                [D("10 魔力值"), D("15GB 上传"), D("10 魔力值"), D("10 魔力值")])
p._interval = 0
text = Plugin.run_blind_box(p)
check("先免费后付费", client.calls == [True, False, False, False], client.calls)
check("汇总文案符合约定",
      text == "盲盒：免费 1 次 + 付费 3 次，消耗 9,000 魔力值，获得 10 魔力值 ×3、15GB 上传 ×1",
      text)
check("进度落盘：今日抽了 4 次", (_STORE.get("progress") or {}).get("drawn") == 4, _STORE.get("progress"))
check("进度落盘：免费 1 / 付费 3",
      ((_STORE.get("progress") or {}).get("free"), (_STORE.get("progress") or {}).get("paid")) == (1, 3))
check("只发一条通知", len(_MESSAGES) == 1, len(_MESSAGES))
check("通知正文是那句话",
      _MESSAGES[:1] and _MESSAGES[0].get("text") == text, _MESSAGES[:1])
check("通知标题带插件名",
      _MESSAGES[:1] and _MESSAGES[0].get("title") == "UltraHD幸运盲盒", _MESSAGES[:1])
check("失败不重试：没有排一次性任务", _SCHEDULED == [], _SCHEDULED)

# ---------------------------------------------------------------- 6. 同日防重
print("\n=== 6. 同一天不重复抽")
client2 = attach(p, blindbox.BoxState(free_left=1, total_left=26, magic=None),
                 [D("10 魔力值")])
text2 = Plugin.run_blind_box(p)
check("第二次运行不再抽", client2.calls == [], client2.calls)
check("提示今天已抽过", "已抽过" in text2, text2)
check("没有多发通知", len(_MESSAGES) == 1, len(_MESSAGES))

# ---------------------------------------------------------------- 7. N=1 免费已用
print("\n=== 7. N=1 且免费额度已用掉 → 跳过，不抽付费")
reset_state()
p = make_plugin(enabled=True, notify=True, site_id=21, daily=1)
client = attach(p, blindbox.BoxState(free_left=0, total_left=29, magic=334148),
                [D("10 魔力值")])
text = Plugin.run_blind_box(p)
check("一次都没抽", client.calls == [], client.calls)
check("不消耗魔力值", (_STORE.get("progress") or {}).get("cost") == 0, _STORE.get("progress"))
check("不发通知", _MESSAGES == [], _MESSAGES)
check("记一笔「跳过」",
      any(r.get("status") == "skipped" for r in
          (_STORE.get("last_run") or {}).get("records", [])),
      (_STORE.get("last_run") or {}).get("records"))

# ---------------------------------------------------------------- 8. N=5 免费已用
print("\n=== 8. N=5 且免费额度已用掉 → 抽 4 次付费")
reset_state()
p = make_plugin(enabled=True, notify=True, site_id=21, daily=5)
client = attach(p, blindbox.BoxState(free_left=0, total_left=29, magic=None),
                [D("10 魔力值"), D("10 魔力值"), D("15GB 上传"), D("10 魔力值")])
p._interval = 0
text = Plugin.run_blind_box(p)
check("抽了 4 次且全是付费", client.calls == [False] * 4, client.calls)
check("汇总写 4 次付费",
      text.startswith("盲盒：付费 4 次，消耗 12,000 魔力值，获得"), text)

# ---------------------------------------------------------------- 9. 中途失败即停
print("\n=== 9. 中途失败即停，且不重试")
reset_state()
p = make_plugin(enabled=True, notify=True, site_id=21, daily=5)
client = attach(p, blindbox.BoxState(free_left=1, total_left=30, magic=None),
                [D("10 魔力值"), blindbox.BlindBoxError("魔力值不足")])
p._interval = 0
text = Plugin.run_blind_box(p)
check("第 2 次失败后不再抽", client.calls == [True, False], client.calls)
check("汇总里写明第 2 次失败", "第 2 次失败：魔力值不足" in text, text)
check("失败也没有排重试任务", _SCHEDULED == [], _SCHEDULED)
check("仍然只发一条通知", len(_MESSAGES) == 1, len(_MESSAGES))

# ---------------------------------------------------------------- 10. 站点/凭据缺失
print("\n=== 10. 站点或 Cookie 缺失 → 说清原因，不抽")
reset_state()
p = make_plugin(enabled=True, notify=True, site_id=0, daily=1)
text = Plugin.run_blind_box(p)
check("没选站点 → 明确报错", "还没有选择站点" in text, text)
check("失败也发一条通知", len(_MESSAGES) == 1, _MESSAGES)
check("失败记进记录",
      any(r.get("status") == "failed" for r in
          (_STORE.get("last_run") or {}).get("records", [])),
      (_STORE.get("last_run") or {}).get("records"))

reset_state()
p = make_plugin(enabled=True, notify=False, site_id=30, daily=1)
text = Plugin.run_blind_box(p)
check("选了非 UltraHD 站点 → 明确拒绝", "不是 UltraHD" in text, text)

reset_state()
_keep2 = list(_FAKE_SITES)
try:
    _FAKE_SITES.clear()
    _FAKE_SITES.extend([_FakeSite(21, "ultrahd", "ultrahd.net", "https://ultrahd.net/", None)])
    p = make_plugin(enabled=True, notify=False, site_id=21, daily=1)
    text = Plugin.run_blind_box(p)
    check("UltraHD 没有 Cookie → 提示去站点管理配", "没有 Cookie" in text, text)
finally:
    _FAKE_SITES.clear()
    _FAKE_SITES.extend(_keep2)

# ---------------------------------------------------------------- 11. 接口层异常
print("\n=== 11. 接口层报错时如实转达，不误报成功")


class _BrokenClient:
    """模拟首页读不到盲盒面板（Cookie 失效 / 站点改版）的客户端。"""

    def state(self):
        raise blindbox.BlindBoxError("首页里没有找到盲盒面板：Cookie 可能已失效")


reset_state()
p = make_plugin(enabled=True, notify=False, site_id=21, daily=1)
p._load_site = lambda: _FAKE_SITES[0]
p._make_client = lambda _site: _BrokenClient()
text = Plugin.run_blind_box(p)
check("把接口层的原因原样透出", "Cookie 可能已失效" in text, text)
check("没有误报成功", "成功" not in text, text)
check("失败记进记录",
      any(r.get("status") == "failed" for r in
          (_STORE.get("last_run") or {}).get("records", [])),
      (_STORE.get("last_run") or {}).get("records"))

# ---------------------------------------------------------------- 12. 详情页
print("\n=== 12. 详情页样式与数据")
reset_state()
p = make_plugin(enabled=True, site_id=21, daily=2)
page = Plugin.get_page(p)
check("没有记录时给提示", isinstance(page, list) and page
      and page[0].get("component") == "VAlert", page[:1])

reset_state()
p = make_plugin(enabled=True, site_id=21, daily=2)
attach(p, blindbox.BoxState(free_left=1, total_left=30, magic=None), [D("10 魔力值"), D("10 魔力值")])
p._interval = 0
Plugin.run_blind_box(p)
page = Plugin.get_page(p)
blob = str(page)
check("有记录后渲染页面", isinstance(page, list) and len(page) >= 1)
check("不铺不透明底色", "rgb(var(--v-theme-surface))" not in blob)
check("页面底色透明", "background:transparent" in blob)
check("用了宿主的 --app-* token",
      "--app-surface-border" in blob and "--app-surface-radius" in blob
      and "--app-surface-shadow" in blob)
check("语义色走主题变量", "--v-theme-success" in blob and "--v-theme-error" in blob)
check("表格背景透明", ".bb-table{width:100%;background:transparent}" in blob)
check("统计里有「今日抽取」", "今日抽取" in blob)
check("最近记录里能看到奖励", "10 魔力值" in blob)

# ---------------------------------------------------------------- 13. 服务与命令
print("\n=== 13. 服务 / 命令 / API / 停用")
reset_state()
p = make_plugin(enabled=True, cron="30 8 * * *", site_id=21)
services = Plugin.get_service(p)
check("启用时注册定时服务", len(services) == 1, services)
check("服务 id 是 UltraHDBlindBox.Daily",
      services and services[0].get("id") == "UltraHDBlindBox.Daily", services)
p = make_plugin(enabled=False)
check("未启用时不注册服务", Plugin.get_service(p) == [])

cmds = Plugin.get_command()
check("命令字段齐备", cmds and all(k in cmds[0] for k in ("cmd", "event", "desc", "category", "data")), cmds)
check("命令是 /ultrahdblindbox", cmds and cmds[0].get("cmd") == "/ultrahdblindbox", cmds)
apis = Plugin.get_api(p)
check("API 字段齐备", apis and all(k in apis[0] for k in ("path", "endpoint", "methods", "summary")), apis)

Plugin.stop_service(p)
check("停用后 get_state 为假", Plugin.get_state(p) is False)

# ---------------------------------------------------------------- 14. 接口层解析
print("\n=== 14. 接口层：解析首页面板与抽取回包")

SAMPLE_HOME = """
<div class="blindbox-info">
  <div class="blindbox-balance">当前魔力值: <span id="userBonus">332168</span></div>
  <div class="blindbox-cost">每次消耗: <span id="drawCost">3000</span> 魔力值</div>
  <div class='blindbox-limit'>今日剩余: <span>28</span> / 30 次</div>
  <div class='blindbox-free-info'>免费次数: <span id='freeRemaining'>0</span> / 1 次</div>
</div>
"""
st = blindbox.parse_state(SAMPLE_HOME)
check("解析出当前魔力值", st.magic == 332168.0, st.magic)
check("解析出每次消耗", st.cost == 3000, st.cost)
check("解析出今日剩余", st.total_left == 28, st.total_left)
check("解析出免费次数", st.free_left == 0, st.free_left)

try:
    blindbox.parse_state("<html>没有面板</html>", site_name="ultrahd")
    check("没有盲盒面板时应报错", False, "没有抛异常")
except blindbox.BlindBoxError as err:
    check("没有盲盒面板时报 Cookie 可能失效", "Cookie 可能已失效" in str(err), str(err))

SAMPLE_FREE = {"success": True,
               "prize": {"name": "谢谢参与", "value": "10 魔力值"},
               "new_bonus": 332178, "remaining_draws": 29, "free_remaining": 0,
               "daily_limit": 30}
r = blindbox.parse_draw(SAMPLE_FREE)
check("抽取成功 → ok", r.ok is True)
check("奖励用站点原文", r.reward == "10 魔力值", r.reward)
check("抽完的魔力值取 new_bonus", r.magic_after == 332178.0, r.magic_after)

r = blindbox.parse_draw({"success": True,
                         "prize": {"name": "上传量", "value": "15 GB上传量",
                                   "extra_message": "已直接发放"},
                         "new_bonus": 100})
check("上传量奖励原样返回", r.reward == "15 GB上传量（已直接发放）", r.reward)

r = blindbox.parse_draw({"success": False, "message": "魔力值不足"})
check("抽取失败 → ok 为假", r.ok is False)
check("失败原因用站点原文", r.reward == "魔力值不足", r.reward)

r = blindbox.parse_draw("不是字典")
check("回包不是字典也不崩", r.ok is False and "回包看不懂" in r.reward, r.reward)

check("COST_PER_DRAW 是 3000", blindbox.COST_PER_DRAW == 3000, blindbox.COST_PER_DRAW)
check("Cookie 解析成字典",
      blindbox._parse_cookies("c_secure_pass=abc; x=y") == {"c_secure_pass": "abc", "x": "y"},
      blindbox._parse_cookies("c_secure_pass=abc; x=y"))
check("付费单价优先取页面上的 drawCost",
      Plugin._cost_of([(False, D("10 魔力值")), (False, D("10 魔力值"))], None, 3000) == 6000)
check("页面读不到单价时退回 COST_PER_DRAW",
      Plugin._cost_of([(False, D("10 魔力值"))], None, 0) == blindbox.COST_PER_DRAW)

# 数字解析：「3,000.0」直接删非数字字符会变成 30000（单价十倍膨胀），必须先按数字解析
check("_to_int 认得带小数的千分位", blindbox._to_int("3,000.0") == 3000, blindbox._to_int("3,000.0"))
check("_to_int 认得纯小数", blindbox._to_int("3000.0") == 3000, blindbox._to_int("3000.0"))
check("_to_int 认得千分位", blindbox._to_int("1,234") == 1234, blindbox._to_int("1,234"))
check("_to_int 空值返回 None", blindbox._to_int("") is None)

# 站点显示今日次数已用完 → 直接跳过，不要拿付费次数去撞墙
print("\n=== 今日次数清零时跳过")
reset_state()
p = make_plugin(enabled=True, notify=False, site_id=21, daily=5)
client = attach(p, blindbox.BoxState(free_left=0, total_left=0, magic=1000.0), [D("10 魔力值")])
p._interval = 0
text = Plugin.run_blind_box(p)
check("次数用完 → 不抽", text == "盲盒：今日次数已用完，跳过", text)
check("次数用完 → 没有调用 draw", client.calls == [], client.calls)

# 同一天分两轮跑：进度必须按天累计，不能只记最后一轮
print("\n=== 同一天第二轮：免费/付费/消耗要累加")
reset_state()
p = make_plugin(enabled=True, notify=False, site_id=21, daily=3)
attach(p, blindbox.BoxState(free_left=1, total_left=30, magic=20000.0),
       [D("10 魔力值", magic_after=17000.0), D("魔力值不足", ok=False)])
p._interval = 0
Plugin.run_blind_box(p)                      # 第一轮：免费成功、第 2 次失败
_first = dict(_STORE.get("progress") or {})
check("第一轮记 1 次（免费 1、付费 0）",
      (_first.get("drawn"), _first.get("free"), _first.get("paid")) == (1, 1, 0), _first)

attach(p, blindbox.BoxState(free_left=0, total_left=29, magic=17000.0),
       [D("10 魔力值", magic_after=14000.0)])   # 第二轮：只剩付费 1 次
Plugin.run_blind_box(p)
_second = dict(_STORE.get("progress") or {})
check("第二轮后累计 2 次", _second.get("drawn") == 2, _second)
check("免费次数没被第二轮冲掉", _second.get("free") == 1, _second)
check("付费次数累加", _second.get("paid") == 1, _second)
check("消耗魔力值累加（3000 + 3000）", _second.get("cost") == 6000, _second)

print()
if fails:
    print(f"❌ {len(fails)} 项失败：" + "、".join(fails))
    raise SystemExit(1)
print("全部通过")
