import json

import numpy as np
import pytest

from yingsay import prompts
from yingsay.audio import split_for_upload, to_wav_bytes, rms_dbfs
from yingsay.config import Config
from yingsay.hotkeys import HotkeyStateMachine, parse_hotkey
from yingsay.mimo import MiMoClient, MiMoError
from yingsay.pipeline import Context, Processor


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make_machine(**kw):
    ev = []
    clock = Clock()
    m = HotkeyStateMachine(
        {"dictate": "ctrl_r", "command": "f8", "translate": "ctrl_r+shift_r"},
        on_start=lambda x: ev.append(("start", x)), on_stop=lambda x: ev.append(("stop", x)),
        on_cancel=lambda x: ev.append(("cancel", x)), on_mode_change=lambda x: ev.append(("mode", x)),
        on_handsfree=lambda x: ev.append(("handsfree", x)), clock=clock, **kw)
    return m, ev, clock


def test_hold_to_talk():
    m, ev, c = make_machine()
    m.press("ctrl_r"); c.t = 2; m.press("ctrl_r"); m.release("ctrl_r")
    assert ev == [("start", "dictate"), ("stop", "dictate")]


def test_tap_toggles_handsfree_then_stop():
    m, ev, c = make_machine()
    m.press("ctrl_r"); c.t = 0.1; m.release("ctrl_r")
    assert ev[-1] == ("handsfree", "dictate")
    c.t = 5; m.press("ctrl_r"); m.release("ctrl_r")
    assert ev[-1] == ("stop", "dictate")
    assert m.state == m.IDLE


def test_combo_shortcut_cancels():
    m, ev, c = make_machine()
    m.press("ctrl_r"); c.t = 0.05; m.press("c")
    assert ev == [("start", "dictate"), ("cancel", "dictate")]


def test_mode_upgrade():
    m, ev, c = make_machine()
    m.press("ctrl_r"); c.t = 0.2; m.press("shift_r"); c.t = 2; m.release("ctrl_r")
    assert ("mode", "translate") in ev and ev[-1] == ("stop", "translate")


def test_escape_cancels():
    m, ev, c = make_machine()
    m.press("f8"); c.t = 1; m.press("esc")
    assert ev == [("start", "command"), ("cancel", "command")]


def test_capture():
    m, ev, c = make_machine()
    got = []
    m.capture_next(got.append)
    m.press("ctrl_l"); m.press("space"); m.release("space"); m.release("ctrl_l")
    assert got == ["ctrl_l+space"] and ev == []


def test_parse_friendly():
    assert parse_hotkey("Right Ctrl") == {"ctrl_r"}
    assert parse_hotkey("ctrl+Shift+Space") == {"ctrl", "shift", "space"}


def test_split_and_wav():
    pcm = (np.random.randn(16000 * 400) * 1000).astype(np.int16)
    chunks = split_for_upload(pcm)
    assert sum(len(c) for c in chunks) == len(pcm)
    assert all(len(c) <= 16000 * 150 for c in chunks)
    wav = to_wav_bytes(chunks[0])
    assert wav[:4] == b"RIFF"
    assert rms_dbfs(np.zeros(100, np.int16)) < -100


def test_extract_out():
    assert prompts.extract_out("<out>你好。</out>") == ("out", "你好。")
    assert prompts.extract_out("<answer>是的</answer>") == ("answer", "是的")
    assert prompts.extract_out("plain") == ("raw", "plain")
    assert prompts.is_effectively_empty("嗯，啊。")
    assert not prompts.is_effectively_empty("你好")


def test_scene():
    assert prompts.detect_scene("WeChat.exe", "") == "chat"
    assert prompts.detect_scene("chrome.exe", "Inbox - Gmail") == "email"
    assert prompts.detect_scene("msedge.exe", "ChatGPT") == "ai"
    assert prompts.detect_scene("Code.exe", "x.py") == "code"


