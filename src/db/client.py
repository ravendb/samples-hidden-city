import os
from typing import Optional

import OpenSSL.crypto
from ravendb import DocumentStore

_store: Optional[DocumentStore] = None


def _pfx_to_pem(pfx_path: str, pem_path: str) -> str:
    """Convert a PKCS#12 client certificate (as issued by RavenDB's own setup
    wizard — see k8s/ravendb/values.yaml) to the combined cert+key PEM file
    the ravendb client's certificate_pem_path expects. Idempotent: skips the
    conversion if pem_path is already there from a previous call in this pod's
    lifetime."""
    if os.path.exists(pem_path):
        return pem_path
    with open(pfx_path, "rb") as f:
        p12 = OpenSSL.crypto.load_pkcs12(f.read(), b"")
    with open(pem_path, "wb") as f:
        f.write(OpenSSL.crypto.dump_privatekey(OpenSSL.crypto.FILETYPE_PEM, p12.get_privatekey()))
        f.write(OpenSSL.crypto.dump_certificate(OpenSSL.crypto.FILETYPE_PEM, p12.get_certificate()))
    return pem_path


def get_store() -> DocumentStore:
    global _store
    if _store is None:
        url = os.environ["RAVENDB_URL"]
        database = os.environ["RAVENDB_DATABASE"]
        _store = DocumentStore(urls=[url], database=database)

        client_cert_pfx = os.environ.get("RAVENDB_CLIENT_CERT_PATH")
        if client_cert_pfx:
            _store.certificate_pem_path = _pfx_to_pem(client_cert_pfx, "/tmp/ravendb-client.pem")

        ca_cert = os.environ.get("RAVENDB_CA_CERT_PATH")
        if ca_cert:
            _store.trust_store_path = ca_cert

        _store.initialize()
    return _store


def reset_store() -> None:
    """For tests only — force a new store on next get_store() call."""
    global _store
    if _store is not None:
        _store.close()
        _store = None


def put_document(store: DocumentStore, key: str, data: dict) -> None:
    """Writes `data` (a plain dict, typically from `model.model_dump(mode='json')`)
    under `key`. `data` may carry an `@metadata` key (e.g. {"@collection": ...,
    "@expires": ...}) — those fields are moved onto the session's real metadata
    object before saving, since the modern client's session.store() doesn't read
    metadata out of the document body the way the old PutDocumentCommand did."""
    metadata_overrides = data.pop("@metadata", {})
    with store.open_session() as session:
        session.store(data, key)
        session.advanced.get_metadata_for(data).update(metadata_overrides)
        session.save_changes()


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
    """Recursively convert a RavenDB dynamic document object (or plain dict) to a plain dict."""
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
