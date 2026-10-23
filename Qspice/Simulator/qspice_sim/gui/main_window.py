"""Main window: model selection, parameter screens, run control and the result plot."""
from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PyQt6 import QtCore, QtGui, QtWidgets

from .. import model_loader, runner
from ..config_utils import apply_overrides, format_number, get_path, parse_number, set_path
from ..decimate import envelope
from ..model_loader import Model, ModelError
from ..qraw import QRaw
from .param_form import ParamForm, TableEditor
from .plot_panel import Marker, PlotPanel, Trace
from .worker import SimWorker

SIM_DIR = Path(__file__).resolve().parents[2]
RUNS_DIR = SIM_DIR / "runs"
LAYOUTS = [("One plot per group (stacked)", "group"), ("One plot per node", "node"), ("Single plot (overlay)", "overlay")]


def deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass
class ResultSet:
    qraw: QRaw
    prepared: runner.Prepared
    nodes: list[str]
    cfg: dict
    seconds: float
    columns: dict[str, np.ndarray] = field(default_factory=dict)

    def column(self, node: str) -> np.ndarray:
        if node not in self.columns:
            self.columns[node] = self.qraw.column(node)
        return self.columns[node]


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, models: list[Model], qspice_path: str | None = None):
        super().__init__()
        self.settings = QtCore.QSettings("arduino_adc_streamer", "QspiceSimulator")
        self.models = models
        self.model: Model | None = None
        self.cfg: dict = {}
        self.defaults: dict = {}
        self.qspice_path = qspice_path or self.settings.value("qspice_path", "", str)
        self.viz = {"nodes": set(), "layout": "group", "show_samples": True, "t0": None, "t1": None,
                    "line_width": 1.2, "show_ref": True, "annotate": True}
        self.result: ResultSet | None = None
        self.reference: dict | None = None
        self.worker: SimWorker | None = None
        self._est_bytes = float("nan")
        self._discovered: list[str] = []
        self._tab_forms: list[ParamForm] = []
        self._tables: list[TableEditor] = []
        self._build_ui()
        self.populate_model_combo()
        if self.models:
            self.load_model(self.models[0])
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1500, 900)

    # ================================================================== UI construction
    def _build_ui(self) -> None:
        self.setWindowTitle("Qspice Simulator")
        central = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setMinimumWidth(520)
        self.tabs.setUsesScrollButtons(False)
        self.tabs.tabBar().setExpanding(True)
        central.addWidget(self.tabs)
        right = QtWidgets.QWidget()
        rlay = QtWidgets.QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        self.banner = QtWidgets.QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("background: #fff4cc; color: #000000; padding: 4px; border: 1px solid #e0c060;")
        self.banner.hide()
        rlay.addWidget(self.banner)
        self.plot = PlotPanel()
        self.plot.cursor_text.connect(lambda s: self.cursor_label.setText(s))
        rlay.addWidget(self.plot, 1)
        central.addWidget(right)
        central.setStretchFactor(0, 0)
        central.setStretchFactor(1, 1)
        central.setSizes([600, 900])
        self.setCentralWidget(central)

        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        style = self.style()
        SP = QtWidgets.QStyle.StandardPixmap
        self.model_combo = QtWidgets.QComboBox()
        self.model_combo.setMinimumWidth(240)
        self.model_combo.currentIndexChanged.connect(self._on_model_combo)
        tb.addWidget(QtWidgets.QLabel(" Model: "))
        tb.addWidget(self.model_combo)
        tb.addSeparator()
        self.act_run = tb.addAction(style.standardIcon(SP.SP_MediaPlay), "Run", self.run)
        self.act_run.setShortcut("F5")
        self.act_stop = tb.addAction(style.standardIcon(SP.SP_MediaStop), "Stop", self.stop)
        self.act_stop.setShortcut("Esc")
        self.act_stop.setEnabled(False)
        tb.addSeparator()
        self.act_img = tb.addAction(style.standardIcon(SP.SP_DialogSaveButton), "Save image\u2026", self.save_image)
        self.act_csv = tb.addAction(style.standardIcon(SP.SP_FileIcon), "Export data\u2026", self.export_csv)
        tb.addSeparator()
        self.act_ref = tb.addAction("Hold as reference", self.hold_reference)
        self.act_ref.setToolTip("Keep the current traces as a dashed reference so the next run can be compared with it")
        self.act_clear_ref = tb.addAction("Clear reference", self.clear_reference)
        tb.addSeparator()
        tb.addAction(style.standardIcon(SP.SP_DialogOpenButton), "Load settings\u2026", self.load_settings)
        tb.addAction(style.standardIcon(SP.SP_DialogSaveButton), "Save settings\u2026", self.save_settings)
        for a in (self.act_img, self.act_csv, self.act_ref, self.act_clear_ref):
            a.setEnabled(False)

        sb = self.statusBar()
        self.status_label = QtWidgets.QLabel("Ready")
        self.cursor_label = QtWidgets.QLabel("")
        self.progress = QtWidgets.QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setRange(0, 100)
        self.progress.hide()
        sb.addWidget(self.status_label, 1)
        sb.addPermanentWidget(self.cursor_label)
        sb.addPermanentWidget(self.progress)

    # ================================================================== model handling
    def load_model(self, model: Model) -> None:
        self.model = model
        self.defaults = model.default_config()
        self.cfg = model.default_config()
        self.result = None
        self.reference = None
        self._set_result_actions(False)
        self.plot.clear()
        self.banner.hide()
        self.viz["nodes"] = {n["name"].lower() for n in model.manifest.get("nodes", []) if n.get("default")}
        if self.model_combo.findData(model.id) < 0:
            self.model_combo.blockSignals(True)
            self.model_combo.addItem(model.name, model.id)
            self.model_combo.blockSignals(False)
        self.model_combo.blockSignals(True)
        self.model_combo.setCurrentIndex(max(0, self.model_combo.findData(model.id)))
        self.model_combo.blockSignals(False)
        self._discover_nodes()
        self._build_tabs()
        self.setWindowTitle(f"Qspice Simulator \u2013 {model.name}")
        self._set_status(f"Loaded model {model.name}")

    def populate_model_combo(self) -> None:
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for m in self.models:
            self.model_combo.addItem(m.name, m.id)
        self.model_combo.blockSignals(False)

    def _on_model_combo(self, index: int) -> None:
        if self.worker is not None or index < 0:
            return
        mid = self.model_combo.itemData(index)
        for m in self.models:
            if m.id == mid:
                self.load_model(m)
                return

    def _discover_nodes(self) -> None:
        try:
            self._discovered = runner.prepare(self.model, self.cfg).nodes_in_netlist
        except Exception:  # node discovery is best effort; the manifest still lists the known nodes
            self._discovered = []

    # ------------------------------------------------------------------ tabs
    def _get(self, path: str):
        return get_path(self.cfg, path)

    def _set(self, path: str, value) -> None:
        set_path(self.cfg, path, value)

    def _default(self, path: str):
        return get_path(self.defaults, path, None)

    def _build_tabs(self) -> None:
        m = self.model
        while self.tabs.count():
            old = self.tabs.widget(0)
            self.tabs.removeTab(0)
            old.deleteLater()
        self._tab_forms, self._tables = [], []
        self.tabs.addTab(self._build_model_tab(), "Model")

        comp = ParamForm(m.manifest.get("components", []), self._get, self._set, self._default)
        comp.changed.connect(self._on_param_changed)
        self._tab_forms.append(comp)
        self.tabs.addTab(comp, "Components")

        self.summary_box = QtWidgets.QGroupBox("Derived values")
        self.summary_layout = QtWidgets.QFormLayout(self.summary_box)
        system = ParamForm(m.manifest.get("system", []), self._get, self._set, self._default, footer=self.summary_box)
        system.changed.connect(self._on_param_changed)
        self._tab_forms.append(system)
        self.tabs.addTab(system, "System")

        self.tabs.addTab(self._build_simulation_tab(), "Simulation")
        self.tabs.addTab(self._build_visualization_tab(), "Visualization")
        self._refresh_summary()

    def _build_model_tab(self) -> QtWidgets.QWidget:
        m = self.model
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        title = QtWidgets.QLabel(f"<h3>{m.name}</h3>")
        lay.addWidget(title)
        desc = QtWidgets.QLabel(m.description)
        desc.setWordWrap(True)
        lay.addWidget(desc)
        form = QtWidgets.QFormLayout()
        form.addRow("Folder", QtWidgets.QLabel(str(m.root)))
        self.modified_label = QtWidgets.QLabel("")
        form.addRow("Changed parameters", self.modified_label)
        lay.addLayout(form)
        if m.problems:
            warn = QtWidgets.QLabel("<b>Manifest problems:</b><br>" + "<br>".join(m.problems))
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #a00;")
            lay.addWidget(warn)
        row = QtWidgets.QHBoxLayout()
        reset = QtWidgets.QPushButton("Reset all parameters to the model defaults")
        reset.clicked.connect(self._reset_all)
        row.addWidget(reset)
        reload_btn = QtWidgets.QPushButton("Reload model files")
        reload_btn.setToolTip("Re-read the manifest, defaults and adapter from disk (after editing the model)")
        reload_btn.clicked.connect(self._reload_model)
        row.addWidget(reload_btn)
        lay.addLayout(row)
        box = QtWidgets.QGroupBox("QSPICE and model folders")
        bl = QtWidgets.QGridLayout(box)
        self.qspice_edit = QtWidgets.QLineEdit(self.qspice_path)
        self.qspice_edit.setPlaceholderText("auto-detect (QSPICE_EXE or C:\\Program Files\\QSPICE\\QSPICE64.exe)")
        self.qspice_edit.editingFinished.connect(self._qspice_edited)
        browse = QtWidgets.QPushButton("Browse\u2026")
        browse.clicked.connect(self._browse_qspice)
        bl.addWidget(QtWidgets.QLabel("QSPICE64.exe"), 0, 0)
        bl.addWidget(self.qspice_edit, 0, 1)
        bl.addWidget(browse, 0, 2)
        add_model = QtWidgets.QPushButton("Add model folder\u2026")
        add_model.setToolTip("Load a model folder that contains simulator_model.json")
        add_model.clicked.connect(self._add_model_folder)
        bl.addWidget(add_model, 1, 0, 1, 3)
        lay.addWidget(box)
        lay.addStretch(1)
        return w

    def _build_simulation_tab(self) -> QtWidgets.QWidget:
        m = self.model
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        preset_row = QtWidgets.QHBoxLayout()
        preset_row.addWidget(QtWidgets.QLabel("Preset scenario:"))
        self.preset_combo = QtWidgets.QComboBox()
        self.preset_combo.addItem("(current parameters)", "")
        for name, sc in m.scenarios.items():
            self.preset_combo.addItem(name, name)
            self.preset_combo.setItemData(self.preset_combo.count() - 1, sc.get("description", ""), QtCore.Qt.ItemDataRole.ToolTipRole)
        self.preset_combo.activated.connect(self._apply_preset)
        preset_row.addWidget(self.preset_combo, 1)
        lay.addLayout(preset_row)
        self.preset_desc = QtWidgets.QLabel("")
        self.preset_desc.setWordWrap(True)
        self.preset_desc.setStyleSheet("color: #555;")
        lay.addWidget(self.preset_desc)
        for spec in m.manifest.get("tables", []):
            editor = TableEditor(spec, self._get, self._set)
            editor.changed.connect(self._on_param_changed)
            self._tables.append(editor)
            lay.addWidget(editor)
        est_row = QtWidgets.QHBoxLayout()
        self.estimate_label = QtWidgets.QLabel("Estimate: press \u201cUpdate estimate\u201d")
        self.estimate_label.setWordWrap(True)
        btn = QtWidgets.QPushButton("Update estimate")
        btn.clicked.connect(self.update_estimate)
        est_row.addWidget(self.estimate_label, 1)
        est_row.addWidget(btn)
        est_widget = QtWidgets.QWidget()
        est_widget.setLayout(est_row)
        form = ParamForm(m.manifest.get("simulation", []), self._get, self._set, self._default, footer=est_widget)
        form.changed.connect(self._on_param_changed)
        self._tab_forms.append(form)
        lay.addWidget(form, 1)
        return w

    def _build_visualization_tab(self) -> QtWidgets.QWidget:
        m = self.model
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(QtWidgets.QLabel("Nodes to display (only these are saved by the simulator):"))
        self.node_filter = QtWidgets.QLineEdit()
        self.node_filter.setPlaceholderText("Filter nodes\u2026")
        self.node_filter.setClearButtonEnabled(True)
        self.node_filter.textChanged.connect(self._filter_nodes)
        lay.addWidget(self.node_filter)
        self.node_tree = QtWidgets.QTreeWidget()
        self.node_tree.setHeaderLabels(["Node", "Name"])
        self.node_tree.itemChanged.connect(self._on_node_toggled)
        lay.addWidget(self.node_tree, 1)
        row = QtWidgets.QHBoxLayout()
        for text, fn in (("Defaults", self._nodes_defaults), ("Select none", lambda: self._nodes_all(False)),
                         ("Refresh node list", self._refresh_nodes)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)

        box = QtWidgets.QGroupBox("Display")
        form = QtWidgets.QFormLayout(box)
        self.layout_combo = QtWidgets.QComboBox()
        for text, key in LAYOUTS:
            self.layout_combo.addItem(text, key)
        self.layout_combo.currentIndexChanged.connect(lambda _: self._viz_changed())
        form.addRow("Layout", self.layout_combo)
        self.t0_edit = QtWidgets.QLineEdit()
        self.t1_edit = QtWidgets.QLineEdit()
        for e in (self.t0_edit, self.t1_edit):
            e.setPlaceholderText("auto")
            e.editingFinished.connect(self._viz_changed)
        window = QtWidgets.QHBoxLayout()
        window.addWidget(self.t0_edit)
        window.addWidget(QtWidgets.QLabel("to"))
        window.addWidget(self.t1_edit)
        window.addWidget(QtWidgets.QLabel("s"))
        form.addRow("Time window", window)
        self.lw_spin = QtWidgets.QDoubleSpinBox()
        self.lw_spin.setRange(0.5, 5)
        self.lw_spin.setSingleStep(0.25)
        self.lw_spin.setValue(self.viz["line_width"])
        self.lw_spin.valueChanged.connect(lambda _: self._viz_changed())
        form.addRow("Line width", self.lw_spin)
        self.samples_chk = QtWidgets.QCheckBox(m.manifest.get("sample_marker", {}).get("label", "Sample markers"))
        self.samples_chk.setChecked(True)
        self.samples_chk.setVisible(bool(m.manifest.get("sample_marker")))
        self.samples_chk.toggled.connect(lambda _: self._viz_changed())
        form.addRow("", self.samples_chk)
        self.ref_chk = QtWidgets.QCheckBox("Show the held reference run")
        self.ref_chk.setChecked(True)
        self.ref_chk.toggled.connect(lambda _: self._viz_changed())
        form.addRow("", self.ref_chk)
        self.annot_chk = QtWidgets.QCheckBox("Show changed parameters in the plot title")
        self.annot_chk.setChecked(True)
        self.annot_chk.toggled.connect(lambda _: self._viz_changed())
        form.addRow("", self.annot_chk)
        lay.addWidget(box)
        self._populate_nodes()
        return w

    # ================================================================== node tree
    def _populate_nodes(self) -> None:
        m = self.model
        info = m.node_info()
        known = set(self._discovered)
        tree = self.node_tree
        tree.blockSignals(True)
        tree.clear()
        groups = m.plot_groups()
        by_group: dict[str, list[str]] = {g["id"]: [] for g in groups}
        for name, nd in info.items():
            if not known or name in known:
                by_group.setdefault(nd.get("group", "other"), []).append(name)
        for name in sorted(known - set(info)):
            by_group["other"].append(name)
        for g in groups:
            names = by_group.get(g["id"], [])
            if not names:
                continue
            parent = QtWidgets.QTreeWidgetItem([g["title"], ""])
            parent.setFlags(parent.flags() | QtCore.Qt.ItemFlag.ItemIsAutoTristate | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            tree.addTopLevelItem(parent)
            for name in names:
                label = info.get(name, {}).get("label", name)
                item = QtWidgets.QTreeWidgetItem([label, name])
                item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, QtCore.Qt.CheckState.Checked if name in self.viz["nodes"] else QtCore.Qt.CheckState.Unchecked)
                item.setData(0, QtCore.Qt.ItemDataRole.UserRole, name)
                parent.addChild(item)
            parent.setExpanded(g["id"] in ("adc", "other") or any(n in self.viz["nodes"] for n in names))
        tree.resizeColumnToContents(0)
        tree.blockSignals(False)

    def _selected_nodes(self) -> list[str]:
        out = []
        for i in range(self.node_tree.topLevelItemCount()):
            parent = self.node_tree.topLevelItem(i)
            for j in range(parent.childCount()):
                child = parent.child(j)
                if child.checkState(0) == QtCore.Qt.CheckState.Checked:
                    out.append(child.data(0, QtCore.Qt.ItemDataRole.UserRole))
        return out

    def _on_node_toggled(self, *_):
        self.viz["nodes"] = set(self._selected_nodes())
        self._viz_changed(replot_only=True)

    def _nodes_defaults(self) -> None:
        self.viz["nodes"] = {n["name"].lower() for n in self.model.manifest.get("nodes", []) if n.get("default")}
        self._populate_nodes()
        self._viz_changed()

    def _nodes_all(self, state: bool) -> None:
        self.viz["nodes"] = set() if not state else set(self._discovered)
        self._populate_nodes()
        self._viz_changed()

    def _refresh_nodes(self) -> None:
        self._discover_nodes()
        self._populate_nodes()
        self._set_status(f"{len(self._discovered)} nodes found in the netlist")

    def _filter_nodes(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.node_tree.topLevelItemCount()):
            parent = self.node_tree.topLevelItem(i)
            visible = False
            for j in range(parent.childCount()):
                child = parent.child(j)
                hit = not needle or needle in child.text(0).lower() or needle in child.text(1).lower()
                child.setHidden(not hit)
                visible |= hit
            parent.setHidden(not visible)
            if needle and visible:
                parent.setExpanded(True)

    # ================================================================== parameter changes
    def _on_param_changed(self, _path: str) -> None:
        self._refresh_summary()
        self.estimate_label.setText("Estimate: parameters changed \u2013 press \u201cUpdate estimate\u201d")
        self._update_modified_label()
        self.preset_combo.setCurrentIndex(0)
        self.preset_desc.setText("")

    def _all_paths(self) -> list[str]:
        return self.model.all_paths()

    def _modified_paths(self) -> list[str]:
        return [p for p in self._all_paths() if get_path(self.cfg, p, None) != get_path(self.defaults, p, None)]

    def _update_modified_label(self) -> None:
        n = len(self._modified_paths())
        self.modified_label.setText(f"{n} parameter(s) differ from the model defaults" if n else "none (model defaults)")

    def _refresh_summary(self) -> None:
        while self.summary_layout.rowCount():
            self.summary_layout.removeRow(0)
        try:
            for label, text in self.model.summary(self.cfg):
                self.summary_layout.addRow(label + ":", QtWidgets.QLabel(text))
        except Exception as exc:
            self.summary_layout.addRow("Invalid parameters:", QtWidgets.QLabel(str(exc)))
        self._update_modified_label()

    def _refresh_all_forms(self) -> None:
        for f in self._tab_forms:
            f.refresh()
        for t in self._tables:
            t.refresh()
        self._refresh_summary()

    def _reset_all(self) -> None:
        self.cfg = self.model.default_config()
        self._refresh_all_forms()
        self._set_status("All parameters reset to the model defaults")

    def _reload_model(self) -> None:
        try:
            fresh = model_loader.load_model(self.model.root)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Reload model", str(exc))
            return
        for i, m in enumerate(self.models):
            if m.root == fresh.root:
                self.models[i] = fresh
        self.load_model(fresh)

    def _apply_preset(self, index: int) -> None:
        name = self.preset_combo.itemData(index)
        if not name:
            return
        sc = self.model.scenarios[name]
        self.cfg = apply_overrides(self.model.default_config(), sc.get("overrides", {}))
        self._refresh_all_forms()
        self.preset_combo.setCurrentIndex(index)
        self.preset_desc.setText(sc.get("description", ""))
        self.estimate_label.setText("Estimate: press \u201cUpdate estimate\u201d")

    # ================================================================== settings files
    def save_settings(self) -> None:
        self._commit_pending_edits()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save settings", str(SIM_DIR / f"{self.model.id}_settings.json"),
                                                        "Settings (*.json)")
        if not path:
            return
        data = {"format": 1, "model": self.model.id, "config": self.cfg,
                "visualization": {**self.viz, "nodes": sorted(self.viz["nodes"])}}
        Path(path).write_text(json.dumps(data, indent=1), encoding="utf-8")
        self._set_status(f"Settings saved to {path}")

    def load_settings(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load settings", str(SIM_DIR), "Settings (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            target = next(m for m in self.models if m.id == data["model"])
        except (OSError, ValueError, KeyError, StopIteration) as exc:
            QtWidgets.QMessageBox.warning(self, "Load settings", f"Cannot use this settings file: {exc}")
            return
        if target is not self.model:
            self.load_model(target)
        self.cfg = deep_merge(self.model.default_config(), data.get("config", {}))
        viz = data.get("visualization", {})
        for k in ("layout", "show_samples", "t0", "t1", "line_width", "show_ref", "annotate"):
            if k in viz:
                self.viz[k] = viz[k]
        if "nodes" in viz:
            self.viz["nodes"] = set(viz["nodes"])
        self._refresh_all_forms()
        self._populate_nodes()
        self._apply_viz_widgets()
        self._set_status(f"Settings loaded from {path}")

    def _apply_viz_widgets(self) -> None:
        self.layout_combo.setCurrentIndex(max(0, self.layout_combo.findData(self.viz["layout"])))
        self.t0_edit.setText("" if self.viz["t0"] is None else format_number(self.viz["t0"]))
        self.t1_edit.setText("" if self.viz["t1"] is None else format_number(self.viz["t1"]))
        self.lw_spin.setValue(self.viz["line_width"])
        self.samples_chk.setChecked(self.viz["show_samples"])
        self.ref_chk.setChecked(self.viz["show_ref"])
        self.annot_chk.setChecked(self.viz["annotate"])

    # ================================================================== QSPICE / model folders
    def _qspice_edited(self) -> None:
        self.qspice_path = self.qspice_edit.text().strip()
        self.settings.setValue("qspice_path", self.qspice_path)

    def _browse_qspice(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Locate QSPICE64.exe", "", "QSPICE (QSPICE64.exe);;All files (*)")
        if path:
            self.qspice_edit.setText(path)
            self._qspice_edited()

    def _add_model_folder(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Model folder (contains simulator_model.json)")
        if not path:
            return
        try:
            model = model_loader.load_model(Path(path))
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Add model", str(exc))
            return
        self.models.append(model)
        self.load_model(model)

    # ================================================================== estimate / run
    def _nodes_to_save(self, prepared: runner.Prepared) -> list[str]:
        existing = set(prepared.nodes_in_netlist)
        return [n for n in sorted(self.viz["nodes"]) if n in existing]

    def update_estimate(self) -> None:
        self._commit_pending_edits()
        try:
            prepared = runner.prepare(self.model, self.cfg)
        except Exception as exc:
            self.estimate_label.setText(f"Invalid parameters: {exc}")
            return
        est = prepared.estimate(len(self._nodes_to_save(prepared)))
        self.estimate_label.setText(self._estimate_text(prepared, est))

    @staticmethod
    def _estimate_text(prepared: runner.Prepared, est: dict) -> str:
        t = f"Simulated time {prepared.t_stop * 1e3:.4g} ms"
        if est["points"] == est["points"]:
            secs = est["seconds"]
            when = f"{secs:.0f} s" if secs < 120 else f"{secs / 60:.1f} min"
            t += f", \u2248 {est['points'] / 1e6:.2f} M time points, run time \u2248 {when}, result file \u2248 {est['bytes'] / 1e6:.0f} MB"
        return "Estimate: " + t

    def _commit_pending_edits(self) -> None:
        """Apply a value that was typed but not confirmed.

        A line edit only commits on Enter or when it loses focus, and the toolbar / F5 do not take focus, so a value typed right
        before pressing Run would otherwise be ignored and the run would use the old parameter.
        """
        w = QtWidgets.QApplication.focusWidget()
        if isinstance(w, (QtWidgets.QLineEdit, QtWidgets.QAbstractSpinBox)) and self.isAncestorOf(w):
            w.clearFocus()

    def run(self) -> None:
        if self.worker is not None or self.model is None:
            return
        self._commit_pending_edits()
        try:
            prepared = runner.prepare(self.model, self.cfg)
            qspice = runner.find_qspice(self.qspice_path or None)
        except (ValueError, ModelError, KeyError, runner.SimulationError) as exc:
            QtWidgets.QMessageBox.warning(self, "Cannot run", str(exc))
            return
        nodes = self._nodes_to_save(prepared)
        if not nodes:
            QtWidgets.QMessageBox.information(self, "Nothing to display",
                                              "Select at least one node on the Visualization tab (nodes that are in the netlist).")
            return
        est = prepared.estimate(len(nodes))
        self._est_bytes = est["bytes"]
        self.estimate_label.setText(self._estimate_text(prepared, est))
        if est["seconds"] == est["seconds"] and (est["seconds"] > 90 or est["bytes"] > 5e8):
            answer = QtWidgets.QMessageBox.question(
                self, "Long simulation", self._estimate_text(prepared, est) + "\n\nStart anyway?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        if prepared.warnings:
            self.banner.setText("\u26a0 " + "\n\u26a0 ".join(prepared.warnings))
            self.banner.show()
        else:
            self.banner.hide()
        if self.result is not None:
            self.result.qraw.close()
            self.result = None
        self._pending = (prepared, nodes, copy.deepcopy(self.cfg), time.time())
        self.worker = SimWorker(prepared.netlist, nodes, RUNS_DIR / self.model.id, qspice, self)
        self.worker.progress.connect(self._on_progress)
        self.worker.succeeded.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.cancelled.connect(self._on_cancelled)
        self.worker.finished.connect(self._worker_finished)
        self._set_running(True)
        self._set_status("Running QSPICE\u2026")
        self.worker.start()

    def stop(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self._set_status("Stopping\u2026")

    def _on_progress(self, elapsed: float, nbytes: int) -> None:
        pct = 0
        if self._est_bytes == self._est_bytes and self._est_bytes > 0:
            pct = int(min(99, 100 * nbytes / self._est_bytes))
        self.progress.setValue(pct)
        self._set_status(f"Running QSPICE\u2026 {elapsed:.0f} s, result file {nbytes / 1e6:.1f} MB")

    def _on_done(self, qraw: QRaw) -> None:
        prepared, nodes, cfg, t0 = self._pending
        self.result = ResultSet(qraw, prepared, nodes, cfg, time.time() - t0)
        self._set_result_actions(True)
        missing = [n for n in nodes if not qraw.has(n)]
        msg = f"Done in {self.result.seconds:.1f} s: {qraw.n_points:,} time points"
        if missing:
            msg += f" (not saved by QSPICE: {', '.join(missing)})"
        self._set_status(msg)
        self._replot()

    def _on_failed(self, message: str) -> None:
        self._set_status("Simulation failed")
        QtWidgets.QMessageBox.critical(self, "Simulation failed", message)

    def _on_cancelled(self) -> None:
        self._set_status("Simulation cancelled")

    def _worker_finished(self) -> None:
        self.worker = None
        self._set_running(False)

    def _set_running(self, running: bool) -> None:
        self.act_run.setEnabled(not running)
        self.act_stop.setEnabled(running)
        self.model_combo.setEnabled(not running)
        self.progress.setVisible(running)
        self.progress.setValue(0)

    def _set_result_actions(self, has: bool) -> None:
        for a in (self.act_img, self.act_csv, self.act_ref):
            a.setEnabled(has)
        self.act_clear_ref.setEnabled(self.reference is not None)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    # ================================================================== plotting
    def _viz_changed(self, replot_only: bool = False) -> None:
        self.viz["layout"] = self.layout_combo.currentData()
        self.viz["line_width"] = self.lw_spin.value()
        self.viz["show_samples"] = self.samples_chk.isChecked()
        self.viz["show_ref"] = self.ref_chk.isChecked()
        self.viz["annotate"] = self.annot_chk.isChecked()
        for key, edit in (("t0", self.t0_edit), ("t1", self.t1_edit)):
            text = edit.text().strip()
            try:
                self.viz[key] = parse_number(text) if text else None
                edit.setStyleSheet("")
            except ValueError:
                edit.setStyleSheet("QLineEdit { background: #ffd6d6; color: #000000; }")
        if self.result is not None:
            self._replot()

    def _changes_text(self, cfg: dict) -> str:
        """Short description of the parameters of `cfg` that differ from the model defaults."""
        label = {}
        for tab in ("components", "system", "simulation"):
            for grp in self.model.manifest.get(tab, []):
                for it in grp["items"]:
                    label[it["path"]] = (it["label"], it.get("unit", ""))
        parts = []
        for p in self.model.all_paths():
            if p in label and get_path(cfg, p, None) != get_path(self.defaults, p, None):
                value = get_path(cfg, p, None)
                text = "" if value is None else (format_number(value) if not isinstance(value, (list, bool, str)) else str(value))
                parts.append(f"{label[p][0]} = {text} {label[p][1]}".strip())
        for tbl in self.model.manifest.get("tables", []):
            if get_path(cfg, tbl["path"], None) != get_path(self.defaults, tbl["path"], None):
                parts.append(f"{tbl['title']}: {len(get_path(cfg, tbl['path'], []))} row(s)")
        if not parts:
            return "model defaults"
        return "; ".join(parts[:6] + ([f"+{len(parts) - 6} more"] if len(parts) > 6 else []))

    def _title(self) -> str:
        if not self.viz["annotate"]:
            return ""
        cfg = self.result.cfg if self.result else self.cfg
        title = f"{self.model.name} – {self._changes_text(cfg)}"
        if self.reference and self.viz["show_ref"]:
            title += f"   |   dashed = reference: {self.reference['desc']}"
        return title

    def _replot(self) -> None:
        r = self.result
        if r is None:
            return
        info = self.model.node_info()
        traces = []
        missing = []
        for node in sorted(self.viz["nodes"]):
            if not r.qraw.has(node):
                missing.append(node)
                continue
            nd = info.get(node, {})
            traces.append(Trace(node, nd.get("label", node), nd.get("group", "other"), r.qraw.time, r.column(node)))
        order = {g["id"]: i for i, g in enumerate(self.model.plot_groups())}
        traces.sort(key=lambda t: (order.get(t.group, 99), t.node))
        markers = []
        sm = self.model.manifest.get("sample_marker")
        if sm and self.viz["show_samples"] and r.prepared.samples and r.qraw.has(sm["node"]):
            xs = np.array([s["t"] for s in r.prepared.samples])
            ys = np.interp(xs, r.qraw.time, r.column(sm["node"]))
            markers.append(Marker(sm["node"], sm.get("label", "samples"), xs, ys))
        reference = self.reference["traces"] if (self.reference and self.viz["show_ref"]) else None
        self.plot.show_traces(traces, self.model.plot_groups(), self.viz["layout"], markers, reference, self.viz["line_width"],
                              (self.viz["t0"], self.viz["t1"]), self._title())
        if missing:
            self._set_status(f"Not in the last run (press Run to include them): {', '.join(missing)}")

    def hold_reference(self) -> None:
        self._commit_pending_edits()
        r = self.result
        if r is None:
            return
        if self.reference is not None:
            answer = QtWidgets.QMessageBox.question(
                self, "Replace the reference?",
                f"A reference is already held ({self.reference['desc']}).\n\nReplace it with the current run "
                f"({self._changes_text(r.cfg)})?\n\nChoose No to keep comparing new runs against the original reference.",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No)
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        traces = {}
        t = r.qraw.time
        for node in self.viz["nodes"]:
            if r.qraw.has(node):
                traces[node] = tuple(np.array(a) for a in envelope(t, r.column(node), float(t[0]), float(t[-1]), n_buckets=4000))
        self.reference = {"traces": traces, "desc": self._changes_text(r.cfg)}
        self.act_clear_ref.setEnabled(True)
        self._set_status(f"Reference held ({len(traces)} node(s)): {self.reference['desc']}. Change parameters and press Run to compare")
        self._replot()

    def clear_reference(self) -> None:
        self.reference = None
        self.act_clear_ref.setEnabled(False)
        self._replot()

    # ================================================================== export
    def save_image(self) -> None:
        if not self.plot.has_data():
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save image", str(SIM_DIR / "simulation.png"),
                                                        "PNG image (*.png);;SVG image (*.svg)")
        if not path:
            return
        try:
            self.plot.save_image(path, width=1800)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Save image", str(exc))
            return
        self._set_status(f"Image saved to {path}")

    def export_csv(self) -> None:
        r = self.result
        if r is None:
            return
        nodes = [n for n in sorted(self.viz["nodes"]) if r.qraw.has(n)]
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export data (visible time range)", str(SIM_DIR / "simulation.csv"),
                                                        "CSV (*.csv)")
        if not path or not nodes:
            return
        x0, x1 = self.plot.visible_range()
        t = r.qraw.time
        i0, i1 = int(np.searchsorted(t, x0)), int(np.searchsorted(t, x1, side="right"))
        stride = max(1, (i1 - i0) // 2_000_000)
        cols = [t[i0:i1:stride]] + [r.column(n)[i0:i1:stride] for n in nodes]
        np.savetxt(path, np.column_stack(cols), delimiter=",", header="time_s," + ",".join(f"V({n})" for n in nodes), comments="")
        self._set_status(f"Exported {len(cols[0]):,} rows to {path}" + (f" (every {stride}th point)" if stride > 1 else ""))

    # ================================================================== lifecycle
    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.worker.wait(5000)
        self.settings.setValue("geometry", self.saveGeometry())
        if self.result is not None:
            self.result.qraw.close()
        super().closeEvent(event)
