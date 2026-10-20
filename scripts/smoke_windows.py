"""在真实 Windows（CI）上冒烟测试：构建完整 App、打开所有窗口、测试剪贴板，然后退出。"""
import os
import sys
import tempfile
import threading

os.environ["YINGSAY_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yingsay import winutil  # noqa: E402
from yingsay.app import App  # noqa: E402
from yingsay.config import Config  # noqa: E402
from yingsay.pipeline import Context  # noqa: E402

errors = []


def annotate(name, exc_text):
    """以 GitHub Actions 注释输出错误（公开可见，便于远程排查）。"""
    msg = exc_text.strip().replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::error title=smoke {name}::{msg[-3000:]}", flush=True)


def _hook(t, v, tb):
    import traceback
    annotate("uncaught", "".join(traceback.format_exception(t, v, tb)))
    sys.__excepthook__(t, v, tb)
    sys.stdout.flush()
    os._exit(1)


sys.excepthook = _hook

# 剪贴板
winutil.set_clipboard_text("原始剪贴板 ✓", transient=False)
snap = winutil.snapshot_clipboard()
winutil.set_clipboard_text("临时内容")
assert winutil.get_clipboard_text() == "临时内容"
winutil.restore_clipboard(snap)
assert winutil.get_clipboard_text() == "原始剪贴板 ✓", winutil.get_clipboard_text()
print("clipboard ok; fg =", winutil.foreground_info())

cfg = Config()
cfg.update({"api_key": "tp-test-key"})
assert Config().get("api_key") == "tp-test-key"  # DPAPI 往返
raw = open(cfg.path, encoding="utf-8").read()
assert "tp-test-key" not in raw, "API key 应被 DPAPI 加密"
print("dpapi ok")

from yingsay.audio import encode_audio  # noqa: E402
import numpy as np  # noqa: E402

data, mime = encode_audio((np.sin(np.arange(32000) / 9) * 5000).astype(np.int16), "mp3")
assert mime == "audio/mpeg" and len(data) > 100, (mime, len(data))
print("mp3 ok", len(data))
from yingsay.app import probe_clusters  # noqa: E402
print("clusters:", probe_clusters("tp-invalid"))

app = App(cfg)


def step(fn, name):
    def run():
        try:
            fn()
            print("ok:", name)
        except Exception as e:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            annotate(name, traceback.format_exc())
            errors.append(name)
    return run


ui = app.ui
ui.call(step(lambda: ui.overlay.recording("dictate"), "overlay recording"))
ui.root.after(400, step(lambda: ui.overlay.recording("translate", handsfree=True), "overlay handsfree"))
ui.root.after(700, step(lambda: ui.overlay.message("busy", "识别中…"), "overlay message"))
ui.root.after(900, step(app.open_settings, "settings"))
ui.root.after(1100, step(app.open_history, "history"))
ui.root.after(1300, step(lambda: app._show_answer("问题", "回答", Context()), "answer"))
ui.root.after(1400, step(lambda: print(app.local.describe()), "local describe"))
ui.root.after(1500, step(lambda: app.apply_settings({"hotkey_dictate": "ctrl_r"}), "apply settings"))
ui.root.after(2500, step(app.quit, "quit"))
threading.Timer(15, lambda: (annotate("timeout", "app did not quit in 15s"), os._exit(3))).start()
app.run()
if errors:
    print("FAILED:", errors)
    os._exit(1)
print("SMOKE OK")
os._exit(0)
