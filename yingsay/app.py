"""主程序：把热键、录音、MiMo、粘贴、界面串起来。"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor

from . import APP_NAME, DISPLAY_NAME, __version__, winutil
from .config import Config, app_dir
from .config import TOKEN_PLAN_CLUSTERS
from .mimo import MiMoClient, MiMoError
from .pipeline import Context, Processor

log = logging.getLogger("yingsay")


def setup_logging(home: Path, verbose: bool = False) -> None:
    handler = logging.handlers.RotatingFileHandler(home / "app.log", maxBytes=2_000_000, backupCount=2,
                                                   encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None and not getattr(sys, "frozen", False):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


class App:
    def __init__(self, cfg: Config):
        from .audio import Recorder
        from .hotkeys import GlobalHotkeys, HotkeyStateMachine
        from .ui import UI, Tray

        self.cfg = cfg
        self.home = app_dir()
        self.rec_dir = self.home / "recordings"
        self.rec_dir.mkdir(exist_ok=True)
        self.history_path = self.home / "history.jsonl"
        self.sounds = winutil.make_sounds(self.home / "sounds")
        self.ui = UI()
        self.root = self.ui.root
        self.recorder = Recorder(on_level=self._on_level)
        self.pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="net")
        self._client: MiMoClient | None = None
        self.live = None
        self.ctl: queue.Queue = queue.Queue()  # 热键事件 → 控制线程
        self.jobs: queue.Queue = queue.Queue()  # 录音 → 处理线程
        self.session = 0
        self.ctx: Context | None = None
        self.last_text = ""
        self.last_pcm = None
        self.last_ctx: Context | None = None
        self.last_mode = "dictate"
        self.machine = HotkeyStateMachine(
            self._bindings(),
            on_start=lambda m: self.ctl.put(("start", m)),
            on_stop=lambda m: self.ctl.put(("stop", m)),
            on_cancel=lambda m: self.ctl.put(("cancel", m)),
            on_mode_change=lambda m: self.ctl.put(("mode", m)),
            on_handsfree=lambda m: self.ctl.put(("handsfree", m)),
            tap_to_toggle=bool(cfg.get("tap_to_toggle")),
            tap_threshold=float(cfg.get("tap_threshold_ms")) / 1000.0,
            cancel_on_other_key=bool(cfg.get("cancel_on_other_key")),
        )
        self.hotkeys = GlobalHotkeys(self.machine)
        self.tray = Tray(self)
        threading.Thread(target=self._control_loop, daemon=True, name="control").start()
        threading.Thread(target=self._job_loop, daemon=True, name="jobs").start()

    # ------------------------------------------------------------ helpers
    def _bindings(self) -> dict:
        return {"dictate": self.cfg.get("hotkey_dictate"), "command": self.cfg.get("hotkey_command"),
                "translate": self.cfg.get("hotkey_translate")}

    def client(self, overrides: dict | None = None) -> MiMoClient:
        """无参数时返回长期复用的客户端（保持 HTTPS 长连接）；带参数时临时新建（用于测试设置）。"""
        if not overrides:
            if self._client is None:
                self._client = MiMoClient(self.cfg.get("base_url"), self.cfg.get("api_key"),
                                          timeout=float(self.cfg.get("request_timeout") or 60))
            return self._client
        o = overrides
        return MiMoClient(o.get("base_url") or self.cfg.get("base_url"),
                          o.get("api_key") if o.get("api_key") is not None else self.cfg.get("api_key"),
                          timeout=float(o.get("request_timeout") or self.cfg.get("request_timeout") or 60))

    def _on_level(self, v: float):
        self.ui.overlay.level = max(self.ui.overlay.level, v)

    def sound(self, name: str):
        if self.cfg.get("sounds"):
            winutil.play_sound(self.sounds.get(name))

    def overlay(self, fn_name: str, *args, **kw):
        if self.cfg.get("show_overlay") or fn_name == "hide":
            self.ui.call(lambda: getattr(self.ui.overlay, fn_name)(*args, **kw))

    # ------------------------------------------------------------ control thread
    def _control_loop(self):
        while True:
            ev, mode = self.ctl.get()
            try:
                getattr(self, f"_ev_{ev}")(mode)
            except Exception as e:
                log.exception("控制事件 %s 出错", ev)
                self.machine.reset()
                self.recorder.cancel()
                self.sound("error")
                self.overlay("message", "error", str(e)[:60], hide_after=3500)

    def _ev_start(self, mode: str):
        hwnd, exe, title = winutil.foreground_info()
        self.ctx = Context(exe=exe, title=title, hwnd=hwnd)
        self.session += 1
        sid = self.session
        self.recorder.start(self.cfg.get("input_device"))
        log.info("开始录音 mode=%s app=%s", mode, exe)
        client = self.client()
        if self.cfg.get("warmup"):
            self.pool.submit(client.warmup)
        self.live = None
        if self.cfg.get("live_segmenting") and self.cfg.get("engine") != "omni":
            from .live import LiveTranscriber

            proc = Processor(self.cfg, client, self.pool)
            self.live = LiveTranscriber(self.recorder, proc.transcribe_segment, self.pool)
            self.live.start()

        def confirm():  # 延迟一点再给反馈，避免 右Ctrl+C 之类的组合键闪一下
            if self.recorder.recording and self.session == sid:
                self.sound("start")
                self.overlay("recording", self.machine.mode or mode)

        threading.Timer(0.15, confirm).start()
        max_s = float(self.cfg.get("max_record_seconds") or 600)

        def auto_stop():
            if self.recorder.recording and self.session == sid:
                log.info("达到最长录音时长，自动结束")
                m = self.machine.mode or mode
                self.machine.reset()
                self.ctl.put(("stop", m))

        threading.Timer(max_s, auto_stop).start()

    def _ev_mode(self, mode: str):
        if self.recorder.recording:
            self.overlay("recording", mode)

    def _ev_handsfree(self, mode: str):
        if self.recorder.recording:
            self.overlay("recording", mode, handsfree=True)

    def _ev_cancel(self, mode: str):
        was = self.recorder.recording
        if self.live:
            self.live.cancel()
            self.live = None
        self.recorder.cancel()
        self.session += 1
        if was:
            self.sound("cancel")
            self.overlay("hide")

    def _ev_stop(self, mode: str):
        if not self.recorder.recording:
            return
        elapsed = self.recorder.elapsed
        live, self.live = self.live, None
        if live:
            live.stop_scanning()
        pcm = self.recorder.stop()
        self.sound("stop")
        if elapsed * 1000 < float(self.cfg.get("min_record_ms") or 350) or len(pcm) < 1600:
            if live:
                live.cancel()
            self.overlay("hide")
            return
        from .audio import rms_dbfs

        if rms_dbfs(pcm) < -60:
            if live:
                live.cancel()
            self.overlay("message", "info", "没有听到声音，请检查麦克风", hide_after=2500)
            return
        self.overlay("message", "busy", "识别中…")
        self.jobs.put((mode, pcm, self.ctx or Context(), live, time.monotonic()))

    # ------------------------------------------------------------ job thread
    def _job_loop(self):
        while True:
            mode, pcm, ctx, live, t_release = self.jobs.get()
            try:
                self._run_job(mode, pcm, ctx, live, t_release)
            except MiMoError as e:
                log.error("MiMo 错误: %s", e)
                self.sound("error")
                self.overlay("message", "error", str(e)[:60], hide_after=5000)
                self.tray.notify(str(e))
            except Exception as e:
                log.exception("处理失败")
                self.sound("error")
                self.overlay("message", "error", f"出错：{e}"[:60], hide_after=5000)

    def _save_recording(self, pcm):
        from .audio import to_wav_bytes

        try:
            keep = int(self.cfg.get("keep_recordings") or 0)
            if keep <= 0:
                return
            p = self.rec_dir / (datetime.now().strftime("%Y%m%d-%H%M%S") + ".wav")
            p.write_bytes(to_wav_bytes(pcm))
            files = sorted(self.rec_dir.glob("*.wav"))
            for old in files[:-keep]:
                old.unlink(missing_ok=True)
        except Exception as e:
            log.warning("保存录音失败: %s", e)

    def _run_job(self, mode, pcm, ctx: Context, live=None, t_release=None):
        t_release = t_release or time.monotonic()
        self.last_pcm, self.last_ctx, self.last_mode = pcm, ctx, mode
        self.pool.submit(self._save_recording, pcm)
        if mode == "command":
            ctx.selected_text = None
            winutil.focus_window(ctx.hwnd)
            ctx.selected_text = winutil.get_selected_text()
            log.info("指令模式，选中文字长度=%s", len(ctx.selected_text or ""))
        proc = Processor(self.cfg, self.client(), self.pool)
        raw = None
        if live is not None:
            t_a = time.monotonic()
            raw = live.finish(pcm)
            asr_wait = time.monotonic() - t_a
        res = proc.process(mode, pcm, ctx, raw=raw)
        if live is not None:
            res.timings["asr"] = asr_wait
            res.timings["segments"] = live.segments + 1
        res.timings["total"] = time.monotonic() - t_release
        res.timings["audio"] = len(pcm) / 16000
        log.info("完成 mode=%s action=%s %s", mode, res.action,
                 {k: round(v, 2) if isinstance(v, float) else v for k, v in res.timings.items()})
        if res.action == "none" or not res.text:
            self.overlay("message", "info", res.note or "没有识别到内容", hide_after=2000)
            return
        self._append_history(mode, ctx, res)
        self.last_text = res.text
        if res.action == "show":
            self.overlay("hide")
            q = res.raw
            self.ui.call(lambda: self._show_answer(q, res.text, ctx))
            return
        winutil.focus_window(ctx.hwnd)
        winutil.paste_text(res.text, self.cfg.get("paste_method"), bool(self.cfg.get("restore_clipboard")))
        if res.note:
            self.overlay("message", "info", res.note, hide_after=3000)
        else:
            self.sound("done")
            self.overlay("message", "ok", f"✓ {res.timings['total']:.1f}s  " + res.text.replace("\n", " "),
                         hide_after=1400)

    def _show_answer(self, q, a, ctx):
        from .ui import AnswerWindow

        def insert(text):
            def run():
                winutil.focus_window(ctx.hwnd)
                winutil.paste_text(text, self.cfg.get("paste_method"), bool(self.cfg.get("restore_clipboard")))

            threading.Thread(target=run, daemon=True).start()

        AnswerWindow(self.root, f"{DISPLAY_NAME} 回答", q, a, insert)

    def _append_history(self, mode, ctx, res):
        item = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "mode": mode, "app": ctx.exe,
                "raw": res.raw, "text": res.text,
                "timings": {k: round(v, 2) if isinstance(v, float) else v for k, v in res.timings.items()}}
        try:
            with open(self.history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("写入历史失败: %s", e)

    def load_history(self) -> list[dict]:
        if not self.history_path.exists():
            return []
        items = []
        limit = int(self.cfg.get("history_limit") or 2000)
        for line in self.history_path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                items.append(json.loads(line))
            except ValueError:
                pass
        return items

    # ------------------------------------------------------------ tray actions
    def repaste_last(self):
        if self.last_text:
            def run():
                time.sleep(0.25)
                winutil.paste_text(self.last_text, self.cfg.get("paste_method"), bool(self.cfg.get("restore_clipboard")))

            threading.Thread(target=run, daemon=True).start()

    def copy_last(self):
        if self.last_text:
            winutil.set_clipboard_text(self.last_text, transient=False)

    def reprocess_last(self):
        if self.last_pcm is not None:
            self.overlay("message", "busy", "重新处理中…")
            self.jobs.put((self.last_mode, self.last_pcm, self.last_ctx or Context(), None, time.monotonic()))

    def open_settings(self):
        from .ui import SettingsWindow

        SettingsWindow(self)

    def open_history(self):
        from .ui import HistoryWindow

        HistoryWindow(self.root, self.load_history())

    def capture_hotkey(self, cb):
        self.machine.capture_next(cb)

    def apply_settings(self, values: dict):
        old_auto = bool(self.cfg.get("autostart"))
        self.cfg.update(values)
        self._client = None  # 设置可能改了 Key / 节点，重建连接
        self.machine.set_bindings(self._bindings())
        self.machine.tap_to_toggle = bool(self.cfg.get("tap_to_toggle"))
        self.machine.cancel_on_other_key = bool(self.cfg.get("cancel_on_other_key"))
        if bool(self.cfg.get("autostart")) != old_auto:
            try:
                winutil.set_autostart(bool(self.cfg.get("autostart")))
            except Exception as e:
                log.warning("设置开机自启失败: %s", e)
        self.overlay("message", "ok", "设置已保存", hide_after=1200)

    def test_connection(self, values: dict | None = None) -> str:
        return test_connection(self.client(values), values or {}, self.cfg)

    def quit(self):
        log.info("退出")
        self.hotkeys.stop()
        self.tray.stop()
        self.recorder.cancel()
        self.root.quit()

    # ------------------------------------------------------------ run
    def run(self):
        self.hotkeys.start()
        self.tray.start()
        if self.cfg.get("autostart"):
            try:  # 刷新自启路径（exe 移动或从旧名字升级）
                winutil.set_autostart(True)
            except Exception:
                pass
        if self.cfg.get("api_key") and self.cfg.get("warmup"):
            self.pool.submit(self.client().warmup, 0)
        if not self.cfg.get("api_key"):
            self.ui.call(self.open_settings)
            self.ui.call(lambda: self.ui.overlay.message("info", "请先填写 MiMo API Key", hide_after=4000))
        else:
            self.ui.call(lambda: self.ui.overlay.message(
                "ok", f"{DISPLAY_NAME} 已就绪：按住 {self.cfg.get('hotkey_dictate')} 说话", hide_after=2500))
        log.info("%s %s 启动", APP_NAME, __version__)
        self.ui.run()


def probe_clusters(api_key: str) -> list[tuple[str, str, float | None, int | None]]:
    """并发测量三个 Token Plan 节点的时延与鉴权状态。"""
    def one(item):
        name, url = item
        lat, code = MiMoClient(url, api_key, timeout=8).probe()
        return name, url, lat, code

    with ThreadPoolExecutor(max_workers=3) as ex:
        return list(ex.map(one, TOKEN_PLAN_CLUSTERS.items()))


def best_cluster(results) -> tuple[str, str, float] | None:
    ok = [(lat, name, url) for name, url, lat, code in results if lat is not None and code not in (401, 403)]
    if not ok:
        return None
    lat, name, url = min(ok)
    return name, url, lat


def test_connection(client: MiMoClient, values: dict, cfg: Config) -> str:
    import numpy as np

    from .audio import encode_audio

    try:
        client.warmup(0)
        t0 = time.monotonic()
        client.chat(values.get("llm_model") or cfg.get("llm_model"),
                    [{"role": "user", "content": "回复 OK"}], max_tokens=16)
        t1 = time.monotonic()
        tone = (0.05 * np.sin(2 * np.pi * 440 * np.arange(8000) / 16000) * 32767).astype(np.int16)
        data, mime = encode_audio(tone, values.get("upload_format") or cfg.get("upload_format") or "mp3")
        client.transcribe(data, values.get("asr_model") or cfg.get("asr_model"), "auto", mime=mime)
        t2 = time.monotonic()
        msg = f"✓ 连接正常（LLM {t1 - t0:.1f}s，ASR {t2 - t1:.1f}s）"
    except Exception as e:
        msg = f"✗ {e}"[:120]
    if "token-plan" in (client.base_url or ""):
        try:
            res = probe_clusters(client.api_key)
            parts = []
            for name, url, lat, code in res:
                if lat is None:
                    parts.append(f"{name}:不可达")
                elif code in (401, 403):
                    parts.append(f"{name}:Key无效")
                else:
                    parts.append(f"{name}:{lat * 1000:.0f}ms")
            msg += "\n节点：" + "  ".join(parts)
            best = best_cluster(res)
            if best and best[1] != client.base_url:
                msg += f"\n建议切换到更快的「{best[0]}」节点"
        except Exception:
            pass
    return msg


# ------------------------------------------------------------------ CLI
def cli_transcribe(cfg: Config, path: str, mode: str) -> None:
    from .audio import read_wav

    pcm = read_wav(path)
    client = MiMoClient(cfg.get("base_url"), cfg.get("api_key"), timeout=float(cfg.get("request_timeout") or 60))
    res = Processor(cfg, client).process(mode, pcm, Context(exe="cli", title="命令行测试"))
    print("原始识别:", res.raw)
    print("最终文本:", res.text)
    print("耗时:", {k: round(v, 2) for k, v in res.timings.items()})


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="yingsay", description=f"{APP_NAME} {__version__}")
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--test", action="store_true", help="测试 API 连接")
    ap.add_argument("--transcribe", metavar="WAV", help="识别一个 wav 文件并输出结果")
    ap.add_argument("--mode", default="dictate", choices=["dictate", "translate"])
    ap.add_argument("--set-key", metavar="KEY", help="保存 API Key")
    ap.add_argument("--set-base-url", metavar="URL", help="保存 Base URL")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    if args.version:
        print(__version__)
        return
    home = app_dir()
    setup_logging(home, args.verbose)
    cfg = Config()
    if args.set_key or args.set_base_url:
        upd = {}
        if args.set_key:
            upd["api_key"] = args.set_key.strip()
        if args.set_base_url:
            upd["base_url"] = args.set_base_url.strip()
        cfg.update(upd)
        print("已保存到", cfg.path)
        return
    if args.test:
        print(test_connection(MiMoClient(cfg.get("base_url"), cfg.get("api_key")), {}, cfg))
        return
    if args.transcribe:
        cli_transcribe(cfg, args.transcribe, args.mode)
        return

    if sys.platform != "win32":
        print("图形界面目前只支持 Windows；在其它平台可使用 --transcribe / --test。")
        return
    winutil.set_dpi_aware()
    if not winutil.acquire_single_instance():
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, f"{DISPLAY_NAME} 已在运行（见系统托盘）。", DISPLAY_NAME, 0x40)
        return
    try:
        App(cfg).run()
    except Exception:
        log.exception("致命错误")
        raise


if __name__ == "__main__":
    main()
