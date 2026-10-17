import uuid


def new_id(prefix: str, length: int = 12) -> str:
    """Prefixed opaque IDs (pat_..., wf_...), kept from the Mongo era so API contracts don't change."""
    return f"{prefix}_{uuid.uuid4().hex[:length]}"
