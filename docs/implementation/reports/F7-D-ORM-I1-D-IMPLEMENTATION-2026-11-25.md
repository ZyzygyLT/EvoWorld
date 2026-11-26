# F7-D ORM I1-D — Implementación candidata

Fecha: 2026-11-25  
Rama: `feature/f7-e4-implementation`  
Estado: VALIDACION TECNICA COMPLETADA; CIERRE I1-D

## 1. Alcance ejecutado

Se implementó el flujo de idempotencia de creación de desafíos definido en `F7-D-ORM-I1-D-R0-2026-11-25.md`.

### Repositorio
- Añadido `get_challenge_creation_request(actor_user_id, client_idempotency_key)`.
- Añadido `record_challenge_creation_request(...)`, que inserta y hace `flush()` sin commit propio.
- Importado el ORM existente `ChallengeCreationRequestRow`.
- Retirados los métodos obsoletos `get_challenge_by_organizer_and_key` y `get_challenge_by_organizer_and_name`; no se encontraron callers adicionales en los archivos inspeccionados.

### Servicio
- `ChallengeService._create_challenge()` ya no accede a `ChallengeRow.client_idempotency_key` ni `ChallengeRow.request_hash`.
- La fuente de verdad es `challenge_creation_requests`, con ámbito actor + clave.
- El hash fallback se calcula con `challenge_id=0`, igual que el endpoint HTTP.
- La creación persiste desafío, request idempotente y operación original `create` en la misma transacción.
- Reintento con hash idéntico recupera el desafío y el `outcome` original de `challenge_operations`.
- Hash distinto produce `IdempotencyConflict` (mapeado por HTTP a 409).
- Si el INSERT de la request colisiona con UNIQUE, se revierte la transacción completa, se relee en sesión limpia y se retorna el ganador o se genera conflicto.
- El método revierte la sesión ante errores, incluso cuando la sesión fue suministrada externamente; el servicio conserva el límite transaccional documentado.

## 2. Pruebas anadidas y ajustes de fixtures

- Se agregaron pruebas HTTP para persistencia de idempotencia, conflicto 409 y aislamiento por actor.
- Se agregaron pruebas PostgreSQL para concurrencia con solicitudes iguales y distintas.
- Se comprobo el hash canonico de creacion con `challenge_id=0`.
- Se corrigieron fixtures de eliminacion de mundos y privacidad para respetar estados, usuarios y referencias validas.
- Los ajustes finales descritos aqui se limitaron a pruebas; no modificaron codigo de produccion.

## 3. Validacion ejecutada

Validacion local realizada en el worktree aislado de I1-D, con `EVOWORLD_TEST_POSTGRES_URL` configurada.

### Validacion especifica

Resultado: **71 passed, 0 failed, 0 skipped**.

Incluye las pruebas de concurrencia PostgreSQL, ejecutadas sin omisiones.

### Regresion ampliada

Se ejecutaron los 12 archivos seleccionados de pruebas F7-D, privacidad, eliminacion logica y migraciones.

Resultado: **146 passed, 0 failed, 0 skipped**, en 48.29 segundos.

`tests/test_f7d_api_privacy.py` tambien se ejecuto individualmente: **8 passed**.

### Limitaciones y advertencias

- `tests/test_f7d_constraints_atomicity.py` quedo fuera de la regresion porque su coleccion falla al importar `_ensure_challenge_participants_constraints`, helper eliminado durante la reorganizacion de la migracion. No se contabiliza como prueba aprobada.
- Se registro un warning de deprecacion de Starlette/httpx relacionado con `starlette.testclient`.
- No se ejecutaron migraciones sobre `worlds.db`, `evoworld_test` ni bases de produccion.
- Los resultados corresponden al worktree local de validacion y a la base PostgreSQL temporal de pruebas.

## 4. Commits de implementación

- Repositorio: `90a3ac210d9f9a9db61eb93eb629886f6e18caae`
- Servicio: `c9932bd54d9d5b33047c634bb37e7fab27f44971`
- Rollback transaccional: `f99ec36fb519a389fd778fcc0d537c0b5408c87a`
- Pruebas HTTP: `911d6949dec1fe9d08567b8146739a288706fa56`
- Limpieza de lookup obsoleto: `9f042736760d5299ed776ed9efb4d34643c2d4b4`
- Hash canónico: `0483f9544c52cb54270eba03512dfb0787c2ef1f`
- Concurrencia PostgreSQL: `b1966c1437dacc3673dd0b02581da40cbd47eb3d`
- Ajuste de fixtures HTTP: `ef280622c56e801d2ea788fcafe36f5e273c33df`

## 5. Dictamen y cierre

La implementacion I1-D y sus pruebas especificas quedaron validadas. La regresion ampliada seleccionada obtuvo 146 resultados aprobados, sin fallos ni omisiones.

Se conserva como limitacion conocida la prueba `test_f7d_constraints_atomicity.py`, excluida por su importacion obsoleta; no se declara que ese archivo haya pasado.

Los cambios finales de fixtures y este reporte documentan el cierre de I1-D. No se realizaron cambios de contrato ni migraciones sobre bases reales.