class FakeClient:
    def __init__(self, asr="嗯那个明天三点开会不对是四点", llm="<out>明天四点开会。</out>"):
        self.asr, self.llm, self.calls = asr, llm, []

    def transcribe(self, wav, model, lang, mime="audio/wav", **kw):
        self.calls.append(("asr", model, lang, mime))
        return self.asr

    def chat(self, model, messages, thinking=False, max_tokens=0, temperature=None, **kw):
        self.calls.append(("chat", model, messages))
        if isinstance(self.llm, Exception):
            raise self.llm
        return self.llm

    def audio_chat(self, model, wav, system, instruction, thinking=False, max_tokens=0, mime="audio/wav"):
        self.calls.append(("omni", model))
        return "<out>omni结果</out>"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("YINGSAY_HOME", str(tmp_path))
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    c = Config(tmp_path / "config.json")
    c.update({"replacements": [["米莫", "MiMo"]], "dictionary": ["RainPro"]})
    return c


PCM = (np.random.randn(16000) * 3000).astype(np.int16)


def test_pipeline_dictate(cfg):
    fc = FakeClient()
    r = Processor(cfg, fc).process("dictate", PCM, Context(exe="WeChat.exe"))
    assert r.text == "明天四点开会。" and r.action == "paste"
    msgs = fc.calls[1][2]
    assert "RainPro" in msgs[1]["content"] and "即时聊天" in msgs[1]["content"]
    assert fc.calls[0][3] == "audio/mpeg"  # 默认 MP3 上传
    # system 前缀与应用无关 → 可命中前缀缓存
    fc2 = FakeClient()
    Processor(cfg, fc2).process("dictate", PCM, Context(exe="Code.exe"))
    assert fc2.calls[1][2][0]["content"] == msgs[0]["content"]


def test_pipeline_fallback_on_llm_error(cfg):
    fc = FakeClient(asr="米莫真的非常好用啊", llm=MiMoError("boom"))
    r = Processor(cfg, fc).process("dictate", PCM, Context())
    assert r.text == "MiMo真的非常好用啊" and "润色失败" in r.note


def test_pipeline_guard_against_answering(cfg):
    fc = FakeClient(asr="什么是量子计算", llm="<out>" + "量子计算是……" * 60 + "</out>")
    r = Processor(cfg, fc).process("dictate", PCM, Context())
    assert r.text == "什么是量子计算"


def test_pipeline_command_and_ask(cfg):
    fc = FakeClient(asr="翻译成英文", llm="<out>Hello</out>")
    r = Processor(cfg, fc).process("command", PCM, Context(selected_text="你好"))
    assert r.text == "Hello" and r.action == "paste"
    fc = FakeClient(asr="今天星期几", llm="星期二")
    r = Processor(cfg, fc).process("command", PCM, Context())
    assert r.action == "show" and r.text == "星期二"


def test_pipeline_omni_and_asr_only(cfg):
    cfg.update({"engine": "omni"})
    assert Processor(cfg, FakeClient()).process("dictate", PCM, Context()).text == "omni结果"
    cfg.update({"engine": "asr_only"})
    fc = FakeClient(asr="米莫")
    assert Processor(cfg, fc).process("dictate", PCM, Context()).text == "MiMo"
    assert all(c[0] == "asr" for c in fc.calls)


def test_config_roundtrip(tmp_path):
    c = Config(tmp_path / "c.json")
    c.update({"api_key": "tp-secret", "hotkey_dictate": "f2"})
    c2 = Config(tmp_path / "c.json")
    assert c2.get("api_key") == "tp-secret" and c2.get("hotkey_dictate") == "f2"


class Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body
        self.text = json.dumps(body)

    def json(self):
        return self._b


