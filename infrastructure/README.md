# Infrastructure

## Estado de infraestructura en esta revisión

<!-- NXS:DOC_STATUS_START -->
> Generado desde `.nxs/phase-registry.json` y `.nxs/project-state.json`. **Representa el estado de esta revisión**, no necesariamente de `main` cuando se consulta una PR. No acredita merge ni certificación externa.

**Fases READY/GO:** 22 de 33. **Próxima fase permitida:** `NXS-P22` (`DEPENDENCIES_READY`).

| Área | Fase | Estado |
|---|---|---|
| Telefonía/ARI | `NXS-P11` | `READY / GO` |
| Placement | `NXS-P18` | `READY / GO` |
| SIP/Kamailio | `NXS-P19` | `READY / GO` |
| Sentinel | `NXS-P20` | `READY / GO` |
| Compliance | `NXS-P21` | `READY / GO` |
| Auditoría | `NXS-P22` | `PLANNED / PENDING` |
| Capacidad | `NXS-P28` | `PLANNED / PENDING` |
| Failover | `NXS-P29` | `PLANNED / PENDING` |
| Certificación backend | `NXS-P30` | `PLANNED / PENDING` |
| Release | `NXS-P31` | `PLANNED / PENDING` |
| Despliegue | `NXS-P32` | `PLANNED / PENDING` |

Los fixtures y configuraciones de referencia **no constituyen infraestructura de producción desplegada**. La autorización humana, el artefacto verificable y las evidencias operativas son requisitos separados.
<!-- NXS:DOC_STATUS_END -->

## Qué existe y qué falta desplegar

- `compose.yaml` implementa dependencias locales de PostgreSQL, Valkey
  y NATS/JetStream para desarrollo y validaciones aisladas.
- El contrato Asterisk/ARI de P11, el placement de P18 y SIP/Kamailio de
  P19 disponen de implementación y/o fixtures ejecutables.
  `infrastructure/kamailio/` contiene configuraciones de referencia y
  artefactos para pruebas reales del protocolo.
- **No existe evidencia de que esas configuraciones constituyan una instalación
  productiva de Asterisk, Kamailio o una flota de Cells.**
- La gestión de flota, objetivos de capacidad, failover, backups y
  despliegue productivo requieren sus fases posteriores y aprobaciones.

Los resultados de implementación, las pruebas de integración y los
despliegues de infraestructura son hitos separados. Los estados
históricos de P19 se conservan en `.nxs/evidence/NXS-P19/`.

Véanse `docs/adr/0100-sip-edge-routing-authority.md`,
`docs/engineering/nxs-p19-sip-edge-scaling-design.md` y
`docs/runbooks/oracle-a1-host.md` (prerrequisitos, no prueba de despliegue).
