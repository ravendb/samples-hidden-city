# CFP — KubeCon + CloudNativeCon

## Title options

**A — concept-first**
> Stateful Minds on Stateless Infrastructure: Building Cost-Efficient LLM Agents on Kubernetes

**B — resource framing**
> Context Is a Resource: Applying Distributed Systems Thinking to LLM Agents

---

## Abstract

LLM agents are stateless processes pretending to have memory. Every conversation
turn they reconstruct context from scratch — hitting external APIs, loading
history, injecting it all into the prompt. We treat this as a product problem.
It's actually a distributed systems problem.

This talk reframes LLM agent design through a lens engineers already know:
co-location, caching, push vs pull, and resource budgeting. The vehicle is a
hidden city flight search agent — a use case that forces the agent to reason
over live prices, route history, and user constraints simultaneously, making
retrieval patterns explicit and measurable.

We cover four architectural patterns and the cost consequence of each:

**Token budget as a first-class resource.** Context window space is billed per
token. We enforce a hard 800-token cap on tool results, measure it in CI, and
show what happens to cost curves at 10k requests/day when you treat it like
memory pressure instead of an afterthought.

**Tool-calling as the retrieval interface.** Instead of sidecar injection that
blindly pre-loads context into every prompt, the agent calls retrieval tools
explicitly. The model decides what it needs and when. Same data, 96% fewer tokens.

**In-cluster co-location.** Moving the working memory store inside the cluster
converts cross-boundary retrieval (egress-billed, latency-taxed) into a local
function call. We show the egress delta with actual measurements.

**Push subscriptions over polling.** Price-drop alerts are delivered via database
change subscriptions. No scheduler, no broker, no polling loop — the agent's
working memory is event-driven.

Attendees leave with a concrete architecture, measured cost numbers, and a
checklist for applying these patterns to any LLM workload running on Kubernetes —
regardless of which database or inference provider they use.

---

## Suggested tags

`AI/ML` · `Cost Optimization` · `Architecture` · `Operators` · `Observability`

## Suggested session length

35 min + Q&A (to fit the live demo)

## Notes for reviewers

The demo repo is open source. All cost numbers are derived from measurements
against the running agent (see `scripts/measure_tokens.py` and
`scripts/measure_egress.py`). The architecture is inference-provider-agnostic —
the talk uses Anthropic but the patterns apply equally to any tool-calling model.
