"""On-node broker client (agent-runtime.md §6).

The broker is the ONLY path between the controller and the agent VM. It lives on
the node's guest-bridge gateway (10.20.<n>.1:7071) and is reachable from the VM;
it is not the controller. This client never addresses anything else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("termini_agent.broker")


class BrokerUnavailable(RuntimeError):
    pass


@dataclass
class Task:
    task_id: str
    kind: str
    ttl_s: int
    model_preference: str | None
    payload: dict[str, Any]

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "Task":
        return cls(
            task_id=str(obj["task_id"]),
            kind=str(obj.get("kind", "chat")),
            ttl_s=int(obj.get("ttl_s", 300)),
            model_preference=obj.get("model_preference"),
            payload=obj.get("payload") or {},
        )


class BrokerClient:
    def __init__(self, endpoint: str, vm_id: str, token: str, *, timeout_s: float = 3.0):
        self.endpoint = endpoint.rstrip("/")
        self.vm_id = vm_id
        self._token = token
        self._client = httpx.Client(base_url=self.endpoint, timeout=timeout_s)

    def _body(self, **extra: Any) -> dict[str, Any]:
        return {"vm_id": self.vm_id, "token": self._token, **extra}

    def claim(self, max_tasks: int = 1) -> list[Task]:
        try:
            resp = self._client.post("/v1/tasks/claim", json=self._body(max=max_tasks))
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise BrokerUnavailable(str(exc)) from exc
        data = resp.json()
        return [Task.from_json(t) for t in data.get("tasks", [])]

    def ack(self, task_id: str, state: str, summary: dict[str, Any] | None = None) -> None:
        try:
            resp = self._client.post(
                f"/v1/tasks/{task_id}/ack",
                json=self._body(state=state, summary=summary or {}),
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise BrokerUnavailable(str(exc)) from exc

    def put_result(self, task_id: str, body: dict[str, Any]) -> None:
        try:
            resp = self._client.post(f"/v1/tasks/{task_id}/result",
                                     json=self._body(result=body))
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise BrokerUnavailable(str(exc)) from exc

    def health(self) -> dict[str, Any]:
        try:
            resp = self._client.get("/v1/status")
            resp.raise_for_status()
            return {"reachable": True, "body": resp.json()}
        except httpx.HTTPError as exc:
            return {"reachable": False, "error": str(exc)}

    def close(self) -> None:
        self._client.close()
