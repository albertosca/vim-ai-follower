#!/usr/bin/env bash
# Assemble the frames scripts/record_demo.sh captured into the demo GIF + MP4.
#
#   scripts/render_demo.sh <frames-dir> [fps]
#
# vhs writes each frame as a text layer and a cursor layer
# (frame-text-NNNNN.png + frame-cursor-NNNNN.png, captured at 50 fps); the two
# are overlaid here. H.264 needs even dimensions and vhs's frame is the
# terminal minus its padding (1350x665 for this tape), so the MP4 drops the
# odd row. vhs's own ffmpeg step is not used: against ffmpeg 9.x it
# exits 0 and writes nothing (measured 2026-09-23).
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
FRAMES=${1:?usage: scripts/render_demo.sh <frames-dir> [fps]}
FPS=${2:-12}
OUT="$REPO/assets/demo"
MAX_GIF_BYTES=$((5 * 1024 * 1024))

[[ -f "$FRAMES/frame-text-00001.png" ]] || { echo "render_demo: no frames in $FRAMES" >&2; exit 1; }
INPUTS=(-framerate 50 -i "$FRAMES/frame-text-%05d.png" -framerate 50 -i "$FRAMES/frame-cursor-%05d.png")

ffmpeg -hide_banner -loglevel error -y "${INPUTS[@]}" \
    -filter_complex "[0][1]overlay,fps=$FPS,split[a][b];[a]palettegen[p];[b][p]paletteuse" \
    "$OUT/follow.gif"
ffmpeg -hide_banner -loglevel error -y "${INPUTS[@]}" \
    -filter_complex "[0][1]overlay,crop=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p" \
    -c:v libx264 -crf 28 -movflags +faststart "$OUT/follow.mp4"

gif_bytes=$(stat -f %z "$OUT/follow.gif")
mp4_bytes=$(stat -f %z "$OUT/follow.mp4")
echo "render_demo: follow.gif $gif_bytes bytes, follow.mp4 $mp4_bytes bytes"
if ((gif_bytes > MAX_GIF_BYTES)); then
    echo "render_demo: follow.gif is over 5 MB; rerun with a lower fps" >&2
    exit 1
fi
