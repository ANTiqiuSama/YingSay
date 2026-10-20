"""Tk 界面：录音浮窗、设置窗口、历史记录、问答弹窗；pystray 托盘图标。"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import APP_NAME, DISPLAY_NAME, __version__, winutil
from .config import TOKEN_PLAN_CLUSTERS

log = logging.getLogger(__name__)

BG = "#1f1f24"
FG = "#f5f5f7"
ACCENT = {"dictate": "#ff4d5e", "translate": "#3d9bff", "command": "#a46bff", "busy": "#f5b301",
          "ok": "#30c26b", "error": "#ff6b6b", "info": "#8e8e93"}
MODE_LABEL = {"dictate": "听写", "translate": "翻译", "command": "指令"}
KEY = "#010101"  # 透明色


class Overlay:
    W, H = 300, 46

    def __init__(self, root: tk.Tk):
        self.root = root
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass
        self.win.attributes("-alpha", 0.0)
        self.win.configure(bg=KEY)
        self.canvas = tk.Canvas(self.win, width=self.W, height=self.H, bg=KEY, highlightthickness=0)
        self.canvas.pack()
        self._place()
        self.win.update_idletasks()
        try:
            hwnd = int(self.win.wm_frame(), 16)
            winutil.make_overlay_window(hwnd)
        except Exception as e:
            log.debug("overlay style: %s", e)
        self.level = 0.0
        self.levels = [0.0] * 18
        self.state = "hidden"
        self.mode = "dictate"
        self.text = ""
        self.t0 = 0.0
        self._hide_job = None
        self._tick()

    def _place(self):
        wa = winutil.work_area()
        if wa:
            l, t, r, b = wa
        else:
            l, t, r, b = 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight() - 40
        x = l + (r - l - self.W) // 2
        y = b - self.H - 36
        self.win.geometry(f"{self.W}x{self.H}+{x}+{y}")

    def _pill(self, color):
        c, w, h, r = self.canvas, self.W, self.H, self.H // 2
        c.create_oval(0, 0, h, h, fill=BG, outline=BG)
        c.create_oval(w - h, 0, w, h, fill=BG, outline=BG)
        c.create_rectangle(r, 0, w - r, h, fill=BG, outline=BG)
        c.create_oval(14, h / 2 - 6, 26, h / 2 + 6, fill=color, outline=color)

    def _draw(self):
        import time

        c = self.canvas
        c.delete("all")
        if self.state == "hidden":
            return
        color = ACCENT.get(self.mode if self.state in ("recording", "handsfree") else self.state, ACCENT["info"])
        self._pill(color)
        if self.state in ("recording", "handsfree"):
            secs = int(time.monotonic() - self.t0)
            label = MODE_LABEL.get(self.mode, "")
            if self.state == "handsfree":
                label += "·免提"
            c.create_text(36, self.H / 2, text=f"{label}  {secs // 60}:{secs % 60:02d}", fill=FG, anchor="w",
                          font=("Microsoft YaHei UI", 10, "bold"))
            x0 = 150
            for i, v in enumerate(self.levels):
                bh = 4 + min(1.0, v * 3.0) * 26
                x = x0 + i * 7
                c.create_rectangle(x, self.H / 2 - bh / 2, x + 4, self.H / 2 + bh / 2, fill=color, outline="")
        else:
            txt = self.text if len(self.text) <= 22 else self.text[:21] + "…"
            c.create_text(36, self.H / 2, text=txt, fill=FG, anchor="w", font=("Microsoft YaHei UI", 10))

    def _tick(self):
        if self.state in ("recording", "handsfree"):
            self.levels = self.levels[1:] + [self.level]
            self.level *= 0.6
        if self.state != "hidden":
            self._draw()
        self.root.after(60, self._tick)

    def _show(self):
        if self._hide_job:
            self.root.after_cancel(self._hide_job)
            self._hide_job = None
        self.win.attributes("-alpha", 0.94)

    # 公共接口（必须在 Tk 线程调用）
    def recording(self, mode: str, handsfree: bool = False):
        import time

        if self.state not in ("recording", "handsfree"):
            self.t0 = time.monotonic()
            self.levels = [0.0] * len(self.levels)
        self.mode = mode
        self.state = "handsfree" if handsfree else "recording"
        self._show()
        self._draw()

    def message(self, kind: str, text: str, hide_after: int | None = None):
        self.state, self.text = kind, text
        self._show()
        self._draw()
        if hide_after:
            self._hide_job = self.root.after(hide_after, self.hide)

    def hide(self):
        self.state = "hidden"
        self._hide_job = None
        self.win.attributes("-alpha", 0.0)
        self.canvas.delete("all")


class AnswerWindow:
    def __init__(self, root, title: str, question: str, answer: str, on_insert):
        w = tk.Toplevel(root)
        w.title(title)
        w.attributes("-topmost", True)
        w.geometry("560x420")
        ttk.Label(w, text="问：" + question[:200], wraplength=530).pack(anchor="w", padx=12, pady=(10, 4))
        txt = tk.Text(w, wrap="word", font=("Microsoft YaHei UI", 10), padx=8, pady=8)
        txt.insert("1.0", answer)
        txt.pack(fill="both", expand=True, padx=12, pady=4)
        bar = ttk.Frame(w)
        bar.pack(fill="x", padx=12, pady=8)

        def copy():
            w.clipboard_clear()
            w.clipboard_append(txt.get("1.0", "end-1c"))

        def insert():
            content = txt.get("1.0", "end-1c")
            w.destroy()
            on_insert(content)

        ttk.Button(bar, text="复制", command=copy).pack(side="left")
        ttk.Button(bar, text="插入到原窗口", command=insert).pack(side="left", padx=8)
        ttk.Button(bar, text="关闭 (Esc)", command=w.destroy).pack(side="right")
        w.bind("<Escape>", lambda e: w.destroy())
        w.focus_force()


class HistoryWindow:
    def __init__(self, root, items: list[dict]):
        w = tk.Toplevel(root)
        w.title(f"{DISPLAY_NAME} 历史记录")
        w.geometry("820x480")
        cols = ("time", "mode", "app", "text")
        tree = ttk.Treeview(w, columns=cols, show="headings")
        for c, t, wd in zip(cols, ("时间", "模式", "应用", "内容"), (140, 60, 120, 480)):
            tree.heading(c, text=t)
            tree.column(c, width=wd, anchor="w")
        for i, it in enumerate(reversed(items)):
            tree.insert("", "end", iid=str(i), values=(it.get("time", ""), MODE_LABEL.get(it.get("mode"), ""),
                                                        it.get("app", ""), (it.get("text") or "").replace("\n", " ⏎ ")))
        tree.pack(fill="both", expand=True, padx=8, pady=8)
        rev = list(reversed(items))
        detail = tk.Text(w, height=6, wrap="word", font=("Microsoft YaHei UI", 10))
        detail.pack(fill="x", padx=8)

        def on_sel(_=None):
            s = tree.selection()
            if s:
                it = rev[int(s[0])]
                detail.delete("1.0", "end")
                detail.insert("1.0", f"{it.get('text', '')}\n\n—— 原始识别 ——\n{it.get('raw', '')}")

        def copy(which="text"):
            s = tree.selection()
            if s:
                w.clipboard_clear()
                w.clipboard_append(rev[int(s[0])].get(which, ""))

        tree.bind("<<TreeviewSelect>>", on_sel)
        tree.bind("<Double-1>", lambda e: copy())
        bar = ttk.Frame(w)
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Button(bar, text="复制整理后文本", command=copy).pack(side="left")
        ttk.Button(bar, text="复制原始识别", command=lambda: copy("raw")).pack(side="left", padx=8)


class SettingsWindow:
    def __init__(self, app):
        self.app = app
        cfg = app.cfg
        self.w = w = tk.Toplevel(app.root)
        w.title(f"{DISPLAY_NAME} {__version__} 设置")
        w.geometry("720x840")
        nb = ttk.Notebook(w)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.vars: dict[str, tk.Variable] = {}

        def tab(name):
            f = ttk.Frame(nb, padding=12)
            nb.add(f, text=name)
            f.columnconfigure(1, weight=1)
            return f

        def row(f, r, label, key, kind="entry", values=None, show=None, hint=None):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 8))
            val = cfg.data.get(key)
            if kind == "check":
                v = tk.BooleanVar(value=bool(val))
                ttk.Checkbutton(f, variable=v).grid(row=r, column=1, sticky="w")
            elif kind == "combo":
                v = tk.StringVar(value=str(val))
                ttk.Combobox(f, textvariable=v, values=values or [], state="normal").grid(row=r, column=1, sticky="ew")
            else:
                v = tk.StringVar(value=str(val) if val is not None else "")
                ttk.Entry(f, textvariable=v, show=show or "").grid(row=r, column=1, sticky="ew")
            if hint:
                ttk.Label(f, text=hint, foreground="#888").grid(row=r + 1, column=1, sticky="w")
            self.vars[key] = v
            return v

        # --- 基础
        f = tab("基础")
        row(f, 0, "API Key", "api_key", show="•", hint="Token Plan 控制台里的专属密钥（tp-xxxx）")
        row(f, 2, "Base URL", "base_url", "combo",
            list(TOKEN_PLAN_CLUSTERS.values()) + ["https://api.xiaomimimo.com/v1"],
            hint="Token Plan 有中国/新加坡/欧洲三个节点，点下方“自动选最快节点”")
        row(f, 4, "识别引擎", "engine", "combo", ["asr+llm", "asr_only", "omni"],
            hint="asr+llm=专用ASR+AI润色（推荐） / asr_only=只识别 / omni=全模态一步到位")
        row(f, 6, "识别语言", "asr_language", "combo", ["auto", "zh", "en"])
        devs = [""] + winutil_devices()
        row(f, 7, "麦克风", "input_device", "combo", devs, hint="留空=系统默认")
        row(f, 9, "开机自启", "autostart", "check")
        row(f, 10, "提示音", "sounds", "check")
        row(f, 11, "显示浮窗", "show_overlay", "check")
        bar = ttk.Frame(f)
        bar.grid(row=12, column=0, columnspan=2, sticky="w", pady=12)
        ttk.Button(bar, text="测试连接", command=self.test).pack(side="left")
        ttk.Button(bar, text="自动选最快节点", command=self.pick_cluster).pack(side="left", padx=8)
        self.status = ttk.Label(f, text="", wraplength=560, justify="left")
        self.status.grid(row=13, column=0, columnspan=2, sticky="w")
        ttk.Separator(f).grid(row=14, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Label(f, text="用量（本机估算）").grid(row=15, column=0, sticky="nw")
        self.usage_label = ttk.Label(f, text="", wraplength=500, justify="left")
        self.usage_label.grid(row=15, column=1, sticky="w")
        ttk.Label(f, text="按官方规则折算：ASR 30M Credits/小时，flash 输入100/输出200 每 token，闲时0.8倍；以 MiMo 控制台为准",
                  foreground="#888", wraplength=500).grid(row=16, column=1, sticky="w")
        self._refresh_usage()

        # --- 热键
        f = tab("热键")
        hk = [("hotkey_dictate", "听写（按住说话）"), ("hotkey_command", "指令（改写选中文字 / 提问）"),
              ("hotkey_translate", "边说边翻译")]
        for i, (k, label) in enumerate(hk):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=6)
            v = tk.StringVar(value=cfg.data.get(k, ""))
            self.vars[k] = v
            ttk.Entry(f, textvariable=v, width=24).grid(row=i, column=1, sticky="w")
            ttk.Button(f, text="录制", command=lambda v=v: self.capture(v)).grid(row=i, column=2, padx=6)
        row(f, 3, "翻译目标语言", "translate_target", "combo",
            ["English", "简体中文", "日本語", "한국어", "Français", "Deutsch", "Español"])
        row(f, 4, "轻点切换免提", "tap_to_toggle", "check",
            hint="轻点一下热键开始免提录音，再按一下结束；Esc 随时取消")
        row(f, 6, "按组合键时取消", "cancel_on_other_key", "check", hint="例如按 右Ctrl+C 时不会误触发录音")
        ttk.Label(f, text="写法示例：ctrl_r（右Ctrl）、alt_r、f8、caps_lock、ctrl+shift+space",
                  foreground="#888").grid(row=8, column=0, columnspan=3, sticky="w", pady=10)

        # --- 个性化
        f = tab("个性化")
        f.rowconfigure(1, weight=1)
        f.rowconfigure(3, weight=1)
        ttk.Label(f, text="个人词典（每行一个：人名、术语、品牌…）").grid(row=0, column=0, columnspan=2, sticky="w")
        self.dict_text = tk.Text(f, height=7)
        self.dict_text.insert("1.0", "\n".join(cfg.data.get("dictionary") or []))
        self.dict_text.grid(row=1, column=0, columnspan=2, sticky="nsew")
        ttk.Label(f, text="强制替换（每行：错词=>正词）").grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.rep_text = tk.Text(f, height=5)
        self.rep_text.insert("1.0", "\n".join(f"{a}=>{b}" for a, b in (cfg.data.get("replacements") or [])))
        self.rep_text.grid(row=3, column=0, columnspan=2, sticky="nsew")
        ttk.Label(f, text="额外写作要求（全局）").grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.ci_text = tk.Text(f, height=3)
        self.ci_text.insert("1.0", cfg.data.get("custom_instructions") or "")
        self.ci_text.grid(row=5, column=0, columnspan=2, sticky="ew")
        ttk.Label(f, text="按应用定制风格（每行：程序名.exe=风格描述）").grid(row=6, column=0, columnspan=2, sticky="w",
                                                                    pady=(8, 0))
        self.app_text = tk.Text(f, height=3)
        self.app_text.insert("1.0", "\n".join(f"{k}={v}" for k, v in (cfg.data.get("app_styles") or {}).items()))
        self.app_text.grid(row=7, column=0, columnspan=2, sticky="ew")

        # --- 本地模型
        f = tab("本地模型")
        from .local import LOCAL_ASR_MODELS, LOCAL_LLM_MODELS

        row(f, 0, "识别用", "asr_backend", "combo", ["auto", "local", "mimo"],
            hint="auto=本地服务在线就用本地（零网络延迟），否则用 MiMo 云端")
        row(f, 2, "本地识别模型", "local_asr_model", "combo", list(LOCAL_ASR_MODELS),
            hint="qwen3-asr-1.7b 最准 / 0.6b 更快更省显存 / sensevoice 极快、不占显存")
        row(f, 4, "润色用", "llm_backend", "combo", ["auto", "local", "mimo"],
            hint="auto=听写/翻译用本地润色，F8 指令和提问仍用云端大模型")
        row(f, 6, "本地润色模型", "local_llm_model", "combo", LOCAL_LLM_MODELS)
        row(f, 7, "随应声自动启动", "local_autostart", "check")
        row(f, 8, "退出时释放显存", "local_stop_on_exit", "check", hint="不勾选则模型常驻显存，下次秒开")
        row(f, 10, "WSL 发行版", "local_wsl_distro", hint="留空=默认发行版（wsl -l -v 查看）")
        lb = ttk.Frame(f)
        lb.grid(row=12, column=0, columnspan=2, sticky="w", pady=10)
        ttk.Button(lb, text="一键安装（WSL2）", command=lambda: self._local("open_installer")).pack(side="left")
        ttk.Button(lb, text="启动", command=lambda: self._local("start")).pack(side="left", padx=6)
        ttk.Button(lb, text="停止", command=lambda: self._local("stop")).pack(side="left")
        ttk.Button(lb, text="测速对比", command=self.bench).pack(side="left", padx=6)
        self.local_status = ttk.Label(f, text="", wraplength=600, justify="left")
        self.local_status.grid(row=13, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="需要：Windows 10/11 + WSL2 + NVIDIA 显卡（推荐 16GB+ 显存）。默认 Qwen3-ASR-1.7B + Qwen3-4B 约占 20GB 显存。",
                  foreground="#888", wraplength=600).grid(row=14, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self._refresh_local()

        # --- 高级
        f = tab("高级")
        row(f, 0, "ASR 模型", "asr_model", "combo", ["mimo-v2.5-asr"])
        row(f, 1, "润色模型", "llm_model", "combo", ["mimo-v2.6-flash", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"])
        row(f, 2, "指令模型", "command_model", "combo", ["mimo-v2.6-flash", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"])
        row(f, 3, "全模态模型", "omni_model", "combo", ["mimo-v2.6-flash", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"])
        row(f, 4, "开启深度思考", "llm_thinking", "check", hint="会明显变慢，听写场景不建议开启")
        row(f, 6, "粘贴方式", "paste_method", "combo", ["ctrl_v", "shift_insert", "type"],
            hint="个别终端/游戏不支持 Ctrl+V 时可改为 shift_insert 或 type")
        row(f, 8, "粘贴后恢复剪贴板", "restore_clipboard", "check")
        row(f, 9, "中文字形", "output_script", "combo", ["auto", "simplified", "traditional"])
        row(f, 10, "最长录音(秒)", "max_record_seconds")
        row(f, 11, "超时(秒)", "request_timeout")
        ttk.Separator(f).grid(row=12, column=0, columnspan=2, sticky="ew", pady=8)
        row(f, 13, "边说边识别", "live_segmenting", "check", hint="在停顿处提前上传已说完的部分，长句几乎零等待")
        row(f, 15, "上传格式", "upload_format", "combo", ["mp3", "wav"], hint="mp3 体积约 1/8，网络慢时明显更快")
        row(f, 17, "短句跳过润色(字数)", "polish_skip_chars", hint="不超过该字数且无口头禅时直接输入识别结果；0=总是润色")
        row(f, 19, "按键时预热连接", "warmup", "check")
        row(f, 20, "对冲慢请求", "hedge_requests", "check", hint="请求迟迟不返回时再发一份，谁快用谁（偶尔多耗一点用量）")
        row(f, 22, "套餐", "plan", "combo", ["Lite", "Standard", "Pro", "Max"], hint="用于显示月额度占比")
        ttk.Button(f, text="打开配置/日志目录", command=lambda: os.startfile(str(app.home))
                   if hasattr(os, "startfile") else None).grid(row=24, column=0, sticky="w", pady=12)

        bottom = ttk.Frame(w)
        bottom.pack(fill="x", padx=8, pady=(0, 10))
        ttk.Button(bottom, text="保存", command=self.save).pack(side="right")
        ttk.Button(bottom, text="取消", command=w.destroy).pack(side="right", padx=8)
        w.focus_force()

    def capture(self, var: tk.StringVar):
        var.set("请按下热键…")
        self.app.capture_hotkey(lambda s: self.app.ui.call(lambda: var.set(s)))

    def collect(self) -> dict:
        out = {}
        for k, v in self.vars.items():
            val = v.get()
            if isinstance(v, tk.StringVar):
                val = val.strip()
            out[k] = val
        for k in ("max_record_seconds", "request_timeout", "polish_skip_chars"):
            try:
                out[k] = int(float(out[k]))
            except (ValueError, TypeError):
                out.pop(k, None)
        out["dictionary"] = [l.strip() for l in self.dict_text.get("1.0", "end").splitlines() if l.strip()]
        reps = []
        for l in self.rep_text.get("1.0", "end").splitlines():
            if "=>" in l:
                a, b = l.split("=>", 1)
                if a.strip():
                    reps.append([a.strip(), b.strip()])
        out["replacements"] = reps
        out["custom_instructions"] = self.ci_text.get("1.0", "end").strip()
        styles = {}
        for l in self.app_text.get("1.0", "end").splitlines():
            if "=" in l:
                a, b = l.split("=", 1)
                if a.strip():
                    styles[a.strip().lower()] = b.strip()
        out["app_styles"] = styles
        return out

    def _local(self, action: str):
        self.local_status.config(text="处理中…")

        def run():
            try:
                msg = getattr(self.app.local, action)()
            except Exception as e:  # noqa: BLE001
                msg = f"出错：{e}"
            self.app.ui.call(lambda: self.local_status.config(text=msg))
            self.app.tray.refresh()

        threading.Thread(target=run, daemon=True).start()

    def bench(self):
        self.local_status.config(text="测速中…")

        def run():
            from .app import benchmark

            try:
                msg = benchmark(self.app)
            except Exception as e:  # noqa: BLE001
                msg = f"测速失败：{e}"
            self.app.ui.call(lambda: self.local_status.config(text=msg))

        threading.Thread(target=run, daemon=True).start()

    def _refresh_local(self):
        try:
            if not self.w.winfo_exists():
                return

            def run():
                d = self.app.local.describe()
                self.app.ui.call(lambda: self.w.winfo_exists() and self.w.title(
                    f"{DISPLAY_NAME} {__version__} 设置 — {d}"))

            threading.Thread(target=run, daemon=True).start()
            self.w.after(3000, self._refresh_local)
        except Exception:
            pass

    def _refresh_usage(self):
        try:
            if not self.w.winfo_exists():
                return
            plan = self.vars.get("plan").get() if self.vars.get("plan") else self.app.cfg.get("plan")
            self.usage_label.config(text=self.app.usage.summary(plan or "Max"))
            self.w.after(3000, self._refresh_usage)
        except Exception:
            pass

    def save(self):
        values = self.collect()
        self.app.apply_settings(values)
        self.w.destroy()

    def pick_cluster(self):
        key = self.vars["api_key"].get().strip()
        self.status.config(text="正在测量三个节点的时延…")

        def run():
            from .app import best_cluster, probe_clusters

            res = probe_clusters(key)
            best = best_cluster(res)
            from .app import KEY_CLUSTER_NOTE

            detail = "  ".join(f"{n}:{'不可达' if l is None else ('Key不属于此节点' if c in (401, 403) else f'{l * 1000:.0f}ms')}"
                               for n, u, l, c in res)
            if any(c in (401, 403) for *_, c in res):
                detail += "\n" + KEY_CLUSTER_NOTE

            def done():
                if best:
                    self.vars["base_url"].set(best[1])
                    self.status.config(text=f"已选择「{best[0]}」节点（{best[2] * 1000:.0f}ms），记得点保存。\n{detail}")
                else:
                    self.status.config(text=f"没有可用节点，请检查 API Key 是否正确。\n{detail}")

            self.app.ui.call(done)

        threading.Thread(target=run, daemon=True).start()

    def test(self):
        values = self.collect()
        self.status.config(text="测试中…")

        def run():
            msg = self.app.test_connection(values)
            self.app.ui.call(lambda: self.status.config(text=msg))

        threading.Thread(target=run, daemon=True).start()


def winutil_devices():
    from .audio import list_input_devices

    return list_input_devices()


class UI:
    """持有 Tk 根窗口，其它线程通过 call() 把操作投递到 Tk 线程。"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass
        self.q: queue.Queue = queue.Queue()
        self.overlay = Overlay(self.root)
        self.root.after(30, self._pump)

    def _pump(self):
        try:
            while True:
                fn = self.q.get_nowait()
                try:
                    fn()
                except Exception:
                    log.exception("UI 回调出错")
        except queue.Empty:
            pass
        self.root.after(30, self._pump)

    def call(self, fn):
        self.q.put(fn)

    def run(self):
        self.root.mainloop()


