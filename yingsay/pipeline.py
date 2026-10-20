"""把一段录音变成最终要输入的文字。"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np

from . import prompts
from .audio import encode_audio, split_for_upload
from .live import smart_join
from .mimo import MiMoError

log = logging.getLogger(__name__)


@dataclass
class Context:
    exe: str = ""
    title: str = ""
    hwnd: int = 0
    selected_text: str | None = None


@dataclass
class Result:
    mode: str
    raw: str = ""
    text: str = ""
    action: str = "paste"  # paste / show / none
    note: str = ""
    timings: dict = field(default_factory=dict)


class Processor:
    _wav_only = False  # MP3 被接口拒绝过一次后，本次运行内都用 WAV

    def __init__(self, cfg, client, executor: ThreadPoolExecutor | None = None):
        self.cfg = cfg
        self.client = client
        self.ex = executor

    # -------------------------------------------------------- ASR
    def transcribe_segment(self, pcm: np.ndarray) -> str:
        """识别一段（≤150 秒）音频；MP3 失败时自动用 WAV 重试。"""
        model = self.cfg.get("asr_model")
        lang = self.cfg.get("asr_language")
        fmt = "wav" if Processor._wav_only else (self.cfg.get("upload_format") or "mp3")
        data, mime = encode_audio(pcm, fmt)
        sec = len(pcm) / 16000
        hedge = self.hedge_delay(1.6 + sec * 0.04)
        try:
            return self.client.transcribe(data, model, lang, mime=mime, seconds=sec, hedge=hedge).strip()
        except MiMoError as e:
            if mime == "audio/wav" or "鉴权" in str(e):
                raise
            log.warning("MP3 上传失败（%s），改用 WAV 重试", e)
            Processor._wav_only = True
            data, mime = encode_audio(pcm, "wav")
            return self.client.transcribe(data, model, lang, mime=mime, seconds=sec, hedge=hedge).strip()

    def hedge_delay(self, base: float) -> float | None:
        if not self.cfg.get("hedge_requests"):
            return None
        return max(0.8, base * float(self.cfg.get("hedge_scale") or 1.0))

    def transcribe(self, pcm: np.ndarray) -> str:
        chunks = split_for_upload(pcm)
        if len(chunks) == 1:
            return self.transcribe_segment(chunks[0])
        ex = self.ex or ThreadPoolExecutor(max_workers=4)
        return smart_join(list(ex.map(self.transcribe_segment, chunks)))

    def _llm(self, model_key: str, messages: list, raw_len: int, hedge: bool = False) -> str:
        return self.client.chat(
            self.cfg.get(model_key), messages,
            thinking=bool(self.cfg.get("llm_thinking")),
            max_tokens=max(512, raw_len * 3),
            hedge=self.hedge_delay(2.2 + raw_len * 0.012) if hedge else None,
        )

    # -------------------------------------------------------- main
    def process(self, mode: str, pcm: np.ndarray, ctx: Context, raw: str | None = None) -> Result:
        """raw 不为 None 表示已经由“边说边识别”得到了转写结果。"""
        res = Result(mode=mode)
        engine = self.cfg.get("engine")
        reps = self.cfg.get("replacements")
        t0 = time.monotonic()

        if mode in ("dictate", "translate") and engine == "omni" and raw is None:
            data, mime = encode_audio(pcm, "wav" if Processor._wav_only else (self.cfg.get("upload_format") or "mp3"))
            out = self.client.audio_chat(self.cfg.get("omni_model"), data,
                                         prompts.omni_system(self.cfg, ctx.exe, ctx.title, translate=(mode == "translate")),
                                         prompts.omni_instruction(self.cfg, ctx.exe, ctx.title),
                                         thinking=bool(self.cfg.get("llm_thinking")), mime=mime)
            res.timings["omni"] = time.monotonic() - t0
            _, text = prompts.extract_out(out)
            res.raw = text
            res.text = prompts.apply_replacements(text, reps)
            if not res.text:
                res.action = "none"
            return res

        if raw is None:
            raw = self.transcribe(pcm)
        res.timings["asr"] = time.monotonic() - t0
        res.raw = raw
        if prompts.is_effectively_empty(raw):
            res.action = "none"
            res.note = "未识别到语音"
            return res

        if mode == "command":
            return self._command(res, raw, ctx)

        if mode == "dictate" and (engine == "asr_only" or
                                  not prompts.needs_polish(raw, self.cfg.get("polish_skip_chars"))):
            res.text = prompts.apply_replacements(raw, reps)
            return res

        t1 = time.monotonic()
        try:
            out = self._llm("llm_model", prompts.dictate_messages(self.cfg, raw, ctx.exe, ctx.title,
                                                                  translate=(mode == "translate")), len(raw), hedge=True)
            kind, text = prompts.extract_out(out)
        except Exception as e:  # 润色失败绝不丢字：退回原始识别
            log.warning("润色失败，使用原始识别结果: %s", e)
            res.text = prompts.apply_replacements(raw, reps)
            res.note = f"润色失败，已输入原始识别：{e}"
            return res
        res.timings["llm"] = time.monotonic() - t1

        # 安全阀：防止模型“回答”了口述内容而不是整理它
        if mode == "dictate":
            if (not text and len(raw) > 8) or len(text) > len(raw) * 3 + 80:
                log.info("润色结果异常(len %d -> %d)，退回原文", len(raw), len(text))
                text = raw
        res.text = prompts.apply_replacements(text, reps)
        if not res.text:
            res.action = "none"
        return res

    def _command(self, res: Result, instruction: str, ctx: Context) -> Result:
        t1 = time.monotonic()
        if ctx.selected_text:
            out = self._llm("command_model",
                            prompts.command_messages(self.cfg, ctx.selected_text, instruction, ctx.exe, ctx.title),
                            len(ctx.selected_text) + len(instruction))
            kind, text = prompts.extract_out(out)
            res.text = text
            res.action = "show" if kind == "answer" else "paste"
        else:
            out = self._llm("command_model", prompts.ask_messages(self.cfg, instruction), 400)
            res.text = out.strip()
            res.action = "show"
        res.timings["llm"] = time.monotonic() - t1
        if not res.text:
            res.action = "none"
        return res
