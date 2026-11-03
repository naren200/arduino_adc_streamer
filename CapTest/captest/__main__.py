"""Entry point: `python -m captest`, or `uv run captest` / `python captest` from this folder."""

import sys

if not __package__:
    # Started as a directory/script (`python captest`, `uv run captest`): there is no
    # parent package, so make the project folder importable and use absolute imports.
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from captest.cli import main
else:
    from .cli import main

sys.exit(main())
