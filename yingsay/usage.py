"""本机统计 MiMo Token Plan 用量（按官方扣费规则把 token / 音频时长折算成 Credits）。

规则（https://mimo.mi.com/docs/en-US/price/tokenplan/subscription）：
- 语言模型按 token：mimo-v2.6-flash 输入 100 / 缓存命中 2 / 输出 200 Credits；pro 300 / 2.5 / 600
- ASR（mimo-v2.5-asr）按音频时长：30M Credits / 小时（按秒计）
- 北京时间 00:00–08:00 闲时 0.8 倍
实际扣费以 MiMo 控制台为准，这里只是本机估算。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# (输入未命中, 输入缓存命中, 输出) Credits / token
TOKEN_PRICES = [
    ("mimo-v2.6-pro", (300, 2.5, 600)),
    ("mimo-v2.5-pro", (300, 2.5, 600)),
    ("mimo-v2-pro", (300, 2.5, 600)),
    ("mimo-v2.6-flash", (100, 2, 200)),
    ("mimo-v2.5", (100, 2, 200)),
]
DEFAULT_PRICE = (100, 2, 200)
ASR_CREDITS_PER_SECOND = 30_000_000 / 3600
PLAN_MONTHLY = {"Lite": 4.1e9, "Standard": 11e9, "Pro": 38e9, "Max": 82e9}
_BJ = _dt.timezone(_dt.timedelta(hours=8))


def fmt_num(n: float) -> str:
    """12 / 3.4K / 56.7M / 1.23B —— 自动选单位，保留 3 位有效数字。"""
    n = float(n or 0)
    for unit, base in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= base:
            v = n / base
            s = f"{v:.2f}" if v < 10 else (f"{v:.1f}" if v < 100 else f"{v:.0f}")
            return s.rstrip("0").rstrip(".") + unit if "." in s else s + unit
    return f"{n:.0f}"


def fmt_duration(sec: float) -> str:
    sec = float(sec or 0)
    if sec < 60:
        return f"{sec:.0f} 秒"
    if sec < 3600:
        return f"{sec / 60:.1f} 分钟"
    return f"{sec / 3600:.2f} 小时"


def token_price(model: str) -> tuple:
    m = (model or "").lower()
    for prefix, price in TOKEN_PRICES:
        if m.startswith(prefix):
            return price
    return DEFAULT_PRICE


def off_peak_factor(now: _dt.datetime | None = None) -> float:
    now = (now or _dt.datetime.now(_BJ)).astimezone(_BJ)
    return 0.8 if now.hour < 8 else 1.0


def estimate_credits(model: str, usage: dict | None, audio_seconds: float | None,
                     now: _dt.datetime | None = None) -> tuple[float, dict]:
    """返回 (credits, 明细)。ASR 按时长；其他按 token。"""
    usage = usage or {}
    f = off_peak_factor(now)
    if "asr" in (model or "").lower():
        sec = max(1, round(audio_seconds or 0)) if audio_seconds else 0
        return sec * 30_000_000 / 3600 * f, {"asr_seconds": sec}
    p_in = int(usage.get("prompt_tokens") or 0)
    out = int(usage.get("completion_tokens") or 0)
    cached = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    miss, hit, o = token_price(model)
    credits = ((p_in - cached) * miss + cached * hit + out * o) * f
    return credits, {"in_tokens": p_in, "cached_tokens": cached, "out_tokens": out}


class UsageMeter:
    FIELDS = ("credits", "requests", "asr_seconds", "in_tokens", "cached_tokens", "out_tokens")

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        try:
            self.data: dict = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self.data = {}

    @staticmethod
    def _today() -> str:
        return _dt.date.today().isoformat()

    def record(self, model: str, usage: dict | None, audio_seconds: float | None = None) -> float:
        credits, detail = estimate_credits(model, usage, audio_seconds)
        with self.lock:
            day = self.data.setdefault(self._today(), {})
            day["credits"] = day.get("credits", 0) + credits
            day["requests"] = day.get("requests", 0) + 1
            for k, v in detail.items():
                day[k] = day.get(k, 0) + v
            # 只保留最近 400 天
            if len(self.data) > 400:
                for k in sorted(self.data)[:-400]:
                    self.data.pop(k, None)
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self.path)
            except Exception as e:  # pragma: no cover
                log.debug("写入用量失败: %s", e)
        return credits

    def _sum(self, days) -> dict:
        tot = {k: 0 for k in self.FIELDS}
        with self.lock:
            for d in days:
                for k in self.FIELDS:
                    tot[k] += (self.data.get(d) or {}).get(k, 0)
        return tot

    def today(self) -> dict:
        return self._sum([self._today()])

    def month(self) -> dict:
        prefix = self._today()[:7]
        return self._sum([d for d in list(self.data) if d.startswith(prefix)])

    def short(self) -> str:
        return f"{fmt_num(self.today()['credits'])} Credits"

    def summary(self, plan: str = "Max") -> str:
        t, m = self.today(), self.month()
        tokens = t["in_tokens"] + t["out_tokens"]
        lines = [f"今日：{fmt_num(t['credits'])} Credits（语音 {fmt_duration(t['asr_seconds'])} · "
                 f"文本 {fmt_num(tokens)} tokens · {t['requests']} 次请求）"]
        quota = PLAN_MONTHLY.get(plan)
        mline = f"本月：{fmt_num(m['credits'])} Credits"
        if quota:
            mline += f" / {fmt_num(quota)}（{plan} 月额度的 {m['credits'] / quota * 100:.3g}%）"
        lines.append(mline)
        return "\n".join(lines)
