# Placeholder — replace with actual pre-recorded WAV files.
# Required files:
#   pakketje.wav    — "Zet het pakketje maar bij de deur, dankjewel!"
#   kom_eraan.wav   — "Ik kom eraan! Eén momentje alstublieft."
#
# Specifications (must match EZVIZ DB1 Pro backchannel):
#   Format:   WAV (PCM)
#   Codec:    PCM signed 16-bit little-endian (or PCM A-law for direct passthrough)
#   Rate:     8000 Hz
#   Channels: Mono (1)
#   Duration: 2-5 seconds
#
# Generate test files with FFmpeg:
#   ffmpeg -f lavfi -i "sine=frequency=440:duration=3" -ar 8000 -ac 1 pakketje.wav
#   ffmpeg -f lavfi -i "sine=frequency=523:duration=3" -ar 8000 -ac 1 kom_eraan.wav
