"""Firebot Web V2.1 contract vectors (2.1.0-draft.2). Pure, no DB/MQTT."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from services import contracts_v21 as c

VEC_DIR = Path(c.SCHEMA_PATH).parent / "vectors"


def _load(name: str) -> dict:
    return c.strict_loads((VEC_DIR / f"{name}.vectors.json").read_text(encoding="utf-8"))


def _cases(kind: str):
    for contract in c.CONTRACTS:
        for vec in _load(contract).get(kind, []):
            yield pytest.param(contract, vec, id=f"{contract}-{vec['name']}")


def test_bundle_is_valid_2020_12_and_covers_all_contracts() -> None:
    defs = c.bundle()["$defs"]
    for contract in c.CONTRACTS:
        assert contract in defs
        c.validator(contract)  # check_schema inside
        vec = _load(contract)
        assert vec["valid"] and vec["invalid"], contract


def test_vectors_are_regenerable_and_committed_in_sync(tmp_path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bv", Path(c.SCHEMA_PATH).parent / "build_vectors.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    for name, vec in mod.ALL.items():
        committed = _load(name)
        assert json.loads(json.dumps(vec)) == committed, (
            f"{name} vectors drifted; rerun build_vectors.py"
        )


@pytest.mark.parametrize(("contract", "vec"), list(_cases("valid")))
def test_valid_vectors_pass(contract: str, vec: dict) -> None:
    c.validate(contract, vec["payload"])


@pytest.mark.parametrize(("contract", "vec"), list(_cases("invalid")))
def test_invalid_vectors_fail_with_expected_code(contract: str, vec: dict) -> None:
    with pytest.raises(c.ContractError) as err:
        c.validate(contract, vec["payload"])
    assert err.value.code == vec["expect"], str(err.value)


@pytest.mark.parametrize(("contract", "vec"), list(_cases("invalid_raw")))
def test_raw_text_rejected_before_schema(contract: str, vec: dict) -> None:
    with pytest.raises(c.ContractError) as err:
        c.validate(contract, c.strict_loads(vec["text"]))
    assert err.value.code == vec["expect"]


def test_nan_object_rejected_even_if_parser_was_lenient() -> None:
    payload = copy.deepcopy(_load("VehicleSnapshot")["valid"][0]["payload"])
    payload["pose"]["value"]["yaw"] = float("nan")
    with pytest.raises(c.ContractError) as err:
        c.validate("VehicleSnapshot", payload)
    assert err.value.code == "NON_FINITE_NUMBER"


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _named(contract: str, key: str) -> list:
    return [pytest.param(case, id=case["name"]) for case in _load(contract)[key]]


@pytest.mark.parametrize("case", _named("MissionFeedback", "apply_cases"))
def test_feedback_reducer_cases(case: dict) -> None:
    c.validate("MissionFeedback", case["feedback"])  # every case is schema-valid on purpose
    ok, code, nxt = c.apply_feedback(case["task"], case["feedback"], now=_dt(case["now"]))
    assert code == case["expect"]
    assert ok is (case["expect"] == "OK")
    if not ok:
        assert nxt is case["task"]  # rejected feedback never mutates the task


@pytest.mark.parametrize("case", _named("SceneLoadReceipt", "confirm_cases"))
def test_scene_load_confirm_cases(case: dict) -> None:
    manifest = _load("SceneManifest")["valid"][1]["payload"]
    c.validate("SceneLoadRequest", case["request"])
    c.validate("SceneLoadReceipt", case["receipt"])
    ok, code = c.scene_load_confirms(
        manifest, case["request"], case["receipt"], current_boot_id=case["current_boot_id"]
    )
    assert (ok, code) == (case["expect"] == "OK", case["expect"])


@pytest.mark.parametrize("case", _named("OperationReadiness", "admission_cases"))
def test_mission_admission_cases(case: dict) -> None:
    ok, code = c.mission_admissible(
        case["manifest"], case["snapshot"], case["readiness"], case["action"], now=_dt(case["now"])
    )
    assert (ok, code) == (case["expect"] == "OK", case["expect"])


@pytest.mark.parametrize("case", _named("VehicleSnapshot", "threshold_cases"))
def test_threshold_exact_boundaries(case: dict) -> None:
    c.validate("VehicleSnapshot", case["snapshot"])
    assert c.threshold_alarms(case["snapshot"]) == case["expect"]


def test_full_multi_waypoint_sequence_then_resume_generation() -> None:
    """DISPATCHED -> ACCEPTED -> 3 waypoints (new goal per waypoint) -> PAUSED;
    old-generation feedback is then rejected after a gen-2 resume is dispatched."""
    vec = _load("MissionFeedback")
    base = next(x["payload"] for x in vec["valid"] if x["name"] == "executing")
    task = next(x["task"] for x in vec["apply_cases"] if x["name"] == "dispatched_to_accepted_ok")
    now = _dt("2026-11-26T09:00:06+08:00")
    steps = [
        ({"state": "ACCEPTED", "goal_id": None, "waypoint_index": 0, "progress": 0}, "OK"),
        ({"state": "EXECUTING", "goal_id": "g0", "waypoint_index": 0, "progress": 10}, "OK"),
        ({"state": "EXECUTING", "goal_id": "g0", "waypoint_index": 0, "progress": 20}, "OK"),
        ({"state": "EXECUTING", "goal_id": None, "waypoint_index": 0, "progress": 25}, "SCHEMA"),
        ({"state": "WAITING_OBSTACLE", "goal_id": "g0", "waypoint_index": 0, "progress": 25}, "OK"),
        ({"state": "EXECUTING", "goal_id": "g1", "waypoint_index": 1, "progress": 50}, "OK"),
        (
            {"state": "EXECUTING", "goal_id": "g0", "waypoint_index": 1, "progress": 55},
            "STALE_GOAL_ID",
        ),
        (
            {"state": "EXECUTING", "goal_id": "g0", "waypoint_index": 0, "progress": 55},
            "WAYPOINT_REGRESSION",
        ),
        (
            {"state": "PAUSED", "goal_id": None, "waypoint_index": 1, "progress": 55},
            "GOAL_REQUIRED",
        ),
        ({"state": "PAUSED", "goal_id": "g1", "waypoint_index": 1, "progress": 55}, "OK"),
        (
            {"state": "ACCEPTED", "goal_id": "g1", "waypoint_index": 1, "progress": 55},
            "ILLEGAL_TRANSITION",
        ),
    ]
    seq = 0
    for over, expect in steps:
        seq += 1
        fb = {**base, **over, "seq": seq, "observed_at": "2026-11-26T09:00:05+08:00"}
        fb.pop("obstacle_since", None)
        if fb["state"] == "WAITING_OBSTACLE":
            fb["obstacle_since"] = fb["observed_at"]
        if expect == "SCHEMA":
            with pytest.raises(c.ContractError):
                c.validate("MissionFeedback", fb)
            continue
        c.validate("MissionFeedback", fb)
        ok, code, task = c.apply_feedback(task, fb, now=now)
        assert code == expect, (over, code)
    assert task["state"] == "PAUSED" and task["waypoint_index"] == 1 and task["goal_id"] == "g1"
    # resume dispatched as generation 2 with a new command: gen-1 stragglers are rejected
    resumed = {**task, "execution_gen": 2, "command_id": "f6000000-0000-4000-8000-000000000003"}
    straggler = {**base, "seq": seq + 1, "goal_id": "g1", "waypoint_index": 1}
    assert c.apply_feedback(resumed, straggler, now=now)[1] == "COMMAND_MISMATCH"
    same_cmd = {**straggler, "command_id": resumed["command_id"]}
    assert c.apply_feedback(resumed, same_cmd, now=now)[1] == "STALE_EXECUTION_GEN"


def test_reviewed_counterexamples_all_rejected() -> None:
    """The five independently reviewed counterexamples (CE1..CE5) are each present and rejected."""
    names = {
        "CE1_bound_goal_null_goal_feedback": ("MissionFeedback", "apply_cases"),
        "CE2_executing_back_to_accepted": ("MissionFeedback", "apply_cases"),
        "CE3_other_vehicle": ("SceneLoadReceipt", "confirm_cases"),
        "CE3_other_load_operation": ("SceneLoadReceipt", "confirm_cases"),
        "CE4_readiness_expired": ("OperationReadiness", "admission_cases"),
        "CE5_archived_scene": ("OperationReadiness", "admission_cases"),
    }
    for name, (contract, key) in names.items():
        case = next(x for x in _load(contract)[key] if x["name"] == name)
        assert case["expect"] != "OK", name


def test_real_default_deny_vector_allows_nothing() -> None:
    r = _load("OperationReadiness")["valid"][0]["payload"]
    assert r["environment"] == "REAL" and r["control_frozen"] is True
    assert not any(a["allowed"] for a in r["actions"].values())


def test_delete_referenced_point_refused_and_published_immutable() -> None:
    draft = copy.deepcopy(_load("SceneManifest")["valid"][0]["payload"])
    route_point = draft["routes"][0]["point_refs"][1]
    with pytest.raises(c.ContractError) as err:
        c.can_delete_point(draft, route_point)
    assert err.value.code == "POINT_REFERENCED_BY_ROUTE"
    free_point = next(p["point_id"] for p in draft["points"] if p["kind"] == "FIRE_OPERATION")
    c.can_delete_point(draft, free_point)
    with pytest.raises(c.ContractError) as err:
        c.can_delete_point(_load("SceneManifest")["valid"][1]["payload"], free_point)
    assert err.value.code == "SCENE_NOT_DRAFT"


def test_samples_never_use_demo_parking_geometry() -> None:
    text = "".join((VEC_DIR / f"{n}.vectors.json").read_text(encoding="utf-8") for n in c.CONTRACTS)
    for forbidden in ("DEMO_PARKING", "REMOTE_WAITING", "RIGHT_SIDE_S_CRUISE", '"A-01"'):
        assert forbidden not in text
    for contract in c.CONTRACTS:
        for vec in _load(contract)["valid"]:
            p = vec["payload"]
            if p.get("environment") == "REAL":
                assert "route_snapshot" not in p and "target" not in p  # no REAL task samples


def test_handoff_index_matches_committed_schema_and_vectors() -> None:
    import hashlib

    here = Path(c.SCHEMA_PATH).parent
    index = c.strict_loads((here / "handoff" / "handoff.json").read_text(encoding="utf-8"))
    assert index["contract_version"] == c.CONTRACT_VERSION
    assert index["schema"]["sha256"] == hashlib.sha256(Path(c.SCHEMA_PATH).read_bytes()).hexdigest()
    for name, entry in index["case_files"].items():
        data = (here / entry["path"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(data).hexdigest(), name
    assert set(index["extension_contracts"]).isdisjoint(index["core_contracts"])
    assert {"DeviceOperation", "AlarmEvent"} == set(index["extension_contracts"])
