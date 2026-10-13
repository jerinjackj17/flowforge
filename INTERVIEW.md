# flowforge, in 60 seconds

flowforge is a durable invoice-approval workflow I built with LangGraph. An
invoice travels a DAG: intake, validation, policy review, payment,
notification. The project is really about the four ways production workflows
die.

First, crashes. Every state transition is checkpointed to Redis, so I can
kill the server mid-payment and the run resumes from the exact step. Nothing
executes twice. That is what the simulate-crash button in the demo proves.

Second, flaky APIs. The payment step fails twice on purpose and retries with
exponential backoff, and every attempt is in the run log.

Third, humans. Anything over ten thousand dollars parks at an approval gate
using LangGraph's interrupt. In production that pages Slack; the demo uses a
web inbox with identical resume semantics.

Fourth, cost. A ledger tracks projected spend per run, and before each model
call it checks the next tier's projected cost against a fifty-cent budget. If
it would breach, the run degrades down a chain, frontier to mid to local to
cache, instead of dying or burning money.

Honest limits: the live demo runs in demo mode with a deterministic policy
engine, no API keys needed. The model interface is pluggable, so pointing it
at real providers is an env var, not a rewrite. And I picked LangGraph over
Temporal deliberately. Temporal is the gold standard but needs a cluster; I
wanted the whole thing to deploy as one Vercel project. That tradeoff is in
the ADR.
