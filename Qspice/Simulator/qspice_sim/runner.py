"""Run QSPICE on a netlist (headless) and report progress; also build/estimate a run from a model + configuration."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import netlist_tools
from .model_loader import Model
from .qraw import QRaw

DEFAULT_QSPICE_PATHS = [r"C:\Program Files\QSPICE\QSPICE64.exe", r"C:\Program Files (x86)\QSPICE\QSPICE64.exe"]
# Measured on this model: ~60 000 time points per second of QSPICE time.
POINTS_PER_SECOND = 60_000.0


class SimulationError(Exception):
    pass


class Cancelled(SimulationError):
    pass


def find_qspice(explicit: str | None = None) -> Path:
    candidates = [explicit, os.environ.get("QSPICE_EXE"), shutil.which("QSPICE64"), *DEFAULT_QSPICE_PATHS]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    raise SimulationError("QSPICE64.exe not found. Set the QSPICE_EXE environment variable or choose it in the Model tab.")


@dataclass
class Prepared:
    netlist: str
    t_stop: float
    samples: list[dict]
    warnings: list[str]
    nodes_in_netlist: list[str]
    max_step: float | None

    @property
    def est_points(self) -> float:
        """Rough number of time points: simulated time / maximum step (the solver also adds breakpoints)."""
        if self.max_step:
            return self.t_stop / self.max_step
        return float("nan")

    def estimate(self, n_saved: int) -> dict:
        pts = self.est_points
        return {"points": pts, "seconds": pts / POINTS_PER_SECOND if pts == pts else float("nan"),
                "bytes": pts * 8 * (1 + max(1, n_saved)) if pts == pts else float("nan")}


def prepare(model: Model, cfg: dict) -> Prepared:
    built = model.build(cfg)
    text = built["netlist"]
    t_stop, t_max = netlist_tools.parse_tran(text)
    return Prepared(text, float(built["t_stop"]) if t_stop is None else t_stop, built["samples"], built["warnings"],
                    netlist_tools.discover_nodes(text), t_max)


def run_netlist(netlist: str, nodes: list[str], workdir: Path, qspice: Path,
                progress: Callable[[float, int], None] | None = None,
                cancel: threading.Event | None = None) -> QRaw:
    """Write the netlist with a .save of the requested nodes, run QSPICE, return the parsed result."""
    workdir.mkdir(parents=True, exist_ok=True)
    cir = workdir / "run.cir"
    for stale in workdir.glob("run.*"):
        try:
            stale.unlink()
        except OSError:
            pass
    cir.write_text(netlist_tools.set_save(netlist, nodes), encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    t0 = time.time()
    proc = subprocess.Popen([str(qspice), str(cir), "-binary", "-o", str(cir.with_suffix(".out"))],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(workdir), creationflags=flags)
    raw = cir.with_suffix(".qraw")
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            proc.kill()
            proc.wait()
            raise Cancelled("simulation cancelled")
        if progress:
            progress(time.time() - t0, raw.stat().st_size if raw.exists() else 0)
        time.sleep(0.25)
    output = proc.stdout.read() if proc.stdout else ""
    if not raw.exists():
        raise SimulationError("QSPICE produced no result file.\n" + output.strip()[-1500:])
    if progress:
        progress(time.time() - t0, raw.stat().st_size)
    try:
        return QRaw(raw)
    except ValueError as exc:
        raise SimulationError(f"{exc}\n{output.strip()[-1500:]}") from exc
