# F7-E.3 — Auditoría final de evidencia

**Fecha:** 2026-11-24  
**Rama:** `feature/f7-e4-implementation`  
**Estado:** auditoría técnica focalizada completada; aceptación formal pendiente del usuario.

## 1. Alcance

Esta auditoría contrasta el contrato F7-E.3 con la implementación inspeccionada y los resultados de pruebas comunicados por el usuario. No autoriza ni realiza cambios de código de producción, no amplía el alcance a F7-D Nivel 2 y no declara limpia la regresión global.

## 2. Contrato inspeccionado

La implementación revisada conserva las reglas acordadas:

- Solo una cuenta con `is_admin` puede operar los endpoints administrativos; `user_token` no concede autoridad.
- El mundo debe ser público, no eliminado, tener propietario existente y estar en estado `running` o `paused`.
- El propietario no puede ser el mismo administrador que concede el destacado.
- La elegibilidad requiere un checkpoint candidato `is_valid=true` cuyo payload coincida con su SHA-256, examinando candidatos por `sequence DESC`.
- La inspección es de solo lectura: no invalida checkpoints ni utiliza rutas de recuperación/mutación.
- La concesión/retirada conserva historial y la serialización por mundo evita más de un destacado activo.

## 3. Evidencia ejecutada

| Batería | Resultado comunicado |
|---|---:|
| `tests/test_world_highlight_service.py`, `tests/test_world_highlight_api.py`, `tests/test_world_highlights_schema.py` | **24 passed**, 1 warning, 10.70s |
| `tests/test_world_highlight_postgresql.py` (P09/P10, PostgreSQL 16) | **2 passed**, 2.45s |
| **Total de casos en ejecuciones focalizadas separadas** | **26 passed** |

El warning fue `StarletteDeprecationWarning` relacionado con `httpx` y `starlette.testclient`; no hubo fallos en la batería reportada.

P09 verificó dos concesiones concurrentes, con exactamente una exitosa y una en conflicto, y una sola fila activa. P10 verificó concesión y retirada concurrentes, conservando historial y limitando a una la cantidad de filas activas.

El fixture PostgreSQL exige `EVOWORLD_TEST_POSTGRES_URL`, valida el motor y el nombre de base `evoworld_test`, crea un esquema aleatorio por prueba, no lee `WORLDSIM_DB` y elimina exclusivamente el esquema creado por sí mismo.

## 4. Cobertura y límites

Los P0 de elegibilidad, verificación de checkpoint, no mutación, exclusión de rutas de recuperación y concurrencia están cubiertos por las pruebas inspeccionadas y ejecutadas. P03 ya estaba cubierto por el caso parametrizado de mundo `closed`.

Los casos P1 administrativos adicionales enumerados en la matriz no se declaran completos ni fueron condición de esta validación P0. Los resultados corresponden a ejecuciones focalizadas separadas; no sustituyen una regresión global. Fallos históricos de otras áreas no se atribuyen a F7-E.3 sin aislamiento causal.

## 5. Cambios realizados en esta actualización

- Se actualizó `F7-E.3-TEST-MATRIX-2026-11-24.md` con los resultados reales de 24 y 2 pruebas, la configuración PostgreSQL utilizada, el warning y los límites de cobertura.
- Se añadió este informe de auditoría.
- No se modificó código de producción, fixtures ni datos de la aplicación en esta actualización documental.

## 6. Dictamen

**Dictamen técnico:** la evidencia focalizada P0 y P09/P10 satisface los criterios de validación definidos para F7-E.3. No se identifica en esta auditoría una brecha P0 pendiente que requiera modificar producción.

**Decisión de producto:** F7-E.3 queda preparada para aceptación formal del usuario. Este informe no se autoatribuye esa aceptación. No se creó PR ni se realizó merge.
