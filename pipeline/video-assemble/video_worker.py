"""Kaggle worker: runs official Wan 2.2 TI2V-5B code over a submitted batch."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import traceback
import zipfile

INPUT = Path("/kaggle/input")
WORK = Path("/kaggle/working")
SCRATCH = Path("/kaggle/tmp")
REPO = Path("/kaggle/working/Wan2.2")


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


def _install_wan_requirements(requirements: Path) -> None:
    lines = requirements.read_text(encoding="utf-8").splitlines()
    regular: list[str] = []
    flash_attention: list[str] = []
    for line in lines:
        requirement = line.strip()
        if not requirement or requirement.startswith("#"):
            continue
        if requirement.casefold().split("[", 1)[0].split("=", 1)[0].strip() in {"flash_attn", "flash-attn"}:
            flash_attention.append(line)
        else:
            regular.append(line)

    regular_file = WORK / "wan-requirements-without-flash-attn.txt"
    regular_file.write_text("\n".join(regular) + "\n", encoding="utf-8")
    if regular:
        _pip_install("-r", str(regular_file))
    if flash_attention:
        # The pinned Wan attention module falls back to PyTorch SDPA when FA2/FA3
        # is unavailable. Building flash-attn from source can exceed Kaggle's RAM.
        print("Skipping optional flash_attn build; Wan will use PyTorch SDPA.", flush=True)


def main() -> int:
    try:
        import torch
        from PIL import Image
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable; check that the Kaggle kernel has a GPU accelerator.")
        props = torch.cuda.get_device_properties(0)
        gpu = {"name": props.name, "total_memory_bytes": int(props.total_memory),
               "free_memory_bytes": int(torch.cuda.mem_get_info(0)[0])}
        print("GPU:", json.dumps(gpu))

        bundle = _find_jobs_manifest()
        data = json.loads(bundle.read_text(encoding="utf-8"))
        jobs = data["jobs"]
        WORK.mkdir(parents=True, exist_ok=True)
        SCRATCH.mkdir(parents=True, exist_ok=True)
        if not REPO.is_dir():
            subprocess.run(["git", "clone", "https://github.com/Wan-Video/Wan2.2.git", str(REPO)], check=True)
        revision = str(data.get("code_revision", "1ea34ff"))
        subprocess.run(["git", "-C", str(REPO), "checkout", revision], check=True)
        _install_wan_requirements(REPO / "requirements.txt")
        _pip_install("huggingface_hub")
        from huggingface_hub import snapshot_download
        sys.path.insert(0, str(REPO))
        checkpoint_dir = SCRATCH / "Wan2.2-TI2V-5B"
        print(f"Downloading model checkpoint to scratch storage: {checkpoint_dir}", flush=True)
        checkpoint = snapshot_download(
            repo_id=data.get("model", "Wan-AI/Wan2.2-TI2V-5B"),
            revision=data.get("model_revision") or None,
            local_dir=str(checkpoint_dir),
        )
        import wan
        from wan.configs import WAN_CONFIGS
        from wan.utils.utils import save_video
        config = WAN_CONFIGS["ti2v-5B"]
        # Official Wan code uses T5 on CPU and low-memory model conversion for a single GPU.
        pipeline = wan.WanTI2V(config=config, checkpoint_dir=checkpoint, device_id=0, rank=0,
                               t5_fsdp=False, dit_fsdp=False, use_sp=False, t5_cpu=True,
                               convert_model_dtype=True)
        results: list[dict[str, object]] = []
        for job in jobs:
            shot_id = str(job["shot_id"])
            output = WORK / "results" / f"{shot_id}.mp4"
            output.parent.mkdir(parents=True, exist_ok=True)
            try:
                image = Image.open(bundle.parent / job["image"]).convert("RGB")
                video = pipeline.generate(
                    str(job["prompt"]), img=image, size=tuple(job["wan_size"]),
                    max_area=int(job["wan_max_area"]), frame_num=int(job["frame_count"]),
                    shift=float(job["sampling_shift"]), sample_solver=str(job["sampling_solver"]), sampling_steps=int(job["steps"]),
                    guide_scale=float(job["guidance"]), n_prompt=str(job["negative_prompt"]),
                    seed=int(job["seed"]), offload_model=True,
                )
                save_video(tensor=video[None], save_file=str(output), fps=config.sample_fps,
                           nrow=1, normalize=True, value_range=(-1, 1))
                del video
                digest = hashlib.sha256(output.read_bytes()).hexdigest()
                results.append({"shot_id": shot_id, "status": "completed", "output": output.name,
                                "sha256": digest, "gpu": gpu})
            except Exception as exc:  # preserve earlier completed outputs
                results.append({"shot_id": shot_id, "status": "failed", "error": str(exc)[:1000],
                                "traceback": traceback.format_exc()[-3000:], "gpu": gpu})
                print(f"Generation failed for {shot_id}: {exc}", file=sys.stderr)
        (WORK / "results" / "results.json").write_text(json.dumps({"jobs": results, "gpu": gpu}, indent=2), encoding="utf-8")
        # A partial shot failure must still publish successful MP4s and results.json for recovery.
        return 0
    except Exception as exc:
        (WORK / "worker_error.json").write_text(json.dumps({"status": "failed", "error": str(exc),
                                "traceback": traceback.format_exc()[-4000:]}, indent=2), encoding="utf-8")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
