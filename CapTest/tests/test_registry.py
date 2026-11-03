"""Board registry: the real boards.json validates, a broken entry is disabled without
breaking the others, and a throw-away board is picked up by the loader and the lists."""

import copy
import json
import os
import re
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

from captest import boards, cli
from captest.boards import Registry, get_registry
from captest.settings import Settings

REGISTRY_FILE = Path(boards.__file__).resolve().parents[2] / "boards.json"
REPO = REGISTRY_FILE.parent.parent


def real_entries():
    return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))["boards"]


def test_real_registry_validates():
    registry = Registry(REGISTRY_FILE)
    assert not registry.load_error
    assert len(registry.ids()) == len(set(registry.ids())) >= 2
    for entry in registry.entries:
        assert entry.enabled, "%s: %s" % (entry.id, entry.error)
        driver = entry.driver
        assert driver.id == entry.id and entry.descriptor["id"] == entry.id
        fw = entry.firmware
        for which in ("test", "production"):
            ini = entry.project_dir(which) / "platformio.ini"
            assert re.search(r"^\s*\[env:%s\]" % re.escape(fw["%s_env" % which]),
                             ini.read_text(encoding="utf-8"), re.M), (entry.id, which)
        for kind, stem in fw["flash_scripts"].items():
            assert (REPO / (stem + ".ps1")).is_file() and (REPO / (stem + ".sh")).is_file()
        for kind, path in entry.docs.items():
            assert path.is_file(), (entry.id, kind)
        # The identity the firmware reports is what the registry matches on.
        assert registry.match_firmware(firmware=fw["firmware_name"])[0] is entry
        assert registry.match_firmware(mcu=fw["production_mcu"]) == (entry, "production")


def test_descriptor_defaults_match_the_settings_dataclass():
    """The 7953 descriptor documents exactly the defaults of Settings()."""
    descriptor = json.loads((REGISTRY_FILE.parent / "boards" / "TestBoard_7953.json").read_text())
    legacy = Settings()
    for name, value in descriptor["defaults"].items():
        assert getattr(legacy, name) == value, name
    from captest import settings as settings_module

    assert descriptor["channels"]["routed"] == {str(k): v for k, v in
                                                settings_module.ROUTED_CHANNELS.items()}


def test_every_descriptor_default_is_a_setting_and_every_field_has_a_widget_spec():
    from captest.gui import FIELD_SPECS

    for entry in get_registry().enabled:
        defaults = entry.descriptor["defaults"]
        settings = Settings.for_board(entry.id)
        for name, value in defaults.items():
            assert hasattr(settings, name), (entry.id, name)
            assert getattr(settings, name) == value
        for name in entry.descriptor["settings_fields"]:
            assert name in FIELD_SPECS, (entry.id, name)
        assert settings.board_id == entry.id and settings.vref == entry.driver.vref


def write_registry(tmp_path, entries, name="boards.json"):
    folder = tmp_path / "CapTest"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_text(json.dumps({"schema": 1, "boards": entries}), encoding="utf-8")
    return path


def absolute(entry):
    """A copy of a real entry whose paths are absolute (so a temporary registry can use it)."""
    entry = copy.deepcopy(entry)
    entry["descriptor"] = str(REGISTRY_FILE.parent / entry["descriptor"])
    fw = entry["firmware"]
    for key in ("test_project", "production_project"):
        fw[key] = str(REPO / fw[key])
    fw["flash_scripts"] = {k: str(REPO / v) for k, v in fw["flash_scripts"].items()}
    entry["docs"] = {k: str(REPO / v) for k, v in entry["docs"].items()}
    return entry


@pytest.mark.parametrize("mutate,reason", [
    (lambda e: e.pop("driver"), "missing key 'driver'"),
    (lambda e: e["firmware"].pop("test_env"), "missing key 'test_env'"),
    (lambda e: e.update(descriptor="boards/nope.json"), "not found"),
    (lambda e: e["firmware"].update(test_env="teensy41_does_not_exist"), "has no [env:teensy41_does_not_exist]"),
    (lambda e: e.update(driver="captest.boards.no_such_driver"), "does not load"),
    (lambda e: e["docs"].update(protocol="docs/nope.md"), "not found"),
    (lambda e: e["firmware"]["flash_scripts"].update(test="Arduino_Sketches/nope/flash"), "not found"),
])
def test_broken_entry_is_disabled_without_breaking_the_others(tmp_path, mutate, reason):
    good = [absolute(e) for e in real_entries()]
    broken = copy.deepcopy(good[1])
    descriptor = json.loads(Path(broken["descriptor"]).read_text(encoding="utf-8"))
    descriptor["id"] = broken["id"] = "TestBoard_Broken"
    (tmp_path / "CapTest" / "boards").mkdir(parents=True)
    (tmp_path / "CapTest" / "boards" / "broken.json").write_text(json.dumps(descriptor), encoding="utf-8")
    broken["descriptor"] = "boards/broken.json"
    mutate(broken)
    registry = Registry(write_registry(tmp_path, [good[0], broken, good[1]]))
    states = {i: (e.enabled, e.error) for i, e in enumerate(registry.entries)}
    assert states[0][0] and states[2][0]
    assert not states[1][0] and reason in states[1][1]
    assert [e.id for e in registry.enabled] == [good[0]["id"], good[1]["id"]]


