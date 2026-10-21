# Qspice

QSPICE circuit models of the hardware in this repository and a GUI to run them.

| Folder | Content |
| --- | --- |
| [TestBoard_ADC124/](TestBoard_ADC124/README.md) | Model of the TestBoard_ADC124 front end (7 PZT sensors, TMUX1308A, OPA365): netlist generator, command-line scenarios, reports. |
| [Simulator/](Simulator/README.md) | GUI that loads any model folder with a `simulator_model.json`, edits component / system / simulation parameters, plots chosen nodes, saves images and compares runs. |

Start the GUI with `python Qspice/Simulator/run_simulator.py`. QSPICE (`QSPICE64.exe`) must be installed; the models are simulated, not measured.
