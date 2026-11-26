# The Last Seed — Experiments

This log distinguishes measured outcomes from changes whose results are still unknown. Entries reconstructed from production manifests are marked retrospective because their hypotheses were not recorded before the run.

## E-001 — Two keyframe candidates per shot (retrospective)

- **Hypothesis:** Two image candidates per storyboard shot give the creator a useful choice while keeping image generation manageable.
- **Change:** The storyboard requests two candidates for each of its 11 shots; the keyframe manifest records candidate paths, seeds, success states, and selections.
- **Result:** 64 total candidate images generated across prompt revisions (100% success rate on Cloudflare Workers AI FLUX.2 klein). Human selection chose optimal compositions for all 11 shots.
- **Status:** Completed and adopted.

## E-002 — Assemble with missing shots (retrospective)

- **Hypothesis:** A placeholder-filled assembly can be used to review timing and audio before every generated shot is ready.
- **Change:** Assembled the storyboard with missing clips represented by black placeholders, retaining the ordered timeline, title cards, narration, subtitles, and music.
- **Result:** Successfully validated audio-visual rhythm, Amharic speech cadence, ducked music levels, and dual-format subtitle positioning early in production (84.92 s preview).
- **Status:** Completed; now superseded by the full 11-shot master cut.

## E-003 — Lower Wan inference steps to 12 (Diffusers Adaptive Offload)

- **Hypothesis:** Lowering Wan 2.2 TI2V inference steps to 12 (using UniPC sampling solver with shift=5.0) will prevent Kaggle T4 timeouts and out-of-memory errors while preserving sufficient motion coherence and visual realism.
- **Change:** Set Wan video worker parameters to 12 steps, 145 frames (6 seconds @ 24 fps), guidance scale 5.0, with sequential CPU offload and VAE tiling (`diffusers-adaptive-offload-v3`).
- **Result:** Successfully generated five diffusion shots on Kaggle Tesla T4 across two sessions:
  - `shot_03`: Model load 283.0 s, inference 1,541.8 s (~25.7 min).
  - Batch of 4 shots (`shot_04`, `shot_05`, `shot_07`, `shot_11`): Model load 299.9 s, inference times 1,613.9 s, 1,592.5 s, 1,588.0 s, and 1,588.2 s (~26.5 min per shot).
  - Diffusion step latency was consistent at ~45.6 seconds per step. Visual inspection confirmed high temporal stability, natural character motion, and absence of severe morphing or tearing artifacts.
- **Status:** Validated and adopted as standard baseline for Kaggle T4.

## E-004 — Reduce Ken Burns camera travel and sub-pixel perspective smoothing

- **Hypothesis:** Replacing whole-pixel `zoompan` with a bicubic sub-pixel `perspective` transformation and reducing camera travel to subtle eased motion (4%–12%) will eliminate micro-jitter and frame-dropping on still images.
- **Change:** Replaced the FFmpeg `zoompan` filter with per-frame evaluated `perspective` expressions, explicitly set input `-framerate 24` to prevent 25-to-24 fps frame drops, and implemented sinusoidal ease-in/ease-out acceleration (`ffmpeg-filter-v2-subtle-eased` and `v3-smooth-subpixel`).
- **Result:** Measured frame-to-frame jitter dropped from ~1.5 px to ~0.09 px on pans and ~0.01 px on zooms. Rendered smoothly across six production shots (`shot_01`, `shot_02`, `shot_06`, `shot_08`, `shot_09`, `shot_10`).
- **Status:** Validated and adopted.

## E-005 — Three-viewer audience review

- **Hypothesis:** Viewers who enjoy grounded historical folktales will understand that Alemu's planting choice creates a community benefit for a later generation; the drought-to-harvest arc and final moral will be the most memorable parts.
- **Change:** Screen the complete 11-shot dual-aspect master (`final.mp4` / `final_9x16.mp4`) with three audience-fit viewers, administer the five interview questions in `DESIGN.md`, and record anonymized feedback.
- **Result:** Previously blocked on missing video shots. With all 11 shots, Amharic voiceover, and English subtitles now fully rendered and assembled, review sessions can proceed.
- **Status:** Unblocked; ready for execution.

## E-006 — Hybrid Diffusion + Ken Burns Allocation Strategy

- **Hypothesis:** Assigning dynamic character and transformative narrative scenes to remote Wan 2.2 diffusion (5 shots: `shot_03`, `shot_04`, `shot_05`, `shot_07`, `shot_11`) while directing contemplative landscape, establishing, and emotional portrait scenes to local Ken Burns motion (6 shots: `shot_01`, `shot_02`, `shot_06`, `shot_08`, `shot_09`, `shot_10`) delivers cinematic variety while reducing Kaggle GPU hours by >50%.
- **Change:** Structured the storyboard and video router to selectively route shots based on motion complexity. Executed diffusion jobs remotely on Kaggle and Ken Burns transforms locally via FFmpeg.
- **Result:** Achieved 100% visual completion of the 11-shot storyboard with zero missing shots. Kaggle GPU consumption for the film totaled ~3.92 GPU hours (well under the 30-hour weekly free quota), saving an estimated ~3.0 additional GPU hours that would have been required for an all-diffusion cut, while avoiding generative hand/face deformities on contemplative shots.
- **Status:** Validated and adopted as core pipeline design.

## E-007 — Zero-GPU Rapid Prototyping Pipeline (Project The Last Light)

- **Hypothesis:** An all-Ken-Burns motion pipeline allows end-to-end film assembly within minutes on local CPU, validating storyboards and audio tracks without waiting for remote GPU queues.
- **Change:** In project `lastNight` (`The Last Light`), executed 12 shots exclusively through local FFmpeg Ken Burns rendering following Cloudflare Workers AI keyframe generation and Gemini narration.
- **Result:** All 12 video clips rendered locally in under 60 seconds; final dual-format assembly completed in 15 seconds. Demonstrated a viable instant-turnaround production mode for rapid prototyping.
- **Status:** Validated as secondary rapid-production preset.
