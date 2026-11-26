"""Model provider contracts and generation request normalization."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VideoRequest:
    shot_id: str
    image: str
    prompt: str
    negative_prompt: str
    seed: int
    duration_s: float
    fps: int
    width: int
    height: int
    frame_count: int
    steps: int
    guidance: float
    sampling_solver: str
    sampling_shift: float
    provider: str
    model: str
    model_revision: str
    implementation_revision: str
    image_sha256: str
    pipeline_version: str = "1.0"
    preprocessing_version: str = "rgb-no-resize-v1"
    motion_parameters: dict[str, Any] | None = None

    def fingerprint_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": 1, "pipeline_version": self.pipeline_version,
            "provider": self.provider, "model": self.model,
            "model_revision": self.model_revision, "implementation_revision": self.implementation_revision,
            "image_sha256": self.image_sha256,
            "prompt": self.prompt, "negative_prompt": self.negative_prompt,
            "seed": self.seed, "width": self.width, "height": self.height,
            "fps": self.fps, "requested_duration_s": round(self.duration_s, 6),
            "frame_count": self.frame_count, "steps": self.steps,
            "guidance": self.guidance, "sampling_solver": self.sampling_solver,
            "sampling_shift": self.sampling_shift, "motion_parameters": self.motion_parameters or {},
            "preprocessing_version": self.preprocessing_version,
        }
        if self.provider == "ffmpeg-kenburns":
            for unused in ("negative_prompt", "seed", "steps", "guidance", "sampling_solver", "sampling_shift"):
                payload.pop(unused, None)
        return payload


class VideoProvider:
    provider_id = "abstract"
    model_id = "abstract"
    model_revision = "unversioned"
    supported_parameters: tuple[str, ...] = ()
    required_input_format = "RGB image"
    resolution_constraints = ""
    frame_constraints = ""
    vram_requirements = ""
    supports_negative_prompt = False
    implemented = False

    def validate(self, request: VideoRequest) -> None:
        if not self.implemented:
            raise NotImplementedError(f"Provider {self.provider_id!r} is not implemented.")


class Wan22Provider(VideoProvider):
    provider_id = "wan22"
    model_id = "Wan-AI/Wan2.2-TI2V-5B-Diffusers"
    model_revision = "4c6ca6c2ded5c79550a3ca25555efc561112891a"
    implementation_revision = "diffusers-adaptive-offload-v3"
    supported_parameters = (
        "image", "prompt", "negative_prompt", "generator", "height", "width",
        "num_frames", "num_inference_steps", "guidance_scale",
    )
    required_input_format = "RGB image file"
    resolution_constraints = "Official TI2V sizes; 1280x704 for landscape 720p, preserving input aspect ratio"
    frame_constraints = "4n+1 frames"
    vram_requirements = "Uses Diffusers sequential CPU offload and VAE tiling to reduce GPU memory; Kaggle T4 runs still depend on available system RAM"
    supports_negative_prompt = True
    implemented = True

    def validate(self, request: VideoRequest) -> None:
        if request.frame_count < 5 or (request.frame_count - 1) % 4:
            raise ValueError("Wan TI2V frame count must be 4n+1 and at least 5.")


class HunyuanVideo15Provider(VideoProvider):
    provider_id = "hunyuan15"
    model_id = "Tencent-Hunyuan/HunyuanVideo-1.5-480P-I2V"
    model_revision = "main"
    implementation_revision = "main"
    supported_parameters = ("image_path", "prompt", "negative_prompt", "seed", "video_length", "resolution", "aspect_ratio")
    required_input_format = "Image file accepted by official HunyuanVideo-1.5 I2V workflow"
    resolution_constraints = "Official 480p or 720p presets"
    frame_constraints = "Official --video_length; model default is 121 frames"
    vram_requirements = "Not validated for Kaggle in this implementation"
    # Deliberately unavailable until the official worker path is validated.
    implemented = False
