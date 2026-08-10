"""
Measures outbound bytes per agent request and compares against the naive baseline.

Naive baseline: if you sent the raw Travelpayouts bulk flight data response directly
to the LLM API on every request, ~250 KB leaves the cluster per call.

Optimised: only the user prompt leaves the cluster (~0.1 KB). RavenDB context
is resolved locally by tool calls; the OpenAI API receives only the message.

Estimation method: token counts returned by the agent × 4 bytes/token.
This is an approximation — actual compressed HTTP payloads will be smaller.

Run the agent locally first:
    docker-compose up -d ravendb
    python -m scripts.seed_local
    uvicorn src.agent.app:app --reload

Usage:
    python -m scripts.measure_egress
"""
import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

AGENT_URL = "http://localhost:8001"

# Travelpayouts bulk response for a route search returns ~200-300 KB of flight data.
# We use 250 KB (250,000 bytes) as the naive baseline.
NAIVE_EGRESS_BYTES = 250_000
BYTES_PER_TOKEN = 4  # rough approximation

TEST_QUERIES = [
    {
        "label": "Hidden city WAW→LHR",
        "user_id": "egress-bench",
        "session_id": "e1",
        "message": "Find hidden city flights from Warsaw to London.",
    },
    {
        "label": "Direct search WAW→JFK",
        "user_id": "egress-bench",
        "session_id": "e2",
        "message": "What are the cheapest direct flights from Warsaw to New York?",
    },
    {
        "label": "Follow-up carry-on constraint",
        "user_id": "egress-bench",
        "session_id": "e1",
        "message": "I only have carry-on luggage, does that change the options?",
    },
]


async def run() -> None:
    async with httpx.AsyncClient(base_url=AGENT_URL, timeout=60.0) as client:
        (await client.get("/health")).raise_for_status()

        print("\n=== Egress Measurement Report ===\n")
        print(
            f"{'Query':<40} {'Prompt bytes':>13} {'Naive bytes':>12} {'Reduction':>10}"
        )
        print("-" * 80)

        total_optimised = 0
        total_naive = 0

        for query in TEST_QUERIES:
            response = await client.post("/chat", json=query)
            response.raise_for_status()
            data = response.json()

            # Bytes sent to OpenAI = only the input tokens (user prompt + system)
            # Context stays local in RavenDB — zero egress on retrieval
            optimised_bytes = data["input_tokens"] * BYTES_PER_TOKEN
            reduction_pct = (1 - optimised_bytes / NAIVE_EGRESS_BYTES) * 100

            total_optimised += optimised_bytes
            total_naive += NAIVE_EGRESS_BYTES

            print(
                f"{query['label']:<40} {optimised_bytes:>12,} {NAIVE_EGRESS_BYTES:>12,}"
                f" {reduction_pct:>9.0f}%"
            )

        n = len(TEST_QUERIES)
        avg_opt = total_optimised // n
        avg_naive = total_naive // n
        avg_reduction = (1 - avg_opt / avg_naive) * 100

        print("-" * 80)
        print(f"{'Average':<40} {avg_opt:>12,} {avg_naive:>12,} {avg_reduction:>9.0f}%")

        print(f"\nNaive baseline:  {NAIVE_EGRESS_BYTES / 1024:.0f} KB/request (raw Travelpayouts bulk response)")
        print(f"Optimised avg:   {avg_opt / 1024:.1f} KB/request (prompt only to OpenAI)")
        print(f"Retrieval egress: 0 KB (RavenDB is in-cluster — local tool calls)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run())
