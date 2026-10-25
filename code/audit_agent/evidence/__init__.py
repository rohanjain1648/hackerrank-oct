"""Evidence ingestion: every manifest file becomes a list of Observations."""

from .store import EvidenceStore, load_evidence

__all__ = ["EvidenceStore", "load_evidence"]
