"""Module execution entrypoint for `python -m vid_forge`."""

from __future__ import annotations

import sys

from vid_forge.cli import main

if __name__ == "__main__":
    sys.exit(main())
