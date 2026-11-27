"""Firebot Web V2.1 contract vectors (2.1.0-draft.1). Pure, no DB/MQTT."""

from __future__ import annotations

import copy
import json
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


@pytest.mark.parametrize("case", _load("MissionFeedback")["stale_cases"], ids=lambda x: x["name"])
def test_stale_feedback_never_updates_current_task(case: dict) -> None:
    ok, code = c.feedback_applies(case["task"], case["feedback"])
    assert code == case["expect"]
    assert ok is (case["expect"] == "OK")


def _scene_pub() -> dict:
    return copy.deepcopy(_load("SceneManifest")["valid"][1]["payload"])


def _receipt() -> dict:
    return copy.deepcopy(_load("SceneLoadReceipt")["valid"][0]["payload"])


BOOT = _receipt()["boot_id"]


def test_scene_load_confirmation_requires_exact_match() -> None:
    m, r = _scene_pub(), _receipt()
    assert c.scene_load_confirms(m, r, BOOT) == (True, "OK")
    assert c.scene_load_confirms(m, r, None)[1] == "RECEIPT_STALE_BOOT_ID"
    assert (
        c.scene_load_confirms(m, r, "00000000-0000-4000-8000-000000000000")[1]
        == "RECEIPT_STALE_BOOT_ID"
    )
    assert (
        c.scene_load_confirms(m, {**r, "scene_digest": "0" * 64}, BOOT)[1]
        == "RECEIPT_DIGEST_MISMATCH"
    )
    assert c.scene_load_confirms(m, {**r, "scene_version": 2}, BOOT)[1] == "RECEIPT_SCENE_MISMATCH"
    assert (
        c.scene_load_confirms(m, {**r, "nav_map_checksum": "other-map-1"}, BOOT)[1]
        == "RECEIPT_NAV_MAP_MISMATCH"
    )
    assert c.scene_load_confirms(m, {**r, "result": "REJECTED"}, BOOT)[1] == "RECEIPT_REJECTED"
    assert c.scene_load_confirms({**m, "status": "DRAFT"}, r, BOOT)[1] == "SCENE_NOT_PUBLISHED"


def _snap_loaded(m: dict) -> dict:
    snap = copy.deepcopy(_load("VehicleSnapshot")["valid"][0]["payload"])
    snap["scene"] = {
        "loaded_scene_id": m["scene_id"],
        "loaded_scene_version": m["scene_version"],
        "loaded_scene_digest": m["digest"],
        "load_state": "LOADED_CONFIRMED",
    }
    return snap


def test_unloaded_scene_cannot_create_mission_and_publish_is_not_motion_authorization() -> None:
    m = _scene_pub()
    ready = copy.deepcopy(_load("OperationReadiness")["valid"][1]["payload"])
    snap = copy.deepcopy(_load("VehicleSnapshot")["valid"][0]["payload"])  # LOAD_PENDING
    assert c.mission_admissible(m, snap, ready, "patrol_start")[1] == "SCENE_NOT_LOADED_ON_VEHICLE"
    assert c.mission_admissible(None, snap, ready, "patrol_start")[1] == "SCENE_MISSING"
    loaded = _snap_loaded(m)
    c.validate("VehicleSnapshot", loaded)
    assert c.mission_admissible(m, loaded, ready, "patrol_start") == (True, "OK")
    # loaded + published but readiness denies -> still refused (地图发布/加载 != 运动授权)
    denied = copy.deepcopy(ready)
    denied["actions"]["patrol_start"] = {
        "allowed": False,
        "reasons": ["REAL_CONTROL_NOT_AUTHORIZED"],
        "required_permission": "patrol.create",
        "permitted_for_user": False,
    }
    assert c.mission_admissible(m, loaded, denied, "patrol_start")[1] == "READINESS_DENIED"
    assert (
        c.mission_admissible(m, loaded, ready, "navigate_to_point")[1] == "READINESS_DENIED"
    )  # absent = deny
    other = copy.deepcopy(loaded)
    other["scene"]["loaded_scene_version"] = 2
    assert (
        c.mission_admissible(m, other, ready, "patrol_start")[1] == "SCENE_LOADED_VERSION_MISMATCH"
    )
    stale = copy.deepcopy(ready)
    stale["boot_id"] = "00000000-0000-4000-8000-000000000000"
    assert (
        c.mission_admissible(m, loaded, stale, "patrol_start")[1] == "READINESS_SNAPSHOT_MISMATCH"
    )


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
        c.can_delete_point(_scene_pub(), free_point)
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