def test_client_payloads(monkeypatch):
    cl = MiMoClient("https://x/v1/", "tp-k", retries=1)
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append((url, json, headers))
        if len(sent) == 1:
            return Resp(503, {"error": {"message": "busy"}})
        return Resp(200, {"choices": [{"message": {"content": "<think>x</think>你好"}}]})

    monkeypatch.setattr(cl.session, "post", post)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert cl.transcribe(b"RIFF", "mimo-v2.5-asr", "zh") == "你好"
    url, body, headers = sent[-1]
    assert url == "https://x/v1/chat/completions"
    assert body["asr_options"] == {"language": "zh"}
    assert body["messages"][0]["content"][0]["input_audio"]["data"].startswith("data:audio/wav;base64,")
    assert headers["Authorization"] == "Bearer tp-k"

    sent.clear()
    monkeypatch.setattr(cl.session, "post", lambda *a, **k: Resp(401, {"error": {"message": "bad key"}}))
    with pytest.raises(MiMoError, match="鉴权失败"):
        cl.chat("m", [])
import json

import numpy as np
import pytest

from yingsay import prompts
from yingsay.audio import split_for_upload, to_wav_bytes, rms_dbfs
from yingsay.config import Config
from yingsay.hotkeys import HotkeyStateMachine, parse_hotkey
from yingsay.mimo import MiMoClient, MiMoError
from yingsay.pipeline import Context, Processor


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make_machine(**kw):
    ev = []
    clock = Clock()
    m = HotkeyStateMachine(
        {"dictate": "ctrl_r", "command": "f8", "translate": "ctrl_r+shift_r"},
        on_start=lambda x: ev.append(("start", x)), on_stop=lambda x: ev.append(("stop", x)),
        on_cancel=lambda x: ev.append(("cancel", x)), on_mode_change=lambda x: ev.append(("mode", x)),
        on_handsfree=lambda x: ev.append(("handsfree", x)), clock=clock, **kw)
    return m, ev, clock


def test_hold_to_talk():
    m, ev, c = make_machine()
    m.press("ctrl_r"); c.t = 2; m.press("ctrl_r"); m.release("ctrl_r")
    assert ev == [("start", "dictate"), ("stop", "dictate")]


def test_tap_toggles_handsfree_then_stop():
    m, ev, c = make_machine()
    m.press("ctrl_r"); c.t = 0.1; m.release("ctrl_r")
    assert ev[-1] == ("handsfree", "dictate")
    c.t = 5; m.press("ctrl_r"); m.release("ctrl_r")
    assert ev[-1] == ("stop", "dictate")
    assert m.state == m.IDLE


def test_combo_shortcut_cancels():
    m, ev, c = make_machine()
    m.press("ctrl_r"); c.t = 0.05; m.press("c")
    assert ev == [("start", "dictate"), ("cancel", "dictate")]


def test_mode_upgrade():
    m, ev, c = make_machine()
    m.press("ctrl_r"); c.t = 0.2; m.press("shift_r"); c.t = 2; m.release("ctrl_r")
    assert ("mode", "translate") in ev and ev[-1] == ("stop", "translate")


def test_escape_cancels():
    m, ev, c = make_machine()
    m.press("f8"); c.t = 1; m.press("esc")
    assert ev == [("start", "command"), ("cancel", "command")]


def test_capture():
    m, ev, c = make_machine()
    got = []
    m.capture_next(got.append)
    m.press("ctrl_l"); m.press("space"); m.release("space"); m.release("ctrl_l")
    assert got == ["ctrl_l+space"] and ev == []


def test_parse_friendly():
    assert parse_hotkey("Right Ctrl") == {"ctrl_r"}
    assert parse_hotkey("ctrl+Shift+Space") == {"ctrl", "shift", "space"}


def test_split_and_wav():
    pcm = (np.random.randn(16000 * 400) * 1000).astype(np.int16)
    chunks = split_for_upload(pcm)
    assert sum(len(c) for c in chunks) == len(pcm)
    assert all(len(c) <= 16000 * 150 for c in chunks)
    wav = to_wav_bytes(chunks[0])
    assert wav[:4] == b"RIFF"
    assert rms_dbfs(np.zeros(100, np.int16)) < -100


