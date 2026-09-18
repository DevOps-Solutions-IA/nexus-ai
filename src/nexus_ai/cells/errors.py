"""Safe errors never include tenant topology or raw database failures."""

from nexus_ai.core.errors import NxsError


class PlacementRequiredError(NxsError):
    code = "NXS_CELL_PLACEMENT_REQUIRED"
    status = 409
    title = "Explicit placement required"


class PlacementFencedError(NxsError):
    code = "NXS_CELL_PLACEMENT_FENCED"
    status = 409
    title = "Placement authority is not current"


class PlacementConflictError(NxsError):
    code = "NXS_CELL_PLACEMENT_CONFLICT"
    status = 409
    title = "Placement mutation conflicts with durable state"


class CellUnavailableError(NxsError):
    code = "NXS_CELL_UNAVAILABLE"
    status = 409
    title = "Cell is not available for placement"
