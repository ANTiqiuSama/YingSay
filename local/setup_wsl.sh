#!/usr/bin/env bash
# 应声 YingSay —— 在 WSL2 里一键安装本地语音模型（RTX 50 系 / 40 系 / 30 系 NVIDIA 显卡）
#   Qwen3-ASR-1.7B（vLLM，识别主力） + Qwen3-4B-Instruct-2507（vLLM，本地润色） + SenseVoice-Small（CPU，秒开备用）
# 用法（在 Windows PowerShell 里）：
#   wsl -e bash -lc "curl -fsSL https://raw.githubusercontent.com/ANTiqiuSama/YingSay/main/local/setup_wsl.sh | bash"
# 可选环境变量：YINGSAY_ASR_MODEL  YINGSAY_LLM_MODEL  HF_ENDPOINT（例如 https://hf-mirror.com）  YINGSAY_SKIP_LLM=1
set -euo pipefail

D="$HOME/.yingsay-local"
RAW="https://raw.githubusercontent.com/ANTiqiuSama/YingSay/main/local"
ASR_MODEL="${YINGSAY_ASR_MODEL:-Qwen/Qwen3-ASR-1.7B}"
LLM_MODEL="${YINGSAY_LLM_MODEL:-Qwen/Qwen3-4B-Instruct-2507}"
SV_NAME="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
mkdir -p "$D"
say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

say "1/6 检查 GPU"
if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi >/dev/null 2>&1; then
  echo "WSL 里看不到 NVIDIA 显卡。请在 Windows 安装最新 NVIDIA 驱动（不要在 WSL 里装驱动），然后在 PowerShell 运行 wsl --update 后重试。"
  exit 1
fi
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader

say "2/6 安装 uv（Python 环境管理）"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

say "3/6 创建 Python 3.12 环境并安装 vLLM + Qwen3-ASR（约 5-10 分钟）"
[ -d "$D/venv" ] || uv venv --python 3.12 "$D/venv"
set +u
# shellcheck disable=SC1091
source "$D/venv/bin/activate"
set -u
uv pip install -U "qwen-asr[vllm]" "huggingface_hub[cli,hf_transfer]" sherpa-onnx numpy
python - <<'PY'
import torch
print("torch", torch.__version__, "CUDA", torch.version.cuda, "可用" if torch.cuda.is_available() else "不可用")
if torch.cuda.is_available():
    print("GPU:", torch.cuda.get_device_name(0), "算力", torch.cuda.get_device_capability(0))
PY

say "4/6 下载模型（Qwen3-ASR 约 4.5GB，Qwen3-4B 约 8GB）"
export HF_HUB_ENABLE_HF_TRANSFER=1
hf download "$ASR_MODEL" >/dev/null || huggingface-cli download "$ASR_MODEL" >/dev/null
if [ "${YINGSAY_SKIP_LLM:-0}" != "1" ]; then
  hf download "$LLM_MODEL" >/dev/null || huggingface-cli download "$LLM_MODEL" >/dev/null
fi

say "5/6 下载 SenseVoice-Small（约 160MB）"
if [ ! -f "$D/$SV_NAME/tokens.txt" ]; then
  curl -fL "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$SV_NAME.tar.bz2" -o "$D/sv.tar.bz2"
  tar xjf "$D/sv.tar.bz2" -C "$D" && rm -f "$D/sv.tar.bz2"
fi
curl -fsSL "$RAW/sensevoice_server.py" -o "$D/sensevoice_server.py"

say "6/6 写入启动脚本"
if [ ! -f "$D/env" ]; then
cat > "$D/env" <<ENV
# 应声本地模型配置（改完后重启本地模型生效）
ASR_MODEL=$ASR_MODEL
LLM_MODEL=$LLM_MODEL
ASR_PORT=8765
LLM_PORT=8766
SV_PORT=8767
HOST=127.0.0.1
# 显存占比（RTX 5090 32GB：ASR 约 7GB，LLM 约 13GB）
ASR_GPU=0.22
LLM_GPU=0.40
SV_DIR=$D/$SV_NAME
ENABLE_LLM=$([ "${YINGSAY_SKIP_LLM:-0}" = "1" ] && echo 0 || echo 1)
ENV
fi

