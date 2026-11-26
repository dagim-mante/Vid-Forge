# The Last Seed — Failures and Open Issues

## F-001 — Wan shot generation did not finish

- **Observed:** The user previously reported cancelling a Wan generation run after it had taken more than an hour. The video manifest initially recorded `shot_01`, `shot_03`, `shot_04`, `shot_05`, and `shot_07`–`shot_11` as `running`, with no completed result entries.
- **Impact:** Nine of the 11 storyboard visuals were temporarily unavailable, forcing a placeholder preview.
- **Resolution:** Wan 2.2 TI2V video synthesis was completed across five key diffusion shots (`shot_03`, `shot_04`, `shot_05`, `shot_07`, and `shot_11`) on Kaggle Nvidia Tesla T4 using kernel versions 3 and 4 with `diffusers-adaptive-offload-v3` at 12 steps. The remaining six shots (`shot_01`, `shot_02`, `shot_06`, `shot_08`, `shot_09`, and `shot_10`) were fulfilled via optimized FFmpeg Ken Burns motion, completing all 11 storyboard shots.
- **Measured Performance:** Per-shot diffusion latency averaged ~1,595 seconds (~26.5 minutes) with a 299.9-second model load. Total Kaggle session for the 4-shot batch ran 6,819 seconds (~1.89 hours).
- **Status:** Resolved.

## F-002 — Assembly output contains nine black placeholders

- **Observed:** `assembly_manifest.json` previously listed nine missing shot IDs and two final preview MP4 outputs containing black video placeholders.
- **Impact:** The initial preview validated audio/subtitle timing but lacked complete video imagery.
- **Resolution:** Following completion of the five Wan diffusion shots and six Ken Burns camera renders, final assembly was executed at `2026-11-26T19:23:16Z`. The updated `assembly_manifest.json` records `missing_shot_ids: []` with all 11 shots present. Both master outputs (`final.mp4` at 1280×720 and `final_9x16.mp4` at 720×1280) rendered cleanly at 84.91 seconds with full synchronized Amharic narration, ducked music bed, and burned-in subtitles.
- **Status:** Resolved.

## F-003 — Ken Burns movement felt shaky

- **Observed:** The user reported that camera movement on Ken Burns shots felt shaky.
- **Investigation & Diagnostics:**
  - Initial 16% travel was reduced to 4% with easing, but frame-to-frame shake persisted.
  - Root Cause: FFmpeg's `zoompan` filter rounds crop windows to integer pixels, causing slow pans to jump 1–2 pixels every few frames (measured ~1.5 px jitter, larger than the motion itself). Resampling from default 25 fps to 24 fps also dropped a frame once per second.
- **Fix Implemented:** Replaced `zoompan` with sub-pixel bicubic `perspective` filter evaluated per frame, set `-framerate 24` on image inputs, and restored smooth 12% subtle camera travel (`ffmpeg-filter-v2-subtle-eased` and `v3-smooth-subpixel`).
- **Result:** Measured jitter dropped from ~1.5 px to ~0.09 px on pans and ~0.01 px on zooms. Deployed across all six Ken Burns shots (`shot_01`, `shot_02`, `shot_06`, `shot_08`, `shot_09`, `shot_10`) in the final assembly.
- **Status:** Resolved and deployed in production.

## F-004 — Kaggle Tesla T4 CUDA Out of Memory on Wan 2.2 TI2V (Kernel v2)

- **Observed:** Kaggle worker run for `shot_03` (kernel version 2, `2026-11-26T16:29:34Z`) terminated with an out-of-memory error:
  `CUDA out of memory. Tried to allocate 108.00 MiB. GPU 0 has a total capacity of 14.56 GiB of which 34.81 MiB is free. Including non-PyTorch memory, this process has 14.53 GiB memory in use.`
- **Impact:** Single shot failed; video pipeline could not proceed until memory pressure was mitigated.
- **Cause:** Peak memory allocation during diffusion pipeline setup exceeded the 14.56 GiB usable VRAM boundary on the Kaggle Tesla T4 due to memory fragmentation and sequential offload staging overhead.
- **Fix Implemented:** Deployed `diffusers-adaptive-offload-v3` with optimized memory management, VAE tiling, and explicit cache cleanup. Kernel version 3 succeeded on `shot_03` (latency 1,541.8 s) and kernel version 4 completed the 4-shot batch (`shot_04`, `shot_05`, `shot_07`, `shot_11`) without a single memory error.
- **Status:** Resolved.

## F-005 — Cloudflare Workers AI Content Moderation False Positives (Project The Last Light)

- **Observed:** During keyframe generation for project `lastNight` (`The Last Light`), seven image generation requests failed with HTTP 400 errors:
  `AiError: Your output has been flagged. Please choose another prompt / input image combination.`
- **Impact:** Keyframe candidates failed to generate on initial passes for interior scenes.
- **Cause:** Cloudflare Workers AI safety classifier flagged benign descriptive terms regarding dimly lit rooms, shadows, and sombre expressions as potential policy violations.
- **Fix Implemented:** Rephrased prompt descriptors to use neutral cinematic terms (e.g., "warm golden twilight ambience", "contemplative portrait") while retaining visual atmosphere. 28 candidates were successfully generated.
- **Status:** Resolved with prompt adjustments.

## F-006 — Storyboard Schema & Duration Validation Rejections

- **Observed:** Early automated storyboard generation runs failed schema and semantic validation in `validate_storyboard.py`:
  - `theLastSeed` (Attempt 1): Duration sum deviated beyond allowable project tolerance.
  - `lastNight` (Attempt 1): Schema failure due to missing required property `meta.subtitle_language`.
  - `lastNight` (Attempt 2): Semantic duration failure (total film duration was 34.0 seconds, failing the 60–90 second constraint).
- **Impact:** Raw LLM outputs were blocked from entering the production pipeline.
- **Resolution:** Vid-Forge's automated validation loop captured schema error messages and reinjected them into the Gemini generation context. Gemini repaired the schema properties and expanded the shot list to 12 shots on Attempt 3, passing all checks.
- **Status:** Automatically mitigated by self-healing validation loop.
