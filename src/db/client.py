import os
from typing import Optional

from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, pkcs12
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
        pfx_bytes = f.read()
    try:
        private_key, certificate, _ = pkcs12.load_key_and_certificates(pfx_bytes, b"")
    except Exception as e:
        # This file was generated with openssl's `-legacy` flag (3DES/RC2
        # encryption -- see Ensure-RavenDbCerts in start-k8s.ps1) because the
        # operator can't read openssl 3.x's SHA-256 PKCS12 default. Assuming
        # `cryptography` can always read that back is the same "existence,
        # not capability" mistake the openssl -legacy provider bug was: some
        # wheels/OpenSSL backings don't support legacy-encrypted PKCS12.
        # Surface that plainly instead of the raw parse exception.
        raise RuntimeError(
            f"cryptography couldn't parse the legacy-encrypted PKCS12 file at {pfx_path!r}: {e}. "
            "This usually means the installed `cryptography` package's OpenSSL backing lacks "
            "legacy-provider support. Try `uv sync --upgrade cryptography`, or confirm with "
            "`python -c \"from cryptography.hazmat.backends.openssl.backend import backend; "
            "print(backend.openssl_version_text())\"`."
        ) from e
    with open(pem_path, "wb") as f:
        f.write(private_key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()))
        f.write(certificate.public_bytes(Encoding.PEM))
    return pem_path


def get_store() -> DocumentStore:
    global _store
    if _store is None:
        url = os.environ["RAVENDB_URL"]
        database = os.environ["RAVENDB_DATABASE"]
        store = DocumentStore(urls=[url], database=database)

        client_cert_pfx = os.environ.get("RAVENDB_CLIENT_CERT_PATH")
        if client_cert_pfx:
            store.certificate_pem_path = _pfx_to_pem(client_cert_pfx, "/tmp/ravendb-client.pem")

        ca_cert = os.environ.get("RAVENDB_CA_CERT_PATH")
        if ca_cert:
            store.trust_store_path = ca_cert

        # Only assign to the module-level cache once initialize() actually
        # succeeds -- otherwise a transient failure here (e.g. cert conversion)
        # would leave a half-initialized store cached, and every later caller
        # in this pod's lifetime would hit "did you forget calling initialize()?"
        # instead of the real underlying error.
        store.initialize()
        _store = store
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

    Uses the batched multi-document load (`session.load([...])`, one
    GetDocumentCommand for every id) rather than one `session.load(id)` call
    per code — a route with many distinct destinations (real scraped data can
    easily have 30+) would otherwise exceed RavenDB's default 30-requests-per-
    session cap on its own.

    Quirk in this client version: `session.load([single_id])` collapses to the
    bare document (or None) instead of a {id: doc} mapping when the list has
    exactly one element — only 2+ elements get the real batched dict shape. A
    single id is loaded via the singular, unambiguous form instead.
    """
    unique = {c.upper() for c in codes if c}
    if not unique:
        return {}

    ids = [f"airports/{code}" for code in unique]
    with store.open_session() as session:
        if len(ids) == 1:
            loaded = {ids[0]: session.load(ids[0])}
        else:
            loaded = session.load(ids)

    result: dict[str, dict] = {}
    for code in unique:
        doc = loaded.get(f"airports/{code}")
        if doc is not None:
            d = doc_to_dict(doc)
            result[code] = {"city": d.get("city"), "country": d.get("country")}
    return result


def load_all_airport_coords(store: DocumentStore) -> dict[str, tuple[float, float]]:
    """
    (lat, lng) for every airport document, keyed by IATA code. Static
    reference data — loaded once per scraper run and reused across every
    origin/route in that run rather than queried per-route. Backs the
    geographic hub-inference heuristic in src/scraper/hub_inference.py.
    """
    result: dict[str, tuple[float, float]] = {}
    with store.open_session() as session:
        for doc in session.query_collection("Airports").take(10_000):
            d = doc_to_dict(doc)
            iata = d.get("iata")
            coords = d.get("coordinates") or {}
            if iata and "lat" in coords and "lng" in coords:
                result[iata.upper()] = (coords["lat"], coords["lng"])
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
