"""站点自动签到 —— MoviePilot V3 插件

针对非 PT 的普通站点做每日签到，每个站点一个模块（``sites/<key>.py``）：
能纯 HTTP 完成的走 HTTP；需要真实浏览器交互的用浏览器引擎
（re0 走云端 Browserless，栖影走本地浏览器 + 注入 Cookie）。

模块契约（见 ``sites/__init__.py``）：

    class Xxx:
        key = "xxx"          # 配置里的键名
        name = "站点名"
        url = "https://..."
        needs_browser = False
        def signin(self, conf, ctx):  -> (ok: bool, msg: str)
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from app.plugins import _PluginBase
from app.sdk.logging import logger

# V3 规范：后台任务（立即执行、失败重试）交给宿主调度器，不自建 BackgroundScheduler
try:
    from app.sdk.scheduler import add_plugin_once_job, remove_plugin_once_job
except Exception:  # pragma: no cover - 老宿主没有这个 SDK
    add_plugin_once_job = None
    remove_plugin_once_job = None

try:  # 事件与命令（/autocheckin 靠它落地）
    from app.sdk.events import Event, eventmanager
except Exception:  # pragma: no cover
    Event = None
    eventmanager = None

try:  # 通知类型 / 事件类型（V3 在 app.schemas.types，旧版本在 app.schemas）
    from app.schemas.types import EventType, NotificationType
except Exception:  # pragma: no cover
    try:
        from app.schemas import NotificationType  # type: ignore
        from app.schemas.types import EventType  # type: ignore
    except Exception:
        NotificationType = None  # type: ignore
        EventType = None  # type: ignore

# 同一时刻只允许一个站点占用浏览器：云端 Browserless 并发只有 1~2，
# 两个会话重叠会被直接掐断（实测报错 RemoteDisconnected）；本地浏览器也串行，免得同时开多个 Chromium
_SIGN_LOCK = threading.Lock()

# 一轮执行的汇总状态（主流程与宿主调度器里的重试任务会并发改它）
_ROUND_LOCK = threading.Lock()

from .sites import SITES, get_site  # noqa: E402  (sites 依赖上面注入的 logger)
from .base import SignContext


class AutoCheckIn(_PluginBase):
    """多个站点每日自动签到。"""

    plugin_name = "站点自动签到"
    plugin_desc = "实现站点自动签到，目前支持re0、栖影、聚影、蜗牛4K。"
    plugin_icon = "https://raw.githubusercontent.com/ibearxxx/MoviePilot-Plugins/main/icons/autocheckin.png"
    plugin_version = "1.0.0"
    plugin_author = "ibearxxx"
    author_url = "https://github.com/"
    plugin_config_prefix = "autocheckin_"
    plugin_order = 60
    auth_level = 1

    # ---- 运行状态（类属性给默认值，init_plugin 里按配置重建） ----
    _enabled: bool = False
    _notify: bool = False
    _cron: str = "30 8 * * *"
    _retry: int = 1
    _timeout: int = 30
    _browserless_url: str = ""
    _browserless_token: str = ""
    _default_proxy: str = ""
    _sites: Dict[str, Dict[str, Any]] = {}

    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置；必须允许重复调用。"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._cron = str(config.get("cron") or "30 8 * * *")
        self._retry = max(0, int(config.get("retry") or 0))
        self._timeout = max(5, int(config.get("timeout") or 30))
        self._browserless_url = str(config.get("browserless_url") or "").strip()
        self._browserless_token = str(config.get("browserless_token") or "").strip()
        self._default_proxy = str(config.get("default_proxy") or "").strip()

        raw_sites = config.get("sites") or {}
        if isinstance(raw_sites, str):  # 表单以 JSON 文本保存时
            try:
                raw_sites = json.loads(raw_sites)
            except Exception:
                logger.warning("站点配置不是合法 JSON，按空处理")
                raw_sites = {}
        legacy_sites = raw_sites if isinstance(raw_sites, dict) else {}
        self._sites = self._collect_sites(config, legacy_sites)

        # 一次性迁移：把旧版点号键（sites.wn4k.username）和旧嵌套结构写成扁平键，
        # 让表单界面和运行时看到同一份配置。写一次之后扁平键就存在了，不会重复写。
        if not getattr(self, "_config_migrated", False):
            migrated = {}
            for key, site_conf in self._sites.items():
                for field, value in site_conf.items():
                    flat = f"{key}_{field}"
                    if flat not in config and value not in (None, ""):
                        migrated[flat] = value
            if migrated:
                try:
                    config.update(migrated)
                    self._config_migrated = True
                    self.update_config(config)
                    logger.info(f"已把旧配置迁移为扁平键：{sorted(migrated)}")
                except Exception as err:
                    logger.warning(f"配置迁移失败（不影响运行）：{err}")

        if config.get("onlyonce"):
            self._schedule_once("checkin_once", self.checkin_all,
                                "站点自动签到立即运行一次", delay_seconds=3)
            config["onlyonce"] = False
            self.update_config(config)

    def get_state(self) -> bool:
        return self._enabled

    # ------------------------------------------------------- 宿主调度器
    def _schedule_once(self, job_id: str, func, name: str,
                       delay_seconds: float = 0, func_kwargs: dict | None = None) -> bool:
        """通过宿主调度器安排一次性任务；没有该 SDK 时返回 False。

        V3 规范要求后台任务（立即执行、失败重试）走宿主调度器，
        插件不要自建 BackgroundScheduler 或线程。
        """
        if add_plugin_once_job is None:
            logger.warning("当前宿主没有 app.sdk.scheduler.add_plugin_once_job，跳过一次性任务")
            return False
        try:
            add_plugin_once_job(self.__class__.__name__, job_id, func, name,
                                delay_seconds=delay_seconds, func_kwargs=func_kwargs)
            return True
        except Exception as err:
            logger.warning(f"注册一次性任务失败（{job_id}）：{err}")
            return False

    def _cancel_once(self, job_id: str) -> None:
        if remove_plugin_once_job is None:
            return
        try:
            remove_plugin_once_job(self.__class__.__name__, job_id)
        except Exception as err:
            logger.warning(f"取消一次性任务失败（{job_id}）：{err}")

    # ------------------------------------------------------------ 配置解析
    # 重要：MoviePilot 的 Vuetify 表单**不支持点号路径**（"sites.wn4k.username"）
    # 它会把 model 原样当成一个扁平 key 写进配置，于是 sites.wn4k.username 这种
    # 键会平铺在 config 顶层，嵌套的 sites 字典永远保持默认值。
    # 所以表单一律用扁平 model，例如 wn4k_username，这里再按站点归类。
    SITE_FIELDS = ("enabled", "username", "password", "cookie", "use_proxy")

    @staticmethod
    def _pick(config: dict, site_key: str, field: str, legacy: dict,
              default: Any = None) -> Any:
        """按优先级取值：扁平键 → 旧版点号键 → 旧版嵌套 → 默认值。"""
        for candidate in (config.get(f"{site_key}_{field}"),
                          config.get(f"sites.{site_key}.{field}"),
                          legacy.get(field)):
            if candidate not in (None, ""):
                return candidate
        return default

    def _collect_sites(self, config: dict, legacy_sites: dict) -> Dict[str, Dict[str, Any]]:
        """把扁平配置归类成 {站点: {字段: 值}}。"""
        sites: Dict[str, Dict[str, Any]] = {}
        for site in SITES:
            legacy = legacy_sites.get(site.key)
            legacy = legacy if isinstance(legacy, dict) else {}
            conf: Dict[str, Any] = {}
            for field in self.SITE_FIELDS:
                default = {"use_proxy": bool(getattr(site, "needs_proxy", False)),
                           "enabled": False}.get(field, "")
                conf[field] = self._pick(config, site.key, field, legacy, default)
            for field, default in (getattr(site, "extra_defaults", None) or {}).items():
                conf[field] = self._pick(config, site.key, field, legacy, default)
            sites[site.key] = conf
        return sites

    # ---------------------------------------------------------------- 表单
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """配置页：全局设置 + 「站点选择」下拉，选哪个只显示哪个站点的设置。

        用到的宿主能力（见 moviepilot-frontend v3 的 components/render/FormRender.vue）：
        - `props.show`：表达式，按 model 求值，false 时 display:none；
        - `props.items` 支持 `{{表达式}}`，可以跟着 model 实时变化。

        所以站点再多也不会把表单撑爆：每个站点就是下拉里的一个选项 + 同一套模板块。
        新增站点只需在 sites/ 里加一个适配器，这里自动多一个选项。
        """

        def field(kind: str, model: str, label: str, md: int = 4,
                  hint: str = "", **props: Any) -> dict:
            """栅格里的单个控件。"""
            cfg: Dict[str, Any] = {"model": model, "label": label,
                                   "density": "comfortable",
                                   "hide-details": "auto" if not hint else False}
            if kind != "VSwitch":
                cfg["variant"] = "outlined"
            cfg.update(props)
            if hint:
                cfg.update({"hint": hint, "persistent-hint": True})
            return {"component": "VCol", "props": {"cols": 12, "md": md},
                    "content": [{"component": kind, "props": cfg}]}

        def row(*cols: dict, props: Optional[dict] = None) -> dict:
            return {"component": "VRow", "props": {"class": "mb-1", **(props or {})},
                    "content": list(cols)}

        def section(title: str, subtitle: str = "",
                    switches: Optional[List[tuple]] = None,
                    extra: Optional[dict] = None) -> dict:
            """分区标题：左边标题+说明，右侧可以挂若干个开关（启用 / 走代理）。"""
            content: List[dict] = [
                {"component": "div", "props": {"class": "text-subtitle-1 font-weight-bold"},
                 "text": title}]
            if subtitle:
                content.append({"component": "div",
                                "props": {"class": "text-caption text-medium-emphasis"},
                                "text": subtitle})
            switches = switches or []
            title_md = 12 - 3 * len(switches)
            cols: List[dict] = [{"component": "VCol", "props": {"cols": 12, "md": title_md},
                                 "content": content}]
            for model, label in switches:
                cols.append({"component": "VCol", "props": {"cols": 12, "md": 3},
                             "content": [{"component": "VSwitch",
                                          "props": {"model": model, "label": label,
                                                    "color": "primary",
                                                    "density": "comfortable",
                                                    "hide-details": True}}]})
            return {"component": "VRow",
                    "props": {"class": "mt-4 mb-1 align-center", **(extra or {})},
                    "content": cols}

        # 站点下拉选项：标题跟着「是否启用」实时变化
        option_titles: List[str] = []
        for site in SITES:
            title = json.dumps(site.name, ensure_ascii=False)
            option_titles.append(
                '{"title": %s + (%s_enabled ? "（已启用）" : ""), "value": %s}'
                % (title, site.key, json.dumps(site.key, ensure_ascii=False)))
        site_options = "{{[" + ", ".join(option_titles) + "]}}"

        def card(title: str, icon: str, content: List[dict], cls: str = "mt-2") -> dict:
            """一张卡片：标题行 + 分隔线 + 内容，和 MoviePilot 里其它插件的观感一致。"""
            return {"component": "VCard", "props": {"class": cls}, "content": [
                {"component": "VCardTitle", "props": {"class": "d-flex align-center py-2"},
                 "content": [
                     {"component": "VIcon",
                      "props": {"style": "color: #16b1ff;", "class": "mr-2"}, "text": icon},
                     {"component": "span", "text": title}]},
                {"component": "VDivider"},
                {"component": "VCardText", "props": {"class": "pt-3"}, "content": content},
            ]}

        def site_select_col() -> dict:
            return {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                {"component": "VSelect",
                 "props": {"model": "edit_site", "label": "编辑站点",
                           "items": site_options, "variant": "outlined",
                           "density": "comfortable", "hide-details": True,
                           "prepend-icon": "mdi-web",
                           # 关掉下拉动画：默认过渡在深色界面上很跳
                           "menu-props": {"maxHeight": 320, "transition": False}}}]}

        # ------------------------------------------------------- 每个站点一块
        site_blocks: List[dict] = []
        for site in SITES:
            k = site.key
            cells: List[dict] = []
            # auth == "cookie" 的站点（栖影）只显示 Cookie 输入框：账号密码对它没用
            cookie_only = getattr(site, "auth", "") == "cookie"
            for name, item in (getattr(site, "extra_config", None) or {}).items():
                raw = dict(item.get("props") or {})
                label = str(raw.pop("label", name))
                raw.pop("model", None)
                hint = str(raw.pop("hint", ""))
                cells.append(field(item.get("component", "VTextField"),
                                   f"{k}_{name}", label, 4, hint=hint, **raw))
            site_blocks.append({
                "component": "div",
                "props": {"show": f"edit_site === '{k}'"},
                "content": [
                    # 站点选择器 + 启用/走代理：只显示当前站点这一组
                    row(site_select_col(),
                        {"component": "VCol", "props": {"cols": 12, "md": 6},
                         "content": [{"component": "div",
                                      "props": {"class": "d-flex align-center"},
                                      "content": [
                                          {"component": "VSwitch",
                                           "props": {"model": f"{k}_enabled", "label": "启用",
                                                     "color": "primary", "density": "comfortable",
                                                     "hide-details": True, "class": "mr-8"}},
                                          {"component": "VSwitch",
                                           "props": {"model": f"{k}_use_proxy",
                                                     "label": "通过代理", "color": "primary",
                                                     "density": "comfortable",
                                                     "hide-details": True}}]}]},
                        props={"class": "align-center mb-2"}),
                    *([row(field("VTextField", f"{k}_cookie", "Cookie（必填）", 12,
                                hint="浏览器走 NAS 代理登录站点后复制"))]
                      if cookie_only else
                      [row(field("VTextField", f"{k}_username", "账号", 4, autocomplete="off"),
                           field("VTextField", f"{k}_password", "密码", 4,
                                 type="password", autocomplete="new-password"),
                           field("VTextField", f"{k}_cookie", "Cookie（可选）", 4))]),
                    *([row(*cells)] if cells else []),
                ],
            })

        form = [{"component": "VForm", "content": [
            {"component": "VAlert",
             "props": {"type": "info", "variant": "tonal", "density": "compact",
                       "class": "mb-1",
                       "text": "蜗牛4K、聚影走纯 HTTP；re0 走云端 Browserless；"
                               "栖影走 Cookie + 本地浏览器（出口 IP 需与取 Cookie 时一致）。"}},
            card("基础设置", "mdi-cog", [
                row(field("VSwitch", "enabled", "启用插件", 4, color="primary"),
                    field("VSwitch", "notify", "发送通知", 4, color="primary"),
                    field("VSwitch", "onlyonce", "保存后立即运行一次", 4, color="primary")),
                row(field("VTextField", "cron", "执行周期（cron）", 4,
                          placeholder="30 8 * * *",
                          hint="如 30 8 * * *（每天 08:30）"),
                    field("VTextField", "retry", "失败重试次数", 4, type="number",
                          hint="0 为不重试"),
                    field("VTextField", "timeout", "HTTP 超时（秒）", 4, type="number",
                          hint="普通站点 30 秒足够")),
            ], cls="mt-0"),
            card("Browserless", "mdi-robot", [
                row(field("VTextField", "browserless_url", "地址", 6,
                          placeholder="https://production-sfo.browserless.io"),
                    field("VTextField", "browserless_token", "Token", 6,
                          type="password", autocomplete="new-password")),
            ]),
            card("代理", "mdi-shield-lock", [
                row(field("VTextField", "default_proxy", "代理地址", 8,
                          placeholder="http://127.0.0.1:7890",
                          hint="留空表示不用代理")),
            ]),
            card("站点设置", "mdi-web", site_blocks),
        ]}]

        defaults: Dict[str, Any] = {
            "enabled": False, "notify": False, "onlyonce": False,
            "cron": "30 8 * * *", "retry": 1, "timeout": 30,
            "browserless_url": "https://production-sfo.browserless.io", "browserless_token": "",
            "default_proxy": "",
            "edit_site": SITES[0].key if SITES else "",
        }
        # 每个站点的默认值也用扁平键，和表单一致
        for site in SITES:
            defaults.update({
                f"{site.key}_enabled": False,
                f"{site.key}_username": "",
                f"{site.key}_password": "",
                f"{site.key}_cookie": "",
                f"{site.key}_use_proxy": bool(getattr(site, "needs_proxy", False)),
            })
            for name, value in (getattr(site, "extra_defaults", None) or {}).items():
                defaults[f"{site.key}_{name}"] = value
        return form, defaults

    # ---------------------------------------------------------------- 页面
    # 状态 -> (标签, 颜色, 图标)
    STATUS_META = {
        "success": ("签到成功", "success", "mdi-check-circle"),
        # 老记录里存过 already 状态，仍然显示成「签到成功」，不给两种说法
        "already": ("签到成功", "success", "mdi-check-circle"),
        "failed": ("签到失败", "error", "mdi-alert-circle"),
    }

    # 插件页里的样式（类名统一 ac- 前缀，避免污染主程序）
    PAGE_STYLE = (
        ".ac-page{background:rgb(var(--v-theme-surface));border-radius:14px;padding:14px 14px 6px;"
        "max-width:100%;box-sizing:border-box;overflow-x:hidden;overflow-wrap:anywhere}"
        ".ac-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));"
        "gap:8px;margin:2px 0 10px}"
        ".ac-stat{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:10px;"
        "border:1px solid rgba(var(--v-theme-on-surface),.10);background:rgba(var(--v-theme-on-surface),.03)}"
        ".ac-stat-num{font-size:1.12rem;font-weight:700;line-height:1.1;font-variant-numeric:tabular-nums}"
        ".ac-stat-label{font-size:.72rem;line-height:1.2;color:rgba(var(--v-theme-on-surface),.62)}"
        ".ac-num-info{color:rgb(var(--v-theme-info,33,150,243))}"
        ".ac-num-success{color:#4caf50}"
        ".ac-num-error{color:#ef5350}"
        ".ac-num-warning{color:#fb8c00}"
        ".ac-num-grey{color:rgba(var(--v-theme-on-surface),.38)}"
        ".ac-updated{display:flex;align-items:center;gap:6px;margin-bottom:8px;font-size:.75rem;"
        "color:rgba(var(--v-theme-on-surface),.55)}"
        ".ac-section{display:flex;align-items:center;gap:8px;margin:18px 0 8px}"
        ".ac-table-wrap{border:1px solid rgba(var(--v-theme-on-surface),.10);border-radius:12px;"
        "overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.05)}"
        ".ac-table{width:100%}"
        ".ac-table th:nth-child(1),.ac-table td:nth-child(1){width:26%;white-space:nowrap}"
        ".ac-table th:nth-child(2),.ac-table td:nth-child(2){width:14%;white-space:nowrap}"
        ".ac-table thead th{background:rgba(var(--v-theme-on-surface),.04);font-size:.78rem;"
        "color:rgba(var(--v-theme-on-surface),.7);white-space:nowrap}"
        ".ac-table tbody tr:nth-child(even){background:rgba(var(--v-theme-on-surface),.02)}"
        ".ac-table tbody tr:hover{background:rgba(var(--v-theme-primary),.06)}"
        ".ac-table td{vertical-align:middle;padding:10px 14px}"
        ".ac-table td:nth-child(2){white-space:nowrap}"
        ".ac-site{font-weight:600;line-height:1.25}"
        ".ac-caption{font-size:.72rem;color:rgba(var(--v-theme-on-surface),.55);margin-top:2px}"
        ".ac-result{font-size:.84rem;line-height:1.45;word-break:break-word;"
        "font-variant-numeric:tabular-nums;white-space:normal}"
    )

    @classmethod
    def _status_of(cls, ok: bool, msg: str) -> str:
        """把 (成功?, 文案) 归成页面状态：只有成功和失败两种，细节看结果列。"""
        text = str(msg or "")
        if ok:
            return "success"
        # 明确失败：没找到入口、账号/凭据问题
        for marker in ("没找到", "登录失败", "未配置", "账号", "不匹配", "无效", "401", "403"):
            if marker in text:
                return "failed"
        # 其余（过盾没过、请求失败、超时、没读到结果……）一律算失败，
        # 具体原因写在结果列里，不再单独分「未确认」这一类
        return "failed"

    def _load_records(self) -> List[dict]:
        data = self.get_data("last_run") or {}
        records = data.get("records") if isinstance(data, dict) else None
        return records if isinstance(records, list) else []

    def _upsert_record(self, site_key: str, site_name: str, ok: bool, msg: str) -> dict:
        """记录一次执行结果（同站点多次执行累加「共 N 条记录」）。"""
        now = datetime.now()
        status = self._status_of(ok, msg)
        records = self._load_records()
        entry = next((r for r in records if isinstance(r, dict) and r.get("key") == site_key), None)
        if entry is None:
            entry = {"key": site_key, "site": site_name, "count": 0}
            records.append(entry)
        entry.update({"status": status, "message": str(msg or ""),
                      "ran_at": now.strftime("%Y-%m-%d %H:%M")})
        entry["count"] = int(entry.get("count") or 0) + 1
        self.save_data("last_run", {"updated": now.strftime("%Y-%m-%d %H:%M"),
                                    "records": records})
        return entry

    def get_page(self) -> List[dict]:
        """插件详情页：顶部统计 + 「签到状态」方框表格（站点 / 状态 / 结果）。"""
        records = self._load_records()
        if not records:
            return [{"component": "VAlert", "props": {
                "type": "info", "variant": "tonal",
                "text": "还没有执行记录；保存配置后点「保存后立即运行一次」，或等定时任务触发。"}}]

        by_key = {str(r.get("key") or ""): r for r in records if isinstance(r, dict)}
        enabled = [s for s in SITES if (self._sites.get(s.key) or {}).get("enabled")]
        # 已启用的排前面；只显示真正跑过的站点，没有记录的不占一行
        rows: List[Tuple[str, dict]] = [(s.name, by_key[s.key]) for s in enabled
                                        if by_key.get(s.key)]
        # 已停用站点的历史记录：名称按 key 查回当前的站点名，不用记录里存的老名字
        current_names = {s.key: s.name for s in SITES}
        rows += [(current_names.get(str(r.get("key") or ""))
                  or str(r.get("site") or r.get("key") or "未知站点"), r) for r in records
                 if isinstance(r, dict) and str(r.get("key") or "")
                 not in {s.key for s in enabled}]
        if not rows:
            return [{"component": "VAlert", "props": {
                "type": "info", "variant": "tonal",
                "text": "还没有可显示的执行结果；点「保存后立即运行一次」或等定时任务触发。"}}]

        counts = {"success": 0, "failed": 0}
        for _name, record in rows:
            status = str(record.get("status") or "failed")
            counts["success" if status in ("success", "already") else "failed"] += 1

        metrics = [("站点", len(rows), "info"),
                   ("成功", counts["success"], "success"),
                   ("失败", counts["failed"], "error")]
        updated = str((self.get_data("last_run") or {}).get("updated") or "")

        body: List[dict] = [{
            "component": "div", "props": {"class": "ac-stats"},
            "content": [{
                "component": "div", "props": {"class": "ac-stat"},
                "content": [{
                    "component": "div", "content": [
                        {"component": "div",
                         "props": {"class": f"ac-stat-num ac-num-{color if value else 'grey'}"},
                         "text": str(value)},
                        {"component": "div", "props": {"class": "ac-stat-label"}, "text": label},
                    ]}]} for label, value, color in metrics]}]
        if updated:
            body.append({"component": "div", "props": {"class": "ac-updated"},
                         "text": f"上次执行 {updated[5:] if len(updated) > 10 else updated}"})

        body.append({"component": "div", "props": {"class": "ac-section"}, "content": [
            {"component": "VChip",
             "props": {"size": "small", "variant": "tonal", "color": "primary",
                       "prepend-icon": "mdi-calendar-check"},
             "text": f"签到状态（{len(rows)}）"}]})

        trs: List[dict] = []
        for name, record in rows:
            data = record or {}
            status = str(data.get("status") or "failed")
            label, color, icon = self.STATUS_META.get(status, self.STATUS_META["failed"])
            moment = str(data.get("ran_at") or "")
            count = int(data.get("count") or 0)
            caption = (f"今日 {moment[11:16]} · 共 {count} 条记录" if moment and count
                       else (f"共 {count} 条记录" if count else "本轮没有记录"))
            trs.append({"component": "tr", "content": [
                {"component": "td", "content": [
                    {"component": "div", "props": {"class": "ac-site"}, "text": name},
                    {"component": "div", "props": {"class": "ac-caption"}, "text": caption}]},
                {"component": "td", "content": [
                    {"component": "VChip",
                     "props": {"size": "small", "variant": "tonal", "color": color,
                               "prepend-icon": icon},
                     "text": label}]},
                {"component": "td", "props": {"class": "ac-result"},
                 "text": str(data.get("message") or "—")},
            ]})

        body.append({
            "component": "div", "props": {"class": "ac-table-wrap"},
            "content": [{"component": "VTable",
                         "props": {"density": "comfortable", "hover": True, "class": "ac-table"},
                         "content": [
                             {"component": "thead", "content": [
                                 {"component": "tr", "content": [
                                     {"component": "th", "text": "站点"},
                                     {"component": "th", "text": "状态"},
                                     {"component": "th", "text": "结果"}]}]},
                             {"component": "tbody", "content": trs}]}]})

        return [{"component": "style", "text": self.PAGE_STYLE},
                {"component": "div", "props": {"class": "ac-page"}, "content": body}]

    # ---------------------------------------------------------------- 命令与服务
    @staticmethod
    def get_command() -> List[dict]:
        try:
            from app.schemas.types import EventType  # 延迟导入，避免启动期依赖
        except Exception:  # 主程序没有这个符号时不要让插件加载失败
            logger.warning("未找到 EventType，跳过远程命令注册")
            return []

        return [{
            "cmd": "/autocheckin",
            "event": EventType.PluginAction,
            "desc": "立即执行一次站点签到",
            "category": "签到",
            "data": {"action": "autocheckin_now"},
        }]

    def get_service(self) -> List[dict]:
        if not self.get_state():
            return []
        return [{
            "id": "AutoCheckIn.Daily",
            "name": "站点自动签到",
            "trigger": CronTrigger.from_crontab(self._cron),
            "func": self.checkin_all,
            "kwargs": {},
        }]

    def get_api(self) -> List[dict]:
        return [{
            "path": "/autocheckin/run",
            "endpoint": self.checkin_all,
            "methods": ["POST"],
            "summary": "立即执行一次签到",
            "description": "手动触发一轮站点签到，返回每个站点的结果。",
        }]

    def stop_service(self) -> None:
        """停用插件：取消还没跑的重试任务，状态可重复调用。"""
        self._enabled = False
        for site in SITES:
            self._cancel_once(f"retry_{site.key}")
        self._cancel_once("checkin_once")
        # 停用后不再为上一轮补发汇总（重试都取消了，等不到收尾条件）
        with _ROUND_LOCK:
            state = self._round_state()
            state["running"] = False
            state["pending"] = 0
            state["sent"] = True

    # ------------------------------------------------------------ 命令事件
    def _handle_plugin_action(self, event) -> None:
        """处理 /autocheckin 远程命令（EventType.PluginAction）。"""
        event_data = getattr(event, "event_data", None) or {}
        if event_data.get("action") != "autocheckin_now":
            return
        logger.info("收到远程命令：立即执行一次站点签到")
        self._schedule_once("checkin_once", self.checkin_all,
                            "站点自动签到立即运行一次", delay_seconds=0.5)

    if eventmanager is not None and EventType is not None:  # V3 规范：用 SDK 事件总线
        _handle_plugin_action = eventmanager.register(EventType.PluginAction)(_handle_plugin_action)

    # ---------------------------------------------------------------- 执行
    def _site_ctx(self, site, conf: dict):
        return SignContext(conf=conf, timeout=self._timeout,
                           browserless_url=self._browserless_url,
                           browserless_token=self._browserless_token,
                           logger=logger,
                           default_proxy=self._default_proxy,
                           default_use_browser=bool(getattr(site, "needs_browser", False)))

    def _run_site(self, site_key: str) -> tuple[bool, str]:
        """跑单个站点一次（不重试）。"""
        site = get_site(site_key)
        conf = self._sites.get(site_key) or {}
        if site is None:
            return False, f"未知站点 {site_key}"
        if not conf.get("enabled"):
            return False, "站点未启用"
        ctx = self._site_ctx(site, conf)
        use_browser = bool(ctx.use_browser)
        if not use_browser:
            engine = "HTTP"
        elif getattr(site, "engine", "") == "local":
            engine = "本地浏览器"
        else:
            engine = "云端 Browserless"
        logger.info(f"{site.name} 开始签到（{ctx.route_note()}，{engine}）")
        try:
            if use_browser:
                # 串行：重试任务和主任务不能同时占用 Browserless，否则会被对端掐断
                with _SIGN_LOCK:
                    return site.signin(conf, ctx)
            return site.signin(conf, ctx)
        except Exception as err:  # 单站异常不能影响其它站点
            return False, f"{type(err).__name__}: {err}"

    def _round_state(self) -> Dict[str, Any]:
        """本轮执行状态（主流程与重试任务共享）。

        汇总消息只在「主流程跑完 + 所有重试链都收尾」时发一次，
        所以状态得跨调用保存：id 区分轮次，running 表示主循环还在跑，
        pending 是本轮还没回来的重试数，sent 保证只发一条。
        """
        state = getattr(self, "_round", None)
        if state is None:
            state = {"id": 0, "running": False, "pending": 0, "sent": True, "results": {}}
            self._round = state
        return state

    def _round_begin(self) -> int:
        """开始新一轮，返回轮次号（重试任务靠它认领自己属于哪一轮）。"""
        with _ROUND_LOCK:
            state = self._round_state()
            if state["pending"] > 0:
                logger.warning(f"上一轮还有 {state['pending']} 个重试没结束，"
                               f"本轮开始后不再为上一轮补发汇总")
            state["id"] += 1
            state["running"] = True
            state["pending"] = 0
            state["sent"] = False
            state["results"] = {}
            return state["id"]

    def _round_record(self, round_id: int, site_key: str, site_name: str,
                      ok: bool, msg: str) -> None:
        """记录站点在本轮的**最新**结果（重试成功会原地覆盖首轮失败）。"""
        with _ROUND_LOCK:
            state = self._round_state()
            if state["id"] == round_id:
                state["results"][site_key] = {"name": site_name, "ok": ok, "msg": msg}

    def _round_add_pending(self, round_id: int) -> None:
        with _ROUND_LOCK:
            state = self._round_state()
            if state["id"] == round_id:
                state["pending"] += 1

    def _round_retry_done(self, round_id: int) -> None:
        with _ROUND_LOCK:
            state = self._round_state()
            if state["id"] == round_id:
                state["pending"] = max(0, state["pending"] - 1)

    def _round_main_done(self, round_id: int) -> None:
        with _ROUND_LOCK:
            state = self._round_state()
            if state["id"] == round_id:
                state["running"] = False
        self._round_maybe_notify(round_id)

    def _round_maybe_notify(self, round_id: int) -> None:
        """主流程和所有重试链都收尾后，发**唯一一条**汇总；重复调用不会再发。"""
        rows = None
        with _ROUND_LOCK:
            state = self._round_state()
            if (state["id"] != round_id or state["sent"]
                    or state["running"] or state["pending"] > 0):
                return
            state["sent"] = True
            rows = [(item["name"], item["ok"], item["msg"])
                    for item in state["results"].values()]
        self._notify_summary(rows)

    def _notify_summary(self, rows) -> None:
        if not (self._notify and rows and NotificationType is not None):
            return
        ok_count = sum(1 for _, ok, _ in rows if ok)
        text = "\n".join(f"{name}: {'✅ ' if ok else '❌ '}{msg}"
                         for name, ok, msg in rows)
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title=f"站点自动签到：成功 {ok_count}/{len(rows)}",
                text=text,
            )
        except Exception as err:
            logger.warning(f"发送通知失败：{err}")

    def _retry_site(self, site_key: str, attempt: int = 2,
                    round_id: Optional[int] = None):
        """宿主调度器安排的单站重试（不在主流程里 sleep 或开线程）。

        重试**不再单独发消息**：结果回写到本轮的汇总里，等整轮（含所有重试链）
        都跑完再一次性发一条，避免「X 重试成功」和「成功 2/4」两条消息互相打架。
        """
        site = get_site(site_key)
        if site is None:
            if round_id is not None:
                self._round_retry_done(round_id)
                self._round_maybe_notify(round_id)
            return None
        ok, msg = self._run_site(site_key)
        self._upsert_record(site_key, site.name, ok, msg)
        logger.info(f"{site.name} 第 {attempt} 次尝试：{'成功' if ok else '失败'} — {msg}")
        if round_id is not None:
            self._round_record(round_id, site.key, site.name, ok, msg)
        if not ok and attempt <= self._retry:
            if self._schedule_once(f"retry_{site_key}", self._retry_site,
                                   f"站点自动签到重试：{site.name}",
                                   delay_seconds=180,
                                   func_kwargs={"site_key": site_key, "attempt": attempt + 1,
                                                "round_id": round_id}):
                return ok  # 下一棒已经排上，本轮继续等
            logger.warning(f"{site.name} 后续重试没排上，按最终结果收尾")
        if round_id is not None:
            self._round_retry_done(round_id)
            self._round_maybe_notify(round_id)
        return ok

    def checkin_all(self):
        """按配置逐个站点签到，返回 {站点: 结果}。

        每站只跑一次；失败且还有重试预算时，交给宿主调度器稍后重试——
        不在这里 sleep，也不自建线程或 BackgroundScheduler。

        通知只发一条：有站点排了重试就等所有重试跑完，
        用**最终结果**汇总（重试成功会覆盖首轮的失败），不再单独发重试消息。
        """
        round_id = self._round_begin()
        results: Dict[str, str] = {}
        try:
            for site in SITES:
                conf = self._sites.get(site.key) or {}
                if not conf.get("enabled"):
                    continue
                ok, msg = self._run_site(site.key)
                note = ""
                if not ok and self._retry > 0:
                    self._round_add_pending(round_id)
                    if self._schedule_once(f"retry_{site.key}", self._retry_site,
                                           f"站点自动签到重试：{site.name}",
                                           delay_seconds=150,
                                           func_kwargs={"site_key": site.key, "attempt": 2,
                                                        "round_id": round_id}):
                        note = "（已安排 150 秒后重试）"
                    else:
                        self._round_retry_done(round_id)
                        note = "（重试没排上）"
                logger.info(f"{site.name} 签到结果：{'成功' if ok else '失败'} — {msg}{note}")
                results[site.name] = ("✅ " if ok else "❌ ") + msg
                self._upsert_record(site.key, site.name, ok, msg)
                self._round_record(round_id, site.key, site.name, ok, msg)
        finally:
            # 主循环结束：没有待回的重试就在这里把唯一一条汇总发出去
            self._round_main_done(round_id)
        return results
