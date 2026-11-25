# F7-E.3 — Matriz de cobertura y especificación de pruebas

**Fecha:** 2026-11-24  
**Rama de referencia:** `feature/f7-e4-implementation`  
**HEAD inspeccionado:** `89cbbceb279257c234d7e99a453c63f67e2ac7fd`  
**Estado:** especificación documental con implementación parcial de pruebas añadida el 2026-11-24; ejecución pendiente.

## 1. Objetivo

Separar lo que ya está demostrado por los tests inspeccionados de lo que conviene completar antes de la aceptación formal de F7-E.3. Esta matriz no autoriza cambios de código ni amplía el alcance funcional.

## 2. Cobertura existente identificada

| ID | Caso | Archivo / prueba | Evidencia observada |
|---|---|---|---|
| E3-S01 | Candidato de checkpoint más reciente corrupto y candidato anterior íntegro | `test_world_highlight_service.py::test_grant_uses_newest_intact_checkpoint_without_mutating_candidates` | La concesión prospera pese a que seq=2 tiene checksum incorrecto y seq=1 es íntegro; se comprueba que ambos siguen `is_valid=1`. |
| E3-S02 | Rechazo si ningún checkpoint candidato coincide con checksum | `test_world_highlight_service.py::test_world_without_intact_checkpoint_is_rejected` | La concesión falla con `WorldNotEligible`. |
| E3-S03 | Rechazo de mundo privado, cerrado, eliminado o sin propietario | `test_world_highlight_service.py::test_ineligible_world_is_rejected` | Parametriza cuatro precondiciones. |
| E3-S04 | Una concesión activa no vencida impide otra concesión | `test_world_highlight_service.py::test_grant_rejects_existing_unexpired_highlight` | El servicio lanza `ActiveHighlightExists`. |
| E3-S05 | Reemplazo de fila activa vencida sin falsificar revocación | `test_world_highlight_service.py::test_grant_closes_expired_row_without_faking_revocation` | La fila anterior queda inactiva, con campos de revocación NULL, y la nueva queda activa. |
| E3-S06 | Concesiones concurrentes serializadas en SQLite | `test_world_highlight_service.py::test_concurrent_grants_serialize_to_one_active_highlight` | Dos concesiones concurrentes producen una sola fila activa. |
| E3-S07 | Retirada auditable y conservación de historial | `test_world_highlight_service.py::test_revoke_is_auditable_and_keeps_history` | Registra instante y administrador de retirada; conserva la fila. |
| E3-A01 | Admin puede conceder; duplicado responde 409; retirada funciona | `test_world_highlight_api.py::test_admin_grant_duplicate_conflict_public_projection_and_revoke` | Cubre ciclo API y consulta administrativa posterior. |
| E3-A02 | Anónimo 401, usuario ordinario 403, duración inválida y motivo vacío 400 | `test_world_highlight_api.py::test_highlight_admin_authorization_and_duration_validation` | Cubre autoridad y validación de entrada. |
| E3-A03 | Mundo sin checkpoint íntegro no se destaca | `test_world_highlight_api.py::test_ineligible_world_cannot_be_highlighted` | Responde 400 y no crea fila de destacado. |
| E3-A04 | Retirada sin fila activa responde 404 | `test_world_highlight_api.py::test_unhighlight_without_active_row_is_404` | Comprueba el caso sin destacado activo. |
| E3-A05 | Propietario administrador no puede destacar su propio mundo | `test_world_highlight_api.py::test_world_owner_cannot_highlight_own_world_even_if_admin` | Responde 400. |

La prueba E3-S01 sí demuestra el fallback operativo: el checkpoint de mayor secuencia tiene checksum inválido, el anterior tiene checksum íntegro y la concesión se completa. No inspecciona una identidad de checkpoint seleccionada porque el servicio no devuelve esa identidad; su aserción relevante es la aceptación del mundo y la ausencia de mutación.

## 3. Casos recomendados para cerrar brechas

Prioridad P0: completar antes de aceptar F7-E.3 salvo aceptación explícita de la limitación.

