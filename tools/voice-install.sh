#!/bin/bash
# Installs the voice assistant (navplotter/voice.py) for the desktop user of the Raspberry, the one whose audio
# (PipeWire) has the Bluetooth headset or the speakerphone. Run it as that user, not with sudo:
#   bash /opt/navplotter/tools/voice-install.sh            speech to text and voice
#   NAVPLOTTER_LLM=1 bash /opt/navplotter/tools/voice-install.sh     also the local language model
# Choices (environment variables):
#   NAVPLOTTER_WHISPER=small|base|tiny   speech recognition model (default small: 466 MB, the best in Italian here)
#   NAVPLOTTER_LLM_MODEL=1.5b|3b          language model (default 1.5b: 1.1 GB and faster; 3b: 2 GB, better answers)
# Everything goes in ~/.local/share/navplotter-voice; downloads that stop are resumed by running it again.
set -euo pipefail

if [ "$(id -u)" = 0 ]; then echo "Run it as the desktop user, without sudo (it asks for sudo only for the packages)"; exit 1; fi
H="$HOME/.local/share/navplotter-voice"
CODE=/opt/navplotter
WHISPER=${NAVPLOTTER_WHISPER:-small}
mkdir -p "$H/models" "$H/voices" "$H/llm"

get() {   # url file: download, resuming a partial file
  echo "   $(basename "$2")"
  curl -fL --retry 5 -C - -o "$2.part" "$1" && mv "$2.part" "$2"
}

echo "== Packages"
sudo apt-get update -qq
sudo apt-get install -y -qq git cmake build-essential curl pipewire-bin pulseaudio-utils >/dev/null

echo "== Speech to text: whisper.cpp (a few minutes to build)"
if [ ! -d "$H/whisper.cpp" ]; then
  git clone -q --depth 1 --branch v1.7.6 https://github.com/ggml-org/whisper.cpp.git "$H/whisper.cpp"
fi
cmake -S "$H/whisper.cpp" -B "$H/whisper.cpp/build" -DCMAKE_BUILD_TYPE=Release >/dev/null
cmake --build "$H/whisper.cpp/build" -j4 --target whisper-server >/dev/null
[ -f "$H/models/ggml-$WHISPER.bin" ] || get "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-$WHISPER.bin" "$H/models/ggml-$WHISPER.bin"

echo "== Voice: Piper, Italian voice Paola"
if [ ! -x "$H/piper/piper" ]; then
  get https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_aarch64.tar.gz "$H/piper.tar.gz"
  tar -xzf "$H/piper.tar.gz" -C "$H" && rm "$H/piper.tar.gz"
fi
V=https://huggingface.co/rhasspy/piper-voices/resolve/main/it/it_IT/paola/medium/it_IT-paola-medium.onnx
[ -f "$H/voices/it_IT-paola-medium.onnx" ] || get "$V" "$H/voices/it_IT-paola-medium.onnx"
[ -f "$H/voices/it_IT-paola-medium.onnx.json" ] || get "$V.json" "$H/voices/it_IT-paola-medium.onnx.json"

if [ "${NAVPLOTTER_LLM:-0}" = 1 ]; then
  echo "== Language model: llama.cpp (about ten minutes to build)"
  if [ ! -d "$H/llama.cpp" ]; then
    git clone -q --depth 1 https://github.com/ggml-org/llama.cpp.git "$H/llama.cpp"
  fi
  cmake -S "$H/llama.cpp" -B "$H/llama.cpp/build" -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF >/dev/null
  cmake --build "$H/llama.cpp/build" -j4 --target llama-server >/dev/null
  case "${NAVPLOTTER_LLM_MODEL:-1.5b}" in
    3b) M=Qwen2.5-3B-Instruct-GGUF; F=qwen2.5-3b-instruct-q4_k_m.gguf ;;
    *)  M=Qwen2.5-1.5B-Instruct-GGUF; F=qwen2.5-1.5b-instruct-q4_k_m.gguf ;;
  esac
  [ -f "$H/llm/$F" ] || { rm -f "$H"/llm/*.gguf; get "https://huggingface.co/Qwen/$M/resolve/main/$F" "$H/llm/$F"; }
fi

echo "== Service navplotter-voice (of this user, started at boot)"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/navplotter-voice.service" <<UNIT
[Unit]
Description=NavPlotter voice assistant (whisper.cpp, Piper, local language model)
After=pipewire.service wireplumber.service

[Service]
WorkingDirectory=$CODE
ExecStart=/usr/bin/python3 -m navplotter.voice
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable -q navplotter-voice
systemctl --user restart navplotter-voice

echo
echo "Done. In the web app, the microphone button at the bottom right: tap, speak, tap again."
echo "Log: journalctl --user -u navplotter-voice -f"
