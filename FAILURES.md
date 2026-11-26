# The Last Seed — Failures and Open Issues

## F-001 — Wan shot generation did not finish

- **Observed:** The user reported cancelling a Wan generation run after it had taken more than an hour. The video manifest records `shot_01`, `shot_03`, `shot_04`, `shot_05`, and `shot_07`–`shot_11` as `running`, with no completed result entries for them.
- **Impact:** Nine of the 11 storyboard visuals are unavailable for assembly.
- **Cause:** Not established by the available artifacts. The worker's sequential CPU offload is intended to fit the 5B model on a T4, but trades speed for memory use. The manifest does not contain a terminal Kaggle error or per-shot runtime for this attempt.
- **Next step:** Check the next completed/cancelled Kaggle run's terminal status and results output before changing model settings. Keep the GPU, runtime, steps, frame count, and shot ID with any measurements.
- **Status:** Incomplete/cancelled; root cause unknown.

## F-002 — Assembly output contains nine black placeholders

- **Observed:** `assembly_manifest.json` lists nine missing shot IDs, two input clips, and two final MP4 outputs. It records an 84.92-second duration and an assembly status of completed.
- **Impact:** The files are useful for checking broad timing/audio integration, but not for assessing the complete visual story. The completed status describes a successful placeholder-capable render, not complete shot coverage.
- **Next step:** Finish and validate the missing Wan clips, reassemble both aspect ratios, and confirm `missing_shot_ids` is empty before the audience test.
- **Status:** Open until a complete assembly is rendered.

## F-003 — Ken Burns movement felt shaky

- **Observed:** The user reported that the camera movement on Ken Burns shots felt shaky.
- **Change made:** Reduced travel from 16% to 4%, added eased motion, and bumped the filter version to invalidate old cached renders.
- **Result:** Not yet reviewed on a newly rendered clip.
- **Next step:** Render a representative Ken Burns shot, watch it at normal playback size, and record whether the movement is calm enough in `EXPERIMENTS.md`.
- **Follow-up (still shaky, not just too fast):** Cutting the travel to 4% did not remove the shake. Root cause was FFmpeg's `zoompan`, which rounds its crop window to whole input pixels, so slow moves advanced in uneven 1-2 px jumps (measured ~1.5 px of frame-to-frame jitter on a pan, larger than the movement itself). A second, smaller cause: the looped still defaulted to 25 fps and was resampled to 24 fps, dropping a frame about once a second.
- **Change made (v3):** Replaced `zoompan` with the sub-pixel `perspective` filter (bicubic, evaluated per frame), set `-framerate` on the still input, and restored a visible but slow 12% move. Filter version bumped to `ffmpeg-filter-v3-smooth-subpixel`.
- **Result:** Measured jitter dropped from ~1.5 px to ~0.09 px on a pan and ~0.01 px on a zoom, matching a float-precision reference render. Not yet reviewed by a human viewer at normal playback size.
- **Status:** Fix implemented and measured; human review pending.

