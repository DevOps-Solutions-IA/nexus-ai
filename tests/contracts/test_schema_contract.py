from pathlib import Path

from scripts.nxs_control.core import validate_all_schemas, validate_invariants


def test_repository_control_documents_are_valid() -> None:
    root = Path(__file__).parents[2]
    validate_all_schemas(root)
    validate_invariants(root)