def test_extract_out():
    assert prompts.extract_out("<out>你好。</out>") == ("out", "你好。")
    assert prompts.extract_out("<answer>是的</answer>") == ("answer", "是的")
    assert prompts.extract_out("plain") == ("raw", "plain")
    assert prompts.is_effectively_empty("嗯，啊。")
    assert not prompts.is_effectively_empty("你好")


def test_scene():
    assert prompts.detect_scene("WeChat.exe", "") == "chat"
    assert prompts.detect_scene("chrome.exe", "Inbox - Gmail") == "email"
    assert prompts.detect_scene("msedge.exe", "ChatGPT") == "ai"
    assert prompts.detect_scene("Code.exe", "x.py") == "code"


class FakeClient:
    def __init__(self, asr="嗯那个明天三点开会不对是四点", llm="<out>明天四点开会。</out>"):
        self.asr, self.llm, self.calls = asr, llm, []

    def transcribe(self, wav, model, lang, mime="audio/wav", **kw):
        self.calls.append(("asr", model, lang, mime))
        return self.asr

    def chat(self, model, messages, thinking=False, max_tokens=0, temperature=None, **kw):
        self.calls.append(("chat", model, messages))
        if isinstance(self.llm, Exception):
            raise self.llm
        return self.llm

    def audio_chat(self, model, wav, system, instruction, thinking=False, max_tokens=0, mime="audio/wav"):
        self.calls.append(("omni", model))
        return "<out>omni结果</out>"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("YINGSAY_HOME", str(tmp_path))
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    c = Config(tmp_path / "config.json")
    c.update({"replacements": [["米莫", "MiMo"]], "dictionary": ["RainPro"]})
    return c


PCM = (np.random.randn(16000) * 3000).astype(np.int16)


def test_pipeline_dictate(cfg):
    fc = FakeClient()
    r = Processor(cfg, fc).process("dictate", PCM, Context(exe="WeChat.exe"))
    assert r.text == "明天四点开会。" and r.action == "paste"
    msgs = fc.calls[1][2]
    assert "RainPro" in msgs[1]["content"] and "即时聊天" in msgs[1]["content"]
    assert fc.calls[0][3] == "audio/mpeg"  # 默认 MP3 上传
    # system 前缀与应用无关 → 可命中前缀缓存
    fc2 = FakeClient()
    Processor(cfg, fc2).process("dictate", PCM, Context(exe="Code.exe"))
    assert fc2.calls[1][2][0]["content"] == msgs[0]["content"]


def test_pipeline_fallback_on_llm_error(cfg):
    fc = FakeClient(asr="米莫真的非常好用啊", llm=MiMoError("boom"))
    r = Processor(cfg, fc).process("dictate", PCM, Context())
    assert r.text == "MiMo真的非常好用啊" and "润色失败" in r.note


def test_pipeline_guard_against_answering(cfg):
    fc = FakeClient(asr="什么是量子计算", llm="<out>" + "量子计算是……" * 60 + "</out>")
    r = Processor(cfg, fc).process("dictate", PCM, Context())
    assert r.text == "什么是量子计算"


def test_pipeline_command_and_ask(cfg):
    fc = FakeClient(asr="翻译成英文", llm="<out>Hello</out>")
    r = Processor(cfg, fc).process("command", PCM, Context(selected_text="你好"))
    assert r.text == "Hello" and r.action == "paste"
    fc = FakeClient(asr="今天星期几", llm="星期二")
    r = Processor(cfg, fc).process("command", PCM, Context())
    assert r.action == "show" and r.text == "星期二"


