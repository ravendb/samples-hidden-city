"""
Document Expiration — price fields auto-expire after 20 min via @expires
metadata, so stale route documents are purged by RavenDB itself with no
housekeeping job.

pyravendb has no built-in ConfigureExpirationOperation, so it's implemented
here the same way the rest of pyravendb's admin operations are: a thin
RavenCommand against the REST endpoint the official clients use.
"""
from datetime import datetime, timedelta, timezone

from pyravendb.commands.raven_commands import RavenCommand
from pyravendb.custom_exceptions import exceptions
from pyravendb.raven_operations.maintenance_operations import MaintenanceOperation

PRICE_TTL_MINUTES = 20


class ConfigureExpirationOperation(MaintenanceOperation):
    """Turns on the Expiration feature for the database (safe to call repeatedly)."""

    def get_command(self, conventions):
        return self._ConfigureExpirationCommand()

    class _ConfigureExpirationCommand(RavenCommand):
        def __init__(self):
            super().__init__(method="POST", is_raft_request=True)

        def create_request(self, server_node):
            self.url = f"{server_node.url}/databases/{server_node.database}/admin/expiration/config"
            self.data = {"Disabled": False, "DeleteFrequencyInSec": 60}

        def set_response(self, response):
            try:
                response = response.json()
                if "Error" in response:
                    raise exceptions.InvalidOperationException(
                        response["Message"], response["Type"], response["Error"]
                    )
            except ValueError:
                response.raise_for_status()


def expires_at(minutes: int = PRICE_TTL_MINUTES) -> str:
    """RavenDB expects @expires as an ISO 8601 UTC timestamp string."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def ensure_expiration_enabled(store) -> None:
    store.maintenance.send(ConfigureExpirationOperation())
