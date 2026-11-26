# F7-D ORM I1-D — Implementación candidata

Fecha: 2026-11-25  
Rama: `feature/f7-e4-implementation`  
Estado: **IMPLEMENTACIÓN EN RAMA — VALIDACIÓN PENDIENTE; NO ACEPTADA TODAVÍA.**

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

## 2. Pruebas añadidas/modificadas

- `tests/test_f7d_challenges.py`: hash fallback de creación comprobado contra la representación canónica con `challenge_id=0`.
- `tests/test_f7d_idempotency_http.py`:
  - primera creación persiste request + operación con el mismo hash;
  - misma clave y payload distinto devuelve HTTP 409 sin duplicar filas;
  - misma clave en actores distintos crea solicitudes independientes.
- Nuevo `tests/test_f7d_creation_idempotency_postgresql.py`:
  - carrera concurrente con mismo payload: ambos reciben el mismo desafío y queda una sola creación;
  - carrera concurrente con payload distinto: un ganador y un conflicto, sin filas parciales.
  - Usa un schema PostgreSQL temporal y se omite si `EVOWORLD_TEST_POSTGRES_URL` no está configurada.

## 3. Límites de validación

**No se ejecutaron pruebas en este entorno.** La conexión GitHub disponible permite leer/escribir el repositorio, pero no ejecutar su suite ni acceder al entorno local del usuario. La consulta de ejecuciones CI asociadas al commit no encontró workflow runs.

Por tanto:
- No se declara ningún test como pasado.
- No se declara I1-D aceptada.
- La validación SQLite + PostgreSQL y la regresión F7-D siguen pendientes.
- No se ejecutaron migraciones sobre `worlds.db`, `evoworld_test` ni producción.

## 4. Commits de implementación

- Repositorio: `90a3ac210d9f9a9db61eb93eb629886f6e18caae`
- Servicio: `c9932bd54d9d5b33047c634bb37e7fab27f44971`
- Rollback transaccional: `f99ec36fb519a389fd778fcc0d537c0b5408c87a`
- Pruebas HTTP: `911d6949dec1fe9d08567b8146739a288706fa56`
- Limpieza de lookup obsoleto: `9f042736760d5299ed776ed9efb4d34643c2d4b4`
- Hash canónico: `0483f9544c52cb54270eba03512dfb0787c2ef1f`
- Concurrencia PostgreSQL: `b1966c1437dacc3673dd0b02581da40cbd47eb3d`
- Ajuste de fixtures HTTP: `ef280622c56e801d2ea788fcafe36f5e273c33df`

## 5. Siguiente gate

Ejecutar en el worktree de validación aislado:

```powershell
git fetch origin feature/f7-e4-implementation
git checkout --detach origin/feature/f7-e4-implementation
$env:EVOWORLD_TEST_POSTGRES_URL = "postgresql+psycopg://evoworld_i1c:i1c_validation_only_2026@127.0.0.1:55432/evoworld_i1c_test"
pytest -q tests/test_f7d_challenges.py tests/test_f7d_idempotency_http.py tests/test_f7d_creation_idempotency_postgresql.py tests/test_schema_migrations.py
```

Después de revisar el resultado, corregir cualquier fallo y ejecutar la regresión ampliada antes de declarar I1-D aceptada.