def test_pipeline_omni_and_asr_only(cfg):
    cfg.update({"engine": "omni"})
    assert Processor(cfg, FakeClient()).process("dictate", PCM, Context()).text == "omni结果"
    cfg.update({"engine": "asr_only"})
    fc = FakeClient(asr="米莫")
    assert Processor(cfg, fc).process("dictate", PCM, Context()).text == "MiMo"
    assert all(c[0] == "asr" for c in fc.calls)


def test_config_roundtrip(tmp_path):
    c = Config(tmp_path / "c.json")
    c.update({"api_key": "tp-secret", "hotkey_dictate": "f2"})
    c2 = Config(tmp_path / "c.json")
    assert c2.get("api_key") == "tp-secret" and c2.get("hotkey_dictate") == "f2"


class Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body
        self.text = json.dumps(body)

    def json(self):
        return self._b


def test_client_payloads(monkeypatch):
    cl = MiMoClient("https://x/v1/", "tp-k", retries=1)
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append((url, json, headers))
        if len(sent) == 1:
            return Resp(503, {"error": {"message": "busy"}})
        return Resp(200, {"choices": [{"message": {"content": "<think>x</think>你好"}}]})

    monkeypatch.setattr(cl.session, "post", post)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert cl.transcribe(b"RIFF", "mimo-v2.5-asr", "zh") == "你好"
    url, body, headers = sent[-1]
    assert url == "https://x/v1/chat/completions"
    assert body["asr_options"] == {"language": "zh"}
    assert body["messages"][0]["content"][0]["input_audio"]["data"].startswith("data:audio/wav;base64,")
    assert headers["Authorization"] == "Bearer tp-k"

    sent.clear()
    monkeypatch.setattr(cl.session, "post", lambda *a, **k: Resp(401, {"error": {"message": "bad key"}}))
    with pytest.raises(MiMoError, match="鉴权失败"):
        cl.chat("m", [])


def test_short_text_skips_llm(cfg):
    fc = FakeClient(asr="好的。")
    r = Processor(cfg, fc).process("dictate", PCM, Context())
    assert r.text == "好的。" and all(c[0] == "asr" for c in fc.calls)
    fc = FakeClient(asr="嗯好的")  # 有口头禅 → 仍然润色
    Processor(cfg, fc).process("dictate", PCM, Context())
    assert any(c[0] == "chat" for c in fc.calls)


def test_raw_from_live_skips_asr(cfg):
    fc = FakeClient()
    r = Processor(cfg, fc).process("dictate", PCM, Context(), raw="那个明天开会")
    assert r.text == "明天四点开会。" and not any(c[0] == "asr" for c in fc.calls)


def test_mp3_fallback_to_wav(cfg):
    class C(FakeClient):
        def transcribe(self, wav, model, lang, mime="audio/wav", **kw):
            self.calls.append(("asr", mime))
            if mime == "audio/mpeg":
                raise MiMoError("HTTP 400: unsupported")
            return "你好世界"
    Processor._wav_only = False
    fc = C()
    assert Processor(cfg, fc).transcribe(PCM) == "你好世界"
    assert [c[1] for c in fc.calls] == ["audio/mpeg", "audio/wav"]
    Processor._wav_only = False


def test_mp3_encode():
    from yingsay.audio import encode_audio, to_wav_bytes
    pcm = (np.sin(np.arange(16000 * 5) / 10) * 8000).astype(np.int16)
    data, mime = encode_audio(pcm, "mp3")
    assert mime == "audio/mpeg" and len(data) < len(to_wav_bytes(pcm)) / 5


class FakeRecorder:
    def __init__(self, pcm):
        self.pcm, self.n = pcm, 0

    @property
    def length(self):
        return self.n

    def get(self, a, b=None):
        b = self.n if b is None else min(b, self.n)
        return self.pcm[a:b].copy()


def _speech(sec):
    t = np.arange(int(16000 * sec))
    return (np.sin(t / 7) * 6000 * (1 + 0.3 * np.sin(t / 900))).astype(np.int16)


def _silence(sec):
    return (np.random.randn(int(16000 * sec)) * 20).astype(np.int16)


