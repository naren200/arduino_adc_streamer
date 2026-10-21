"""Start the Qspice Simulator GUI:   python Qspice/Simulator/run_simulator.py [--model TestBoard_ADC124]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qspice_sim.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
