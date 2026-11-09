import copy
import json

import pytest

from captest import sensor_groups as sg

PROFILE = "TestBoard_7953"

EXPECTED = {
    "A1_PZT6": [(1, i) for i in range(0, 5)],
    "A1_PZT7": [(1, i) for i in range(5, 10)],
    "A1_PZT1": [(2, i) for i in range(0, 5)],
    "A1_PZT3": [(2, i) for i in range(5, 10)],
    "A1_PZT5": [(2, i) for i in range(10, 15)],
    "A2_PZT6": [(3, i) for i in range(0, 5)],
    "A2_PZT7": [(3, i) for i in range(5, 10)],
    "A2_PZT1": [(4, i) for i in range(0, 5)],
    "A2_PZT3": [(4, i) for i in range(5, 10)],
    "A2_PZT5": [(4, i) for i in range(10, 15)],
}


def library():
    return json.loads(sg.default_library_path().read_text(encoding="utf-8"))


def entry_of(data):
    return next(c for c in data["configurations"] if c.get("board_profile") == PROFILE)


def test_all_ten_bundled_groups_resolve():
    gm = sg.resolve(PROFILE)
    assert len(gm.groups) == 10 and len(gm.all_refs()) == 50
    for gid, refs in EXPECTED.items():
        assert list(gm.get(gid).refs) == refs
    assert gm.positions == ("B", "L", "C", "R", "T")
    assert [r.position for r in gm.get("A1_PZT1").routes] == ["B", "L", "C", "R", "T"]
    assert gm.arrays() == [1, 2]


def test_same_pzt_name_in_both_arrays_is_distinct():
    gm = sg.resolve(PROFILE)
    assert gm.get("A1_PZT1").refs != gm.get("A2_PZT1").refs
    assert {r[0] for r in gm.get("A1_PZT1").refs} == {2}
    assert {r[0] for r in gm.get("A2_PZT1").refs} == {4}
    assert "PZT1" not in gm.groups


def test_group_identity_independent_of_selected_name():
    data = library()
    base = sg.resolve(PROFILE, library_data=data)
    data["selected_name"] = "something_else"
    other = sg.resolve(PROFILE, library_data=data)
    assert [g.refs for g in base.ordered()] == [g.refs for g in other.ordered()]
    assert base.layout_hash == other.layout_hash        # entry unchanged


def test_hash_follows_library_and_entry_content():
    gm = sg.resolve(PROFILE)
    assert len(gm.library_hash) == 64 and len(gm.layout_hash) == 64
    data = library()
    entry_of(data)["reverse_polarity"] = True
    changed = sg.resolve(PROFILE, library_data=data)
    assert changed.layout_hash != gm.layout_hash and changed.reverse_polarity


def test_unused_inputs_are_not_errors():
    gm = sg.resolve(PROFILE)
    used_adc1 = {r[1] for r in gm.all_refs() if r[0] == 1}
    assert used_adc1 == set(range(10)) and 10 not in used_adc1


def test_board_mismatch_and_missing_entry():
    with pytest.raises(sg.GroupMappingError):
        sg.resolve("NoSuchBoard")
    with pytest.raises(sg.GroupMappingError):
        sg.resolve(PROFILE, layout_name="nope")


@pytest.mark.parametrize("edit,text", [
    (lambda e: e["mux_mapping"]["PZT1"].update(channels=[0, 1, 2, 3, 15]), "reserved"),
    (lambda e: e["mux_mapping"]["PZT1"].update(channels=[0, 1, 2, 3]), "inputs for"),
    (lambda e: e["mux_mapping"]["PZT1"].update(channels=[0, 0, 1, 2, 3]), "duplicate"),
    (lambda e: e["mux_mapping"]["PZT1"].update(channels=[0, 1, 2, 3, 16]), "outside"),
    (lambda e: e["mux_mapping"]["PZT1"].update(mux=3), "not a lane"),
    (lambda e: e["mux_mapping"]["PZT3"].update(mux=2, channels=[0, 1, 2, 3, 9]), "overlaps"),
    (lambda e: e.update(channel_sensor_map=["B", "L", "C", "R", "R"]), "distinct"),
    (lambda e: e.pop("arrays"), "arrays"),
    (lambda e: e.pop("mux_mapping"), "mux_mapping"),
    (lambda e: e["arrays"].update({"1": {"adc_lanes": []}}), "adc_lanes"),
])
def test_invalid_layouts_rejected(edit, text):
    data = copy.deepcopy(library())
    edit(entry_of(data))
    with pytest.raises(sg.GroupMappingError, match=text):
        sg.resolve(PROFILE, library_data=data)


def test_overlapping_arrays_rejected():
    data = copy.deepcopy(library())
    entry_of(data)["arrays"]["2"]["adc_lanes"] = [1, 2]       # both arrays on the same ADCs
    with pytest.raises(sg.GroupMappingError, match="overlaps"):
        sg.resolve(PROFILE, library_data=data)


def test_custom_compatible_layout():
    data = copy.deepcopy(library())
    e = entry_of(data)
    e["channel_sensor_map"] = ["B", "L", "C"]
    e["channel_layout"] = {"channels_per_sensor": 3}
    e["array_count"] = 1
    e["arrays"] = {"1": {"adc_lanes": [1]}}
    e["mux_mapping"] = {"X": {"mux": 1, "channels": [4, 5, 6]}}
    e["array_layout"] = {"cells": [["X"]]}
    gm = sg.resolve(PROFILE, library_data=data)
    assert list(gm.groups) == ["A1_X"] and gm.get("A1_X").refs == ((1, 4), (1, 5), (1, 6))


def test_group_lookup_by_ref():
    gm = sg.resolve(PROFILE)
    assert gm.group_of((4, 12)).group_id == "A2_PZT5"
    assert gm.group_of((1, 12)) is None
    with pytest.raises(sg.GroupMappingError):
        gm.get("PZT1")