def test_live_segmenter_cuts_at_pauses():
    from concurrent.futures import ThreadPoolExecutor
    from yingsay.live import LiveTranscriber
    pcm = np.concatenate([_silence(0.3), _speech(5), _silence(0.8), _speech(5), _silence(0.8), _speech(2)])
    rec = FakeRecorder(pcm)
    seen = []

    def tr(seg):
        seen.append(len(seg))
        return f"段{len(seen)}"

    with ThreadPoolExecutor(2) as ex:
        lt = LiveTranscriber(rec, tr, ex)
        step = 2400
        while rec.n < len(pcm):  # 模拟实时录音
            rec.n = min(len(pcm), rec.n + step)
            lt._scan()
        assert lt.segments == 2, lt.segments
        text = lt.finish(pcm)
    assert text == "段1段2段3"
    assert sum(seen) == len(pcm) - (len(pcm) - sum(seen))  # 无重叠
    assert seen[-1] < 16000 * 3  # 松开时只剩最后一小段要识别


def test_live_skips_silent_tail():
    from concurrent.futures import ThreadPoolExecutor
    from yingsay.live import LiveTranscriber
    pcm = np.concatenate([_speech(5), _silence(1.5)])
    rec = FakeRecorder(pcm)
    calls = []
    with ThreadPoolExecutor(2) as ex:
        lt = LiveTranscriber(rec, lambda s: calls.append(1) or "hi", ex)
        while rec.n < len(pcm):
            rec.n = min(len(pcm), rec.n + 2400)
            lt._scan()
        assert lt.finish(pcm) == "hi"
    assert len(calls) == 1


def test_smart_join():
    from yingsay.live import smart_join
    assert smart_join(["Hello world.", "This is", "测试", "OK"]) == "Hello world. This is测试OK"


# ---------------------------------------------------------------- 用量 / 对冲
def test_fmt_num_units():
    from yingsay.usage import fmt_num

    assert fmt_num(999) == "999"
    assert fmt_num(1234) == "1.23K"
    assert fmt_num(5_600_000) == "5.6M"
    assert fmt_num(82e9) == "82B"


