#!/usr/bin/env bash
# Compiles the trained model (best.onnx from the Colab notebook) into a .hef for the Hailo-8 of the Pi.
#
#   ./compile.sh best.onnx calib/ [classes.json]
#
# It runs on an x86-64 Linux PC (or WSL2) with the Hailo Dataflow Compiler and the Hailo Model Zoo installed, the
# simplest way being the "Hailo AI Software Suite" Docker image from the Hailo Developer Zone (free account). Take
# the version that goes with the HailoRT of the Pi ("hailortcli --version" on the Pi), or the Pi will not load
# the .hef. calib/ is a folder of a few hundred images like the ones the camera sees (the notebook makes it):
# the compiler uses them to turn the model into 8 bits without losing precision.
#
# The output is navplotter-marine-<date>.hef and the .json with its classes next to it: both go to
# /var/lib/navplotter/models on the Pi (the README says how), where the camera service picks the newest .hef.
set -euo pipefail

ONNX=${1:?usage: compile.sh best.onnx calib/ [classes.json]}
CALIB=${2:?usage: compile.sh best.onnx calib/ [classes.json]}
CLASSES=${3:-$(dirname "$0")/classes.json}
ARCH=${HAILO_ARCH:-hailo8}          # hailo8l for the 13 TOPS AI Kit
MODEL=${HAILO_MODEL:-yolov8s}       # the Model Zoo network the model was trained from
# HAILO_EXTRA="--performance" squeezes more fps out of the chip, at the cost of hours of compiling

command -v hailomz >/dev/null || { echo "hailomz not found: run this in the Hailo AI Software Suite" >&2; exit 1; }
[ -f "$ONNX" ] || { echo "$ONNX not found" >&2; exit 1; }
[ -d "$CALIB" ] || { echo "$CALIB is not a folder" >&2; exit 1; }
N=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["names"]))' "$CLASSES")
OUT=navplotter-marine-$(date +%Y%m%d-%H%M)

echo "Compiling $ONNX ($MODEL, $N classes) for $ARCH, calibration on $(ls "$CALIB" | wc -l) images"
hailomz compile "$MODEL" --ckpt "$ONNX" --hw-arch "$ARCH" --calib-path "$CALIB" --classes "$N" ${HAILO_EXTRA:-}
mv "$MODEL.hef" "$OUT.hef"
python3 - "$CLASSES" "$OUT.json" <<'PY'
import json, sys
j = json.load(open(sys.argv[1]))
json.dump({k: j[k] for k in ("names", "it", "important") if k in j}, open(sys.argv[2], "w"), ensure_ascii=False, indent=1)
PY
echo "Done: $OUT.hef and $OUT.json"
