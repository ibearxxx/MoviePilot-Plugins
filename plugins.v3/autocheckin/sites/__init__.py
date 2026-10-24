"""站点注册表：一个站点一个模块，按顺序执行。

新增站点：在本目录放一个 ``xxx.py``，类里定义 ``key/name/url`` 和
``signin(self, conf, ctx) -> (ok: bool, msg: str)``，然后加到下面的 ``SITES``。
"""
from __future__ import annotations

from .hdhaven import Hdhaven
from .jying import Jying
from .re0 import Re0
from .wn4k import Wn4k

SITES = [Re0(), Hdhaven(), Jying(), Wn4k()]

BY_KEY = {site.key: site for site in SITES}


def get_site(key: str):
    return BY_KEY.get(key)


__all__ = ["SITES", "BY_KEY", "get_site"]
