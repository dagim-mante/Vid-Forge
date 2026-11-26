"""Kaggle worker: generate Vid-Forge shots with Diffusers Wan TI2V and CPU offload."""
from __future__ import annotations

import gc
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time
import traceback
import zipfile

INPUT = Path("/kaggle/input")
WORK = Path("/kaggle/working")
SCRATCH = Path("/kaggle/tmp")


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    destination_resolved = destination.resolve()
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            target = (destination / member.filename).resolve()
            if target != destination_resolved and destination_resolved not in target.parents:
                raise ValueError(f"Unsafe path in Kaggle input archive: {member.filename}")
        zipped.extractall(destination)


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    destination_resolved = destination.resolve()
    with tarfile.open(archive) as packed:
        for member in packed.getmembers():
            target = (destination / member.name).resolve()
            if target != destination_resolved and destination_resolved not in target.parents:
                raise ValueError(f"Unsafe path in Kaggle input archive: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"Links are not allowed in Kaggle input archive: {member.name}")
        packed.extractall(destination)


def _find_jobs_manifest() -> Path:
    manifests = list(INPUT.rglob("jobs.json"))
    if manifests:
        return manifests[0]
    extraction = WORK / "vidforge-job-input"
    extraction.mkdir(parents=True, exist_ok=True)
    archives = [*INPUT.rglob("*.zip"), *INPUT.rglob("*.tar"), *INPUT.rglob("*.tar.gz"), *INPUT.rglob("*.tgz")]
    for archive in archives:
        if archive.suffix == ".zip":
            _safe_extract_zip(archive, extraction)
        else:
            _safe_extract_tar(archive, extraction)
        manifests = list(extraction.rglob("jobs.json"))
        if manifests:
            return manifests[0]
    raise FileNotFoundError(
        "jobs/jobs.json was not found in Kaggle inputs or in their zip/tar archives. "
        "Expected a dataset containing jobs/jobs.json and jobs/images/."
    )


def _pip_install(*args: str) -> None:
    command = [sys.executable, "-m", "pip", "install", *args]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    if output:
        print(output, file=sys.stderr, flush=True)
    if result.returncode:
        tail = output[-12000:] if output else "(pip produced no output)"
        raise RuntimeError(
            f"Dependency installation failed (exit {result.returncode}): {' '.join(command)}\n{tail}"
        )


def _install_diffusers_requirements() -> None:
    _pip_install(
        "diffusers>=0.35.0",
        "transformers>=4.51.3,<5",
        "accelerate>=1.1.1",
        "ftfy",
        "imageio[ffmpeg]",
        "imageio-ffmpeg",
        "sentencepiece",
    )


def _load_pipeline(model_id: str, revision: str, cache_dir: Path, torch: object) -> object:
    """Load the official Diffusers model with low GPU residency for T4 GPUs."""
    from diffusers import AutoencoderKLWan, WanImageToVideoPipeline

    vae = AutoencoderKLWan.from_pretrained(
        model_id,
        subfolder="vae",
        revision=revision,
        cache_dir=str(cache_dir),
        torch_dtype=torch.float32,
    )
    pipeline = WanImageToVideoPipeline.from_pretrained(
        model_id,
        revision=revision,
        cache_dir=str(cache_dir),
        vae=vae,
        torch_dtype=torch.float16,
    )
    # Do not call pipeline.to("cuda") first: sequential offload keeps only the
    # component/layers needed for the current operation on the GPU.
    pipeline.enable_sequential_cpu_offload()
    pipeline.vae.enable_tiling()
    return pipeline


def _run_job(
    job: dict[str, object], bundle: Path, output: Path, pipeline: object,
    torch: object, Image: object, ImageOps: object, export_to_video: object,
    gpu: dict[str, object], model_id: str, load_s: float,
) -> dict[str, object]:
    shot_id = str(job["shot_id"])
    width, height = int(job["width"]), int(job["height"])
    with Image.open(bundle.parent / str(job["image"])) as source:
        image = ImageOps.fit(source.convert("RGB"), (width, height), method=Image.Resampling.LANCZOS)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    started = time.time()
    frames = pipeline(
        image=image,
        prompt=str(job["prompt"]),
        negative_prompt=str(job.get("negative_prompt", "")),
        height=height,
        width=width,
        num_frames=int(job["frame_count"]),
        num_inference_steps=int(job["steps"]),
        guidance_scale=float(job["guidance"]),
        generator=torch.Generator(device="cpu").manual_seed(int(job["seed"])),
        output_type="pil",
    ).frames[0]
    export_to_video(frames, str(output), fps=int(job.get("fps", 24)))
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError(f"Diffusers did not write a video for {shot_id}.")
    return {
        "shot_id": shot_id,
        "status": "completed",
        "output": output.name,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "gpu": gpu,
        "latency_s": round(time.time() - started, 1),
        "model_load_s": round(load_s, 1),
        "model": model_id,
    }


def main() -> int:
    try:
        bundle = _find_jobs_manifest()
        data = json.loads(bundle.read_text(encoding="utf-8"))
        jobs = data["jobs"]
        WORK.mkdir(parents=True, exist_ok=True)
        SCRATCH.mkdir(parents=True, exist_ok=True)

        # This worker intentionally uses one GPU. Set allocator/backend flags
        # before importing torch, Transformers, or Diffusers.
        os.environ["CUDA_VISIBLE_DEVICES"] = "0"
        os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
        os.environ["USE_TF"] = "0"
        os.environ["USE_FLAX"] = "0"
        _install_diffusers_requirements()

        import torch
        from PIL import Image, ImageOps
        from diffusers.utils import export_to_video

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable; check that the Kaggle kernel has a GPU accelerator.")
        props = torch.cuda.get_device_properties(0)
        gpu = {
            "name": props.name,
            "total_memory_bytes": int(props.total_memory),
            "free_memory_bytes": int(torch.cuda.mem_get_info(0)[0]),
        }
        print("Runtime:", json.dumps({
            "python": sys.version.split()[0],
            "torch": torch.__version__,
            "diffusers": __import__("diffusers").__version__,
            "offload": "sequential_cpu",
        }), flush=True)
        print("GPU:", json.dumps(gpu), flush=True)

        results: list[dict[str, object]] = []
        if jobs:
            model_id = str(data["model"])
            revision = str(data["model_revision"])
            cache_dir = SCRATCH / "huggingface-cache"
            print(f"Loading {model_id}@{revision} with sequential CPU offload.", flush=True)
            load_started = time.time()
            pipeline = _load_pipeline(model_id, revision, cache_dir, torch)
            load_s = time.time() - load_started
            print(f"Pipeline ready in {load_s:.0f}s; VAE tiling enabled.", flush=True)
            for job in jobs:
                shot_id = str(job["shot_id"])
                output = WORK / "results" / f"{shot_id}.mp4"
                try:
                    result = _run_job(job, bundle, output, pipeline, torch, Image, ImageOps,
                                      export_to_video, gpu, model_id, load_s)
                    print(f"{shot_id}: completed in {result['latency_s']}s", flush=True)
                except Exception as exc:  # preserve successful earlier outputs
                    result = {
                        "shot_id": shot_id,
                        "status": "failed",
                        "error": str(exc)[:1000],
                        "traceback": traceback.format_exc()[-3000:],
                        "gpu": gpu,
                    }
                    print(f"Generation failed for {shot_id}: {exc}", file=sys.stderr, flush=True)
                results.append(result)
                gc.collect()
                torch.cuda.empty_cache()

        results_dir = WORK / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        (results_dir / "results.json").write_text(
            json.dumps({"jobs": results, "gpu": gpu}, indent=2), encoding="utf-8"
        )
        # Per-shot failures stay in results.json so completed shots can be recovered.
        return 0
    except Exception as exc:
        WORK.mkdir(parents=True, exist_ok=True)
        (WORK / "worker_error.json").write_text(json.dumps({
            "status": "failed",
            "error": str(exc),
            "traceback": traceback.format_exc()[-4000:],
        }, indent=2), encoding="utf-8")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
