"""
Subscription worker — listens for RavenDB price drop events, no polling.

RavenDB Data Subscriptions push documents matching the query whenever they
change. The worker receives a batch, filters for significant drops, and logs
an alert (replace log.info with your notification channel: Slack, webhook, etc.).

Run as a long-lived pod alongside the agent in k8s/worker/.
"""
import logging
import os

from dotenv import load_dotenv
from ravendb.documents.subscriptions.options import (
    SubscriptionCreationOptions,
    SubscriptionOpeningStrategy,
    SubscriptionWorkerOptions,
)
from ravendb.exceptions.raven_exceptions import RavenException

load_dotenv()

from src.db.client import get_store
from src.db.seed import ensure_database

log = logging.getLogger(__name__)

SUBSCRIPTION_NAME = "hidden-city-price-drops"
MIN_SCORE_THRESHOLD = 0.5


def _create_subscription_if_missing(store) -> str:
    options = SubscriptionCreationOptions(
        query=f"from Routes where hidden_city_score > {MIN_SCORE_THRESHOLD}",
        name=SUBSCRIPTION_NAME,
    )
    try:
        return store.subscriptions.create_for_options(options)
    except RavenException as e:
        if "already in use" in str(e).lower():
            log.info("Subscription %r already exists, reusing", SUBSCRIPTION_NAME)
            return SUBSCRIPTION_NAME
        raise


def _handle_batch(batch) -> None:
    for item in batch.items:
        route = item.result
        origin = route.get("origin", "?")
        destination = route.get("destination", "?")
        via = route.get("hidden_city_via", "?")
        score = route.get("hidden_city_score", 0)
        price = route.get("typical_price", {}).get("min", "?")

        log.info(
            "PRICE DROP ALERT: %s→%s (exit at %s) | score=%.2f | from $%s",
            origin,
            destination,
            via,
            score,
            price,
        )
        # TODO: replace with real notification (Slack, webhook, push)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = get_store()
    # Runs as its own long-lived pod, independent of the agent's startup event
    # — don't assume the agent has already created the database by the time
    # this starts. Retries transient RavenDB unavailability on its own (see
    # src/db/seed.py); safe to call even if the agent already did.
    ensure_database(store)

    subscription_name = _create_subscription_if_missing(store)
    log.info("Listening on subscription: %s", subscription_name)

    # WAIT_FOR_FREE (not the OPEN_IF_FREE default): during a k8s RollingUpdate
    # the old and new pod briefly coexist, both trying to open this single
    # subscription. OPEN_IF_FREE rejects (crashes) the incoming pod instead of
    # letting it queue up, which turns a routine rollout into a crash loop.
    # WAIT_FOR_FREE makes the new pod wait for the old one to disconnect (or
    # its k8s terminationGracePeriod to end) and take over cleanly.
    worker = store.subscriptions.get_subscription_worker(
        SubscriptionWorkerOptions(
            subscription_name,
            strategy=SubscriptionOpeningStrategy.WAIT_FOR_FREE,
        )
    )
    try:
        future = worker.run(_handle_batch)
        future.result()
    except KeyboardInterrupt:
        log.info("Worker stopped")
    finally:
        worker.close()


if __name__ == "__main__":
    run()
