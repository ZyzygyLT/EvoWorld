# F7-E — Reconciliación de roadmap y contrato F7-E.3

**Fecha:** 2026-11-24  
**Repositorio:** `ZyzygyLT/EvoWorld`  
**Rama:** `feature/evoworld-review`  
**HEAD auditado antes de esta actualización:** `6b3605d7c2d647bfddaa3c7755e808f85bbde5a7`  
**Tipo:** actualización documental; sin implementación.

## 1. Propósito

Consolidar en el repositorio las decisiones vigentes de F7-E, diferenciar contratos aceptados de implementación demostrada y evitar que la propuesta histórica R0 se use como contrato posterior.

## 2. Estado por subetapa

| Subetapa | Estado | Observación |
|---|---|---|
| F7-E.1 — destacados | Contrato aceptado | No se amplía aquí el estado de implementación sin evidencia verificable. |
| F7-E.2-I — eliminación lógica | Implementada y aceptada; PR #3 fusionado | No reabrir. |
| F7-E.3 — autoridad y elegibilidad | Contrato aceptado | Esta auditoría no encontró en la rama un documento de aceptación específico ni evidencia suficiente para afirmar implementación. |
| F7-E.4 | Pendiente de definición | No implementar todavía. |

## 3. Contrato aceptado de F7-E.3

La concesión y retirada de destacados queda reservada a administradores. Un mundo solo es elegible si cumple simultáneamente estas condiciones:

- Es público.
- Tiene `owner_user_id` no nulo.
- No está eliminado lógicamente.
- Su estado es `running` o `paused`.
- Su checkpoint tiene `is_valid = true` y checksum válido.

La evaluación debe ser estrictamente no destructiva. No puede ejecutar `RecoveryManager.recover()`, alterar el estado del mundo, avanzar la simulación ni iniciar procesos de recuperación como efecto colateral.

La retirada o expiración de un destacado conserva el historial. La operación no debe borrar el registro histórico ni convertir la retirada en eliminación física.

## 4. Precedencia sobre R0

`F7-E-R0-DEF.IN.md` es una propuesta contractual preliminar. Su secuencia F7-E.1–F7-E.6 y sus propuestas de un campo booleano `highlighted`, elegibilidad automática y destacados independientes de la visibilidad anteceden a las decisiones posteriores.

Por tanto:

1. Las decisiones aceptadas posteriores prevalecen sobre las propuestas de R0.
2. Destacar no concede acceso ni cambia la visibilidad; F7-E.3 exige que el mundo sea público.
3. No se debe reemplazar el historial de destacados por un booleano que pierda concesiones/retiros anteriores.
4. No se debe borrar historial al retirar o expirar.
5. La numeración de R0 no redefine por sí sola las subetapas ya aceptadas.

## 5. Base documental y rama

La rama de trabajo auditada es `feature/evoworld-review`, con PR #3 (F7-E.2-I) y PR #4 (corrección de privacidad F7-D) fusionados. El HEAD auditado estaba un commit por delante del merge del PR #4, con una actualización documental de auditoría.

Esta actualización no modifica la historia de PR #3/#4 ni reescribe el documento R0.

## 6. Límites de esta entrega

- Solo se actualiza documentación.
- No se modifica código, pruebas, migraciones, esquema ni base de datos.
- No se declara F7-E.3 implementada o probada por el solo hecho de registrar su contrato.
- F7-E.4 queda pendiente de definición y aceptación independiente.
- F7-E.1 y F7-E.2-I permanecen cerradas/aceptadas y no se reabren.

## 7. Siguiente paso

Definir F7-E.4 como contrato separado, contrastándolo con F7-E.1, F7-E.2-I y F7-E.3. No iniciar implementación hasta que ese contrato sea aceptado explícitamente.
