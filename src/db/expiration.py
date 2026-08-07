"""
Document Expiration — price fields auto-expire after 20 min via @expires
metadata, so stale route documents are purged by RavenDB itself with no
housekeeping job.
"""
from datetime import datetime, timedelta, timezone

from ravendb.documents.operations.expiration.configuration import ExpirationConfiguration
from ravendb.documents.operations.expiration.operations import ConfigureExpirationOperation

PRICE_TTL_MINUTES = 20

# The license on this instance rejects a delete-sweep frequency below 36h;
# the 20-min @expires TTL on documents still applies immediately at query
# time regardless of how often the physical cleanup sweep runs.
EXPIRATION_DELETE_FREQUENCY_SEC = 36 * 60 * 60


def expires_at(minutes: int = PRICE_TTL_MINUTES) -> str:
    """RavenDB expects @expires as an ISO 8601 UTC timestamp string."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def ensure_expiration_enabled(store) -> None:
    store.maintenance.send(
        ConfigureExpirationOperation(
            ExpirationConfiguration(
                disabled=False,
                delete_frequency_in_sec=EXPIRATION_DELETE_FREQUENCY_SEC,
            )
        )
    )
