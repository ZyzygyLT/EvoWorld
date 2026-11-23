---
description: Revisa una entrega de EvoWorld y emite un dictamen (APROBADA / OBSERVACIONES / CORRECCIONES / BLOQUEADA)
---

Revisa la entrega descrita en `$ARGUMENTS` (commits/diffs indicados por el usuario, o el diff actual si no se especifica nada). **No realizar ningún cambio durante esta revisión**: solo lectura e informe.

## Fuentes

1. El estado de Git y los diffs que el usuario haya proporcionado (si no, indícalo como dato faltante: no ejecutes comandos tú mismo si no puedes; usa `read`/`grep`/`glob` sobre los archivos señalados).
2. `docs/implementation/IMPLEMENTATION_STATUS.md` — alcance y estado real.
3. `docs/architecture/CONTRACTS.md` — contratos afectados por los archivos tocados.
4. `docs/architecture/DECISIONS.md` — solo las decisiones vinculadas.
5. `AGENTS.md` — reglas permanentes.

Puedes delegar la inspección de archivos a `implementation-reviewer` (agente de solo lectura) si el diff es grande.

## Checklist de revisión

1. **Alcance autorizado** — ¿los cambios están dentro de lo autorizado para la etapa? Archivos fuera de alcance = hallazgo.
2. **Archivos modificados** — lista exacta; ¿hay archivos tocados que no deberían (contratos, etapas anteriores, docs históricos)?
3. **Contratos afectados** — qué secciones de CONTRACTS.md tocan los cambios y si se respetan (contrasta contra el código, no contra el texto del informe).
4. **Pruebas ejecutadas** — resultados **exactos** (números de pasados/fallidos/skips), no "en verde".
5. **Pruebas no ejecutadas** — suites omitidas o salteadas y por qué (entorno sin `fastapi`/`httpx` = DEC07, PIL, etc.).
6. **Regresiones** — evidencia de suites de regresión ejecutadas y sus resultados.
7. **Estado de Git** — HEAD, staged, no rastreados, y que no se hayan hecho commits/resets no autorizados.

## Dictamen

Emite UNO de estos, justificado con la evidencia de los puntos 1–7:

- `APROBADA` — alcance cumplido, contratos respetados, pruebas exactas y completas en todo lo ejecutable, sin regresiones, Git limpio.
- `APROBADA CON OBSERVACIONES` — sin defectos bloqueantes, pero con desviaciones menores listadas (cada una numerada).
- `REQUIERE CORRECCIONES` — al menos un defecto confirmado (fuera de alcance, contrato violado, resultado de pruebas incoherente o regresión).
- `BLOQUEADA` — no puede evaluarse (alcance desconocido, evidencia insuficiente, entorno que impide verificar, o cambio destructivo detectado).

Distingue siempre **defecto confirmado** (con evidencia ruta:línea) de **riesgo no demostrado**. No declares pruebas ejecutadas si no consta su ejecución. No ocultes errores ni conviertas resultados parciales en éxitos.