cat > "$D/run.sh" <<'RUN'
#!/usr/bin/env bash
# 前台运行三个本地服务；应声会通过 wsl.exe 在后台调用它。手动调试：~/.yingsay-local/run.sh
D="$HOME/.yingsay-local"
# shellcheck disable=SC1091
source "$D/env"
[ -n "${YINGSAY_ASR_MODEL:-}" ] && ASR_MODEL="$YINGSAY_ASR_MODEL"
[ -n "${YINGSAY_LLM_MODEL:-}" ] && LLM_MODEL="$YINGSAY_LLM_MODEL"
[ -n "${YINGSAY_ENABLE_LLM:-}" ] && ENABLE_LLM="$YINGSAY_ENABLE_LLM"
# shellcheck disable=SC1091
source "$D/venv/bin/activate"
export VLLM_LOGGING_LEVEL=WARNING HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-0}
up() { curl -s -m 1 "http://$HOST:$1$2" >/dev/null 2>&1; }
pids=()
cleanup() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done; rm -f "$D"/*.pid; }
trap cleanup EXIT INT TERM

if ! up "$SV_PORT" /health; then
  python "$D/sensevoice_server.py" --model-dir "$SV_DIR" --host "$HOST" --port "$SV_PORT" > "$D/sensevoice.log" 2>&1 &
  pids+=($!); echo $! > "$D/sensevoice.pid"
fi
if [ "$ASR_MODEL" != "none" ] && ! up "$ASR_PORT" /v1/models; then
  qwen-asr-serve "$ASR_MODEL" --served-model-name local-asr --host "$HOST" --port "$ASR_PORT" \
    --gpu-memory-utilization "$ASR_GPU" --max-model-len 8192 --max-num-seqs 16 > "$D/asr.log" 2>&1 &
  pids+=($!); echo $! > "$D/asr.pid"
  # 等 ASR 占好显存再启动 LLM，避免两个 vLLM 同时估算显存
  for _ in $(seq 1 600); do up "$ASR_PORT" /v1/models && break; kill -0 "${pids[-1]}" 2>/dev/null || break; sleep 1; done
fi
if [ "$ENABLE_LLM" = "1" ] && ! up "$LLM_PORT" /v1/models; then
  vllm serve "$LLM_MODEL" --served-model-name local-llm --host "$HOST" --port "$LLM_PORT" \
    --gpu-memory-utilization "$LLM_GPU" --max-model-len 8192 --max-num-seqs 16 > "$D/llm.log" 2>&1 &
  pids+=($!); echo $! > "$D/llm.pid"
fi
echo "YingSay local services started: ${pids[*]}"
wait
RUN

cat > "$D/stop.sh" <<'STOP'
#!/usr/bin/env bash
D="$HOME/.yingsay-local"
for f in "$D"/*.pid; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null; rm -f "$f"; done
pkill -f "[s]ensevoice_server.py" 2>/dev/null
pkill -f "[s]erved-model-name local-asr" 2>/dev/null
pkill -f "[s]erved-model-name local-llm" 2>/dev/null
echo stopped
STOP

cat > "$D/status.sh" <<'ST'
#!/usr/bin/env bash
D="$HOME/.yingsay-local"; source "$D/env"
for x in "SenseVoice $SV_PORT /health" "Qwen3-ASR $ASR_PORT /v1/models" "LLM $LLM_PORT /v1/models"; do
  set -- $x; if curl -s -m 1 "http://$HOST:$2$3" >/dev/null; then echo "$1: ready"; else echo "$1: down"; fi
done
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
ST
chmod +x "$D/run.sh" "$D/stop.sh" "$D/status.sh"
echo installed > "$D/installed"

say "完成！"
echo "回到应声：设置 → 本地模型 → 点「启动本地模型」（首次加载约 1-2 分钟，期间自动用 SenseVoice 顶上）。"
echo "手动调试：wsl -e bash -lc ~/.yingsay-local/run.sh    查看状态：wsl -e bash -lc ~/.yingsay-local/status.sh"
