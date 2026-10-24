"""蜗牛4K —— 苹果CMS 系，纯 HTTP 可完成。

签到接口（从签到页内联 JS 读到，2026-10-20 核实）：
    POST /user/checkin.html  ->  JSON
    {code: 1,    msg: "签到成功", info: {points: "10"}}   # 成功
    {code: 1001, msg: "今日已签到"}                        # 已签
登录：POST /user/login.html，表单字段 user_name / user_pwd。
"""
from __future__ import annotations

import json


class Wn4k:
    key = "wn4k"
    name = "蜗牛4K"
    url = "https://www.wn4k.com"
    needs_browser = False
    needs_proxy = False          # 国内站，直连即可

    SUCCESS = "签到成功"
    ALREADY = "今日已签到"

    # ------------------------------------------------------------ 登录
    def _login(self, conf: dict, ctx) -> tuple[bool, str]:
        username = str(conf.get("username") or "").strip()
        password = str(conf.get("password") or "")
        if not username or not password:
            return False, "未配置账号密码"

        resp = ctx.post(f"{self.url}/user/login.html",
                        data={"user_name": username, "user_pwd": password},
                        headers={"Referer": f"{self.url}/user/login.html",
                                 "X-Requested-With": "XMLHttpRequest"})
        if resp is None:
            return False, "登录请求失败（网络）"

        # 苹果CMS 一般返回 JSON：{"code":1,"msg":"登录成功"}；失败时也可能是跳转
        try:
            data = resp.json()
            if isinstance(data, dict) and data.get("code") in (1, "1"):
                return True, "登录成功"
            if isinstance(data, dict) and data.get("msg"):
                return False, f"登录失败：{data.get('msg')}"
        except (ValueError, json.JSONDecodeError):
            pass

        # 回退：用会话访问会员中心，看是否还停在登录页
        page = ctx.page(f"{self.url}/user/index/")
        if page and "user/login" in page and "登录" in page:
            return False, "登录失败（页面仍要求登录）"
        return True, "登录成功"

    # ------------------------------------------------------------ 签到
    def signin(self, conf: dict, ctx) -> tuple[bool, str]:
        if not ctx.cookie:
            ok, msg = self._login(conf, ctx)
            if not ok:
                return False, msg

        resp = ctx.post(f"{self.url}/user/checkin.html",
                        headers={"Referer": f"{self.url}/user/checkin.html",
                                 "X-Requested-With": "XMLHttpRequest"})
        if resp is None:
            return False, "签到请求失败（网络）"

        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            # 不是 JSON：退回读签到页文案
            page = ctx.page(f"{self.url}/user/checkin.html")
            if self.ALREADY in page:
                return True, "今日已领取"
            if "请先登录" in page:
                return False, "未登录（Cookie 失效）"
            return False, "签到响应无法解析"

        code = data.get("code")
        if code in (1, "1"):
            points = ((data.get("info") or {}).get("points")) or ""
            # 状态列已经写了「签到成功」，这里只留站点返回的积分，不重复
            return True, f"获得 {points} 积分" if points else ""
        if code in (1001, "1001"):
            return True, "今日已领取"
        return False, str(data.get("msg") or f"签到失败（code={code}）")
