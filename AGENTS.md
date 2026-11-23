# AGENTS.md — Reglas permanentes de EvoWorld

EvoWorld es un proyecto con decisiones arquitectónicas formalmente aprobadas (DEC01–DEC19) y contratos técnicos vinculantes.

## Reglas

1. **No reabrir decisiones aprobadas** sin evidencia de un defecto real.
2. **No realizar auditorías documentales generales innecesarias.**
3. **Trabajar exclusivamente dentro del alcance autorizado** por la intervención actual. No iniciar otras etapas funcionales.
4. **Preservar las modificaciones preexistentes** (rastreadas y no rastreadas). Nada de commits, reset, checkout destructivo ni limpieza de archivos sin autorización explícita.
5. **No instalar dependencias ni servidores MCP** sin autorización explícita.
6. **No modificar contratos anteriores por conveniencia.** Los contratos viven en `docs/architecture/CONTRACTS.md` y en el código que los implementa.
7. **No declarar pruebas ejecutadas si no se ejecutaron.** Distinguir siempre entre implementado, probado y aceptado.
8. **No ocultar errores ni convertir resultados parciales en éxitos.**
9. **No realizar cambios funcionales fuera de la etapa autorizada.**
10. **Consultar los documentos de arquitectura antes de implementar** y el estado de implementación antes de proponer el siguiente trabajo.

## Orden de consulta (consumo de contexto)

1. `docs/implementation/IMPLEMENTATION_STATUS.md` — estado real primero.
2. Solo las decisiones de `docs/architecture/DECISIONS.md` relacionadas con la tarea.
3. Solo las secciones pertinentes de `docs/architecture/CONTRACTS.md`.
4. Informes históricos en `docs/implementation/reports/` como evidencia, no como instrucciones.

Este archivo contiene solo reglas permanentes. El detalle vive en DECISIONS.md, CONTRACTS.md e IMPLEMENTATION_STATUS.md; no duplicarlos aquí.
