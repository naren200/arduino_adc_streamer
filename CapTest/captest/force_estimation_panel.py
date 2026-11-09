"""Reviewable, explicit estimated-route recovery dialog for CapTest."""
from PyQt6 import QtCore, QtWidgets

from . import force_estimates
from .force_export import route_quality, ForceExportError


class EstimateDialog(QtWidgets.QDialog):
    def __init__(self, inputs, group_id, parent=None):
        super().__init__(parent)
        self.inputs, self.group_id, self.proposed = inputs, group_id, None
        self.setWindowTitle("Estimate failed electrical route")
        self.resize(850, 600)
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.target = QtWidgets.QComboBox()
        rev = inputs.latest(group_id)
        for route in rev["routes"] if rev else []:
            issues = route_quality(route)
            if issues:
                self.target.addItem("%s ADC%d:%d — %s" %
                                    (route["position"], *route["ref"], "; ".join(issues)), tuple(route["ref"]))
        self.sensor_class = QtWidgets.QLineEdit()
        self.sensor_class.setPlaceholderText("Explicit sensor size and type shared by target and selected donors")
        self.wiring_class = QtWidgets.QLineEdit()
        self.wiring_class.setPlaceholderText("Compatible strip/flex construction")
        self.boundary = QtWidgets.QLineEdit()
        self.boundary.setPlaceholderText("Where the strips were disconnected for every matching blank")
        self.borrow = QtWidgets.QCheckBox("Experimental: borrow switching coefficients from the same ADC")
        for label, widget in (("Failed route", self.target), ("Sensor class", self.sensor_class),
                              ("Wiring class", self.wiring_class), ("Blank boundary", self.boundary),
                              ("", self.borrow)):
            form.addRow(label, widget)
        layout.addLayout(form)
        note = QtWidgets.QLabel("Select only measured healthy donors of the declared same size/type and wiring. "
            "The estimate is median(loaded − own board blank) + target board blank. It includes strip/flex "
            "capacitance. Normal recovery refits the target's own raw transitions; no original evidence is replaced.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.donors = QtWidgets.QTableWidget(0, 6)
        self.donors.setHorizontalHeaderLabels(["Use", "Group / position", "ADC:input", "Loaded pF", "Blank pF", "Added pF"])
        for gid in inputs.group_ids:
            rev = inputs.latest(gid)
            for route in rev["routes"] if rev else []:
                if route_quality(route) or route.get("provenance", "measured") != "measured":
                    continue
                blank = route.get("baseline", {}).get("c_board_pf")
                row = self.donors.rowCount()
                self.donors.insertRow(row)
                item = QtWidgets.QTableWidgetItem()
                item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(QtCore.Qt.CheckState.Unchecked)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, (gid, tuple(route["ref"])))
                self.donors.setItem(row, 0, item)
                values = [gid + " / " + route["position"], "%d:%d" % tuple(route["ref"]),
                          "%.3f" % (route["c_total_f"] * 1e12), str(blank) if blank is not None else "missing",
                          "%.3f" % (route["c_total_f"] * 1e12 - blank) if blank is not None else "unavailable"]
                for col, value in enumerate(values, 1):
                    item = QtWidgets.QTableWidgetItem(value)
                    item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
                    self.donors.setItem(row, col, item)
        layout.addWidget(self.donors)
        self.preview_label = QtWidgets.QLabel("")
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)
        buttons = QtWidgets.QHBoxLayout()
        preview = QtWidgets.QPushButton("Calculate preview")
        self.apply = QtWidgets.QPushButton("Use estimate (provisional)")
        self.apply.setEnabled(False)
        cancel = QtWidgets.QPushButton("Cancel")
        preview.clicked.connect(self.preview)
        self.apply.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        for b in (preview, self.apply, cancel):
            buttons.addWidget(b)
        layout.addLayout(buttons)
        self.donors.itemChanged.connect(self.invalidate_preview)
        self.target.currentIndexChanged.connect(self.invalidate_preview)
        self.borrow.toggled.connect(self.invalidate_preview)
        for widget in (self.sensor_class, self.wiring_class, self.boundary):
            widget.textChanged.connect(self.invalidate_preview)

    def invalidate_preview(self, *_):
        self.proposed = None
        self.apply.setEnabled(False)

    def preview(self):
        donors = [self.donors.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
                  for row in range(self.donors.rowCount())
                  if self.donors.item(row, 0).checkState() == QtCore.Qt.CheckState.Checked]
        try:
            self.proposed = force_estimates.estimate_route(self.inputs, self.group_id, self.target.currentData(),
                donors, sensor_class=self.sensor_class.text().strip(), wiring_class=self.wiring_class.text().strip(),
                boundary=self.boundary.text().strip(), borrow_model=self.borrow.isChecked())
        except (ForceExportError, ValueError, TypeError) as exc:
            self.preview_label.setText("Estimate unavailable: " + str(exc))
            self.apply.setEnabled(False)
            return False
        p, info = self.proposed, self.proposed["estimate"]
        self.preview_label.setText("Estimated loaded capacitance %.3f pF; donor spread (MAD) %.3f pF; "
            "%d donors, confidence: %s. Original fit RMS %s LSB; proposed RMS %s LSB. "
            "Independent package-force validation is still required." %
            (p["c_total_f"] * 1e12, info["c_added_mad_f"] * 1e12, len(donors), info["confidence"],
             info["original_residual_rms_lsb"], info["refit_residual_rms_lsb"]))
        self.apply.setEnabled(True)
        return True
