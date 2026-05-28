# CFP — KubeCon + CloudNativeCon North America
# Event: November 9–12, 2026 | Salt Lake City
# CFP deadline: May 31, 2026 at 11:59pm MT
# Submission platform: Sessionize (events.linuxfoundation.org/kubecon-cloudnativecon-north-america/program/cfp)

---

## Session Title

> Context Is a Resource: Can we get 96% Fewer Tokens and Zero Egress?

*(Alternatives if the program committee prefers a declarative form:)*

- Context Is a Resource: 96% Fewer Tokens by Applying Distributed Systems Thinking to LLM Agents
- Context Is a Resource: Cutting LLM Token and Egress Costs with Distributed Systems Patterns

---

## Suggested Track

**AI Inference + Agentic** (primary)
Covers: agentic systems on Kubernetes, infrastructure for AI workloads, AgentOps, tool-calling protocols.

Alternative: **Operations + Performance** (Operators, performance optimization, resource management)

---

## Submission Type

Session Presentation — 30 minutes, 2 speakers

---

## Description
*(≤ 1000 characters, third person, appears on schedule as-is)*

LLM agents running on Kubernetes reconstruct context from scratch on every turn — calling external APIs, injecting raw responses into prompts, storing conversation history in pod memory. Practitioners recognize these as solved problems in distributed systems. In AI workloads, they are being ignored.

This session walks through a working hidden city flight search agent to demonstrate four patterns: enforcing a token budget as a first-class resource with a hard 800-token cap on tool results, measured in CI; using explicit tool-calling as the retrieval interface instead of sidecar injection; co-locating working memory inside the cluster to eliminate retrieval egress; and replacing polling loops with push-based database subscriptions for event-driven state updates.

All cost numbers are measured, not estimated. Measured results: 1,500 tokens per request versus a 40,000-token naive baseline, zero retrieval egress on cache hits, five infrastructure components replaced by one. The demo repository is open source.

*(~950 characters)*

---

## Benefits to the Ecosystem

Cloud native teams adopting LLM workloads are inheriting cost and architecture problems that distributed systems engineering already has answers to. This session names those patterns explicitly and shows them applied end-to-end on Kubernetes — giving platform engineers a concrete lever for cost control that does not require switching models, providers, or inference infrastructure.

The four patterns covered — token budgeting, explicit retrieval via tool-calling, in-cluster co-location, push subscriptions over polling — are inference-provider-agnostic. Attendees can apply them regardless of which LLM API or database their team uses. The accompanying open source demo repository includes working Kubernetes manifests and measurement scripts that can be run against any cluster.

The cost impact at scale is significant. At 10,000 requests per day, the difference between injecting raw API responses into prompts and structured in-cluster retrieval is approximately $1,155 in daily LLM spend. Framing this as a systems design problem — not a model tuning problem — makes it actionable for the operations and platform engineering community that attends KubeCon.

The session also demonstrates a Kubernetes Operator managing a stateful workload with rolling upgrades and Raft quorum gates, which is directly relevant to the growing number of teams running stateful AI infrastructure on Kubernetes.

---

## Case Study?

**N**

This is an architectural pattern demonstration using an open source reference implementation. It is not a report of a specific organization's production deployment. The talk is intentionally generalized so the patterns are applicable across teams and stacks.

---

## CNCF-Hosted Software

- **Kubernetes** (graduated) — the cluster runtime; the working memory store runs as a Kubernetes Operator managing a custom resource (`RavenDBCluster` CRD); the agent and worker run as standard Deployments and CronJobs

---

## Open Source Projects

- Demo repository: https://github.com/ravendb/samples-hidden-city — full implementation including FastAPI agent, tool-calling loop, Kubernetes manifests, and measurement scripts
- RavenDB — document store deployed in-cluster as the agent working memory; the Operator pattern is the focus of the Kubernetes portion of the talk
- Python `anthropic` / `openai` SDK — tool-calling interface demonstrated in the agent loop (provider-agnostic pattern; both are shown)

---

## Additional Resources

*(Add link to a recording of a previous talk by the speaker — required by the program committee to assess presentation skills. A short YouTube video is acceptable if no conference recording exists.)*

[ INSERT VIDEO LINK ]

---

## Speaker Biography

*(Replace with actual bio before submitting. Guidelines: 3–5 sentences, third person, emphasize relevant technical background and community involvement.)*

**Speaker 1 — [Name], [Title] at RavenDB**

[ INSERT BIO ]

Example structure:
"[Name] is a [role] at RavenDB, where they work on [relevant area]. They have been building distributed systems on Kubernetes since [year] and focus on the intersection of database infrastructure and AI agent architecture. [Community involvement — CNCF Ambassador, meetup organizer, open source contributor, etc.]. [One previous conference or talk if applicable]."

---

## Submission Notes (internal — do not include in Sessionize)

- Max 3 submissions per speaker across the whole CFP — check before adding co-speakers
- Panel rule: if adding a third speaker, ensure not all identify as men and represent more than one company
- If this talk or a substantially similar version was given at a CNCF/LF event in the past year, add a "how this differs" paragraph to the description
- Program committee tip: concrete numbers ("96% reduction", "$1,155/day") and a live demo increase acceptance rate for technical tracks
- Reviewer guideline reference: https://events.linuxfoundation.org/kubecon-cloudnativecon-north-america/program/cfp/#submission-reviewer-guidelines