# ------------------------------------------------------------------ 托盘
def make_icon_image(color=(255, 77, 94)):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(31, 31, 36, 255))
    d.rounded_rectangle((24, 12, 40, 38), radius=8, fill=color + (255,))
    d.arc((17, 22, 47, 46), start=0, end=180, fill=(245, 245, 247, 255), width=3)
    d.line((32, 46, 32, 53), fill=(245, 245, 247, 255), width=3)
    d.line((24, 53, 40, 53), fill=(245, 245, 247, 255), width=3)
    return img


class Tray:
    def refresh(self):
        """刷新托盘菜单里的动态文字（今日用量）。"""
        try:
            icon = getattr(self, "icon", None)
            if icon is not None:
                icon.update_menu()
        except Exception:
            pass

    def __init__(self, app):
        import pystray

        self.app = app
        cfg = app.cfg

        def polish_checked(item):
            return cfg.get("engine") != "asr_only"

        def toggle_polish(icon, item):
            new = "asr_only" if cfg.get("engine") != "asr_only" else "asr+llm"
            cfg.update({"engine": new})

        menu = pystray.Menu(
            pystray.MenuItem(f"{DISPLAY_NAME} {__version__}", None, enabled=False),
            pystray.MenuItem(lambda item: f"听写：按住 {cfg.get('hotkey_dictate')}", None, enabled=False),
            pystray.MenuItem(lambda item: f"今日用量：{app.usage.short()}", lambda: app.ui.call(app.open_settings)),
            pystray.MenuItem(lambda item: app.local.describe(), None, enabled=False),
            pystray.MenuItem("启动本地模型", lambda: app.start_local()),
            pystray.MenuItem("停止本地模型（释放显存）", lambda: app.stop_local()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("设置…", lambda: app.ui.call(app.open_settings), default=True),
            pystray.MenuItem("历史记录…", lambda: app.ui.call(app.open_history)),
            pystray.MenuItem("重新粘贴上一条", lambda: app.repaste_last()),
            pystray.MenuItem("复制上一条", lambda: app.copy_last()),
            pystray.MenuItem("重新处理上一段录音", lambda: app.reprocess_last()),
            pystray.MenuItem("AI 润色", toggle_polish, checked=polish_checked),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开配置目录", lambda: os.startfile(str(app.home)) if hasattr(os, "startfile") else None),
            pystray.MenuItem("退出", lambda: app.ui.call(app.quit)),
        )
        self.icon = pystray.Icon(APP_NAME, make_icon_image(), f"{DISPLAY_NAME} — 按住 {cfg.get('hotkey_dictate')} 说话", menu)

    def start(self):
        threading.Thread(target=self.icon.run, daemon=True, name="tray").start()

    def notify(self, text: str):
        try:
            self.icon.notify(text, DISPLAY_NAME)
        except Exception:
            pass

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            pass
