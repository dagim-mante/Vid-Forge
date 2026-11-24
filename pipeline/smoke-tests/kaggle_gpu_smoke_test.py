"""Kaggle notebook GPU availability smoke test.

Run this script inside a Kaggle notebook (with GPU accelerator enabled) to
verify CUDA availability and GPU VRAM, separately from local Kaggle API auth.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import torch


def main() -> None:
    """Check CUDA GPU availability and write the result JSON."""
    cuda_available = torch.cuda.is_available()
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": "kaggle",
        "operation": "gpu_availability",
        "success": cuda_available,
        "gpu": torch.cuda.get_device_name(0) if cuda_available else None,
        "vram_gb": (
            round(torch.cuda.get_device_properties(0).total_memory / 1024**3, 2)
            if cuda_available
            else None
        ),
    }

    kaggle_working = Path("/kaggle/working")
    if kaggle_working.exists():
        output_path = kaggle_working / "gpu-smoke-test.json"
    else:
        resolved = Path(__file__).resolve()
        root_dir = resolved.parents[2] if len(resolved.parents) > 2 else resolved.parent
        output_path = root_dir / "outputs" / "smoke" / "gpu-smoke-test.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
