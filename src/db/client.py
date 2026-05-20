import os
from typing import Optional

from pyravendb.store.document_store import DocumentStore

_store: Optional[DocumentStore] = None


def get_store() -> DocumentStore:
    global _store
    if _store is None:
        url = os.environ["RAVENDB_URL"]
        database = os.environ["RAVENDB_DATABASE"]
        _store = DocumentStore(urls=[url], database=database)
        _store.initialize()
    return _store


def reset_store() -> None:
    """For tests only — force a new store on next get_store() call."""
    global _store
    if _store is not None:
        _store.dispose()
        _store = None
