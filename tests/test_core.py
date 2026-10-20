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

    def transcribe(self, wav, model, lang, mime="audio/wav"):
        self.calls.append(("asr", model, lang, mime))
        return self.asr

    def chat(self, model, messages, thinking=False, max_tokens=0, temperature=None):
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

    def transcribe(self, wav, model, lang, mime="audio/wav"):
        self.calls.append(("asr", model, lang, mime))
        return self.asr

    def chat(self, model, messages, thinking=False, max_tokens=0, temperature=None):
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
        def transcribe(self, wav, model, lang, mime="audio/wav"):
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
