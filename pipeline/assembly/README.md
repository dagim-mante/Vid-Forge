# Final film assembly

The assembly stage combines completed shot videos into one MP4. The `video` command creates individual clips; `assemble` joins them in the exact order of `shots` in `production/storyboard.json`.

## Requirements

Install FFmpeg with both `ffmpeg` and `ffprobe` on `PATH`. The encoder must include `libx264`.

## Files

Inputs:

```text
projects/<project>/production/storyboard.json
projects/<project>/outputs/shots/<shot_id>.mp4
projects/<project>/outputs/narration/timing.json
projects/<project>/outputs/narration/voice_track.wav
projects/<project>/outputs/narration/narration_16x9.srt
projects/<project>/outputs/radio_cut/placeholder_music.wav
```

Default outputs:

```text
projects/<project>/production/final.mp4
projects/<project>/production/final_9x16.mp4
projects/<project>/production/assembly_manifest.json
```

The stage checks every present shot file, probes its video stream, and fully decodes it before rendering. Missing clips are reported and replaced by black intervals of the same planned duration, so available shots can still be assembled without shifting narration or subtitles. Invalid present clips stop rendering; at least one valid shot clip is required. Storyboard order is preserved and filesystem glob order is never used.

## Usage

```powershell
vid-forge assemble project=tibeb
vid-forge assemble project=tibeb --dry-run
vid-forge assemble project=tibeb --transition cut
vid-forge assemble project=tibeb --transition crossfade
vid-forge assemble project=tibeb --transition crossfade --transition-duration 0.75
vid-forge assemble project=tibeb --output final-v2.mp4
vid-forge assemble project=tibeb --force
```

Run `vid-forge narration project=<project>` first. Assembly shows `meta.title` on a 3-second opening title card, then uses the saved timing plan and voice track, burns in the matching 16:9 or 9:16 English subtitles, and mixes the radio cut's placeholder music under the narration. Missing shots become black intervals; present clips are trimmed or held on their last frame to match the timing file. The end card shows `end_card.text` and optional `end_card.credits`. Narration and subtitles are shifted to follow the opening card. Both final runtimes equal `timing.json` plus the 3-second title card.

Each run writes both `production/final.mp4` (1280x720) and `production/final_9x16.mp4` (720x1280). With `--output final-v2.mp4`, the portrait file is named `final-v2_9x16.mp4`. Both videos use the same audio and shot timing, with independently cropped framing and aspect-specific subtitles.

The default transition is a hard cut. Crossfades use FFmpeg's `xfade` filter and default to 0.5 seconds. A crossfade must be shorter than both adjacent timeline shot durations; otherwise assembly stops. Crossfades preserve each shot's authored start time by extending the previous shot's final frame during the blend.

All clips are normalized with an aspect-ratio-preserving scale and center crop to **1280x720**, storyboard FPS, square pixels, and yuv420p. The final MP4 uses H.264 video, AAC audio, and fast-start. Voice and placeholder music use the radio cut's ducking and loudness target. Per-shot source videos should be silent; unexpected source audio is rejected.

## Dry run and replacement

`--dry-run` probes and fully decodes each present input, then reports storyboard order, clip durations and dimensions, missing/invalid clips, placeholder intervals, narration timing, title/end-card text, both target formats and output paths. It does not render or write the manifest.

Both existing outputs are protected. Use `--force` to replace them. Vid-Forge renders both to temporary MP4 files in `production/`, validates codec, dimensions, FPS, pixel format, duration, and a full decode for each, then replaces the final files. A failed render or validation leaves previous outputs untouched. Temporary files are removed after failures.

## Manifest and troubleshooting

`production/assembly_manifest.json` records the ordered shot IDs, relative input paths, SHA-256 checksums, probed input metadata, transition and overlap settings, both target formats, FFmpeg filter graphs and commands, output checksums, actual durations, and validation results. It is written only after both rendered outputs pass validation.

- **FFmpeg or ffprobe not found:** install FFmpeg and add both executables to `PATH`.
- **Missing clip:** run `vid-forge video project=<project>` for the missing shot(s), then retry assembly.
- **Invalid or truncated clip:** run `vid-forge video project=<project> --validate-only` to check the video-stage cache, then regenerate affected shots if needed.
- **Output already exists:** choose another filename with `--output` or pass `--force`.
- **Crossfade too long:** lower `--transition-duration`; it must be shorter than each adjacent input clip.
- **Narration timing or subtitles missing:** run `vid-forge narration project=<project>` to create the voice track, timing file, subtitles, and radio-cut music bed.
- **Unexpected audio stream:** generated per-shot videos should be silent; investigate or regenerate a clip with embedded audio before assembly.
