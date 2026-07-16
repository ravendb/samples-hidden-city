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


def load_airport_names(store: DocumentStore, codes: list[str]) -> dict[str, dict]:
    """
    Look up city/country for IATA codes from the Airports collection.
    Codes with no matching document are simply absent from the result —
    callers must never fall back to guessing a name for them.
    """
    unique = {c.upper() for c in codes if c}
    if not unique:
        return {}

    result: dict[str, dict] = {}
    with store.open_session() as session:
        for code in unique:
            doc = session.load(f"airports/{code}")
            if doc is not None:
                d = doc_to_dict(doc)
                result[code] = {"city": d.get("city"), "country": d.get("country")}
    return result


def doc_to_dict(obj) -> dict:
    """Recursively convert a pyravendb _DynamicStructure (or plain dict) to a plain dict."""
    if isinstance(obj, dict):
        return {k: doc_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [doc_to_dict(item) for item in obj]
    if hasattr(obj, "__dict__"):
        return {
            k: doc_to_dict(v)
            for k, v in vars(obj).items()
            if not k.startswith("_")
        }
    return obj
