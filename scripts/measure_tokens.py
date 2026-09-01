"""
Measures actual token usage per agent request and compares against the naive baseline.

Naive baseline: full Travelpayouts bulk flight data response for a typical route
search is ~40k-100k tokens. We estimate 50k as the baseline.

Run the agent via docker-compose before using this script.

Usage:
    python -m scripts.measure_tokens
"""
import asyncio
import json
import logging

import httpx

log = logging.getLogger(__name__)

AGENT_URL = "http://localhost:8001"
NAIVE_BASELINE_TOKENS = 50_000  # conservative estimate for raw Travelpayouts bulk response

TEST_QUERIES = [
    {
        "label": "Hidden city search WAW→LHR",
        "user_id": "benchmark-user",
        "session_id": "bench-1",
        "message": "Find me flights from Warsaw to London, interested in hidden city options.",
    },
    {
        "label": "Follow-up with constraint",
        "user_id": "benchmark-user",
        "session_id": "bench-1",
        "message": "I only have carry-on baggage, does that change anything?",
    },
    {
        "label": "Fresh session direct search",
        "user_id": "benchmark-user-2",
        "session_id": "bench-2",
        "message": "Cheapest flights from Warsaw to New York next month?",
    },
]


async def run_benchmark() -> None:
    async with httpx.AsyncClient(base_url=AGENT_URL, timeout=60.0) as client:
        # Verify agent is up
        health = await client.get("/health")
        health.raise_for_status()

        results = []
        for query in TEST_QUERIES:
            response = await client.post("/chat", json=query)
            response.raise_for_status()
            data = response.json()
            results.append(
                {
                    "label": query["label"],
                    "input_tokens": data["input_tokens"],
                    "output_tokens": data["output_tokens"],
                    "total_tokens": data["total_tokens"],
                    "tool_calls": data["tool_calls"],
                }
            )

    print("\n=== Token Usage Report ===\n")
    print(f"{'Query':<45} {'Total':>7} {'vs Naive':>10} {'Tool calls':>11}")
    print("-" * 78)

    for r in results:
        total = r["total_tokens"]
        reduction_pct = (1 - total / NAIVE_BASELINE_TOKENS) * 100
        print(
            f"{r['label']:<45} {total:>7} {reduction_pct:>9.0f}% {r['tool_calls']:>11}"
        )

    totals = sum(r["total_tokens"] for r in results)
    avg = totals // len(results)
    avg_reduction = (1 - avg / NAIVE_BASELINE_TOKENS) * 100

    print("-" * 78)
    print(f"{'Average':<45} {avg:>7} {avg_reduction:>9.0f}%")
    print(f"\nNaive baseline estimate: {NAIVE_BASELINE_TOKENS:,} tokens/request")
    print(f"Token budget target:      2,500 tokens/request")
    print(f"Actual average:           {avg:,} tokens/request")

    budget_metrics = await (await httpx.AsyncClient(base_url=AGENT_URL).get("/metrics")).json()
    print(f"\nBudget targets: {json.dumps(budget_metrics['budget'], indent=2)}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run_benchmark())
