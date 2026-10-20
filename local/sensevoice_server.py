"""应声 YingSay —— SenseVoice-Small 本地识别服务（sherpa-onnx，CPU 即可，约 0.1 秒/句）。

POST /transcribe   body = 16kHz 单声道 16-bit WAV   → {"text": "...", "lang": "zh"}
GET  /health                                        → {"ok": true, "model": "sensevoice"}
"""
from __future__ import annotations

import argparse
import io
import json
import os
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np


def load_recognizer(model_dir: str, threads: int):
    import sherpa_onnx

    model = os.path.join(model_dir, "model.int8.onnx")
    if not os.path.exists(model):
        model = os.path.join(model_dir, "model.onnx")
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=model,
        tokens=os.path.join(model_dir, "tokens.txt"),
        num_threads=threads,
        use_itn=True,  # 输出带标点、数字规范化
        language="auto",
    )


def read_wav(data: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(data), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError("只支持 16-bit WAV")
    x = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


class Handler(BaseHTTPRequestHandler):
    recognizer = None
    lock = threading.Lock()

    def log_message(self, fmt, *args):  # 安静一点
        pass

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._json(200, {"ok": True, "model": "sensevoice"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/transcribe"):
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            samples, sr = read_wav(self.rfile.read(n))
            t0 = time.monotonic()
            with self.lock:
                s = self.recognizer.create_stream()
                s.accept_waveform(sr, samples)
                self.recognizer.decode_stream(s)
                r = s.result
            text = (r.text or "").strip()
            lang = (getattr(r, "lang", "") or "").strip("<|>")
            self._json(200, {"text": text, "lang": lang, "seconds": round(time.monotonic() - t0, 4)})
        except Exception as e:  # noqa: BLE001
            self._json(400, {"error": str(e)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--threads", type=int, default=4)
    a = ap.parse_args()
    Handler.recognizer = load_recognizer(a.model_dir, a.threads)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"SenseVoice ready on http://{a.host}:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
