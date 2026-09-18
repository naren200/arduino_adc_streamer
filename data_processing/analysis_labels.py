"""
Analysis tab texture labeling.

Segments are non-overlapping {start_s, end_s, class} intervals covering (parts
of) a loaded Analysis CSV, persisted to a sidecar `<csv_stem>_labels.json` next
to the CSV. Assigning a class to a time range overwrites whatever was labeled
there before, which is what lets a user label the whole trace as one class and
then carve out and relabel sub-segments.
"""

from __future__ import annotations

import json
from pathlib import Path


def abbreviate_class_names(class_names: list[str]) -> dict[str, str]:
    """Map each class name to a unique 2-character uppercase code.

    Prefers the first letter of each `_`-separated word (e.g. "bumpy_wood" ->
    "BW"); falls back to the first two letters, then scans further into the
    name to resolve collisions, so labels stay compact and distinguishable.
    """
    codes: dict[str, str] = {}
    used: set[str] = set()
    for name in class_names:
        words = [w for w in name.split("_") if w]
        candidates = []
        if len(words) >= 2:
            candidates.append((words[0][0] + words[1][0]).upper())
        letters = name.replace("_", "")
        candidates.append(letters[:2].upper())
        for i in range(len(letters) - 1):
            candidates.append((letters[i] + letters[i + 1]).upper())

        code = next((c for c in candidates if len(c) == 2 and c not in used), None)
        if code is None:
            code = (letters[:2].upper() or "XX")
            suffix = 0
            base = code
            while code in used:
                suffix += 1
                code = (base[0] + str(suffix % 10))
        used.add(code)
        codes[name] = code
    return codes


def label_sidecar_path(csv_path: str) -> Path:
    return Path(csv_path).with_name(Path(csv_path).stem + "_labels.json")


def load_labels(csv_path: str) -> list[dict]:
    """Load segments from the sidecar next to `csv_path`, or [] if absent/invalid."""
    path = label_sidecar_path(csv_path)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    segments = payload.get("segments", [])
    if not isinstance(segments, list):
        return []
    cleaned = []
    for entry in segments:
        if not isinstance(entry, dict):
            continue
        try:
            start_s = float(entry["start_s"])
            end_s = float(entry["end_s"])
            class_name = str(entry["class"])
        except (KeyError, TypeError, ValueError):
            continue
        if end_s > start_s:
            cleaned.append({"start_s": start_s, "end_s": end_s, "class": class_name})
    return sorted(cleaned, key=lambda seg: seg["start_s"])


def save_labels(csv_path: str, class_names: list[str], segments: list[dict]) -> Path:
    path = label_sidecar_path(csv_path)
    payload = {
        "version": 1,
        "class_names": list(class_names),
        "segments": [
            {"start_s": float(seg["start_s"]), "end_s": float(seg["end_s"]), "class": str(seg["class"])}
            for seg in sorted(segments, key=lambda seg: seg["start_s"])
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def remove_range(segments: list[dict], start_s: float, end_s: float) -> list[dict]:
    """Return new segments with [start_s, end_s) unlabeled (trimmed/split, nothing added back)."""
    if end_s <= start_s:
        return list(segments)

    result: list[dict] = []
    for seg in segments:
        seg_start, seg_end, seg_class = seg["start_s"], seg["end_s"], seg["class"]
        if seg_end <= start_s or seg_start >= end_s:
            result.append(dict(seg))
            continue
        if seg_start < start_s:
            result.append({"start_s": seg_start, "end_s": start_s, "class": seg_class})
        if seg_end > end_s:
            result.append({"start_s": end_s, "end_s": seg_end, "class": seg_class})
    result.sort(key=lambda seg: seg["start_s"])
    return result


def assign_segment(segments: list[dict], start_s: float, end_s: float, class_name: str) -> list[dict]:
    """Return new segments with [start_s, end_s) overwritten as `class_name`.

    Existing segments overlapping the range are trimmed or split so the result
    stays a sorted, non-overlapping cover; segments of the same class that end
    up touching are merged.
    """
    if end_s <= start_s:
        return list(segments)

    result: list[dict] = []
    for seg in segments:
        seg_start, seg_end, seg_class = seg["start_s"], seg["end_s"], seg["class"]
        if seg_end <= start_s or seg_start >= end_s:
            result.append(dict(seg))
            continue
        if seg_start < start_s:
            result.append({"start_s": seg_start, "end_s": start_s, "class": seg_class})
        if seg_end > end_s:
            result.append({"start_s": end_s, "end_s": seg_end, "class": seg_class})

    result.append({"start_s": start_s, "end_s": end_s, "class": class_name})
    result.sort(key=lambda seg: seg["start_s"])

    merged: list[dict] = []
    for seg in result:
        if merged and merged[-1]["class"] == seg["class"] and merged[-1]["end_s"] >= seg["start_s"]:
            merged[-1]["end_s"] = max(merged[-1]["end_s"], seg["end_s"])
        else:
            merged.append(dict(seg))
    return merged
