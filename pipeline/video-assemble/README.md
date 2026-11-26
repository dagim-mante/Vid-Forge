# Video assembly stage

This stage renders one MP4 per storyboard shot under `projects/<name>/outputs/shots/`. It does not combine clips into a film.

## Setup

1. Install the project in the repository environment: `python -m pip install -e .` (this installs the official Kaggle CLI and project dependencies).
2. Install FFmpeg with `ffmpeg` and `ffprobe` on `PATH`. FFmpeg must include `libx264`.
3. In Kaggle, create an API token and add `KAGGLE_API_TOKEN`, `KAGGLE_USERNAME` (profile slug), and optionally `KAGGLE_ACCELERATOR` to the repository-root `.env`. The token is read by the Kaggle CLI process and is never written into the uploaded input bundle, worker source, manifest, or run log. Keep `.env` private.
4. Use an accelerator with enough memory. The official Wan 2.2 TI2V-5B 720p configuration documents 24 GB VRAM; the default is `NvidiaTeslaA100`. Kaggle availability varies by account and quota. The worker checks CUDA and reports the actual GPU and memory before it downloads model weights. A different accelerator can be passed with `--accelerator`.
5. Ensure each shot has `keyframe.selected` set in `production/storyboard.json` and the matching candidate is marked successful in `production/keyframes.json`. The selected image is resolved from that manifest, never by globbing for a shot filename.

The first GPU run creates or updates a private Kaggle Dataset with the selected images and full requests, then creates or updates a private GPU Kernel. The worker clones the official [Wan 2.2 repository](https://github.com/Wan-Video/Wan2.2) at code revision `1ea34ff`, installs its runtime requirements, downloads the pinned official `Wan-AI/Wan2.2-TI2V-5B` model revision to Kaggle's temporary scratch storage (`/kaggle/tmp`), initializes the model once, then generates every cache-missing i2v shot in the submitted batch. It skips the optional `flash_attn` source build and uses Wan's PyTorch SDPA fallback. The model weights are not uploaded with the job bundle or saved as notebook output. The first run can take a while because it installs dependencies and downloads the model. Model download and inference require Kaggle internet access and sufficient scratch space. Kaggle removes scratch files between kernel runs, so batching amortizes the model download and load across shots in a run.

The worker installs dependencies before importing PyTorch, NumPy, or Wan. This avoids keeping already-imported compiled modules in memory while pip replaces their NumPy dependency. It imports the Wan/diffusers runtime before downloading model weights, so import and ABI errors fail during setup rather than after the large checkpoint download.

The Wan path uses the official TI2V input-image workflow, model seed, negative prompt (`n_prompt`), and 4n+1 frame count. Requests use 1280x704 landscape generation for a 16:9 selected keyframe, then normalize to 1280x720 H.264/yuv420p at the storyboard FPS. The requested duration is approximated by the closest supported frame count and the actual duration is recorded.

## Commands

```powershell
vid-forge video project=tibeb --dry-run
vid-forge video project=tibeb --shot shot_02 --dry-run
vid-forge video project=tibeb --shot shot_02
vid-forge video project=tibeb --provider wan22
vid-forge video project=tibeb --shot shot_02 --force
vid-forge video project=tibeb --validate-only
vid-forge video project=tibeb --accelerator NvidiaTeslaA100
vid-forge video project=tibeb --shot shot_02 --keyframes-dir projects/tibeb/production/keyframes
vid-forge video project=tibeb --shot shot_02 --steps 50 --guidance 5
```

`--dry-run` reports selected images, fingerprints, cache state, local renders, Kaggle jobs, model, and accelerator. It does not upload data or submit a kernel. `--validate-only` checks the cache metadata, checksum, video stream, and full decode; it does not contact Kaggle. `--force` stores a new immutable generation and preserves previous artifacts.

Ken Burns shots use the storyboard's `motion.camera` (`zoom_in`, `zoom_out`, `pan_left`, `pan_right`) and never consume GPU inference. `i2v` shots use the chosen provider. HunyuanVideo 1.5 is represented by a provider contract, but its worker is not implemented; choosing it for a pending i2v shot returns a clear error rather than pretending to generate clips.

## Cache and recovery

The canonical SHA-256 request key covers the selected image bytes, prompts, provider/model and implementation revisions, seed, dimensions, FPS, duration/frame count, sampling settings, motion parameters, and preprocessing version. Kaggle IDs, paths, timestamps, and retry data do not affect it. Cache entries are immutable under `projects/<name>/cache/videos/<prefix>/<fingerprint>/`; the selected current clip is copied atomically to `outputs/shots/shot_NN.mp4`.

Every cached MP4 must have matching metadata and checksum, a readable video stream, positive duration/dimensions/FPS, and pass a complete FFmpeg decode. Invalid entries are retained for diagnosis and regenerated. `production/video_manifest.json` stores attempts, statuses, checksums, validation, and Kaggle run details; per-attempt events also append to `runs.jsonl`. Successful worker outputs are kept if another shot in the batch fails, so a rerun submits only missing cache keys. Local per-key locks and a project Kaggle lock prevent duplicate inference and races around Kaggle's versioned output download.

## First Kaggle GPU test

1. Install the project and FFmpeg, configure `.env`, and confirm the Kaggle account can run a private GPU notebook.
2. For the project below, ensure `shot_02` has a selected image and successful candidate record in `production/keyframes.json`. If it does not, run `vid-forge keyframes project=tibeb --shot shot_02`, then select a candidate in the storyboard. Add `--candidate-count 5` to request five candidates; that updates the shot's storyboard count.
3. Check the exact plan locally: `vid-forge video project=tibeb --shot shot_02 --dry-run`.
4. Submit one real shot using a GPU with the documented memory: `vid-forge video project=tibeb --shot shot_02 --provider wan22 --accelerator NvidiaTeslaA100`.
5. Watch the Kaggle kernel output/status in the terminal. Check `projects/tibeb/production/video_manifest.json`, `projects/tibeb/runs.jsonl`, and `projects/tibeb/outputs/shots/shot_02.mp4`. The initial run includes the model download. Later batches reuse cached shot videos locally, but a new GPU run may download model weights again.

If authentication fails, check the Kaggle username slug and API token in `.env`, then run `kaggle --version` and `kaggle datasets list --page-size 1`. If GPU initialization or OOM fails, use an available accelerator with at least 24 GB of VRAM; the worker does not switch models or GPUs automatically. If the kernel fails after a partial batch, rerun the same command after correcting the cause: completed clips remain cached. Kaggle output retrieval is version-qualified, and ambiguous latest-run downloads are rejected.

## Current limits

- The current development environment has no Kaggle CLI, FFmpeg, or ffprobe installed, so a real Kaggle run and local media decode cannot be validated here. The first external GPU test is still required.
- Wan weights are fetched inside the worker at the pinned Hugging Face revision. Kaggle internet, execution quota, and disk limits can block setup.
- Hunyuan is not an active provider until its official image-to-video worker is implemented and validated.
- This stage generates landscape per-shot clips (1280x720); final assembly also creates a portrait 9:16 rendition (720x1280).
