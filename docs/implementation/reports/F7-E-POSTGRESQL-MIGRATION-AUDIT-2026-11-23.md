# Auditoría de compatibilidad PostgreSQL — F7-E

**Fecha:** 2026-11-23
**Rama:** `feature/f7e-logical-delete`
**PR relacionado:** #3
**Estado:** revisión técnica aprobada; pendiente de autorización de integración.

## 1. Motivo

Se detectó que `ensure_schema()` aplicaba las migraciones explícitas únicamente a SQLite. PostgreSQL ejecutaba `Base.metadata.create_all()`, que no agrega columnas faltantes a tablas existentes.

Esto dejaba incompleta la migración de esquemas PostgreSQL preexistentes.

## 2. Cambios revisados

- Se mantiene el comportamiento específico de SQLite mediante `BEGIN IMMEDIATE`.
- PostgreSQL ejecuta las migraciones dentro de una transacción.
- Se incorpora un bloqueo advisory transaccional para serializar las migraciones concurrentes que utilizan el mismo identificador.
- Se conserva la protección de las tablas históricas F7-C: si las tablas legacy contienen registros, se rechaza su reemplazo.
- Se agregan las columnas faltantes `users.is_admin` y `worlds.deleted_at`.
- Se agrega `worlds.next_operation_sequence` cuando falta.
- Se rechaza una columna `next_operation_sequence` existente que permita NULL, en lugar de reconstruir implícitamente la tabla.
- Se repara el contador cuando está atrasado, sin retroceder uno que ya está adelantado.
- La comprobación `PRAGMA foreign_key_check` permanece limitada a SQLite.
- Se incorpora `psycopg[binary]>=3.2` a la dependencia opcional `server`.

## 3. Pruebas PostgreSQL

Se añadieron tres pruebas de integración contra PostgreSQL real, usando un esquema temporal aislado por prueba:

1. `test_postgresql_migrates_legacy_schema_and_is_idempotent`
   - Comprueba la migración de columnas faltantes.
   - Comprueba la conservación de datos existentes.
   - Comprueba la idempotencia.

2. `test_postgresql_refuses_legacy_f7c_history_and_rolls_back`
   - Comprueba que no se reemplazan tablas legacy con historial.
   - Comprueba que la transacción revierte los cambios ante el rechazo.

3. `test_postgresql_repairs_stale_counter_without_rewinding_healthy_one`
   - Comprueba la reparación del contador atrasado.
   - Comprueba que un contador adelantado no retrocede.

El fixture crea un esquema PostgreSQL temporal y lo elimina en `finally`, junto con la liberación de los motores.

## 4. Resultados de validación registrados

- `tests/test_schema_migrations.py`: 22 passed.
- Batería de eliminación lógica y regresión: 96 passed, 1 xpassed.
- Suite completa: 788 passed, 92 failed, 5 skipped, 1 xfailed, 1 xpassed.

Los fallos de la suite completa se investigaron parcialmente. Se identificaron incompatibilidades preexistentes en la base `99f55d7`, entre ellas referencias a `ChallengeOperationRow.actor_user_id` y migraciones ausentes de columnas como `visibility` y `status`.

No se atribuyen esos fallos a esta corrección sin una comparación y aislamiento específicos.

## 5. Dictamen técnico

La revisión del código de migración, las tres pruebas PostgreSQL y el ciclo de vida del fixture temporal no identificó defectos bloqueantes en el alcance revisado.

La implementación conserva el camino SQLite y añade el camino transaccional PostgreSQL.

**Dictamen:** corrección técnicamente revisada y aprobada para pasar a validación final del diff.

## 6. Límites y pendientes

- El bloqueo advisory solo coordina ejecuciones que utilicen el mismo identificador; no coordina migraciones externas que no respeten ese bloqueo.
- No se realizó una prueba específica de concurrencia entre dos procesos de migración.
- La corrección todavía no está integrada en la rama base.
- No se ha autorizado commit, push ni merge.

## 7. Próximo paso

Ejecutar la validación final del diff y solicitar autorización explícita antes de crear el commit.