| ID | Caso propuesto | Tipo | Criterio de aceptación |
|---|---|---|---|
| E3-P01 | Propietario inexistente en una fixture controlada | Servicio | Se rechaza con `WorldNotEligible`; no se crea destacado. Respetar las FK y aislar la fixture sin debilitar el esquema. |
| E3-P02 | Estado `paused` elegible | Servicio/API | La concesión se acepta con propietario válido y checkpoint íntegro. |
| E3-P03 | Estados distintos de `running` y `paused` | Servicio | Cada estado no permitido se rechaza. Incluir al menos `closed`; ampliar solo a valores admitidos por el dominio. |
| E3-P04 | Checkpoint con `is_valid=0` aunque el checksum coincida | Servicio | No se considera candidato. |
| E3-P05 | Payload alterado con checksum original | Servicio | Se detecta la corrupción y no se concede usando ese candidato. |
| E3-P06 | Varios candidatos corruptos seguidos de uno íntegro | Servicio | Se continúa descendiendo hasta hallar el primer íntegro; se acepta el mundo. |
| E3-P07 | Verificación estrictamente no destructiva | Servicio | Capturar antes/después `payload`, `checksum`, `is_valid`, `sequence` y `checkpoint_version` de todos los candidatos; deben permanecer idénticos. |
| E3-P08 | No invocar rutas de recuperación o repositorio mutante | Servicio | Espías o sustitutos que fallen si se invocan `CheckpointRepository.get_latest_valid_checkpoint()` o `RecoveryManager.recover()`; la concesión válida no debe llamarlos. |
| E3-P09 | Concesión concurrente contra PostgreSQL real | Integración | Dos transacciones/procesos compiten por el mismo mundo; al final hay exactamente una fila activa y el perdedor recibe conflicto. |
| E3-P10 | Concesión y retirada concurrentes sobre el mismo mundo | Integración | El resultado respeta la serialización por fila del mundo; nunca hay más de una fila activa ni se pierde el registro histórico. |

Prioridad P1: completar si se busca cobertura administrativa integral.

| ID | Caso propuesto | Tipo | Criterio de aceptación |
|---|---|---|---|
| E3-P11 | Ruta de consulta de detalle: anónimo y usuario ordinario | API | 401 y 403 respectivamente. |
| E3-P12 | Consulta de detalle inexistente | API | Administrador recibe 404. |
| E3-P13 | Retirada: anónimo y usuario ordinario | API | 401 y 403 respectivamente; no se altera el registro activo. |
| E3-P14 | Retirada por administrador y comprobación de actor | API/servicio | `revoked_by_admin_user_id` coincide con el administrador autenticado, no con datos del cliente. |
| E3-P15 | Mundo inexistente al conceder | API | Documentar y fijar el comportamiento actual: 400 por `WorldNotEligible`; no introducir 404 sin DEC/cambio de contrato. |
| E3-P16 | Duración no entera, desbordamiento de calendario y límites UTC | Servicio/API | Rechazo controlado sin filas parciales; fechas calculadas en UTC y meses calendario conforme al servicio. |

## 4. Cobertura por entorno

- SQLite: existe prueba concurrente de concesión con dos workers y base temporal.
- PostgreSQL: se añadieron pruebas de concurrencia real en `tests/test_world_highlight_postgresql.py`; su ejecución sigue pendiente de configurar `EVOWORLD_TEST_POSTGRES_URL`.
- No se debe inferir que una prueba SQLite demuestra el comportamiento de bloqueo de PostgreSQL.
- Las pruebas PostgreSQL de migraciones existentes no sustituyen las pruebas de concurrencia del servicio de destacados.
- Las pruebas de API usan TestClient y SQLite temporal; no constituyen evidencia de despliegue ni de concurrencia entre procesos.

## 5. Orden sugerido de ejecución cuando se autorice implementar pruebas

