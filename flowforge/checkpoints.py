"""Checkpoint store: Redis when available, in-memory otherwise.

The durability story depends on this. With REDIS_URL set (Upstash in
production), every state transition is persisted externally: the process can
die mid-run and a fresh process resumes from the exact step. Without it, the
InMemorySaver keeps the demo fully functional in a single process.
"""
from __future__ import annotations

import os


_memory_singleton = None


def get_checkpointer():
    """Return a LangGraph checkpointer. Prefers Redis, falls back to memory.

    The in-memory fallback is a process-wide singleton: serverless-style
    deployments create a fresh checkpointer per request, which would orphan
    every run. Redis (when configured) is stateless per client and safe to
    construct per request.
    """
    global _memory_singleton
    redis_url = os.environ.get("REDIS_URL") or os.environ.get("KV_URL")
    if redis_url:
        try:
            from langgraph_checkpoint_redis import RedisSaver

            saver = RedisSaver.from_conn_string(redis_url)
            saver.setup()
            return saver, "redis"
        except Exception as e:  # pragma: no cover - depends on env
            print(f"Redis checkpointer unavailable ({e}); using memory.")
    if _memory_singleton is None:
        from langgraph.checkpoint.memory import InMemorySaver

        _memory_singleton = InMemorySaver()
    return _memory_singleton, "memory"
