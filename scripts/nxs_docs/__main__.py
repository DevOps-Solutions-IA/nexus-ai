"""Fail-closed synchronization of documentation with machine-readable NXS state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scripts.nxs_control.core import ControlError, repository_root

START = "<!-- NXS:DOC_STATUS_START -->"
END = "<!-- NXS:DOC_STATUS_END -->"
DOCUMENTS = ("README.md", "infrastructure/README.md")


def _load(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    registry = json.loads((root / ".nxs/phase-registry.json").read_text())
    state = json.loads((root / ".nxs/project-state.json").read_text())
    phases = {item["id"]: item for item in registry["phases"]}
    if len(phases) != len(registry["phases"]):
        raise ControlError("duplicate phase in documentation registry")
    if not set(state["completed_phases"]).issubset(phases):
        raise ControlError("documentation references an unknown completed phase")
    return phases, state


def render_block(root: Path, filename: str) -> str:
    if filename not in DOCUMENTS:
        raise ControlError(f"unmanaged documentation: {filename}")
    phases, state = _load(root)
    next_phase = state["next_allowed_execution"]
    authority = (
        "> Generado desde `.nxs/phase-registry.json` y `.nxs/project-state.json`. "
        "**Representa el estado de esta revisión**, no necesariamente de `main` "
        "cuando se consulta una PR. No acredita merge ni certificación externa."
    )
    ready = sum(item["status"] == "READY" and item["decision"] == "GO" for item in phases.values())
    summary = (
        f"**Fases READY/GO:** {ready} de {len(phases)}. "
        f"**Próxima fase permitida:** `{next_phase['phase']}` "
        f"(`{next_phase['condition']}`)."
    )
    lines = [authority, "", summary, ""]
    if filename == "README.md":
        lines += ["| Fase | Capacidad | Estado |", "|---|---|---|"]
        for key, phase in sorted(phases.items()):
            lines.append(
                f"| `{key}` | {phase['title']} | `{phase['status']} / {phase['decision']}` |"
            )
        lines += [
            "",
            "**Límite:** READY/GO de una fase no certifica capacidad, "
            "proveedores reales, failover ni despliegue.",
        ]
    else:
        lines += ["| Área | Fase | Estado |", "|---|---|---|"]
        for key, area in (
            ("NXS-P11", "Telefonía/ARI"),
            ("NXS-P18", "Placement"),
            ("NXS-P19", "SIP/Kamailio"),
            ("NXS-P20", "Sentinel"),
            ("NXS-P21", "Compliance"),
            ("NXS-P22", "Auditoría"),
            ("NXS-P28", "Capacidad"),
            ("NXS-P29", "Failover"),
            ("NXS-P30", "Certificación backend"),
            ("NXS-P31", "Release"),
            ("NXS-P32", "Despliegue"),
        ):
            phase = phases[key]
            lines.append(f"| {area} | `{key}` | `{phase['status']} / {phase['decision']}` |")
        lines += [
            "",
            "Los fixtures y configuraciones de referencia **no constituyen "
            "infraestructura de producción desplegada**. La autorización humana, "
            "el artefacto verificable y las evidencias operativas son requisitos separados.",
        ]
    return START + "\n" + "\n".join(lines) + "\n" + END


def synchronize(root: Path, *, write: bool = False) -> None:
    for filename in DOCUMENTS:
        path = root / filename
        original = path.read_text(encoding="utf-8")
        if original.count(START) != 1 or original.count(END) != 1:
            raise ControlError(f"{filename}: expected one documentation status block")
        begin, end = original.index(START), original.index(END)
        if begin >= end:
            raise ControlError(f"{filename}: invalid documentation marker order")
        updated = original[:begin] + render_block(root, filename) + original[end + len(END) :]
        if updated != original:
            if write:
                path.write_text(updated, encoding="utf-8")
            else:
                raise ControlError(
                    f"{filename}: stale status; run 'uv run python -m scripts.nxs_docs --write'"
                )


def main() -> int:
    parser = argparse.ArgumentParser(description="Check or sync NXS documentation state")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        synchronize(repository_root(), write=args.write)
    except (ControlError, KeyError, ValueError) as exc:
        print(f"NXS_DOC_STATUS=FAIL: {exc}")
        return 1
    print("NXS_DOC_STATUS=UPDATED" if args.write else "NXS_DOC_STATUS=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
