# CFP — Cloud Native Days

## Title options

**A — problem framing**
> Your LLM Agent Doesn't Have a Memory Problem. It Has a Systems Design Problem.

**B — prescriptive**
> Push, Don't Poll. Cache, Don't Inject. Building LLM Agents the Cloud-Native Way.

---

## Abstract

Every senior engineer has learned these lessons the hard way: polling is wasteful,
chatty cross-service calls are expensive, and state doesn't belong in pod memory.
Then AI agents arrived and we forgot all of it.

Most LLM agents running in Kubernetes today poll external APIs on every turn,
inject raw API responses — tens of thousands of tokens — directly into prompts,
store conversation history in pod RAM, and lose it on every restart. We took a
hidden city flight search agent and rebuilt it applying patterns we already know
from distributed systems design.

The talk is structured as four architectural decisions, each with a cost:

**Cache the context, not the response.** Raw API payloads are 250 KB. Pre-structured
tool results are 2 KB. The model doesn't need the raw payload — it needs the answer
to a specific question. Treating retrieval as a cache lookup instead of a data dump
drops input tokens from 40,000 to 1,500 per request.

**Make retrieval explicit.** Sidecar injection pushes data into every prompt whether
the model needs it or not. Tool-calling lets the model pull exactly what it needs.
Same architecture, different coupling — and a measurable difference in token spend.

**Put state where it survives restarts.** Conversation history in pod RAM dies with
the pod. Conversation history in a document store doesn't. Killing the agent
mid-conversation and resuming seamlessly is a two-minute demo that makes the point
better than any diagram.

**Subscribe, don't poll.** A price-drop alert that fires within seconds of a data
change, with zero polling infrastructure, is just a database subscription. The same
pattern that replaced Kafka for internal event delivery replaces the cron-based
polling loop that was burning API quota.

The demo repo is open source. No proprietary tooling required to run it. Attendees
leave with working code and a mental model for applying classic distributed systems
discipline to AI agent architecture.

---

## Suggested tags

`AI/ML` · `Architecture` · `Best Practices` · `Cost` · `Kubernetes`

## Suggested session length

25–30 min + Q&A

## Notes for reviewers

The use case (hidden city flight ticketing) is intentionally concrete — it requires
the agent to juggle live prices, cached history, and user constraints simultaneously,
which makes the retrieval patterns visible rather than theoretical. All four
architectural patterns are demonstrated live from the open source repo.
