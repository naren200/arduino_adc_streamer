"""Force tab: parameters of the force calculation, the formulas with live numbers and the per-sensor result table."""
from __future__ import annotations

from typing import Any, Callable

from PyQt6 import QtCore, QtWidgets

from ..config_utils import format_number
from ..force_calc import REF_FIRST, REF_VMID, SRC_ADC_OUT, SRC_IDEAL
from .param_form import ParamForm

TABLE_HEADERS = ["Channel", "Conversions", "Clipped", "dF min [N]", "dF max [N]", "F min [N]", "F max [N]", "F final [N]",
                 "Fin final [N]", "F - Fin final [N]"]

SCHEMA = [
    {"group": "Sensor values (empty = follow the component settings of each sensor)", "items": [
        {"path": "force.d33_pC_per_N", "label": "d33 charge constant", "unit": "pC/N", "type": "float", "nullable": True,
         "tip": "Charge per newton used for the calculation only. Empty = the d33 (times the force gain) of the sensor component."},
        {"path": "force.cp_pF", "label": "Sensor capacitance", "unit": "pF", "type": "float", "nullable": True,
         "tip": "Capacitance used for the calculation only. Empty = the capacitance of the sensor component (override or calculated)."},
        {"path": "force.cextra_pF", "label": "Extra node capacitance", "unit": "pF", "type": "float", "nullable": True,
         "tip": "Capacitance the node adds in parallel to the sensor (C = sensor + extra). Empty = the model's value (0)."},
    ]},
    {"group": "Extraction", "items": [
        {"path": "force.gain", "label": "Front-end gain G", "unit": "", "type": "float",
         "tip": "Gain between the sensor voltage and the ADC input (1 = unity-gain buffer)."},
        {"path": "force.reference", "label": "Reference for the first sample", "unit": "", "type": "choice",
         "choices": [REF_VMID, REF_FIRST], "tip": "V[-1] of the first conversion of a sensor: VMID, or its own first sample (dF[0] = 0)."},
        {"path": "force.source", "label": "Voltage source", "unit": "", "type": "choice", "choices": [SRC_ADC_OUT, SRC_IDEAL],
         "tip": "ADC-Out includes the quantisation and the white noise; the ideal ADC input is the simulated value at the conversion."},
        {"path": "force.channels", "label": "Sensors to extract", "unit": "", "type": "intlist", "nullable": True,
         "tip": "Comma-separated channels. Empty = every channel that has conversions in the run."},
        {"path": "force.min_fin_N", "label": "Minimum |Fin| for the ratio", "unit": "N", "type": "float", "nullable": True,
         "tip": "Fcal / Fin is left out (undefined) where the applied force |Fin| is below this value, instead of exploding. "
                "Empty = 1 % of the largest |Fin| of the channel in the run. An exact zero is always left out."},
    ]},
]

class ForceTab(QtWidgets.QSplitter):
    """Parameter form on top, formulas and the result table below."""

    changed = QtCore.pyqtSignal(str)
    export_requested = QtCore.pyqtSignal()

    def __init__(self, get: Callable[[str], Any], set_: Callable[[str, Any], None], default: Callable[[str], Any],
                 hints: Callable[[], dict[str, str]]):
        super().__init__(QtCore.Qt.Orientation.Vertical)
        self.form = ParamForm(SCHEMA, get, set_, default, hints=hints)
        self.form.changed.connect(self.changed)
        self.addWidget(self.form)
        bottom = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(bottom)
        lay.setContentsMargins(4, 0, 4, 4)
        self.formulas = QtWidgets.QTextBrowser()
        self.formulas.setMinimumHeight(150)
        lay.addWidget(self.formulas, 2)
        self.note = QtWidgets.QLabel("")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        self.table = QtWidgets.QTableWidget(0, len(TABLE_HEADERS))
        self.table.setHorizontalHeaderLabels(TABLE_HEADERS)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self.table, 3)
        self.export_btn = QtWidgets.QPushButton("Export per-sample table…")
        self.export_btn.setToolTip("CSV with t, address, V, dF, F and the applied force Fin for every conversion of the extracted sensors")
        self.export_btn.clicked.connect(self.export_requested)
        lay.addWidget(self.export_btn, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        self.addWidget(bottom)
        self.setStretchFactor(0, 0)
        self.setStretchFactor(1, 1)
        self.setSizes([370, 500])

    def refresh(self) -> None:
        self.form.refresh()

    def show_results(self, formulas_html: str, rows: list[list], note: str = "") -> None:
        self.formulas.setHtml(formulas_html)
        self.note.setText(note)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                if value is None:
                    text = "n/a"
                elif isinstance(value, int):
                    text = str(value)
                else:
                    text = format_number(float(f"{value:.4g}"))
                item = QtWidgets.QTableWidgetItem(text)
                item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, c, item)
        self.export_btn.setEnabled(bool(rows))
