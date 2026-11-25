"""Content-addressed immutable video cache with strict artifact validation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any


class FileLock:
    """Small blocking OS file lock using msvcrt on Windows and flock on POSIX."""
    def __init__(self, lock_file: str, timeout: float = -1):
        self.lock_file = lock_file
        self.timeout = timeout
        self._handle: Any = None

    def __enter__(self) -> "FileLock":
        Path(self.lock_file).parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.lock_file, "a+b")
        if sys.platform == "win32":
            import msvcrt
            self._handle.seek(0, os.SEEK_END)
            if self._handle.tell() == 0:
                self._handle.write(b"\0")
                self._handle.flush()
            self._handle.seek(0)
            started = time.monotonic()
            while True:
                try:
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if self.timeout >= 0 and time.monotonic() - started >= self.timeout:
                        self._handle.close()
                        self._handle = None
                        raise TimeoutError(f"Timed out waiting for lock: {self.lock_file}")
                    time.sleep(0.1)
        else:
            import fcntl
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_: object) -> None:
        if self._handle is None:
            return
        if sys.platform == "win32":
            import msvcrt
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def probe_video(path: Path, ffprobe: str = "ffprobe", ffmpeg: str = "ffmpeg") -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size < 1024:
        raise ValueError("video file is missing, empty, or implausibly small")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise ValueError(f"ffprobe could not parse video: {result.stderr[-500:]}")
    try:
        data = json.loads(result.stdout)
        streams = [stream for stream in data["streams"] if stream.get("codec_type") == "video"]
        stream = streams[0]
        duration = float(stream.get("duration") or data["format"].get("duration"))
        width, height = int(stream["width"]), int(stream["height"])
        rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
        numerator, denominator = (int(part) for part in rate.split("/"))
        fps = numerator / denominator
        codec = stream.get("codec_name")
        pixel_format = stream.get("pix_fmt")
        if duration <= 0 or width <= 0 or height <= 0 or fps <= 0:
            raise ValueError("video duration, dimensions, or frame rate are invalid")
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"invalid video stream metadata: {exc}") from exc
    # Full decode catches truncated files that still have a readable header.
    decoded = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True, check=False)
    if decoded.returncode:
        raise ValueError(f"video decode failed: {decoded.stderr[-500:]}")
    return {"duration_s": duration, "width": width, "height": height, "fps": fps,
            "codec": codec, "pixel_format": pixel_format, "file_size": path.stat().st_size,
            "decodable": True, "video_stream": True}


class VideoCache:
    def __init__(self, project_dir: Path):
        self.root = project_dir / "cache" / "videos"

    def entry_dir(self, key: str, generation_id: str = "default") -> Path:
        return self.root / key[:2] / key / generation_id

    def validate(self, key: str, *, ffprobe: str = "ffprobe", ffmpeg: str = "ffmpeg") -> tuple[bool, dict[str, Any] | None, str | None]:
        base = self.entry_dir(key).parent
        if not base.is_dir():
            return False, None, "cache entry is missing metadata or video"
        candidates = [self.entry_dir(key)] + sorted(
            (directory for directory in base.iterdir() if directory.is_dir() and directory.name != "default"),
            key=lambda directory: directory.name, reverse=True,
        )
        failures: list[str] = []
        valid: list[tuple[str, dict[str, Any]]] = []
        for directory in candidates:
            video, metadata, request = directory / "video.mp4", directory / "metadata.json", directory / "request.json"
            try:
                meta = json.loads(metadata.read_text(encoding="utf-8"))
                req = json.loads(request.read_text(encoding="utf-8"))
                if meta.get("fingerprint") != key or meta.get("status") != "completed" or fingerprint(req) != key:
                    raise ValueError("metadata/request fingerprint or completion status mismatch")
                if not video.is_file() or sha256_file(video) != meta.get("output_sha256"):
                    raise ValueError("video is missing or checksum does not match metadata")
                actual = probe_video(video, ffprobe, ffmpeg)
                recorded = meta.get("validation", {})
                if actual["width"] != recorded.get("width") or actual["height"] != recorded.get("height"):
                    raise ValueError("video dimensions do not match validated metadata")
                valid.append((str(meta.get("created_at", "")), {**meta, "request": req, "probe": actual, "cache_path": str(video)}))
            except FileNotFoundError:
                failures.append(f"{directory.name}: missing metadata or video")
            except (OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
                failures.append(f"{directory.name}: {exc}")
        if valid:
            valid.sort(key=lambda item: item[0])
            return True, valid[-1][1], None
        return False, None, "; ".join(failures) or "cache entry is invalid"

    def commit(self, key: str, request: dict[str, Any], source: Path, metadata: dict[str, Any], *, force: bool = False,
               ffprobe: str = "ffprobe", ffmpeg: str = "ffmpeg") -> tuple[Path, dict[str, Any]]:
        generation_id = f"forced-{uuid.uuid4().hex}" if force else "default"
        final_dir = self.entry_dir(key, generation_id)
        if final_dir.exists():
            generation_id = f"repair-{uuid.uuid4().hex}"
            final_dir = self.entry_dir(key, generation_id)
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        temp_dir = final_dir.with_name(final_dir.name + f".{uuid.uuid4().hex}.tmp")
        temp_dir.mkdir(parents=True)
        temp_video = temp_dir / "video.mp4"
        try:
            if not source.is_file() or source.stat().st_size < 1024:
                raise ValueError("provider returned a missing or empty video")
            shutil.copyfile(source, temp_video)
            validation = probe_video(temp_video, ffprobe, ffmpeg)
            checksum = sha256_file(temp_video)
            complete = {**metadata, "fingerprint": key, "status": "completed", "created_at": metadata.get("created_at") or datetime.now(timezone.utc).isoformat(),
                        "output_sha256": checksum, "validation": validation}
            write_json_atomic(temp_dir / "request.json", request)
            write_json_atomic(temp_dir / "metadata.json", complete)
            if final_dir.exists():
                raise FileExistsError(f"immutable cache generation already exists: {final_dir}")
            os.replace(temp_dir, final_dir)
            return final_dir / "video.mp4", complete
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    def lock(self, key: str) -> FileLock:
        lock_path = self.root / key[:2] / f"{key}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(str(lock_path), timeout=24 * 60 * 60)
