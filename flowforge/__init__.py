"""flowforge: durable agent workflows that survive the real world.

A LangGraph DAG for invoice approval with:
- Redis-persisted checkpoints (resume after a crash, across serverless invocations)
- Exponential-backoff retries on flaky steps
- Human-in-the-loop approval gates (LangGraph interrupts; Slack webhook optional)
- A cost kill-switch with a Frontier -> Mid -> Local -> Cache degradation chain

Demo mode (default) uses a deterministic policy engine instead of a real LLM,
so the live demo needs zero API keys. Set LLM_PROVIDER + LLM_API_KEY to use
real models; the interfaces are identical.
"""