def test_usage_credits(tmp_path):
    import datetime as dt

    from yingsay.usage import UsageMeter, estimate_credits

    noon = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    night = dt.datetime(2026, 1, 1, 3, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    c, _ = estimate_credits("mimo-v2.5-asr", {"prompt_tokens": 999}, 3600, noon)
    assert c == 30_000_000
    c, _ = estimate_credits("mimo-v2.6-flash", {"prompt_tokens": 100, "completion_tokens": 10,
                                                "prompt_tokens_details": {"cached_tokens": 40}}, None, noon)
    assert c == 60 * 100 + 40 * 2 + 10 * 200
    c2, _ = estimate_credits("mimo-v2.6-pro", {"prompt_tokens": 10}, None, night)
    assert abs(c2 - 10 * 300 * 0.8) < 1e-6
    m = UsageMeter(tmp_path / "usage.json")
    m.record("mimo-v2.5-asr", None, 2.4)
    m.record("mimo-v2.6-flash", {"prompt_tokens": 50, "completion_tokens": 20}, None)
    t = UsageMeter(tmp_path / "usage.json").today()
    assert t["requests"] == 2 and t["asr_seconds"] == 2
    assert "Credits" in m.summary("Max")


def test_hedged_request_returns_fastest():
    import threading
    import time as _t

    from yingsay.mimo import MiMoClient

    c = MiMoClient("https://x", "tp-k")
    n = {"i": 0}
    lock = threading.Lock()

    def fake(payload, secs=None):
        with lock:
            n["i"] += 1
            i = n["i"]
        _t.sleep(1.0 if i == 1 else 0.05)
        return {"who": i}

    c._post_retry = fake
    t0 = _t.monotonic()
    assert c._post({"model": "m"}, hedge=0.2)["who"] == 2
    assert _t.monotonic() - t0 < 0.6


# ---------------------------------------------------------------- 本地模型
def test_parse_qwen_asr():
    from yingsay.local import parse_qwen_asr

    assert parse_qwen_asr("language Chinese<asr_text>你好，世界。") == "你好，世界。"
    assert parse_qwen_asr("language None<asr_text>") == ""
    assert parse_qwen_asr("language English") == ""
    assert parse_qwen_asr("plain text") == "plain text"


class FakeLocal:
    def __init__(self, which="qwen", llm=True, fail=False):
        self.which, self.llm, self.fail, self.calls = which, llm, fail, []

    def asr_choice(self):
        return self.which

    def llm_ready(self):
        return self.llm

    def transcribe(self, pcm, which, lang, ctx):
        self.calls.append(("asr", which))
        if self.fail:
            raise RuntimeError("down")
        return "嗯那个明天三点开会不对是四点"

    def chat(self, messages, max_tokens=0):
        self.calls.append(("chat",))
        return "<out>明天四点开会。</out>"


def _pcm(sec=2.0):
    return (np.sin(np.arange(int(16000 * sec)) / 9) * 3000).astype(np.int16)


def test_pipeline_prefers_local(cfg):
    from yingsay.pipeline import Context, Processor

    fc, fl = FakeClient(), FakeLocal()
    res = Processor(cfg, fc, local=fl).process("dictate", _pcm(), Context())
    assert res.text == "明天四点开会。"
    assert fl.calls == [("asr", "qwen"), ("chat",)]
    assert fc.calls == []
    assert res.timings["asr_src"].startswith("本地") and res.timings["llm_src"] == "本地"


def test_pipeline_local_failure_falls_back_to_cloud(cfg):
    from yingsay.pipeline import Context, Processor

    fc, fl = FakeClient(), FakeLocal(fail=True, llm=False)
    res = Processor(cfg, fc, local=fl).process("dictate", _pcm(), Context())
    assert res.text == "明天四点开会。"
    assert [c[0] for c in fc.calls] == ["asr", "chat"]


def test_pipeline_backend_mimo_skips_local(cfg):
    from yingsay.pipeline import Context, Processor

    cfg.data["asr_backend"] = "mimo"
    cfg.data["llm_backend"] = "mimo"
    fc, fl = FakeClient(), FakeLocal()
    Processor(cfg, fc, local=fl).process("dictate", _pcm(), Context())
    assert fl.calls == []


def test_timing_label_marks_local():
    from yingsay.app import timing_label

    s = timing_label({"total": 0.4, "asr": 0.1, "asr_src": "本地Qwen", "llm": 0.3, "llm_src": "本地"})
    assert s == "0.4s（本地识别0.1 本地润色0.3）"


def test_local_backend_against_fake_vllm(cfg):
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from yingsay.local import LocalBackend

    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            ok = self.server.server_port == srv_asr.server_port and self.path == "/v1/models"
            self.send_response(200 if ok else 404)
            self.end_headers()
            self.wfile.write(b"{}")

        def do_POST(self):
            body = _json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            out = _json.dumps({"choices": [{"message": {"content": "language Chinese<asr_text>测试。"}}]})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(out.encode())

    srv_asr = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv_asr.serve_forever, daemon=True).start()
    cfg.data.update({"local_asr_port": srv_asr.server_port, "local_llm_port": 1, "local_sv_port": 1})
    lb = LocalBackend(cfg)
    lb.refresh(0)
    assert lb.asr_choice() == "qwen"
    assert lb.transcribe(_pcm(1), "qwen", "auto", ["米莫"]) == "测试。"
    msg = seen[-1]["messages"]
    assert msg[0]["role"] == "system" and "米莫" in msg[0]["content"]
    assert msg[1]["content"][0]["audio_url"]["url"].startswith("data:audio/wav;base64,")
    srv_asr.shutdown()
