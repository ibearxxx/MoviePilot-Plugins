"""聚影 —— Django + Vue SPA。

接口（从站点自己的前端 bundle 里读出来并实测，2026-10-20）：

    登录      POST /api/app/login/            JSON {username, password}
              响应体顶层有 ``token``，之后所有请求带 ``X-App-User-Token: <token>``
    签到状态  GET  /api/app/checkin/stats/    {"checked_today":true,"can_checkin":false,
                                               "checkin_mode":"normal","my_total_days":135,...}
    普通签到  POST /api/app/checkin/do/
    幸运签到  GET  /api/app/checkin/luck/     {"can_draw":false,"can_draw_reason":"...",
                                               "min_delta":-5,"max_delta":50,"daily_checked_today":true}
              POST /api/app/checkin/luck/draw/        JSON {"accept_risk": true}
    幸运奖池  GET  /api/app/checkin/bonus-pool/
              {"pool":{"id":4,"remaining_points":0,"distributed_points":500000,
                       "requires_checkin":true,"daily_draw_limit":1,...}}
              POST /api/app/checkin/bonus-pool/draw/  JSON {"pool_id": <id>}

要点：
  * **普通签到与幸运签到二选一**（站点原话：「每日北京时间与普通签到二选一，结果不可重抽」），
    做成配置项，默认普通签到。
  * 奖池是「一次性补充、发完即止、每人每天领一次」——``remaining_points`` 为 0 时
    **不要请求**领取接口，省一次无意义的请求。
"""
from __future__ import annotations