def test_duplicate_ids_and_garbage_entries_are_reported(tmp_path):
    good = [absolute(e) for e in real_entries()]
    registry = Registry(write_registry(tmp_path, [good[0], good[0], "not an object"]))
    assert registry.entries[0].enabled
    assert not registry.entries[1].enabled and "duplicate" in registry.entries[1].error
    assert not registry.entries[2].enabled
    assert Registry(tmp_path / "missing" / "boards.json").load_error


FAKE_DRIVER = textwrap.dedent('''
    """Throw-away driver of a fake board: the 7953 driver under another id."""
    from captest.boards.testboard_7953 import Driver7953


    def create_driver(descriptor, entry=None):
        return Driver7953(descriptor, entry)
''')


@pytest.fixture
def fake_registry(tmp_path, monkeypatch):
    """A registry with the two real boards plus a fake one built from scratch in tmp_path."""
    descriptor = json.loads((REGISTRY_FILE.parent / "boards" / "TestBoard_7953.json").read_text())
    descriptor.update(id="TestBoard_Fake", name="Fake board (test)")
    folder = tmp_path / "CapTest"
    (folder / "boards").mkdir(parents=True)
    (folder / "boards" / "TestBoard_Fake.json").write_text(json.dumps(descriptor), encoding="utf-8")
    for project, env in (("Fake_CapTest", "teensy41_captest"), ("Fake", "teensy41")):
        (tmp_path / "Arduino_Sketches" / project / "scripts").mkdir(parents=True)
        (tmp_path / "Arduino_Sketches" / project / "platformio.ini").write_text(
            "[env:%s]\nplatform = teensy@6.0.0\n" % env, encoding="utf-8")
    for ext in (".ps1", ".sh"):
        (tmp_path / "Arduino_Sketches" / "Fake_CapTest" / "scripts" / ("flash_test" + ext)).write_text("")
        (tmp_path / "Arduino_Sketches" / "Fake_CapTest" / "scripts" / ("flash_production" + ext)).write_text("")
    for name in ("protocol", "guide", "check"):
        (tmp_path / "docs").mkdir(exist_ok=True)
        (tmp_path / "docs" / (name + ".md")).write_text("# fake")
    (tmp_path / "fakeboard_driver.py").write_text(FAKE_DRIVER, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    fake = {
        "id": "TestBoard_Fake", "name": "Fake board (test)", "descriptor": "boards/TestBoard_Fake.json",
        "driver": "fakeboard_driver",
        "firmware": {"test_project": "Arduino_Sketches/Fake_CapTest", "test_env": "teensy41_captest",
                     "firmware_name": "Fake_CapTest", "production_project": "Arduino_Sketches/Fake",
                     "production_env": "teensy41", "production_mcu": "Fake",
                     "flash_scripts": {"test": "Arduino_Sketches/Fake_CapTest/scripts/flash_test",
                                       "production": "Arduino_Sketches/Fake_CapTest/scripts/flash_production"}},
        "docs": {"protocol": "docs/protocol.md", "user_guide": "docs/guide.md", "checklist": "docs/check.md"},
    }
    entries = [absolute(e) for e in real_entries()] + [fake]
    registry = Registry(write_registry(tmp_path, entries))
    monkeypatch.setattr(boards, "_DEFAULT", registry)
    return registry


def test_adding_a_board_is_one_entry_a_descriptor_and_a_driver(fake_registry):
    assert [e.id for e in fake_registry.enabled] == ["TestBoard_7953", "TestBoard_ADC124", "TestBoard_Fake"]
    driver = fake_registry.driver("TestBoard_Fake")
    assert driver.firmware_name == "Fake_CapTest" and driver.production_mcu == "Fake"
    assert len(driver.channels()) == 50
    assert fake_registry.match_firmware(mcu="Fake") == (fake_registry.get("TestBoard_Fake"), "production")
    assert fake_registry.match_firmware(firmware="Fake_CapTest")[1] == "captest"
    # The CLI list and the loader know it.
    listing = cli.list_boards()
    assert "TestBoard_Fake" in listing and "Fake board (test)" in listing
    # The generic settings/driver machinery works for it without further edits.
    settings = Settings.for_board("TestBoard_Fake")
    assert settings.board_id == "TestBoard_Fake" and settings.vref == 2.5


def test_the_gui_board_list_shows_the_new_board_and_disabled_entries(fake_registry):
    pytest.importorskip("PyQt6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6 import QtWidgets
    from captest import gui

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    entries = fake_registry.entries
    entries[1].enabled, entries[1].error = False, "disabled for this test"
    window = gui.MainWindow()
    items = [window.board_combo.itemData(i) for i in range(window.board_combo.count())]
    assert items == ["TestBoard_7953", "TestBoard_ADC124", "TestBoard_Fake"]
    model = window.board_combo.model()
    assert not model.item(1).isEnabled() and model.item(1).toolTip() == "disabled for this test"
    assert window.select_board("TestBoard_Fake")
    assert window.driver.id == "TestBoard_Fake" and "Fake board" in window.windowTitle()
    assert not window.select_board("TestBoard_ADC124")        # disabled: refused, window stays usable
    assert window.driver.id == "TestBoard_Fake"
    window.close()
