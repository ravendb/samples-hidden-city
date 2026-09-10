"""
Subscription worker — listens for RavenDB price drop events, no polling.

RavenDB Data Subscriptions push documents matching the query whenever they
change. The worker receives a batch, compares each route's price against the
last price it saw for that route (persisted in a pricewatchstate/ document so
it survives restarts), and on a real drop logs it and writes a PriceAlert
document. The agent (src/agent/app.py) runs its own Data Subscription on the
PriceAlerts collection and forwards each one to connected browsers over
/ws/alerts -- still push-based end to end, no polling anywhere.

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
from src.db.expiration import expires_at
from src.db.models import PriceAlert
from src.db.seed import ensure_database

log = logging.getLogger(__name__)

# Alerts are transient UI notifications, not durable history -- expire them
# instead of letting the PriceAlerts collection grow forever.
ALERT_TTL_MINUTES = 60

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


def _state_id(route_id: str) -> str:
    return f"pricewatchstate/{route_id}"


def _handle_batch(batch) -> None:
    # The subscription re-sends a route document every time it changes AND
    # matches the query, including once for every document already matching
    # on initial replay against an existing database. Neither of those is a
    # price drop -- only a lower price than the last time *we* looked is. We
    # persist that last-seen price per route (survives worker restarts) so a
    # fresh subscription replay establishes a baseline instead of alerting on
    # every pre-existing candidate.
    with batch.open_session() as session:
        for item in batch.items:
            route = item.result
            route_id = item.key
            price = route.get("typical_price", {}).get("min")

            state = session.load(_state_id(route_id))
            last_price = state.get("last_price") if state else None

            if price is not None and last_price is not None and price < last_price:
                origin = route.get("origin", "?")
                destination = route.get("destination", "?")
                via = route.get("hidden_city_via")
                score = route.get("hidden_city_score", 0)
                log.info(
                    "PRICE DROP ALERT: %s→%s (exit at %s) | score=%.2f | $%s -> $%s",
                    origin,
                    destination,
                    via or "?",
                    score,
                    last_price,
                    price,
                )
                alert = PriceAlert(
                    origin=origin,
                    destination=destination,
                    via=via,
                    hidden_city_score=score,
                    old_price=last_price,
                    new_price=price,
                    currency=route.get("typical_price", {}).get("currency", "USD"),
                )
                alert_data = alert.model_dump(mode="json")
                alert_id = f"pricealerts/{route_id.split('/', 1)[-1]}-{int(alert.created_at.timestamp())}"
                session.store(alert_data, alert_id)
                session.advanced.get_metadata_for(alert_data).update(
                    {
                        "@collection": "PriceAlerts",
                        "@expires": expires_at(ALERT_TTL_MINUTES),
                    }
                )
                # TODO: replace log.info above with a real notification channel
                # (Slack, webhook, push) in addition to the PriceAlerts document.

            if state is None:
                session.store({"last_price": price}, _state_id(route_id))
            else:
                state["last_price"] = price
        session.save_changes()


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
