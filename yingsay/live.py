"""边说边识别：录音过程中在停顿处切段，提前把已说完的部分送去 ASR。

松开热键时只需要识别最后一小段，长句听写的等待时间从“整段识别时间”降为“最后一段识别时间”。
"""
from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor

import numpy as np

from .audio import SAMPLE_RATE, frame_db

log = logging.getLogger(__name__)

FRAME = 480  # 30ms


def smart_join(parts: list[str]) -> str:
    out = ""
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        if out and re.search(r"[A-Za-z0-9,.!?;:]$", out) and re.match(r"[A-Za-z0-9]", p):
            out += " "
        out += p
    return out


class LiveTranscriber:
    def __init__(self, recorder, transcribe_fn, executor: ThreadPoolExecutor,
                 min_seg: float = 2.5, max_seg: float = 15.0, pause: float = 0.35, interval: float = 0.15):
        self.rec = recorder
        self.transcribe_fn = transcribe_fn
        self.ex = executor
        self.min_seg = int(min_seg * SAMPLE_RATE)
        self.max_seg = int(max_seg * SAMPLE_RATE)
        self.pause_frames = max(1, int(pause * SAMPLE_RATE / FRAME))
        self.interval = interval
        self.cut = 0  # 已提交部分的结束位置（样本）
        self.futures: list[Future] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._db: list[float] = []  # 已扫描帧的能量
        self.segments = 0

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="live-asr")
        self._thread.start()

    def _threshold(self) -> float:
        recent = np.asarray(self._db[-400:], dtype=np.float32)
        floor = float(np.percentile(recent, 10)) if len(recent) else -70.0
        return max(-50.0, min(-30.0, floor + 10.0))

    def _scan(self) -> None:
        n = self.rec.length
        start = len(self._db) * FRAME
        if n - start >= FRAME:
            new = self.rec.get(start, start + ((n - start) // FRAME) * FRAME)
            self._db.extend(frame_db(new, FRAME).tolist())
        total = len(self._db) * FRAME
        seg_len = total - self.cut
        if seg_len < self.min_seg:
            return
        thr = self._threshold()
        tail = self._db[-self.pause_frames:]
        if len(tail) == self.pause_frames and max(tail) < thr:
            # 停顿：在静音中间切
            self._submit(total - (self.pause_frames * FRAME) // 2)
        elif seg_len >= self.max_seg:
            # 一直没停顿：在最近 3 秒里能量最低处切
            lo = max(self.cut // FRAME + 1, len(self._db) - int(3 * SAMPLE_RATE / FRAME))
            window = self._db[lo:]
            if window:
                i = lo + int(np.argmin(window))
                self._submit(i * FRAME + FRAME // 2)

    def _has_speech(self, a: int, b: int, db=None) -> bool:
        thr = self._threshold() - 5.0  # 宁可多送一段静音，也不能丢字
        db = self._db[a // FRAME: b // FRAME] if db is None else db
        return sum(1 for v in db if v > thr) >= int(0.2 * SAMPLE_RATE / FRAME)

    def _submit(self, end: int) -> None:
        a, b = self.cut, end
        self.cut = end
        if b <= a or not self._has_speech(a, b):
            return
        seg = self.rec.get(a, b)
        self.futures.append(self.ex.submit(self.transcribe_fn, seg))
        self.segments += 1
        log.debug("提前识别第 %d 段 %.1fs", self.segments, (b - a) / SAMPLE_RATE)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._scan()
            except Exception:
                log.exception("边说边识别出错")

    # ------------------------------------------------------------------
    def stop_scanning(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

    def finish(self, pcm: np.ndarray) -> str:
        """传入完整录音，识别剩余尾段并按顺序拼接全部结果。"""
        self.stop_scanning()
        tail = pcm[self.cut:]
        futures = list(self.futures)
        need_tail = len(tail) >= SAMPLE_RATE * 0.2
        if need_tail and futures and not self._has_speech(0, 0, frame_db(tail, FRAME).tolist()):
            need_tail = False  # 最后只是停顿/静音，不必再等一次网络请求
        tail_future = self.ex.submit(self.transcribe_fn, tail) if need_tail else None
        parts = [f.result() for f in futures]
        if tail_future is not None:
            parts.append(tail_future.result())
        return smart_join(parts)

    def cancel(self) -> None:
        self.stop_scanning()
        for f in self.futures:
            f.cancel()
