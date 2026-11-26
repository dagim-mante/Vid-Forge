# The Last Seed — Experiments

This log distinguishes measured outcomes from changes whose results are still unknown. Entries reconstructed from production manifests are marked retrospective because their hypotheses were not recorded before the run.

## E-001 — Two keyframe candidates per shot (retrospective)

- **Hypothesis:** Two image candidates per storyboard shot give the creator a useful choice while keeping image generation manageable.
- **Change:** The storyboard requests two candidates for each of its 11 shots; the keyframe manifest records candidate paths, seeds, success states, and selections.
- **Result:** Candidate images and selections are recorded in the project artifacts. No blinded comparison or quality ratings were saved, so the artifacts confirm generation/selection only; they do not show that two candidates are enough or which visual approach audiences prefer.
- **Status:** Generated; qualitative evaluation not done.

## E-002 — Assemble with missing shots (retrospective)

- **Hypothesis:** A placeholder-filled assembly can be used to review timing and audio before every generated shot is ready.
- **Change:** Assembled the storyboard with missing clips represented by black placeholders, retaining the ordered timeline, title cards, narration, subtitles, and music.
- **Result:** The manifest records both aspect-ratio MP4s at 84.92 seconds. It also records nine missing shots, so the preview can help inspect timing/audio but cannot validate the complete visual experience.
- **Status:** Completed as an incomplete preview.

## E-003 — Lower Wan inference steps to 20

- **Hypothesis:** Lowering the default from 50 to 20 steps will reduce Wan generation time on Kaggle T4 while retaining enough motion quality for a short film.
- **Change:** Set the CLI and video-stage default to 20; explicit `--steps` remains available. The theLastSeed video manifest records 20 steps for the submitted Wan jobs.
- **Result:** The user reported cancelling after more than an hour. The manifest still says the nine Wan jobs were running and contains no completed Wan clips, so this run provides no completed runtime or quality comparison. A 50-step baseline and a completed 20-step output were not measured side by side.
- **Status:** Runtime/quality hypothesis not yet evaluated.

## E-004 — Reduce Ken Burns camera travel

- **Hypothesis:** 4% camera travel with eased motion will feel calmer than the previous 16% linear movement that the user described as shaking.
- **Change:** Reduced the zoom/pan range to 1.04 and eased the movement across the shot. The effect version was changed so the cache will render new clips.
- **Result:** No post-change render has been reviewed yet.
- **Status:** Awaiting new render and review.

## E-005 — Three-viewer audience review

- **Hypothesis:** Viewers who enjoy grounded historical folktales will understand that Alemu's planting choice creates a community benefit for a later generation; the drought-to-harvest arc and final moral will be the most memorable parts.
- **Change:** Show the same complete 11-shot cut to three audience-fit viewers, then ask the five questions in `DESIGN.md` and record anonymized notes in its call log.
- **Result:** Not run. The currently assembled cut is missing nine shots, and no audience feedback has been collected.
- **Status:** Blocked on completing a reviewable cut and conducting the three real sessions.

