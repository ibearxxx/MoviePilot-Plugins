"""UltraHD 幸运盲盒 —— MoviePilot V3 插件

每天自动抽 UltraHD PT 站的「幸运盲盒」。站点规则：一天最多 30 次，**第一次免费**，
之后每次消耗 3000 魔力值。

插件默认只抽那 1 次免费的；要付费抽必须自己把「每日抽取次数」改大（1–30）。

几条刻在代码里的规矩（都不是可选项）：

* **同一天不重复抽**：当天计划次数已经跑满就跳过，cron 抖动或手动再点一次都不会多抽。
* **失败不自动重试**：抽奖是付费动作，重试等于重复扣魔力值。失败如实报，由人决定要不要再来。
* **单轮内失败即停**：第 N 次失败就收工，不把剩下的次数抽完再报。

站点与 Cookie 取自 MoviePilot 的「站点管理」（``site`` 表），插件只读不写：
Cookie 失效时到站点管理里更新，插件不替用户改库。
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from app.plugins import _PluginBase
from app.sdk.logging import logger

# V3 规范：一次性任务（立即执行）交给宿主调度器，不自建线程或 BackgroundScheduler
try:
    from app.sdk.scheduler import add_plugin_once_job, remove_plugin_once_job
except Exception:  # pragma: no cover - 老宿主没有这个 SDK
    add_plugin_once_job = None
    remove_plugin_once_job = None

try:  # 远程命令走事件总线
    from app.sdk.events import eventmanager
except Exception:  # pragma: no cover
    eventmanager = None

try:  # V3 在 app.schemas.types，旧版本在 app.schemas
    from app.schemas.types import EventType, NotificationType
except Exception:  # pragma: no cover
    try:
        from app.schemas import NotificationType  # type: ignore
        from app.schemas.types import EventType  # type: ignore
    except Exception:
        NotificationType = None  # type: ignore
        EventType = None  # type: ignore

from .blindbox import COST_PER_DRAW, BlindBoxError, Client, DrawResult  # noqa: E402

DEFAULT_CRON = "30 8 * * *"
DEFAULT_DAILY = 1
MAX_DAILY = 30

# 这个插件只服务 UltraHD：站点下拉只列这些站点，站点管理里没有就提示去添加。
# 域名优先，域名缺失时退回按站点名匹配。
SUPPORTED_DOMAINS = ("ultrahd.net",)
SUPPORTED_NAME_HINTS = ("ultrahd",)


class UltraHDBlindBox(_PluginBase):
    """每天自动抽 UltraHD 幸运盲盒。"""

    plugin_name = "UltraHD幸运盲盒"
    plugin_desc = ("每天自动抽取 UltraHD 的幸运盲盒：默认只抽免费的那 1 次，可配置 1–30 次；"
                   "站点与 Cookie 取自 MoviePilot「站点管理」。")
    plugin_icon = ("https://raw.githubusercontent.com/ibearxxx/MoviePilot-Plugins/"
                   "main/icons/ultrahdblindbox.png")
    plugin_version = "1.0.1"
    plugin_author = "ibearxxx"
    author_url = "https://github.com/"
    plugin_config_prefix = "ultrahdblindbox_"
    plugin_order = 61
    auth_level = 1

    # ---- 运行状态（类属性给默认值，init_plugin 里按配置重建） ----
    _enabled: bool = False
    _notify: bool = True
    _site_id: int = 0
    _daily: int = DEFAULT_DAILY
    _cron: str = DEFAULT_CRON
    _interval: float = 2.0
    _timeout: int = 30
    _proxy: str = ""

    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置；必须允许重复调用，且每一行赋值都写在 if 之外。"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify", True))
        self._site_id = int(config.get("site_id") or 0)
        self._daily = min(MAX_DAILY, max(1, int(config.get("daily") or DEFAULT_DAILY)))
        self._cron = str(config.get("cron") or DEFAULT_CRON)
        self._interval = max(0.0, float(config.get("interval") or 2))
        self._timeout = max(5, int(config.get("timeout") or 30))
        self._proxy = str(config.get("proxy") or "").strip()

        if config.get("onlyonce"):
            self._schedule_once("blindbox_once", self.run_blind_box,
                                "UltraHD幸运盲盒立即运行一次", delay_seconds=3)
            config["onlyonce"] = False
            self.update_config(config)

    def get_state(self) -> bool:
        return self._enabled

    # ------------------------------------------------------- 宿主调度器
    def _schedule_once(self, job_id: str, func, name: str,
                       delay_seconds: float = 0, func_kwargs: dict | None = None) -> bool:
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

    # ------------------------------------------------------------ 站点管理
    @staticmethod
    def _site_oper():
        """取 MoviePilot 的站点管理操作类；宿主没有这个模块时返回 None。"""
        try:
            from app.db.oper.site import SiteOper  # 延迟导入：插件加载期不依赖 DB
            return SiteOper()
        except Exception as err:
            logger.warning(f"取不到站点管理模块（app.db.oper.site）：{err}")
            return None

    @classmethod
    def _list_sites(cls) -> List[Any]:
        """只返回**本插件支持的站点**（UltraHD），且要求站点管理里已配 Cookie。

        跟群聊区那类「只服务某几个站点」的插件一个路子：下拉里只列功能相关的站点，
        站点管理里没有 UltraHD（或没填 Cookie）时返回空列表，页面显示一条提示。
        """
        oper = cls._site_oper()
        if oper is None:
            return []
        try:
            return [site for site in (oper.list_order_by_pri() or [])
                    if cls._is_supported(site) and getattr(site, "cookie", None)]
        except Exception as err:
            logger.warning(f"读站点列表失败：{err}")
            return []

    @classmethod
    def _is_supported(cls, site: Any) -> bool:
        """这个站点是不是 UltraHD：按域名判，域名缺失时退回按站点名判。"""
        domain = str(getattr(site, "domain", "") or "").lower()
        name = str(getattr(site, "name", "") or "").lower()
        if any(domain == supported or domain.endswith("." + supported)
               for supported in SUPPORTED_DOMAINS):
            return True
        return any(name == material for material in SUPPORTED_NAME_HINTS)

    @classmethod
    def _default_site_id(cls, sites: Optional[List[Any]] = None) -> int:
        """默认选中 UltraHD；站点管理里没有它就返回 0（下拉为空 + 页面提示）。"""
        sites = cls._list_sites() if sites is None else sites
        return int(sites[0].id) if sites else 0

    def _load_site(self) -> Any:
        """按配置取站点；Cookie / UA / 代理都来自站点管理，插件不另存凭据。"""
        if not self._site_id:
            raise BlindBoxError("还没有选择站点：请在下拉里选一个已在「站点管理」中配好 Cookie 的站点")
        oper = self._site_oper()
        if oper is None:
            raise BlindBoxError("宿主没有站点管理模块（app.db.oper.site），无法读取 Cookie")
        site = oper.get(int(self._site_id))
        if site is None:
            raise BlindBoxError(f"站点管理里找不到 id={self._site_id} 的站点")
        if not self._is_supported(site):
            raise BlindBoxError(f"所选站点「{getattr(site, 'name', '')}」不是 UltraHD；"
                                f"这个插件只支持 UltraHD，请在配置里重新选择")
        if not getattr(site, "cookie", None):
            raise BlindBoxError(f"站点「{getattr(site, 'name', '')}」在站点管理里没有 Cookie，请先配置")
        return site

    def _make_client(self, site: Any) -> Client:
        """建接口层客户端（测试里替换这一个方法即可打桩）。"""
        return Client(site=site, timeout=self._timeout, proxy=self._proxy, logger=logger)

    # ---------------------------------------------------------------- 表单
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """配置页：照官方插件 PTSiteSignIn 的写法——扁平 ``VForm`` + ``VRow/VCol``，
        开关一行、参数一行，说明统一走 info / warning 的 ``VAlert``，不用 ``VCard`` 分组。
        """

        def field(kind: str, model: str, label: str, md: int = 6, **props: Any) -> dict:
            """一列表单控件：只给 model / label 和控件自己的 props。"""
            return {"component": "VCol", "props": {"cols": 12, "md": md},
                    "content": [{"component": kind,
                                 "props": {"model": model, "label": label, **props}}]}

        def row(*cols: dict) -> dict:
            return {"component": "VRow", "content": list(cols)}

        sites = self._list_sites()
        site_items = [{"title": getattr(site, "name", ""), "value": int(site.id)}
                      for site in sites]

        content: List[dict] = [
            row(field("VSwitch", "enabled", "启用插件", 4),
                field("VSwitch", "notify", "发送通知", 4),
                field("VSwitch", "onlyonce", "保存后立即运行一次", 4)),
            row(field("VCronField", "cron", "执行周期", 4,
                      placeholder="5 位 Cron；例如 30 8 * * *"),
                field("VTextField", "daily", "每日抽取次数", 4,
                      type="number", min=1, max=MAX_DAILY),
                field("VSelect", "site_id", "站点", 4,
                      items=site_items, clearable=False)),
        ]
        if not site_items:
            content.append(row({"component": "VCol", "props": {"cols": 12}, "content": [{
                "component": "VAlert",
                "props": {"type": "warning", "variant": "tonal", "density": "compact"},
                "text": "请先到站点管理中添加站点并配置 Cookie"}]}))

        defaults: Dict[str, Any] = {
            "enabled": False,
            "notify": True,
            "site_id": self._default_site_id(sites),
            "daily": DEFAULT_DAILY,
            "cron": DEFAULT_CRON,
            "onlyonce": False,
        }
        return [{"component": "VForm", "content": content}], defaults

    # ------------------------------------------------------------ 记录与状态
    PAGE_STYLE = (
        # 详情页渲染在宿主的 VCard 里（VDialog + VCard，玻璃主题带半透明背景），
        # 这里绝不铺不透明底色，边框/圆角/阴影一律用宿主的 --app-* token。
        ".bb-page{background:transparent;padding:0;max-width:100%;"
        "box-sizing:border-box;overflow-x:hidden;overflow-wrap:anywhere}"
        ".bb-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));"
        "gap:8px;margin:2px 0 10px}"
        ".bb-stat{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:10px;"
        "border:var(--app-surface-border,rgba(var(--v-theme-on-surface),.10));"
        "background:rgba(var(--v-theme-on-surface),.03)}"
        ".bb-stat-num{font-size:1.12rem;font-weight:700;line-height:1.1;"
        "font-variant-numeric:tabular-nums}"
        ".bb-stat-label{font-size:.72rem;line-height:1.2;color:rgba(var(--v-theme-on-surface),.62)}"
        ".bb-num-info{color:rgb(var(--v-theme-info,33,150,243))}"
        ".bb-num-success{color:rgb(var(--v-theme-success,76,175,80))}"
        ".bb-num-error{color:rgb(var(--v-theme-error,244,67,54))}"
        ".bb-num-warning{color:rgb(var(--v-theme-warning,251,140,0))}"
        ".bb-num-grey{color:rgba(var(--v-theme-on-surface),.38)}"
        ".bb-updated{display:flex;align-items:center;gap:6px;margin-bottom:8px;font-size:.75rem;"
        "color:rgba(var(--v-theme-on-surface),.55)}"
        ".bb-section{display:flex;align-items:center;gap:8px;margin:18px 0 8px}"
        ".bb-table-wrap{border:var(--app-surface-border,rgba(var(--v-theme-on-surface),.10));"
        "border-radius:var(--app-surface-radius,12px);overflow:hidden;"
        "box-shadow:var(--app-surface-shadow,none)}"
        ".bb-table{width:100%;background:transparent}"
        ".bb-table th:nth-child(1),.bb-table td:nth-child(1){width:26%;white-space:nowrap}"
        ".bb-table th:nth-child(2),.bb-table td:nth-child(2){width:14%;white-space:nowrap}"
        ".bb-table thead th{background:rgba(var(--v-theme-on-surface),.04);font-size:.78rem;"
        "color:rgba(var(--v-theme-on-surface),.7);white-space:nowrap}"
        ".bb-table tbody tr:nth-child(even){background:rgba(var(--v-theme-on-surface),.02)}"
        ".bb-table tbody tr:hover{background:rgba(var(--v-theme-primary),.06)}"
        ".bb-table td{vertical-align:middle;padding:10px 14px}"
        ".bb-result{font-size:.84rem;line-height:1.45;word-break:break-word;"
        "font-variant-numeric:tabular-nums;white-space:normal}"
    )

    STATUS_META = {
        "success": ("抽奖成功", "success", "mdi-check-circle"),
        "failed": ("抽奖失败", "error", "mdi-alert-circle"),
        "skipped": ("今日跳过", "warning", "mdi-skip-next-circle"),
    }

    # 记录只在页面展示最近这些条；不逐日流水，避免数据无限长
    RECORD_KEEP = 30

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _progress(self) -> Dict[str, Any]:
        """当天的抽取进度（同日防重就靠它）。"""
        data = self.get_data("progress") or {}
        if not isinstance(data, dict) or data.get("date") != self._today():
            return {"date": self._today(), "drawn": 0, "free": 0, "paid": 0, "cost": 0}
        return data

    def _drawn_today(self) -> int:
        return int(self._progress().get("drawn") or 0)

    def _save_progress(self, drawn: int, free: int, paid: int, cost: int) -> None:
        self.save_data("progress", {"date": self._today(), "drawn": int(drawn),
                                    "free": int(free), "paid": int(paid), "cost": int(cost)})

    def _load_records(self) -> List[dict]:
        data = self.get_data("last_run") or {}
        records = data.get("records") if isinstance(data, dict) else None
        return [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []

    def _append_record(self, kind: str, status: str, message: str) -> None:
        """记一笔（成功的每一次、失败的那一次、跳过的那一次各一条）。"""
        records = self._load_records()
        records.insert(0, {"time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                           "kind": kind, "status": status, "message": str(message or "")})
        summary = str((self.get_data("last_run") or {}).get("summary") or "")
        self.save_data("last_run", {
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "summary": summary,
            "records": records[: self.RECORD_KEEP],
        })

    def _save_summary(self, summary: str) -> None:
        data = self.get_data("last_run") or {}
        data = dict(data) if isinstance(data, dict) else {}
        data["summary"] = str(summary or "")
        data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        data.setdefault("records", [])
        self.save_data("last_run", data)

    # ---------------------------------------------------------------- 详情页
    def get_page(self) -> List[dict]:
        progress = self._progress()
        records = self._load_records()
        if not records:
            return [{"component": "VAlert", "props": {
                "type": "info", "variant": "tonal",
                "text": "还没有执行记录；保存配置后点「保存后立即运行一次」，或等定时任务触发。"}}]

        failed = sum(1 for r in records if r.get("status") == "failed")
        metrics = [("今日抽取", int(progress.get("drawn") or 0), "info"),
                   ("免费", int(progress.get("free") or 0), "success"),
                   ("消耗魔力值", int(progress.get("cost") or 0), "warning"),
                   ("失败", failed, "error")]

        body: List[dict] = [{
            "component": "div", "props": {"class": "bb-stats"},
            "content": [{
                "component": "div", "props": {"class": "bb-stat"},
                "content": [{
                    "component": "div", "content": [
                        {"component": "div",
                         "props": {"class": f"bb-stat-num bb-num-{color if value else 'grey'}"},
                         "text": f"{value:,}"},
                        {"component": "div", "props": {"class": "bb-stat-label"}, "text": label},
                    ]}]} for label, value, color in metrics]}]

        updated = str((self.get_data("last_run") or {}).get("updated") or "")
        summary = str((self.get_data("last_run") or {}).get("summary") or "")
        if updated:
            body.append({"component": "div", "props": {"class": "bb-updated"},
                         "text": f"上次执行 {updated}"})
        if summary:
            body.append({"component": "div", "props": {"class": "bb-updated"}, "text": summary})

        body.append({"component": "div", "props": {"class": "bb-section"}, "content": [
            {"component": "VChip",
             "props": {"size": "small", "variant": "tonal", "color": "primary",
                       "prepend-icon": "mdi-dice-multiple"},
             "text": f"最近记录（{len(records)}）"}]})

        trs: List[dict] = []
        for record in records:
            status = str(record.get("status") or "failed")
            label, color, icon = self.STATUS_META.get(status, self.STATUS_META["failed"])
            trs.append({"component": "tr", "content": [
                {"component": "td", "props": {"class": "bb-result"},
                 "text": str(record.get("time") or "—")},
                {"component": "td", "content": [
                    {"component": "VChip",
                     "props": {"size": "small", "variant": "tonal", "color": color,
                               "prepend-icon": icon},
                     "text": label}]},
                {"component": "td", "props": {"class": "bb-result"},
                 "text": str(record.get("message") or "—")},
            ]})

        body.append({
            "component": "div", "props": {"class": "bb-table-wrap"},
            "content": [{"component": "VTable",
                         "props": {"density": "comfortable", "hover": True, "class": "bb-table"},
                         "content": [
                             {"component": "thead", "content": [
                                 {"component": "tr", "content": [
                                     {"component": "th", "text": "时间"},
                                     {"component": "th", "text": "状态"},
                                     {"component": "th", "text": "结果"}]}]},
                             {"component": "tbody", "content": trs}]}]})

        return [{"component": "style", "text": self.PAGE_STYLE},
                {"component": "div", "props": {"class": "bb-page"}, "content": body}]

    # ---------------------------------------------------------------- 命令与服务
    @staticmethod
    def get_command() -> List[dict]:
        try:
            from app.schemas.types import EventType  # 延迟导入，避免启动期依赖
        except Exception:
            logger.warning("未找到 EventType，跳过远程命令注册")
            return []
        return [{
            "cmd": "/ultrahdblindbox",
            "event": EventType.PluginAction,
            "desc": "立即抽取一次幸运盲盒",
            "category": "签到",
            "data": {"action": "ultrahdblindbox_now"},
        }]

    def get_service(self) -> List[dict]:
        if not self.get_state():
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except ValueError:
            logger.error(f"UltraHD幸运盲盒：Cron 表达式无效：{self._cron}")
            return []
        return [{"id": "UltraHDBlindBox.Daily", "name": "UltraHD幸运盲盒",
                 "trigger": trigger, "func": self.run_blind_box, "kwargs": {}}]

    def get_api(self) -> List[dict]:
        return [{
            "path": "/ultrahdblindbox/run",
            "endpoint": self.run_blind_box,
            "methods": ["POST"],
            "summary": "立即抽取一次幸运盲盒",
            "description": "手动触发一轮盲盒抽取（同日防重依然生效，跑满当天次数后不会再抽）。",
        }]

    def stop_service(self) -> None:
        self._enabled = False
        self._cancel_once("blindbox_once")

    # ------------------------------------------------------------ 命令事件
    def _handle_plugin_action(self, event) -> None:
        event_data = getattr(event, "event_data", None) or {}
        if event_data.get("action") != "ultrahdblindbox_now":
            return
        logger.info("收到远程命令：立即抽取一次幸运盲盒")
        self._schedule_once("blindbox_once", self.run_blind_box,
                            "UltraHD幸运盲盒立即运行一次", delay_seconds=0.5)

    if eventmanager is not None and EventType is not None:  # V3 规范：用 SDK 事件总线
        _handle_plugin_action = eventmanager.register(EventType.PluginAction)(_handle_plugin_action)

    # ---------------------------------------------------------------- 执行
    def _planned_draws(self, state) -> int:
        """今天计划抽几次。

        * 免费额度还在：按配置的次数抽（第 1 次免费，其余付费）。
        * 免费额度已被手动用掉：N=1 就跳过；N>1 时抽 N-1 次付费，把免费那格让出来。
        """
        if int(getattr(state, "free_left", 0) or 0) > 0:
            return self._daily
        return 0 if self._daily <= 1 else self._daily - 1

    @staticmethod
    def _cost_of(picks: List[Tuple[bool, DrawResult]], magic_before,
                 unit_cost: int = 0) -> int:
        """这一轮花掉的魔力值：优先用回包前后的差值，拿不到才按单价推算。

        单价优先取站点首页上的 `#drawCost`（站点自己定价），读不到才用 COST_PER_DRAW。
        """
        if magic_before is not None and picks and picks[-1][1].magic_after is not None:
            delta = float(magic_before) - float(picks[-1][1].magic_after)
            if delta >= 0:
                return int(round(delta))
        unit = int(unit_cost or COST_PER_DRAW)
        return sum(1 for free, _result in picks if not free) * unit

    @staticmethod
    def _summary_text(picks: List[Tuple[bool, DrawResult]], cost: int,
                      failed: str = "") -> str:
        """汇总一句话，形状固定（用户指定）：

        盲盒：免费 1 次 + 付费 3 次，消耗 9,000 魔力值，获得 15GB 上传 ×1、10 魔力值 ×3
        """
        free_n = sum(1 for free, _r in picks if free)
        paid_n = len(picks) - free_n
        kinds: List[str] = []
        if free_n:
            kinds.append(f"免费 {free_n} 次")
        if paid_n:
            kinds.append(f"付费 {paid_n} 次")
        head = "盲盒：" + (" + ".join(kinds) if kinds else "没有抽中任何奖励")
        if cost:
            head += f"，消耗 {cost:,} 魔力值"

        rewards: Dict[str, int] = {}
        for _free, result in picks:
            name = str(getattr(result, "reward", "") or "").strip() or "未知奖励"
            rewards[name] = rewards.get(name, 0) + 1
        if rewards:
            head += "，获得 " + "、".join(f"{name} ×{count}" for name, count in rewards.items())
        if failed:
            head += f"；第 {len(picks) + 1} 次失败：{failed}"
        return head

    def _notify_summary(self, text: str) -> None:
        if not (self._notify and NotificationType is not None):
            return
        try:
            self.post_message(mtype=NotificationType.Plugin,
                              title="UltraHD幸运盲盒", text=text)
        except Exception as err:
            logger.warning(f"发送盲盒通知失败：{err}")

    def _fail(self, message: str, kind: str = "盲盒") -> str:
        """统一的失败收尾：写日志、记一笔、发一条通知。"""
        logger.error(f"{kind}失败：{message}")
        self._append_record("失败", "failed", message)
        text = f"盲盒：失败 —— {message}"
        self._save_summary(text)
        self._notify_summary(text)
        return text

    def run_blind_box(self) -> str:
        """跑一轮：读状态 → 按规则决定抽几次 → 抽 → 汇总一条通知。"""
        stamp = datetime.fromtimestamp(os.path.getmtime(__file__)).strftime("%m-%d %H:%M")
        logger.info(f"盲盒开始：代码版本 {self.plugin_version} ，最后修改时间 {stamp}，"
                    f"每日次数 {self._daily}，站点 ID {self._site_id or '未选择'}")

        try:
            site = self._load_site()
            client = self._make_client(site)
            state = client.state()
        except NotImplementedError as err:
            return self._fail(str(err))
        except BlindBoxError as err:
            return self._fail(str(err))
        except Exception as err:
            return self._fail(f"{type(err).__name__}: {str(err)[:120]}")

        site_name = str(getattr(site, "name", "") or "")
        logger.info(f"{site_name} 状态：剩余免费 {state.free_left} 次，"
                    f"今日剩余 {state.total_left} 次，魔力值 {state.magic}，"
                    f"每次消耗 {state.cost or COST_PER_DRAW}")

        total_left = int(getattr(state, "total_left", 0) or 0)
        free_left_now = int(getattr(state, "free_left", 0) or 0)
        if total_left <= 0 and free_left_now <= 0:
            logger.info("站点显示今日已无可抽次数，跳过")
            self._append_record("跳过", "skipped", "站点显示今日抽取次数已用完")
            return "盲盒：今日次数已用完，跳过"

        prior = self._progress()
        prior_drawn = int(prior.get("drawn") or 0)
        prior_free = int(prior.get("free") or 0)
        prior_paid = int(prior.get("paid") or 0)
        prior_cost = int(prior.get("cost") or 0)

        planned = self._planned_draws(state)
        if planned <= 0:
            logger.info("今日免费额度已用掉，且「每日抽取次数」为 1，按规则跳过（不抽付费）")
            self._append_record("跳过", "skipped", "今日免费额度已用掉，按规则不抽付费")
            today = self._progress()          # 原样落一次：只补当天的记录，不覆盖已累计的数字
            self._save_progress(today.get("drawn") or 0, today.get("free") or 0,
                                today.get("paid") or 0, today.get("cost") or 0)
            return "盲盒：今日免费额度已用掉，按规则跳过"

        # 「已完成」必须和 planned 同一口径：
        #   免费还在 → planned = N（含免费那次），已完成用当天总抽取次数；
        #   免费已用 → planned = N-1（只剩付费），已完成只能用当天**已付费**次数。
        # 两处都用总次数就会把免费那一次重复扣掉：N=2、免费已被本插件抽掉时
        # planned=1、done=1 → 直接跳过。用户看到的现象正是「改成 2 还是不抽」。
        done = prior_drawn if free_left_now > 0 else prior_paid
        if done >= planned:
            logger.info(f"今天已经抽过 {done} 次（计划 {planned} 次），跳过——同一天不重复抽")
            return f"盲盒：今天已抽过 {done} 次，跳过"

        picks: List[Tuple[bool, DrawResult]] = []
        failed = ""
        free_left = int(state.free_left or 0)
        remaining = planned - done
        for index in range(remaining):
            use_free = free_left > 0
            try:
                result = client.draw(free=use_free)
            except NotImplementedError as err:
                failed = str(err)
                break
            except BlindBoxError as err:
                failed = str(err)
                break
            except Exception as err:  # 单次异常不能把整轮拖垮，但要停下来
                failed = f"{type(err).__name__}: {str(err)[:120]}"
                break
            if not result.ok:
                failed = str(result.reward or "站点没有返回奖励内容")
                break
            if use_free:
                free_left -= 1
            picks.append((use_free, result))
            # 序号与「今日已抽」用的是当天总次数，不是上面那个按付费口径算的 done
            total_drawn = prior_drawn + len(picks)
            logger.info(f"{site_name} 第 {total_drawn} 次"
                        f"（{'免费' if use_free else '付费'}）：{result.reward}")
            # 每抽成一次就落盘：进程中途被打断也不会在下次运行时重复付费抽
            run_free = sum(1 for free, _r in picks if free)
            run_paid = len(picks) - run_free
            self._save_progress(total_drawn,
                                prior_free + run_free,
                                prior_paid + run_paid,
                                prior_cost + self._cost_of(picks, state.magic,
                                                           int(getattr(state, "cost", 0) or 0)))
            if index < remaining - 1 and self._interval:
                time.sleep(self._interval)

        cost = self._cost_of(picks, state.magic, int(getattr(state, "cost", 0) or 0))
        text = self._summary_text(picks, cost, failed)
        logger.info(f"盲盒结果：{text}")
        self._append_record("抽取", "success" if not failed else "failed", text)
        self._save_summary(text)
        self._notify_summary(text)
        return text
