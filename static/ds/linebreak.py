"""共享断行辅助（由 sync.sh 分发到各系统 ds/linebreak.py）。

模板过滤器：
  {{ name | nobreak_tail }}          门店/群名末尾后缀包进 .ds-nowrap
  {{ f.name | ds_wbr_filename }}     文件名在 _ 与日期 token 处插入 <wbr>

规则：先 HTML 转义，再包 span/wbr；只保护原子 token，不对整段文本 nowrap。
"""
from __future__ import annotations

import re

from markupsafe import Markup, escape

_TAIL_RE = re.compile(r"(零售中心|交付服务中心|用户中心|体验中心|服务中心|专属服务群|服务群|战区)$")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def nobreak_tail(value) -> Markup:
    """把名称末尾的「零售中心/服务群/战区」等后缀包成不换行片段。"""
    html = str(escape("" if value is None else value))
    if _TAIL_RE.search(html):
        html = _TAIL_RE.sub(r'<span class="ds-nowrap">\1</span>', html, count=1)
    return Markup(html)


def ds_wbr_filename(value) -> Markup:
    """文件名在 `_` 后与日期 token 前插入 <wbr>，日期本身 nowrap。"""
    html = str(escape("" if value is None else value)).replace("_", "_<wbr>")

    def _date(match: re.Match[str]) -> str:
        start = match.start()
        prefix = "" if html[max(0, start - 5):start] == "<wbr>" else "<wbr>"
        return prefix + '<span class="ds-nowrap">' + match.group(0) + "</span>"

    return Markup(_DATE_RE.sub(_date, html))


# 兼容更直白的模板写法
wbr_filename = ds_wbr_filename
