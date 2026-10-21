"""
runs.py — background run management for the GUI.

Each run executes in a daemon thread, publishing progress into a shared
dict guarded by a lock.  The annealer polls `should_stop` so runs can be
cancelled from the browser mid-flight.
"""

from __future__ import annotations

import json
import os
import pathlib
import secrets
import tempfile
import threading
import traceback
from datetime import datetime, timezone

from .. import corpus
from ..diagnostics import run_diagnostics
from ..pipeline import save_report, solve_voynich, sweep_references
from ..synthetic import run_benchmark
from ..paths import default_results_dir

MAX_HISTORY_POINTS = 600


class RunManager:
    def __init__(self, history_dir: pathlib.Path | None = None):
        self._lock = threading.Lock()
        self._runs: dict[int, dict] = {}
        self._history_dir = history_dir or default_results_dir() / ".run-history"
        self._load_history()

    def _allocate_id(self) -> int:
        # Integers remain exactly representable by browser JavaScript. An
        # exclusive reservation protects concurrent headless instances too.
        self._history_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(100):
            run_id = secrets.randbelow(2**52) + 1
            reservation = self._history_dir / f".reserved-{run_id}"
            try:
                fd = os.open(reservation, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            os.close(fd)
            if (self._history_dir / f"run-{run_id}.json").exists():
                reservation.unlink()
                continue
            return run_id
        raise OSError("Could not reserve a unique run history identifier")

    def _load_history(self) -> None:
        """Restore only completed workbench snapshots, never arbitrary reports."""
        if not self._history_dir.exists():
            return
        for path in sorted(self._history_dir.glob("run-*.json")):
            try:
                if path.is_symlink() or path.stat().st_size > 20_000_000:
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                run = payload["run"]
                run_id = run["id"]
                if (payload.get("schema_version") != 1
                    or type(run_id) is not int or run_id < 1
                    or path.name != f"run-{run_id}.json"
                    or run.get("status") not in ("done", "stopped", "error")
                    or run.get("kind") not in ("solve", "benchmark", "sweep", "diagnostics")
                    or not isinstance(run.get("config"), dict)
                    or not isinstance(run.get("history"), list)
                    or not isinstance(run.get("progress"), dict)
                    or not isinstance(run.get("started"), str)
                    or not isinstance(run.get("result"), (dict, type(None)))
                    or not all(k in run for k in ("result", "error"))):
                    continue
                run["stop_event"] = threading.Event()
                self._runs[run_id] = run
            except (OSError, ValueError, KeyError, TypeError, RecursionError):
                # A damaged record must not prevent access to healthy records.
                # Preserve the original file for manual recovery.
                continue

    def _persist(self, run: dict) -> None:
        """Atomically keep final state, including benchmark and stopped runs."""
        self._history_dir.mkdir(parents=True, exist_ok=True)
        snapshot = {k: v for k, v in run.items() if k != "stop_event"}
        data = json.dumps({"schema_version": 1, "run": snapshot}, indent=2)
        fd, temp = tempfile.mkstemp(prefix=".run-", dir=self._history_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self._history_dir / f"run-{run['id']}.json")
            reservation = self._history_dir / f".reserved-{run['id']}"
            reservation.unlink(missing_ok=True)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    # ---- public API ----------------------------------------------------

    def list_runs(self) -> list[dict]:
        with self._lock:
            return [self._summary(r) for r in self._runs.values()]

    def get(self, run_id: int) -> dict | None:
        with self._lock:
            run = self._runs.get(run_id)
            return self._detail(run) if run else None

    def stop(self, run_id: int) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            if not run or run["status"] != "running":
                return False
            run["stop_event"].set()
            return True

    def start_solve(self, config: dict) -> int:
        return self._start("solve", config)

    def start_benchmark(self, config: dict) -> int:
        return self._start("benchmark", config)

    def start_sweep(self, config: dict) -> int:
        return self._start("sweep", config)

    def start_diagnostics(self, config: dict) -> int:
        return self._start("diagnostics", config)

    # ---- internals -------------------------------------------------------

    def _start(self, kind: str, config: dict) -> int:
        run_id = self._allocate_id()
        run = {
            "id": run_id,
            "kind": kind,
            "config": config,
            "status": "running",
            "started": datetime.now(timezone.utc).isoformat(),
            "progress": {},
            "history": [],
            "result": None,
            "error": None,
            "stop_event": threading.Event(),
        }
        with self._lock:
            self._runs[run_id] = run
        thread = threading.Thread(target=self._work, args=(run,), daemon=True)
        thread.start()
        return run_id

    def _work(self, run: dict) -> None:
        def on_progress(p: dict) -> None:
            with self._lock:
                run["progress"] = p
                hist = run["history"]
                step = p["restart"] * p["total_iterations"] + p["iteration"]
                # In sweeps, offset by the sub-run so the chart shows the
                # whole experiment end to end.
                if "lang_index" in p:
                    step += p["lang_index"] * p["total_iterations"] * p["restarts"]
                hist.append([step, p["best_score"]])
                if len(hist) > MAX_HISTORY_POINTS:
                    # Decimate to keep payloads small.
                    run["history"] = hist[::2]

        try:
            if run["kind"] == "solve":
                report = solve_voynich(
                    run["config"],
                    progress=on_progress,
                    should_stop=run["stop_event"].is_set,
                )
                path = save_report(report)
                report["saved_to"] = str(path)
            elif run["kind"] == "diagnostics":
                report = run_diagnostics(run["config"])
                path = save_report(report)
                report["saved_to"] = str(path)
            elif run["kind"] == "sweep":
                cfg = dict(run["config"])
                references = cfg.pop("references", None)
                report = sweep_references(
                    cfg,
                    references=references,
                    progress=on_progress,
                    should_stop=run["stop_event"].is_set,
                )
                path = save_report(report)
                report["saved_to"] = str(path)
            else:
                cfg = run["config"]
                text = corpus.load_reference(cfg.get("reference", "english"))
                report = run_benchmark(
                    text,
                    order=int(cfg.get("order", 4)),
                    cipher_chars=int(cfg.get("cipher_chars", 4000)),
                    iterations=int(cfg.get("iterations", 20000)),
                    restarts=int(cfg.get("restarts", 2)),
                    seed=cfg.get("seed"),
                    mode=cfg.get("mode", "substitution"),
                    progress=on_progress,
                    should_stop=run["stop_event"].is_set,
                )
            with self._lock:
                run["result"] = report
                run["status"] = "stopped" if run["stop_event"].is_set() else "done"
                try:
                    self._persist(run)
                except (OSError, ValueError, TypeError) as exc:
                    run["status"] = "error"
                    run["error"] = f"Could not save run history: {type(exc).__name__}: {exc}"
        except Exception as exc:  # surface errors to the GUI
            with self._lock:
                run["status"] = "error"
                run["error"] = f"{type(exc).__name__}: {exc}"
                run["traceback"] = traceback.format_exc()
                try:
                    self._persist(run)
                except (OSError, ValueError, TypeError) as exc:
                    run["status"] = "error"
                    run["error"] = f"Could not save run history: {type(exc).__name__}: {exc}"

    @staticmethod
    def _summary(run: dict) -> dict:
        return {
            "id": run["id"],
            "kind": run["kind"],
            "status": run["status"],
            "started": run["started"],
            "config": run["config"],
            "progress": run["progress"],
            "error": run["error"],
        }

    @classmethod
    def _detail(cls, run: dict) -> dict:
        out = cls._summary(run)
        out["history"] = run["history"]
        result = run["result"]
        if result is not None:
            # History inside the result duplicates the live history; drop it.
            result = {k: v for k, v in result.items() if k != "history"}
        out["result"] = result
        return out
