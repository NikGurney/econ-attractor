"""Byte-stable serialization of witness artifacts (spec Section 12.3).

Knows nothing about particular institutions: event files are named from the
IDs it is given, and every document is written with sorted keys and Unix
newlines so replaying the build is byte-identical.
"""

from __future__ import annotations

import json
from pathlib import Path


def json_bytes(obj) -> bytes:
    return (json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def jsonl_bytes(events) -> bytes:
    return "".join(
        json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n" for event in events
    ).encode("utf-8")


def events_filename(institution_id: str) -> str:
    return f"{institution_id}_events.jsonl"


def render_descriptors(descriptors: dict) -> bytes:
    """institution_specs.json: complete descriptors keyed by institution ID."""
    return json_bytes({iid: desc.to_dict() for iid, desc in descriptors.items()})


def render_files(scenario_dict: dict, descriptors: dict, events_by_institution: dict, summary: dict) -> dict:
    files = {
        "scenario.json": json_bytes(scenario_dict),
        "institution_specs.json": render_descriptors(descriptors),
        "summary.json": json_bytes(summary),
    }
    for iid, events in events_by_institution.items():
        files[events_filename(iid)] = jsonl_bytes(events)
    return files


def write_files(files: dict, output_dir) -> list:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name in sorted(files):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(files[name])
        written.append(path)
    return written