class Jying:
    key = "jying"
    name = "聚影"
    url = "https://www.jying.top"
    needs_browser = False
    needs_proxy = False          # 国内站，直连即可

    TOKEN_HEADER = "X-App-User-Token"

    extra_config = {
        "checkin_type": {
            "component": "VSelect",
            "props": {
                "model": "unused",
                "label": "签到类型",
                "hint": "普通 +5 / 幸运 -5~+50",
                "items": [
                    {"title": "普通签到（+5 积分）", "value": "normal"},
                    {"title": "幸运签到（-5 ~ +50 积分）", "value": "luck"},
                ],
                "clearable": False,
            },
        },
        "claim_bonus_pool": {
            "component": "VSwitch",
            "props": {"model": "unused", "label": "顺带领取幸运奖池",
                      "hint": "每天限一次，发完即止"},
        },
    }
    extra_defaults = {"checkin_type": "normal", "claim_bonus_pool": True}

    # ------------------------------------------------------------ 认证
    def _prepare(self, conf: dict, ctx) -> tuple[bool, str]:
        """确保会话里有 csrftoken 和用户 token。"""
        session = ctx.session()

        # 1) 先拿 csrftoken（访问登录页即可 Set-Cookie）
        if not session.cookies.get("csrftoken"):
            ctx.get(f"{self.url}/login")

        # 2) 配置里直接给了 token 就用它
        token = str(conf.get("token") or "").strip()
        if token:
            session.headers[self.TOKEN_HEADER] = token
            return True, "使用配置的 Token"

        # 3) 否则用账号密码换 token
        username = str(conf.get("username") or "").strip()
        password = str(conf.get("password") or "")
        if not username or not password:
            return False, "未配置账号密码或 Token"

        csrf = session.cookies.get("csrftoken") or ""
        headers = {"Referer": f"{self.url}/login", "X-Requested-With": "XMLHttpRequest"}
        if csrf:
            headers["X-CSRFToken"] = csrf

        resp = ctx.post(f"{self.url}/api/app/login/",
                        json_body={"username": username, "password": password},
                        headers=headers)
        if resp is None:
            return False, "登录请求失败（网络）"
        try:
            data = resp.json()
        except Exception:
            return False, f"登录响应无法解析（HTTP {resp.status_code}）"

        if resp.status_code >= 400 or (isinstance(data, dict) and data.get("status") == "error"):
            msg = ""
            if isinstance(data, dict):
                msg = data.get("message") or data.get("detail") or ""
            return False, f"登录失败：{msg or f'HTTP {resp.status_code}'}"

        # token 在响应体顶层（站点前端代码是 `let a = i.data; a?.token`），
        # 但不同版本可能包一层 data，两种都认。
        token = ""
        if isinstance(data, dict):
            token = str(data.get("token") or "")
            if not token and isinstance(data.get("data"), dict):
                token = str(data["data"].get("token") or "")
        if not token:
            keys = sorted(data.keys()) if isinstance(data, dict) else []
            return False, f"登录成功但没拿到 token（响应字段 {keys}）"
        session.headers[self.TOKEN_HEADER] = token
        return True, "登录成功"

    def _headers(self, ctx) -> dict:
        csrf = ctx.session().cookies.get("csrftoken") or ""
        headers = {"Referer": f"{self.url}/checkin", "X-Requested-With": "XMLHttpRequest"}
        if csrf:
            headers["X-CSRFToken"] = csrf
        return headers

    @staticmethod
    def _json(resp) -> dict:
        try:
            data = resp.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ------------------------------------------------------------ 签到
    def signin(self, conf: dict, ctx) -> tuple[bool, str]:
        ok, login_msg = self._prepare(conf, ctx)
        if not ok:
            return False, login_msg

        headers = self._headers(ctx)
        kind = str(conf.get("checkin_type") or "normal")

        # 1) 今天签没签
        stats = ctx.get(f"{self.url}/api/app/checkin/stats/", headers=headers)
        if stats is None:
            return False, "查询签到状态失败（网络）"
        if stats.status_code == 401:
            return False, "签到接口返回 401（token 无效或已过期）"
        if stats.status_code != 200:
            return False, f"查询签到状态返回 HTTP {stats.status_code}"
        state = self._json(stats)

        notes: list[str] = []
        gain = None          # 本次获得的积分；站点没给就是 None，绝不编
        fresh = False        # 这一轮真的签上了（不是本来就已经签过）
        signed = state.get("checked_today") is True or state.get("can_checkin") is False

        if signed:
            pass
        elif kind == "luck":
            # 2a) 幸运签到：先问能不能抽，再抽
            luck_resp = ctx.get(f"{self.url}/api/app/checkin/luck/", headers=headers)
            luck = self._json(luck_resp) if luck_resp is not None else {}
            if luck.get("can_draw") is not True:
                reason = luck.get("can_draw_reason") or "当前不可幸运签到"
                return False, f"幸运签到不可用：{reason}"
            draw = ctx.post(f"{self.url}/api/app/checkin/luck/draw/",
                            json_body={"accept_risk": True}, headers=headers)
            if draw is None:
                return False, "幸运签到请求失败（网络）"
            info = self._json(draw)
            if info.get("status") == "error":
                return False, f"幸运签到失败：{info.get('message') or '未知原因'}"
            delta = info.get("delta") or info.get("reward_points")
            gain = delta if isinstance(delta, int) else None
            fresh = True
            signed = True
        else:
            # 2b) 普通签到
            resp = ctx.post(f"{self.url}/api/app/checkin/do/", json_body={}, headers=headers)
            if resp is None:
                return False, "签到请求失败（网络）"
            if resp.status_code == 401:
                return False, "签到接口返回 401（token 无效）"
            info = self._json(resp)
            if info.get("status") == "error":
                body = str(info.get("message") or "")
                if "已签" in body:
                    signed = True
                else:
                    return False, f"签到失败：{body or '未知原因'}"
            else:
                points = info.get("reward_points")
                gain = points if isinstance(points, int) else None
                fresh = True
                signed = True

        # 3) 幸运奖池：一次性补充、发完即止，剩 0 就不请求
        if conf.get("claim_bonus_pool", True) and signed:
            pool_resp = ctx.get(f"{self.url}/api/app/checkin/bonus-pool/", headers=headers)
            pool = (self._json(pool_resp) if pool_resp is not None else {}).get("pool") or {}
            remaining = pool.get("remaining_points")
            pool_id = pool.get("id")
            if isinstance(remaining, int) and remaining <= 0:
                if ctx.logger:
                    ctx.logger.info(f"聚影 奖池已发完（剩余 {remaining}），跳过领取接口")
            elif isinstance(remaining, int) and pool_id:
                drawn = ctx.post(f"{self.url}/api/app/checkin/bonus-pool/draw/",
                                 json_body={"pool_id": pool_id}, headers=headers)
                info = self._json(drawn) if drawn is not None else {}
                if info.get("status") == "error":
                    notes.append(f"奖池领取失败：{info.get('message') or '未知原因'}")
                else:
                    got = info.get("reward_points") or info.get("amount")
                    if isinstance(got, int):
                        notes.append(f"奖池 {got:+d}积分")

        # 累计天数：签到前查到的是旧值，签完再问一次才准
        days = state.get("my_total_days")
        if fresh:
            latest = ctx.get(f"{self.url}/api/app/checkin/stats/", headers=headers)
            if latest is not None and latest.status_code == 200:
                days = self._json(latest).get("my_total_days") or days

        parts: list[str] = []
        if days:
            parts.append(f"累计签到 {days} 天")
        if gain is not None:
            parts.append(f"{gain:+d}积分")
        elif not fresh:
            parts.append("今日已领取")
        parts += notes
        if not parts:
            return False, "签到未完成，且没有可用提示"
        return True, "，".join(parts)
