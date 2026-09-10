"""
Measures outbound bytes per agent request and compares against the naive baseline.

Naive baseline: if you sent the raw Travelpayouts bulk flight data response directly
to the LLM API on every request, ~250 KB leaves the cluster per call.

Optimised: only the user prompt leaves the cluster (~0.1 KB). RavenDB context
is resolved locally by tool calls; the OpenAI API receives only the message.

Measurement method: each /chat response carries `openai_egress_bytes`, a before/after
delta of the agent process's own network-interface tx_bytes counter (read by
src/agent/loop.py's `_read_tx_bytes`, from /sys/class/net/eth0/statistics/tx_bytes)
taken around the OpenAI call -- an actual kernel counter, not a number derived from
token counts. It is None when the counter isn't readable (non-Linux host running the
agent outside a container/pod); this script falls back to the token-count estimate
in that case and labels it clearly as an estimate rather than presenting it as measured.

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
BYTES_PER_TOKEN = 4  # fallback estimate, only used when openai_egress_bytes is unavailable

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

        print("\n=== Egress Report ===\n")
        print(
            f"{'Query':<40} {'Bytes':>14} {'Source':>10} {'Naive bytes':>12} {'Reduction':>10}"
        )
        print("-" * 90)

        total_optimised = 0
        total_naive = 0
        any_unmeasured = False

        for query in TEST_QUERIES:
            response = await client.post("/chat", json=query)
            response.raise_for_status()
            data = response.json()

            measured = data.get("openai_egress_bytes")
            if measured is not None:
                optimised_bytes = measured
                source = "measured"
            else:
                # Fallback only: no tx_bytes counter available on this host (e.g. not
                # running in a Linux container/pod). Derived from token count, not observed.
                any_unmeasured = True
                optimised_bytes = data["input_tokens"] * BYTES_PER_TOKEN
                source = "estimate"

            reduction_pct = (1 - optimised_bytes / NAIVE_EGRESS_BYTES) * 100

            total_optimised += optimised_bytes
            total_naive += NAIVE_EGRESS_BYTES

            print(
                f"{query['label']:<40} {optimised_bytes:>14,} {source:>10} {NAIVE_EGRESS_BYTES:>12,}"
                f" {reduction_pct:>9.0f}%"
            )

        n = len(TEST_QUERIES)
        avg_opt = total_optimised // n
        avg_naive = total_naive // n
        avg_reduction = (1 - avg_opt / avg_naive) * 100

        print("-" * 90)
        print(f"{'Average':<40} {avg_opt:>14,} {'':>10} {avg_naive:>12,} {avg_reduction:>9.0f}%")

        print(f"\nNaive baseline:  {NAIVE_EGRESS_BYTES / 1024:.0f} KB/request (raw Travelpayouts bulk response, hardcoded estimate)")
        print(f"Optimised:       {avg_opt / 1024:.1f} KB/request to OpenAI")
        print("Retrieval egress: 0 KB (RavenDB is in-cluster — local tool calls, no network hop to measure)")
        if any_unmeasured:
            print(
                "\nWarning: openai_egress_bytes was unavailable for at least one request "
                "(agent isn't reading a tx_bytes counter on this host) -- those rows fell "
                "back to a token-count estimate, not a real measurement. Run the agent in "
                "its container/pod (see EGRESS_IFACE_STATS_PATH in src/agent/loop.py) to "
                "get real numbers."
            )
        else:
            print("\nAll rows above are measured from the agent's network interface tx_bytes counter.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run())
