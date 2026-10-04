"""In-VM agent runtime: task loop, checkpointing, drain (agent-runtime.md §5.2–§5.4)."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .broker import BrokerClient, BrokerUnavailable, Task
from .config import AgentConfig
from .inference import InferenceClient, InferenceError
from .logging_setup import configure_logging
from .status import StatusServer, StatusState
from .ulid import new_ulid

log = logging.getLogger("termini_agent.runtime")

DEFAULT_STATE_DIR = "/var/lib/termini/agent/tasks"


@dataclass
class WorkerStats:
    completed: int = 0
    failed: int = 0
    requeued: int = 0


class AgentRuntime:
    def __init__(self, cfg: AgentConfig, *, state_dir: str = DEFAULT_STATE_DIR,
                 status_unix: str | None = "/run/termini/agent.sock",
                 status_http_port: int = 9101):
        self.cfg = cfg
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.stop_event = threading.Event()
        self.stats = WorkerStats()
        self._stats_lock = threading.Lock()
        self._inflight: dict[str, Task] = {}
        self._inflight_lock = threading.Lock()

        configure_logging(cfg.logging.sink)
        self.inference = InferenceClient(cfg)
        self.broker = BrokerClient(cfg.broker.endpoint, cfg.vm_id,
                                   cfg.inference_token() or "termini-local")
        self.state = StatusState({
            "schema_version": "1.0",
            "kind": "termini.agent_status",
            "vm_id": cfg.vm_id,
            "project_id": cfg.project_id,
            "agent_image_version": cfg.agent_image_version,
            "state": "starting",
            "pid": os.getpid(),
            "started_at": int(time.time()),
            "broker_reachable": False,
            "inflight": 0,
            "stats": {"completed": 0, "failed": 0, "requeued": 0},
            "governance": {"egress": "deny", "telemetry_class": "C0", "local_only": True},
            "config": cfg.as_public_dict(),
        })
        self.status = StatusServer(self.state, unix_path=status_unix,
                                   http_port=status_http_port)

    # -- lifecycle ------------------------------------------------------------
    def run(self) -> int:
        self.status.start()
        self.state.update(state="idle", status_time=int(time.time()))
        log.info("termini-agent started", extra={"termini": {
            "vm_id": self.cfg.vm_id, "image": self.cfg.agent_image_version,
            "inference": self.cfg.inference.base_url, "broker": self.cfg.broker.endpoint}})

        signal.signal(signal.SIGTERM, self._on_term)
        signal.signal(signal.SIGINT, self._on_term)

        self._reclaim_checkpoints()

        workers = [threading.Thread(target=self._worker, name=f"worker-{i}", daemon=True)
                   for i in range(max(1, self.cfg.broker.max_concurrent_tasks))]
        for w in workers:
            w.start()

        while not self.stop_event.is_set():
            self.state.update(status_time=int(time.time()))
            self._refresh_broker_health()
            time.sleep(1.0)

        # drain: give inflight tasks up to 20s to finish or checkpoint (§5.4)
        self.state.update(state="draining")
        deadline = time.time() + 20.0
        while self._inflight_count() > 0 and time.time() < deadline:
            time.sleep(0.25)
        self.status.stop()
        log.info("termini-agent stopped", extra={"termini": {"stats": self._stats_dict()}})
        return 0

    def _on_term(self, signum: int, _frame: Any) -> None:
        log.info("SIGTERM received; draining", extra={"termini": {"signal": signum}})
        self.stop_event.set()

    # -- worker loop ----------------------------------------------------------
    def _worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                tasks = self.broker.claim(max_tasks=1)
            except BrokerUnavailable as exc:
                self.state.update(broker_reachable=False, last_broker_error=str(exc))
                self.stop_event.wait(self.cfg.broker.pull_interval_s * 2)
                continue
            self.state.update(broker_reachable=True, last_broker_error=None)
            if not tasks:
                self.stop_event.wait(self.cfg.broker.pull_interval_s)
                continue
            for task in tasks:
                if self.stop_event.is_set():
                    break
                self._handle(task)
        # not fatal: worker threads are daemons and exit with the process

    def _handle(self, task: Task) -> None:
        with self._inflight_lock:
            self._inflight[task.task_id] = task
        self._publish_inflight()
        check = self.state_dir / f"{task.task_id}.json"
        check.write_text(json.dumps({"task_id": task.task_id, "kind": task.kind,
                                     "claimed_at": int(time.time())}), encoding="utf-8")
        os.chmod(check, 0o600)
        try:
            self.broker.ack(task.task_id, "running")
            result = self._execute(task)
            self.broker.put_result(task.task_id, result)
            self.broker.ack(task.task_id, "succeeded",
                            {"model": result["model"], "tokens": result["tokens"]})
            with self._stats_lock:
                self.stats.completed += 1
            log.info("task completed", extra={"termini": {
                "task_id": task.task_id, "model": result["model"], "tokens": result["tokens"]}})
        except (InferenceError, BrokerUnavailable) as exc:
            with self._stats_lock:
                self.stats.failed += 1
            code = getattr(exc, "code", type(exc).__name__)
            log.warning("task failed", extra={"termini": {
                "task_id": task.task_id, "code": code, "error": str(exc)}})
            try:
                self.broker.ack(task.task_id, "failed", {"code": code})
            except BrokerUnavailable:
                with self._stats_lock:
                    self.stats.requeued += 1
        finally:
            check.unlink(missing_ok=True)
            with self._inflight_lock:
                self._inflight.pop(task.task_id, None)
            self._publish_inflight()

    def _execute(self, task: Task) -> dict[str, Any]:
        model = self.inference.resolve_model(task.model_preference)
        messages = task.payload.get("messages") or [
            {"role": "user", "content": task.payload.get("prompt", "ping")}
        ]
        max_tokens = int(task.payload.get("max_tokens", 256))
        stream = bool(task.payload.get("stream", self.cfg.inference.stream))
        chunks: list[str] = []
        out = self.inference.complete(messages, model=model, max_tokens=max_tokens, stream=stream)
        if isinstance(out, str):
            chunks.append(out)
        else:
            chunks.extend(out)
        content = "".join(chunks)
        # token count is a C0 counter: characters/4 is good enough for telemetry
        return {"task_id": task.task_id, "model": model, "content": content,
                "tokens": max(1, len(content) // 4)}

    # -- helpers --------------------------------------------------------------
    def _inflight_count(self) -> int:
        with self._inflight_lock:
            return len(self._inflight)

    def _publish_inflight(self) -> None:
        self.state.update(inflight=self._inflight_count(), stats=self._stats_dict())

    def _stats_dict(self) -> dict[str, int]:
        return {"completed": self.stats.completed,
                "failed": self.stats.failed,
                "requeued": self.stats.requeued}

    def _refresh_broker_health(self) -> None:
        health = self.broker.health()
        self.state.update(broker_reachable=bool(health.get("reachable")))

    def _reclaim_checkpoints(self) -> None:
        """Re-queue anything checkpointed by a previous run within TTL (§5.3)."""
        for path in sorted(self.state_dir.glob("*.json")):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
                continue
            age = int(time.time()) - int(rec.get("claimed_at", 0))
            if age > int(rec.get("ttl_s", 300)):
                try:
                    self.broker.ack(str(rec["task_id"]), "failed", {"reason": "task_expired"})
                except BrokerUnavailable:
                    pass
                path.unlink(missing_ok=True)
            else:
                log.info("checkpoint found (offline boot); will re-claim when broker is reachable",
                         extra={"termini": {"task_id": rec.get("task_id"), "age_s": age}})


def run_from_config(config_path: str, *, status_unix: str | None = "/run/termini/agent.sock",
                    status_http_port: int = 9101) -> int:
    from .config import load_config

    cfg = load_config(config_path)
    return AgentRuntime(cfg, status_unix=status_unix, status_http_port=status_http_port).run()
