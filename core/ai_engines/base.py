from typing import Protocol, runtime_checkable


@runtime_checkable
class KYCReviewer(Protocol):
    name: str
    model_version: str

    def review(
        self,
        file_bytes: bytes,
        *,
        requested_doc_type: str | None = None,
        entity_name: str | None = None,
    ) -> dict: ...
