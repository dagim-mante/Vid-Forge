# Video assembly stage

This stage renders one MP4 per storyboard shot under `projects/<name>/outputs/shots/`. It does not combine clips into a film.

## Setup

1. Install the project in the repository environment: `python -m pip install -e .` (this installs the official Kaggle CLI and project dependencies).
2. Install FFmpeg with `ffmpeg` and `ffprobe` on `PATH`. FFmpeg must include `libx264`.
3. In Kaggle, create an API token and add `KAGGLE_API_TOKEN`, `KAGGLE_USERNAME` (profile slug), and optionally `KAGGLE_ACCELERATOR` to the repository-root `.env`. The token is read by the Kaggle CLI process and is never written into the uploaded input bundle, worker source, manifest, or run log. Keep `.env` private.
4. The Wan worker uses Diffusers sequential CPU offload and VAE tiling to reduce GPU memory, including for a single T4. It is slower and depends on Kaggle's available system RAM. The default accelerator is `NvidiaTeslaA100`; availability varies by account and quota. The worker reports the actual GPU before loading weights. A different accelerator can be passed with `--accelerator`.
5. Ensure each shot has `keyframe.selected` set in `production/storyboard.json` and the matching candidate is marked successful in `production/keyframes.json`. The selected image is resolved from that manifest, never by globbing for a shot filename.

The first GPU run creates or updates a private Kaggle Dataset with the selected images and full requests, then creates or updates a private GPU Kernel. The worker installs Diffusers dependencies and loads the pinned `Wan-AI/Wan2.2-TI2V-5B-Diffusers` model revision into Kaggle's temporary scratch cache (`/kaggle/tmp/huggingface-cache`). It initializes one pipeline with sequential CPU offload and VAE tiling, then generates each cache-missing shot in the submitted batch. It uses only GPU 0; the two T4 devices are not combined. Model weights are not uploaded with the job bundle or saved as notebook output. The first run can take a while while dependencies and the model download. Model download and inference require Kaggle internet access and enough system RAM. Kaggle removes scratch files between kernel runs, so batching shares the model download/load across shots in that run.

The worker installs Diffusers, Transformers, Accelerate, SentencePiece, and video export dependencies before importing PyTorch. It sets `CUDA_VISIBLE_DEVICES=0`, `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`, and disables unused TensorFlow/Flax backends before importing the inference stack. Sequential CPU offload moves model components between CPU and GPU as needed; VAE tiling limits decode memory. These settings reduce GPU memory use but make generation slower and still require enough host RAM. The pipeline is imported before model weights are fetched so dependency errors fail during setup.

The Wan path uses the Diffusers TI2V image input, prompt, negative prompt, CPU seed, inference steps, and 4n+1 frame count. It defaults to 20 inference steps to reduce T4 generation time; pass `--steps` to trade speed for quality. Requests generate at 1280x704 for a 16:9 keyframe, then normalize to 1280x720 H.264/yuv420p at the storyboard FPS. The requested duration is approximated by the closest supported frame count and the actual duration is recorded.

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
vid-forge video project=tibeb --shot shot_02 --steps 20 --guidance 5
```

`--dry-run` reports selected images, fingerprints, cache state, local renders, Kaggle jobs, model, and accelerator. It does not upload data or submit a kernel. `--validate-only` checks the cache metadata, checksum, video stream, and full decode; it does not contact Kaggle. `--force` stores a new immutable generation and preserves previous artifacts.

Ken Burns shots use the storyboard's `motion.camera` (`zoom_in`, `zoom_out`, `pan_left`, `pan_right`) and never consume GPU inference. Each move is a slow 12% push-in, pull-out, or sideways pan spread over the whole shot (longer shots move more slowly), with light easing so it neither lurches at the start nor stops dead at the cut. Frames are rendered with FFmpeg's sub-pixel `perspective` filter rather than `zoompan`, which snaps to whole pixels and makes slow moves look shaky. Travel, easing, and quality are tunable via `KEN_BURNS_ZOOM`, `KEN_BURNS_EASE`, and `KEN_BURNS_SUPERSAMPLE` in `video_assemble.py`. `i2v` shots use the chosen provider. HunyuanVideo 1.5 is represented by a provider contract, but its worker is not implemented; choosing it for a pending i2v shot returns a clear error rather than pretending to generate clips.

## Cache and recovery

The canonical SHA-256 request key covers the selected image bytes, prompts, provider/model and implementation revisions, seed, dimensions, FPS, duration/frame count, sampling settings, motion parameters, and preprocessing version. Kaggle IDs, paths, timestamps, and retry data do not affect it. Cache entries are immutable under `projects/<name>/cache/videos/<prefix>/<fingerprint>/`; the selected current clip is copied atomically to `outputs/shots/shot_NN.mp4`.

Every cached MP4 must have matching metadata and checksum, a readable video stream, positive duration/dimensions/FPS, and pass a complete FFmpeg decode. Invalid entries are retained for diagnosis and regenerated. `production/video_manifest.json` stores attempts, statuses, checksums, validation, and Kaggle run details; per-attempt events also append to `runs.jsonl`. Successful worker outputs are kept if another shot in the batch fails, so a rerun submits only missing cache keys. Local per-key locks and a project Kaggle lock prevent duplicate inference and races around Kaggle's versioned output download.

## First Kaggle GPU test

1. Install the project and FFmpeg, configure `.env`, and confirm the Kaggle account can run a private GPU notebook.
2. For the project below, ensure `shot_02` has a selected image and successful candidate record in `production/keyframes.json`. If it does not, run `vid-forge keyframes project=tibeb --shot shot_02`, then select a candidate in the storyboard. Add `--candidate-count 5` to request five candidates; that updates the shot's storyboard count.
3. Check the exact plan locally: `vid-forge video project=tibeb --shot shot_02 --dry-run`.
4. Submit one real shot using a GPU with the documented memory: `vid-forge video project=tibeb --shot shot_02 --provider wan22 --accelerator NvidiaTeslaA100`.
5. Watch the Kaggle kernel output/status in the terminal. Check `projects/tibeb/production/video_manifest.json`, `projects/tibeb/runs.jsonl`, and `projects/tibeb/outputs/shots/shot_02.mp4`. The initial run includes the model download. Later batches reuse cached shot videos locally, but a new GPU run may download model weights again.

If authentication fails, check the Kaggle username slug and API token in `.env`, then run `kaggle --version` and `kaggle datasets list --page-size 1`. If the worker runs out of GPU memory, sequential offload and VAE tiling are already enabled; use an accelerator with more GPU memory or sufficient Kaggle system RAM. The second T4 is not used by this worker. If the kernel fails after a partial batch, rerun the same command after correcting the cause: completed clips remain cached. Kaggle output retrieval is version-qualified, and ambiguous latest-run downloads are rejected.

## Current limits

- The current development environment has no Kaggle CLI, FFmpeg, or ffprobe installed, so a real Kaggle run and local media decode cannot be validated here. The first external GPU test is still required.
- Wan weights are fetched inside the worker at the pinned Hugging Face revision. Kaggle internet, execution quota, and disk limits can block setup.
- Hunyuan is not an active provider until its official image-to-video worker is implemented and validated.
- This stage generates landscape per-shot clips (1280x720); final assembly also creates a portrait 9:16 rendition (720x1280).
