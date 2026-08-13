# Completion Alignment Follow-up Review

## Decision

`DeliveryContract.after` is removed from the reserved contract surface by
ADR-009 Q-05. Execution ordering is owned solely by `DagStep.depends_on`; the
delivery contract no longer carries ordering semantics. This supersedes the
earlier "reserved for future" status.

## Why the Earlier Deferral Was Resolved

`after` was originally reserved to express user-level ordering ("read after
write"). Review concluded that ordering is already a DAG concern and duplicating
it in the contract created two competing dependency models. The single source
of truth for scheduling is `DagStep.depends_on`; `DeliveryContract` answers
only "what must be delivered".

The full four-layer split (delivery contract / LLM self-declaration / DAG
execution / completion gate) is documented in `JAgent-docs/Prd/ADR-009_质量门禁与执行依赖分离设计.md`.

## Remaining Risk

Ordering of user-required operations still depends on the Planner producing
correct `depends_on`. If a future product requirement makes ordering itself a
deliverable invariant, it must be enforced by the trusted guardrail on the DAG
(not by re-introducing a contract-level ordering field).

## Required Future Work (from ADR-009)

- Q-02: rename `Plan.required_operations` → `declared_operations`
- Q-05: delete `DeliveryContract.after` from the model
- Q-06: mutating-step coverage must be satisfied by `DeliveryContract` only
- Q-07/Q-08 are already implemented (single run deadline, no confirmation TTL)
