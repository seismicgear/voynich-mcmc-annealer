import json
import threading

from voynich.webapp.runs import RunManager


def completed(run_id=3, kind="benchmark", status="done"):
    return {"id": run_id, "kind": kind, "config": {"seed": 5},
            "status": status, "started": "2026-10-04T08:00:00+00:00",
            "progress": {}, "history": [[1, 2]], "result": {"accuracy": .9},
            "error": None, "stop_event": threading.Event()}


def test_restart_restores_all_run_kinds(tmp_path):
    manager = RunManager(tmp_path)
    for i, kind in enumerate(("solve", "benchmark", "sweep", "diagnostics"), 1):
        manager._persist(completed(i, kind))
    restored = RunManager(tmp_path)
    assert len(restored.list_runs()) == 4
    assert restored.get(2)["kind"] == "benchmark"
    assert restored.get(2)["result"]["accuracy"] == .9
    assert restored._allocate_id() not in (1, 2, 3, 4)
    assert restored.stop(2) is False


def test_corrupt_and_incomplete_files_do_not_hide_good_history(tmp_path):
    manager = RunManager(tmp_path)
    manager._persist(completed())
    (tmp_path / "run-1.json").write_text("{")
    (tmp_path / "run-2.json").write_text(json.dumps({"schema_version": 1, "run": {"id": 2}}))
    (tmp_path / ".run-incomplete").write_text("{")
    assert [r["id"] for r in RunManager(tmp_path).list_runs()] == [3]
    assert (tmp_path / "run-1.json").read_text() == "{"


def test_final_state_only_and_matching_filename(tmp_path):
    manager = RunManager(tmp_path)
    manager._persist(completed(1, status="running"))
    manager._persist(completed(2, status="stopped"))
    manager._persist(completed(3, status="error"))
    (tmp_path / "run-4.json").write_text((tmp_path / "run-3.json").read_text())
    assert {r["id"] for r in RunManager(tmp_path).list_runs()} == {2, 3}


def test_history_write_failure_is_visible(tmp_path, monkeypatch):
    import voynich.webapp.runs as runs
    monkeypatch.setattr(runs.corpus, "load_reference", lambda _: "synthetic")
    monkeypatch.setattr(runs, "run_benchmark", lambda *a, **k: {"accuracy": 1})
    manager = RunManager(tmp_path)
    monkeypatch.setattr(manager, "_persist", lambda _: (_ for _ in ()).throw(OSError("disk full")))
    run = completed(1, status="running")
    manager._work(run)
    assert run["status"] == "error"
    assert "Could not save run history" in run["error"]
    assert run["result"]["accuracy"] == 1


def test_no_partial_file_after_atomic_replace_failure(tmp_path, monkeypatch):
    import voynich.webapp.runs as runs
    import pytest
    manager = RunManager(tmp_path)
    manager._persist(completed())
    original = (tmp_path / "run-3.json").read_bytes()
    monkeypatch.setattr(runs.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("failure")))
    with pytest.raises(OSError):
        manager._persist(completed())
    assert (tmp_path / "run-3.json").read_bytes() == original
    assert not list(tmp_path.glob(".run-*"))


def test_corrupt_ids_are_not_reused(tmp_path, monkeypatch):
    import voynich.webapp.runs as runs
    (tmp_path / "run-1.json").write_text("{")
    choices = iter([0, 1])
    monkeypatch.setattr(runs.secrets, "randbelow", lambda _: next(choices))
    assert RunManager(tmp_path)._allocate_id() == 2
    assert (tmp_path / "run-1.json").read_text() == "{"


def test_two_instances_reserve_distinct_ids(tmp_path, monkeypatch):
    import voynich.webapp.runs as runs
    first, second = RunManager(tmp_path), RunManager(tmp_path)
    choices = iter([0, 0, 1])
    monkeypatch.setattr(runs.secrets, "randbelow", lambda _: next(choices))
    assert first._allocate_id() == 1
    assert second._allocate_id() == 2
    first._persist(completed(1))
    second._persist(completed(2))
    assert {r["id"] for r in RunManager(tmp_path).list_runs()} == {1, 2}


def test_wrong_result_type_skipped_without_modification(tmp_path):
    manager = RunManager(tmp_path)
    bad = completed(1)
    bad["result"] = []
    manager._persist(bad)
    before = (tmp_path / "run-1.json").read_bytes()
    assert RunManager(tmp_path).get(1) is None
    assert (tmp_path / "run-1.json").read_bytes() == before


def test_deeply_nested_record_does_not_block_healthy_history(tmp_path):
    manager = RunManager(tmp_path)
    manager._persist(completed(2))
    malformed = "[" * 100000 + "0" + "]" * 100000
    (tmp_path / "run-1.json").write_text(malformed)
    assert [r["id"] for r in RunManager(tmp_path).list_runs()] == [2]
    assert (tmp_path / "run-1.json").read_text() == malformed
