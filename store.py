"""Consultation history - the only thing this plugin writes.

The roster under `catalog/` is read-only vendored data; the one piece of mutable
state is "who asked which persona what". It is kept per user, newest first, and
trimmed to `consult.keep_last`.

Everything about *where* that lives is the host's business. The plugin is handed
a storage object already scoped to its own identity, so no method here passes a
plugin name, a directory or a connection string. Two backends:

* Redis, when `storage.client()` returns a client. Keys go through
  `storage.key(...)`, so one plugin cannot read another's.
* A JSON file under `storage.path(...)`, when Redis is down. History is a
  convenience, not a ledger, so degrading beats refusing to answer.

This is not one of the four module names the host imports; it is loaded by file
path - see `_load_local_module` in `tools.py`.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

MAX_QUESTION = 2000
MAX_ANSWER = 20000
MAX_RECORDS = 500
DEFAULT_KEEP_LAST = 50


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


class ConsultationStore:
    """One per-user list of consultations, on whichever backend is up."""

    COLLECTION = "consultations"

    def __init__(self, storage: Any | None) -> None:
        self._storage = storage

    # -- backend selection ---------------------------------------------------

    @property
    def backend(self) -> str:
        """Which backend a call would use right now: redis, file, or none."""
        if self._storage is None:
            return "none"
        # `available()` opens the shared client lazily and pings it; the retry
        # and health-check policy is the host's, so there is nothing to catch.
        return "redis" if self._storage.available() else "file"

    def _client(self) -> Any | None:
        return self._storage.client() if self._storage is not None else None

    def _redis_key(self, actor: str) -> str:
        return self._storage.key(self.COLLECTION, actor or "unknown")

    def _file_path(self) -> Any:
        return self._storage.path(f"{self.COLLECTION}.json")

    # -- reads ---------------------------------------------------------------

    def all_for(self, actor: str) -> list[dict[str, Any]]:
        if self._storage is None:
            return []
        client = self._client()
        if client is not None:
            return [json.loads(item) for item in client.lrange(self._redis_key(actor), 0, -1)]
        return self._read_file().get(actor or "unknown", [])

    def recent(self, actor: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.all_for(actor)[: max(1, min(int(limit or 20), 200))]

    def count(self, actor: str) -> int:
        return len(self.all_for(actor))

    def get(self, actor: str, record_id: str) -> dict[str, Any] | None:
        return next((item for item in self.all_for(actor) if item.get("id") == record_id), None)

    def agent_tally(self, actor: str) -> list[dict[str, Any]]:
        """How often each persona was consulted, most-used first.

        Feeds the dashboard's table widget, and answers the only question the
        history is really for: who does this user keep going back to.
        """
        tally: dict[str, dict[str, Any]] = {}
        for record in self.all_for(actor):
            slug = str(record.get("slug") or "unknown")
            entry = tally.setdefault(
                slug,
                {"slug": slug, "agent": record.get("agent") or slug, "division": record.get("division") or "", "calls": 0},
            )
            entry["calls"] += 1
        return sorted(tally.values(), key=lambda row: (-row["calls"], str(row["agent"])))

    # -- writes --------------------------------------------------------------

    def add(self, actor: str, agent: dict[str, Any], question: str, answer: str, **extra: Any) -> dict[str, Any]:
        """Record one consultation and return the stored record."""
        record = {
            "id": uuid.uuid4().hex[:12],
            "slug": str(agent.get("slug") or ""),
            "agent": str(agent.get("name") or ""),
            "division": str(agent.get("division") or ""),
            "emoji": str(agent.get("emoji") or ""),
            "question": _clip(question, MAX_QUESTION),
            "answer": _clip(answer, MAX_ANSWER),
            "created": _now(),
            **{key: value for key, value in extra.items() if value is not None},
        }
        self._mutate(actor, lambda items: [record, *items][:MAX_RECORDS])
        return record

    def delete(self, actor: str, record_id: str) -> bool:
        before = self.count(actor)
        self._mutate(actor, lambda items: [item for item in items if item.get("id") != record_id])
        return self.count(actor) < before

    def prune(self, actor: str, keep_last: int) -> list[dict[str, Any]]:
        """Trim to the newest `keep_last`, returning what was dropped."""
        keep = max(0, int(keep_last))
        current = self.all_for(actor)
        if len(current) <= keep:
            return []
        self._mutate(actor, lambda items: items[:keep])
        return current[keep:]

    # -- one place that knows how a write reaches each backend ----------------

    def _mutate(self, actor: str, transform: Any) -> None:
        if self._storage is None:
            return
        client = self._client()
        if client is not None:
            key = self._redis_key(actor)
            # A pipeline keeps the rewrite atomic against concurrent readers.
            records = transform([json.loads(item) for item in client.lrange(key, 0, -1)])
            pipeline = client.pipeline()
            pipeline.delete(key)
            if records:
                pipeline.rpush(key, *[json.dumps(record, ensure_ascii=False) for record in records])
            pipeline.execute()
            return
        data = self._read_file()
        data[actor or "unknown"] = transform(data.get(actor or "unknown", []))
        self._write_file(data)

    def _read_file(self) -> dict[str, list[dict[str, Any]]]:
        path = self._file_path()
        if not path.is_file():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt fallback file must not take the plugin down.
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _write_file(self, data: dict[str, list[dict[str, Any]]]) -> None:
        path = self._file_path()
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
