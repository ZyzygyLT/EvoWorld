# F7-E.2-I — Acta de aceptación

- **Fecha:** 2026-11-23
- **Estado:** ACEPTADO
- **Alcance:** implementación de eliminación lógica de mundos.

## Cambios aceptados

- Se incorporó la migración idempotente de `worlds.deleted_at`, nullable, para esquemas existentes.
- `complete_challenge` rechaza mundos eliminados antes de reconstruir el motor.
- Se actualizaron las pruebas de migración, desafíos y repositorio.
- Se preserva el historial y la restricción `world_highlights ON DELETE RESTRICT`.
- Se mantiene el contrato aprobado en F7-E.2-PRE: exclusión de mundos eliminados de consultas normales y recuperación; GET de mundo eliminado responde 404 incluso para su propietario.
- No se incorpora restauración ni limpieza administrativa de `deleted_at`.

## Verificación

Suite ejecutada desde el worktree `evoworld-f7e-logical-delete`, con `PYTHONPATH` apuntando a su directorio `src`:

- `test_world_deletion.py`
- `test_deleted_world_app.py`
- `test_deleted_world_challenge_prepare.py`
- `test_deleted_world_league.py`
- `test_deleted_world_recovery.py`
- `test_deleted_world_repository.py`
- `test_b35_fix4.py`
- `test_schema_migrations.py`

**Resultado:** 93 passed, 1 xpassed, 1 warning, 0 failed.

El warning corresponde a la deprecación de la integración `httpx` con `starlette.testclient`.

`git -c core.whitespace=cr-at-eol diff --check` no reportó errores de whitespace. Git mostró avisos informativos de conversión LF/CRLF en dos archivos de pruebas.

## Límites del cierre

Esta aceptación corresponde a F7-E.2-I. No declara cerrada la totalidad de F7-E ni autoriza F7-F.

La implementación se publicó en `feature/f7e-logical-delete` y se abrió el PR #3. No se realizó merge.