1. Ejecutar primero los tests existentes de servicio, API y esquema para establecer línea base.
2. Incorporar E3-P01 a E3-P08 como pruebas de dominio/servicio.
3. Incorporar E3-P11 a E3-P16 como pruebas API/servicio según corresponda.
4. Ejecutar `tests/test_world_highlight_postgresql.py` con `EVOWORLD_TEST_POSTGRES_URL` apuntando exclusivamente a la base `evoworld_test`; el fixture crea un esquema aleatorio por test y elimina únicamente ese esquema. No simular PostgreSQL con SQLite.
5. Ejecutar regresión focalizada F7-E y reportar por separado cualquier fallo preexistente fuera del alcance.

No ejecutar ni alterar tests, fixtures, esquema o datos hasta recibir autorización específica para la fase de implementación de pruebas.

## 6. Criterio de cierre

F7-E.3 podrá proponerse para aceptación formal cuando:
- la autoridad y elegibilidad coincidan con el contrato consolidado;
- las pruebas P0 estén aprobadas o sus omisiones sean aceptadas explícitamente;
- la verificación no destructiva esté demostrada con aserciones de estado antes/después;
- la cobertura de concurrencia PostgreSQL se ejecute si PostgreSQL forma parte del entorno soportado de producción;
- el resultado de regresión se comunique sin atribuir a F7-E fallos de otras etapas sin aislamiento causal.

Esta matriz es una especificación, no un acta de ejecución ni una aceptación de F7-E.3.


## 7. Estado de implementación de pruebas — 2026-11-24

Se añadieron pruebas de servicio en `tests/test_world_highlight_service.py` para:
- E3-P01: propietario inexistente mediante fixture huérfana confinada a la BD temporal, sin cambiar el esquema.
- E3-P02: mundo `paused` elegible.
- E3-P04: candidatos `is_valid=0` ignorados aunque su checksum coincida.
- E3-P05: payload alterado con checksum original rechazado.
- E3-P06: varios candidatos corruptos seguidos de uno íntegro.
- E3-P07: snapshot antes/después de payload, checksum, is_valid, sequence y checkpoint_version.
- E3-P08: espías que fallan si se invocan `CheckpointRepository.get_latest_valid_checkpoint()` o `RecoveryManager.recover()`.

E3-P03 ya estaba cubierto por `test_ineligible_world_is_rejected` con estado `closed`; no se duplicó.

**Pendiente de entorno:** E3-P09 y E3-P10 requieren PostgreSQL real y una base de integración exclusiva, aislada y desechable. No se añadieron pruebas que puedan conectarse accidentalmente a una base compartida o de producción. Antes de implementarlas se debe proporcionar/configurar un DSN explícitamente dedicado a tests y definir su política de creación/limpieza.

**Estado de ejecución:** las pruebas nuevas todavía no se han ejecutado. Los commits de edición documental/código de pruebas no constituyen evidencia de aprobación. F7-E.3 permanece sin aceptación final hasta ejecutar la batería y resolver o aceptar expresamente la limitación PostgreSQL.


## 8. Implementación PostgreSQL P09/P10 — 2026-11-24

Se añadió `tests/test_world_highlight_postgresql.py`, sin cambios en código de producción:

- E3-P09: dos concesiones concurrentes para el mismo mundo; se exige exactamente una concesión exitosa y un conflicto, con una única fila activa.
- E3-P10: concesión y retirada concurrentes sobre el mismo mundo, partiendo de un destacado activo; se comprueba que la retirada conserva actor e instante, que no se pierde historial y que nunca queda más de una fila activa. Se aceptan los dos órdenes seriales posibles.
- Cada prueba utiliza un esquema PostgreSQL aleatorio e independiente.
- El fixture exige `EVOWORLD_TEST_POSTGRES_URL`, valida el motor PostgreSQL y rechaza cualquier base cuyo nombre no sea exactamente `evoworld_test`.
- El fixture no lee `WORLDSIM_DB`; elimina exclusivamente el esquema aleatorio que creó.
- El motor de prueba se libera antes de ejecutar la limpieza del esquema.

**Commit de implementación:** `4fa24626d2a069fd142ad2b6323936ced713107d`.

**Estado:** implementado, todavía no ejecutado. La ejecución y validación de resultados siguen pendientes; no se declara P09/P10 aprobados ni F7-E.3 aceptada.
