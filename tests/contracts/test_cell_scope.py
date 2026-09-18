"""P18 contract excludes relocation, recovery and unsupported certification claims."""

import json
from pathlib import Path

from nexus_ai.cells.contracts import CellState, PlacementOperation, PlacementState


def test_cell_scope_has_no_reassignment_or_failover_operation() -> None:
    assert set(PlacementOperation) == {"ASSIGN", "SUSPEND", "RESUME"}
    assert set(PlacementState) == {"ACTIVE", "SUSPENDED"}
    assert set(CellState) == {"REGISTERED", "RETIRED"}
    manifest = json.loads(Path(".nxs/phases/NXS-P18.json").read_text())
    non_scope = " ".join(manifest["non_scope"])
    for exclusion in ("NXS-P19", "NXS-P25", "NXS-P28", "NXS-P32", "frontend", "reassignment"):
        assert exclusion in non_scope
    assert manifest["requirements_implemented"] == ["NXS-SCALE-001"]
