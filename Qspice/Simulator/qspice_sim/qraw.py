"""Reader for QSPICE binary .qraw transient results (header + float64 rows, one column per saved variable)."""
from __future__ import annotations

from pathlib import Path

import numpy as np


class QRaw:
    def __init__(self, path: Path):
        self.path = Path(path)
        with self.path.open("rb") as fh:
            head = fh.read(1 << 16)
        split = head.find(b"Binary:")
        if split < 0:
            raise ValueError("not a binary .qraw file (QSPICE must be run with -binary)")
        header = head[:split].decode("latin1").splitlines()
        flags = next((h.split(":", 1)[1].strip().lower() for h in header if h.startswith("Flags")), "real")
        if "complex" in flags:
            raise ValueError("only transient (real) results are supported")
        nvar = int(next(h for h in header if h.startswith("No. Variables")).split(":")[1])
        npts = int(next(h for h in header if h.startswith("No. Points")).split(":")[1])
        start = next(i for i, h in enumerate(header) if h.startswith("Variables"))
        self.names = [header[start + 1 + k].split("\t")[2] for k in range(nvar)]
        self._index = {n.lower(): k for k, n in enumerate(self.names)}
        offset = split + len(b"Binary:") + 1
        usable = (self.path.stat().st_size - offset) // (8 * nvar)
        self.n_points = max(0, min(npts, usable))  # tolerate a run that was cancelled mid-write
        self._data = (np.memmap(self.path, dtype="<f8", mode="r", offset=offset, shape=(self.n_points, nvar))
                      if self.n_points else None)
        self._time: np.ndarray | None = None

    @property
    def time(self) -> np.ndarray:
        if self._time is None:
            self._time = np.array(self._data[:, 0]) if self._data is not None else np.zeros(0)
        return self._time

    def has(self, node: str) -> bool:
        return f"v({node.lower()})" in self._index

    def column(self, node: str) -> np.ndarray:
        """Contiguous copy of V(node)."""
        return np.array(self._data[:, self._index[f"v({node.lower()})"]])

    def nodes(self) -> list[str]:
        return [n[2:-1].lower() for n in self.names if n.lower().startswith("v(") and "," not in n]

    def close(self) -> None:
        self._data = None
        self._time = None
