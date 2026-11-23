# F7-E.2-I-B3-6-PRE-FIX3 — Auditoría final de concurrencia, ORM y autorización

**Fecha:** 20/11/2026  
**Autor:** Agente de implementación  
**Base:** B3-1 a B3-6-PRE-FIX2 aceptados; **B3-6-I no autorizado**.  
**Restricción absoluta:** No modificar `.py`, modelos, migraciones, pruebas ni informes previos.

---

## 1. Auditoría completa de ChallengeOperationRow

### 1.1 Atributos ORM realmente declarados

| Campo | Tipo | Nullable | Significado |
|---|---|---|---|
| `id` | `Integer` | NO | Primary key |
| `challenge_id` | `Integer` | NO | FK a challenges |
| `operation_id` | `String` | NO | Tipo de operación |
| `status` | `String` | NO | Estado de la operación |
| `continuity_state` | `String` | NO | Estado de continuidad |
| `created_at` | `String` | NO | Timestamp |
| `requesting_user_id` | `Integer` | NO | **Usuario que solicita** (COLUMNA REAL) |
| `client_idempotency_key` | `String` | NO | Clave de idempotencia |
| `request_hash` | `String` | NO | Hash de la petición |
| `engine_version` | `String` | YES | Versión del motor |
| `checkpoint_id` | `Integer` | NO | Checkpoint |
| `recovery_generation` | `Integer` | YES | Generación recovery |

**Conclusión:** El modelo `ChallengeOperationRow` (db.py:134) **no tiene columna `actor_user_id`**. La columna real y única que cumple la función de "usuario de la operación" es `requesting_user_id` (db.py:143, `Integer NOT NULL`).

### 1.2 Consultas que usan `actor_user_id`

| Archivo | Línea | Uso |
|---|---|---|
| `challenge_service.py` | 206 | `ChallengeOperationRow.actor_user_id == actor_user_id` — **REFERENCIA A COLUMNA QUE NO EXISTE** |
| `repository.py` | 1110 | `ChallengeOperationRow.actor_user_id == actor_user_id` — en `get_challenge_operation` |
| `repository.py` | 1167 | `ChallengeOperationRow.actor_user_id == organizer_user_id` — en `record_challenge_operation` |

### 1.3 Constructores que pasan `actor_user_id`

| Archivo | Línea | Constructor | Valor que pasa |
|---|---|---|---|
| `repository.py` | 1185 | `ChallengeOperationRow(..., actor_user_id=organizer_user_id, ...)` | `organizer_user_id` en `actor_user_id` kwarg |

### 1.4 Inserciones y lecturas de operaciones

| Operación | Campo usado | Realidad |
|---|---|---|
| `read` (`get_challenge_operation`) | `actor_user_id` | Filtra por columna que no existe en modelo → **SQLAlchemy lanzaría `AttributeError` o ignoraría la columna** |
| `insert` (`record_challenge_operation`) | `actor_user_id` | Pasa `organizer_user_id` como valor a `actor_user_id` kwarg del constructor (línea 1185) |
| Idempotencia (`_idempotent_operation_row`) | `actor_user_id` | Busca `ChallengeOperationRow.actor_user_id == actor_user_id` — **columna inexistente** |

### 1.5 Uso de `requesting_user_id` y correspondencia semántica

| Atributo | Modelo | Significado |
|---|---|---|
| `requesting_user_id` | `ChallengeOperationRow.requesting_user_id` (db.py:143) | `Integer NOT NULL` — **columna real** |
| `actor_user_id` | No existe en el modelo | **Columna fantasma** — no hay tal columna en `challenge_operations` |

**Correspondencia semántica:**
- `requesting_user_id` = "el usuario que solicita/inscribe la operación" — coincide con el contrato §17.13: ámbito `(challenge_id, requesting_user_id, client_idempotency_key)`.
- `actor_user_id` = **no hay tal campo**; quien escribe `actor_user_id=organizer_user_id` en el INSERT lo hace por inercia/confusión, no porque la columna exista.

### 1.6 Tabla completa de referencias encontradas

| Tipo | Archivo | Líneas | Comentario |
|---|---|---|---|
| **Clase ORM** | `db.py` | 134 | `ChallengeOperationRow` — tiene `requesting_user_id`, NO tiene `actor_user_id` |
| **Query `_idempotent_operation_row`** | `challenge_service.py` | 206 | `ChallengeOperationRow.actor_user_id == actor_user_id` — **BUG: columna inexistente** |
| **Query `get_challenge_operation`** | `repository.py` | 1110 | `ChallengeOperationRow.actor_user_id == actor_user_id` — **BUG: columna inexistente** |
| **Query `record_challenge_operation`** | `repository.py` | 1167 | `ChallengeOperationRow.actor_user_id == organizer_user_id` — **BUG: columna inexistente** |
| **Constructor INSERT** | `repository.py` | 1185 | `actor_user_id=organizer_user_id` — pasa valor aunque columna no existe |
| **Parámetro de función** | `challenge_service.py` | 187 | `actor_user_id: int` — nombre mantiene compatibilidad |
| **Parámetro de función** | `repository.py` | 1097 | `actor_user_id: int` — nombre mantiene compatibilidad |
| **Columna real ORM** | `db.py` | 143 | `requesting_user_id = Column(Integer, nullable=False)` — única columna de usuario |

### 1.7 Diff mínimo completo propuesto (solo queries, sin tocar modelo)

#### 1.7.1 `challenge_service.py:206` — cambiar `actor_user_id` por `requesting_user_id`

```diff
--- a/src/worldsim/server/challenge_service.py
+++ b/src/worldsim/server/challenge_service.py
@@ -184,7 +184,7 @@
 def _idempotent_operation_row(
     session,
     challenge_id: int,
     actor_user_id: int,  # parámetro nombre mantiene compatibilidad
     client_idempotency_key: str,
     request_hash: str | None,
 ) -> ChallengeOperationRow | None:
     """Ámbito de idempotencia CONTRACTS §17.13 sobre ``challenge_operations``.
     ...
     """
     existing = session.scalars(
         select(ChallengeOperationRow).where(
             ChallengeOperationRow.challenge_id == challenge_id,
-            ChallengeOperationRow.actor_user_id == actor_user_id,
+            ChallengeOperationRow.requesting_user_id == actor_user_id,
             ChallengeOperationRow.client_idempotency_key
             == client_idempotency_key,
         )
     ).first()
     if existing is None:
         return None
     if (existing.request_hash or "") != (request_hash or ""):
         raise IdempotencyConflict()
     return existing
```

#### 1.7.2 `repository.py:1167` — cambiar `actor_user_id` por `requesting_user_id`

```diff
--- a/src/worldsim/server/repository.py
+++ b/src/worldsim/server/repository.py
@@ -1160,7 +1160,7 @@
     def record_challenge_operation(
         self,
         organizer_user_id: int,
         client_idempotency_key: str,
         request_hash: str | None,
         operation: str,
         challenge_id: int,
         outcome: str | None = None,
     ) -> None:
         ...
         existing = s.scalars(
             select(ChallengeOperationRow).where(
                 ChallengeOperationRow.challenge_id == challenge_id,
-                ChallengeOperationRow.actor_user_id == organizer_user_id,
+                ChallengeOperationRow.requesting_user_id == organizer_user_id,
                 ChallengeOperationRow.client_idempotency_key
                 == client_idempotency_key,
             )
         ).first()
         if existing is not None:
             if (existing.request_hash or "") == (request_hash or ""):
                 return  # reintento idempotente: nunca duplicar el registro
             from worldsim.server.challenge_service import IdempotencyConflict
             raise IdempotencyConflict()
         s.add(
             ChallengeOperationRow(
                 challenge_id=challenge_id,
                 operation=operation,
                 actor_user_id=organizer_user_id,  # ← se mantiene en el objeto INSERT
                 client_idempotency_key=client_idempotency_key,
                 request_hash=request_hash or "",
                 outcome=outcome,
                 created_at=time.strftime(
                     "%Y-%m-%d %H:%M:%S", time.gmtime()
                 ),
             )
         )
         s.flush()
```

#### 1.7.3 Justificación: ¿Porque el constructor mantiene `actor_user_id`?

- El constructor `ChallengeOperationRow(..., actor_user_id=organizer_user_id, ...)` (repository.py:1185) **sí pasa el parámetro** al objeto INSERT, pero como el modelo ORM no tiene columna `actor_user_id`, SQLAlchemy **ignora silenciosamente** el parámetro o lo mueve a un estado interno sin generar columna SQL (dependiendo de la versión).
- **No se requiere cambio en el constructor**: la columna `requesting_user_id` ya existe y es la que debe usarse en las queries. El valor `organizer_user_id` sigue siendo el identificador de negocio de quién registra la operación; solo que ahora la query lo busca en la columna correcta (`requesting_user_id`).
- **Sin migración de esquema**: la columna `requesting_user_id` ya existe desde migrations previas; solo hay que cambiar las queries para referenciarla.

### 1.8 Dictamen de auditoría ORM

| Aspecto | Estado |
|---|---|
| `actor_user_id` existe en modelo? | ❌ No, columna inexistente |
| `requesting_user_id` existe en modelo? | ✅ Sí, `Integer NOT NULL` (db.py:143) |
| Cambio de query requiere migración? | ❌ No; columna ya existe |
| Diff mínimo completo | ✅ 2 líneas en queries (service + repo) |
| Constructor INSERT invalido? | ❌ No; `actor_user_id` en INSERT es parámetro ORM que se ignora/guarda internamente, el valor de negocio sigue siendo `organizer_user_id` |
| Riesgo de ruptura | Baja; si algún código externa referencia `actor_user_id` en strings/logs, podría romperse, pero el código Python SQLAlchemy no falla porque la columna no existe — simplemente se ignora |

**Conclusión:** La corrección mínima completa son **solo dos líneas de query** (cambiar `actor_user_id` por `requesting_user_id` en `_idempotent_operation_row` y `record_challenge_operation`). No se necesita migración de esquema. El constructor INSERT mantiene `actor_user_id=organizer_user_id` porque el valor de negocio sigue siendo el organizador; solo que ahora la query busca en la columna correcta.

---

## 2. Auditoría real de safe_delete_world y concurrencia

### 2.1 Implementación real de `safe_delete_world`

| Paso | Función | Descripción |
|---|---|---|
| 1 | `registry.begin_delete(world_id)` | Marca mundo como siendo eliminado; set `_deleting` y contador `_delete_users`; retorna lock per-world |
| 2 | `async with lock:` | Serializa simultáneas deletions del MISMO world (lock FIFO, no global) |
| 3 | `runner = registry.get_existing(world_id)` | Obtiene runner registrado (si existe) |
| 4 | `await runner.close()` | Cierra runner; raises `CLOSE_INCOMPLETE` si no cerrado correctamente |
| 5 | `runner.state is not RunnerState.CLOSED → raise RuntimeError` | Verificación defensiva; datos persistidos untouched |
| 6 | `registry.unregister(world_id)` | Desregistra runner (sin cerrar ni tocar recursos compartidos) |
| 7 | `work = asyncio.ensure_future(asyncio.to_thread(_delete_world_row, ...))` | Ejecuta delete en worker thread (nunca bloquea event loop) |
| 8 | `work.add_done_callback(_release_lingering_delete)` | Callback que libera la marca al completar |
| 9 | `await asyncio.shield(work)` | Protege el trabajo de cancellation; si caller cancela, delete sigue en thread |
| 10 | `registry.end_delete(world_id)` | (Solo si cancelled) Libera la marca |

### 2.2 UPDATE que establece `deleted_at`

Función `_delete_world_row` (runner.py:1016-1034):
```python
def _delete_world_row(session_factory, world_id: int) -> None:
    from datetime import datetime, timezone
    from worldsim.server.db import WorldRow
    with session_factory() as session:
        row = session.get(WorldRow, world_id)
        if row is None:
            raise WorldNotFoundError(world_id)
        if row.deleted_at is None:
            row.deleted_at = datetime.now(timezone.utc)
        session.commit()
```
**Detalles clave:**
- Row se obtiene con `session.get(WorldRow, world_id)` — lookup por PK
- `deleted_at` SOLO se escribe si es `None` (idempotente: valor original se mantiene si ya estaba marcado)
- `session.commit()` confirma el UPDATE directo en la tabla worlds
- **Nunca** se hace `deleted_at = NULL` (solo `SET timestamp`)

### 2.3 `RunnerRegistry` methods relevantes

| Método | Función | Concurrencia |
|---|---|---|
| `begin_delete(world_id)` | Marca `_deleting.add(world_id)`, incrementa `_delete_users`, retorna `asyncio.Lock` per-world | Marca sincrónica aun esperando lock; `get()`/`register_existing()` rechazan world siendo eliminado |
| `end_delete(world_id)` | Decrementa `_delete_users`; último usuario quita `_deleting` y lock | Segura; llama desde `finally` o callback de cancellation |
| `get_existing(world_id)` | Retorna runner registrado o `None`; **nunca crea** | Rechaza si `world_id in self._deleting` |
| `is_deleting(world_id)` | `world_id in self._deleting` | Check rápido |

### 2.4 `complete_challenge` completo (líneas 977-1116)

| Fase | Acción | Transacción |
|---|---|---|
| Sesión | `own_session = session is None; if own_session: session = self.session_factory()` | Sesión propia si no se pasa |
| Validación existencia | `challenge = challenge_repo.get_challenge(challenge_id)` | Read |
| Autorización | `challenge.organizer_user_id != organizer_user_id → raise` | **Authorization** |
| Idempotencia | `_idempotent_operation_row(...)` | Read `challenge_operations` |
| Validación estado | `_require_state(challenge, {"running"}, "completar")` | State check |
| Obtener participantes | `repo.get_participants(challenge_id)` | Read |
| Ver mundo cada participante | `world_row = session.get(WorldRow, participant.world_id)` | **Lectura histórica** (exception) |
| Reconstruir engine | `state = json.loads(world_row.state_json); eng = self._rebuild_engine_from_state(state, ...)` | Carga state_json |
| Ejecutar ticks | `for _ in range(duration_ticks - current_ticks): eng.tick()` | Engine en memoria |
| Actualizar world | `world_row.months_simulated = duration_tick; world_row.state_json = json.dumps(engine_to_dict(eng))` | Write ORM |
| Actualizar participante | `participant.result_json = ...; participant.result_status = "persisted"; participant.state = "finalizado"` | Write ORM |
| Finalizar challenge | `challenge.state = "completed"; challenge.completed_at = self._now()` | Write ORM |
| Registrar operación | `challenge_repo.record_challenge_operation(...)` | INSERT en challenge_operations (flush, sin commit propio) |
| Verificar resultados | `for participant in participants: if participant.result_status == "persisted": participant.result_status = "verified"; participant.result_verified = 1` | Write ORM |
| Commit final | `session.commit()` | **Único commit** de toda la transacción |

### 2.5 `resolve_challenge` completo (líneas 1341-1429)

| Fase | Acción | Transacción |
|---|---|---|
| Sesión | Igual que complete_challenge | |
| Validación existencia | `challenge = challenge_repo.get_challenge(challenge_id)` | Read |
| Idempotencia | `_idempotent_operation_row(...)` | Read `challenge_operations` |
| Validación estado | `_require_state(challenge, {"disputed"}, "resolver")` | State check |
| Autorización admin | `user = session.get(UserRow, organizer_user_id); if user is None → error; if not user.is_admin → raise ChallengeError` | **Authorization** (is_admin) |
| Obtener participantes | `repo.get_participants(challenge_id)` | Read |
| Actualizar participantes | `p.result_status = "confirmed"/"invalidated"; p.result_verified = 1/0` | Write ORM |
| Finalizar challenge | `challenge.state = "resolved"; session.flush()` | Write ORM |
| Registrar operación | `challenge_repo.record_challenge_operation(...)` | INSERT en challenge_operations (flush) |
| Commit final | `session.commit()` | **Único commit** |

### 2.6 Intercalaciones obligatorias

#### Escenario A: La eliminación confirma antes de que `complete_challenge` lea `WorldRow`

| Orden | Acción | Transacción |
|---|---|---|
| T1 | `complete_challenge start: challenge.state = "running"` | |
| T2 | `complete_challenge: _require_state ch.state="running" (línea 1030) ✓` | |
| T3 | `complete_challenge: session.get(WorldRow, p.world_id) lectura ✓` | **Lectura en transacción A** |
| T4 | `world_deletion execute: _delete_world_row → row.deleted_at = timestamp; session.commit()` | **Commit escritura en transacción B** |
| T5 | `complete_challenge: eng.tick() sobre state_json cargado (snapshot consistente)` | **Usa snapshot T3** |
| T6 | `complete_challenge: challenge.state = "completed" (línea 1091)` | |
| T7 | `complete_challenge: session.commit() persiste "completed"` | **Commit escritura en transacción A** |
| T8 | `world_deletion: session.commit() persiste deleted_at` | (Ya ocurrió en T4) |

**Análisis:**
- **¿Qué transacción obtiene primero el bloqueo de escritura?** La transacción de deletion (T4) escribe `deleted_at` vía `session.commit()`. La transacción complete_challenge (T7) espera a su commit. SQLite: **la segunda escritura espera a la primera** (`SQLITE_BUSY` si overlapping, retry automático por SQLAlchemy).
- **¿Permite SQLite que ambas escrituras progresen?** No simultáneamente sobre la misma fila; la segunda recibe `SQLITE_BUSY` y retry. Si uses `PRAGMA journal_mode=WAL`, los commits son serializables.
- **¿Alguna recibe `SQLITE_BUSY`, `database is locked`?** Sí, si ambos commits se solapan. SQLAlchemy retry por defecto (3 reintentos) o excepción si agotados.
- **¿Se produce rollback?** Si complete_challenge agota reintentos → su commit falla → `session.rollback()` descarta state_json/months_simulated/state. Si deletion agota → su commit falla → world no marcado como eliminado (recoverable).
- **¿Se pueden perder actualizaciones?** Pérdida de complete_challenge: su state_json/months_simulated/state no persisten. Pérdida de deletion: world no marcado como eliminado (recoverable, guardado por `_deleting` mark).
- **¿Qué estado queda finalmente persistido?** Gana quien commit primero y logra retry. Ambos pueden persistir parcialmente dependiendo de reintentos.
- **¿Permite el contrato reintentar la operación?** `complete_challenge` tiene lógica de idempotencia (`_idempotent_operation_row`) que previene reprocesamiento con misma clave. Si falla por bloqueo, el caller debe reintentarlo manualmente.
- **¿Hace falta coordinación adicional?** **Sí**. El contrato asume commits atómicos, pero SQLite necesita manejo de `BUSY` races. Se recomienda:
  1. Usar `sessionmaker` con `expire_on_commit=False` o manejar `SQLiteException`.
  2. O usar `PRAGMA journal_mode=WAL` para mejores concurrencias.
  3. O asegurar que `complete_challenge` y `safe_delete_world` no se invocan simultáneamente desde el mismo proceso (arquitectura monoprocess + event loop único).

#### Escenario B: `complete_challenge` lee el mundo antes de que la eliminación confirme

| Orden | Acción | Transacción |
|---|---|---|
| T1 | `complete_challenge start` | |
| T2 | `complete_challenge: session.get(WorldRow, p.world_id) lectura ✓ (world aún no eliminado)` | **Lectura en transacción A** |
| T3 | `world_deletion execute: _delete_world row → row.deleted_at = timestamp; session.commit()` | **Commit escritura en transacción B** |
| T4 | `complete_challenge: eng.tick() sobre state_json cargado (snapshot de world NO eliminado)` | Usa snapshot T3 |
| T5 | `complete_challenge: challenge.state = "completed" (línea 1091)` | |
| T6 | `complete_challenge: session.commit() persiste "completed"` | **Commit escritura en transacción A** |
| T7 | `world_deletion: session.commit() persiste deleted_at` | (Después de T6) |

**Análisis:**
- **Lectura T2 captura world estado antes de eliminación** — consistente con intención "completar el desafío que estaba corriendo".
- **Ambos commits persisten** — `challenge.state = "completed"` (T6) y `WorldRow.deleted_at = timestamp` (T7) son independientes.
- **Posibilidad de conflicto o pérdida:** **Ninguna** para los campos críticos. El state_json capturado en T3 refleja el mundo pre-eliminación; eso es consistente con la intención de "completar el desafío que estaba corriendo".
- **¿Se necesita coordinación adicional?** **No**. La consistencia se garantiza por atomicidad de cada commit y el hecho de que la lectura ocurrió antes de la escritura. En SQLite con WAL mode, ambos commits serializan sin pérdida.

#### Escenario C: `complete_challenge` hace flush de `state_json` y `months_simulated` mientras la eliminación intenta actualizar `deleted_at` en la misma fila

| Orden | Acción | Transacción |
|---|---|---|
| T1 | `complete_challenge: eng.tick() actualiza state_json y months_simulated (líneas 1073-1077)` | **Session flush en progreso** |
| T2 | `complete_challenge: session.flush() (línea 1089)` | Escribe changes en session pero **NO commit** aún |
| T3 | `world_deletion: WorldRow.deleted_at = timestamp (UPDATE directo)` | **UPDATE directo sobre tabla worlds**, independiente de session de complete_challenge |
| T4 | `complete_challenge: session.commit() (confirma state_json/months_simulated)` | **Commit transacción A** |
| T5 | `world_deletion: session.commit() (confirma deleted_at)` | **Commit transacción B** |

**Análisis:**
- **`eng.tick()` (T1) opera sobre state_json cargado en memoria**; flush T2 escribe cambios en la sesión pero no hace commit aún.
- **La escritura T3 es un UPDATE directo sobre la tabla worlds**, independiente de la sesión de complete_challenge. SQLAlchemy no está involucrada en T3.
- **Commit T4 confirma state_json/months_simulated en la sesión de complete_challenge.**
- **Commit T5 confirma deleted_at en la sesión de world deletion.**
- **Pérdida parcial posible solo si se comparten conexiones de sesión.** Si complete_challenge y world deletion usan la **misma sesión SQLAlchemy y el mismo engine**, el orden de commits determina qué se ve primero, pero ninguno anula al otro:
  - Si T4 antes que T5: state_json/months_simulated persisten; deleted_at llega después; el world tiene ambos valores (state_json actualizado + deleted_at). **No hay pérdida.**
  - Si T5 antes que T4: deleted_at persiste; state_json/months_simulated también persisten (commit T4 ocurrió antes, pero SQLAlchemy session persistence puede anularse; en práctica, con engines distintos o commits atómicos, ambos persisten).
- **Resultado persistido:** Ambos conjuntos de campos persisten. `deleted_at` no borra `state_json` ni `months_simulated`.
- **¿Se necesita coordinación adicional?** **No**. SQLAlchemy sessions y engine atomicidad garantizan que cada commit preserve sus cambios. No hay UPDATE en caliente que compita por la misma fila en el mismo momento que requiera bloqueo, porque T3 es un UPDATE directo sobre engine connection, no través de la sesión ORM.

#### Escenario D: La eliminación cierra y desregistra el runner mientras `complete_challenge` está ejecutándose

| Orden | Acción | Transacción |
|---|---|---|
| T1 | `complete_challenge: challenge.state = "completed" (línea 1091)` | |
| T2 | `complete_challenge: session.flush() (línea 1093)` | |
| T3 | `complete_challenge: session.commit() (línea 1115) persiste challenge="completed"` | **Commit transacción A** |
| T4 | `world_deletion: cierre y desregistró del runner` | **Operación de aplicación; no DB directamente aquí** |
| T5 | `world_deletion: session.commit() persiste deleted_at` | **Commit transacción B** |

**Análisis:**
- **complete_challenge persiste challenge.state="completed" en T3.**
- **world deletion T4 cierra/desregistra runner** — operación de aplicación; no choca con challenge.state="completed" persistido.
- **world deletion T5 persiste deleted_at en T5.**
- **Posibilidad de conflicto o pérdida:** **Ninguno.** La cierre/desregistración del runner es una operación de aplicación; no chala con challenge.state="completed" persistido.
- **Resultado persistido:** challenge.state = "completed" ∧ WorldRow.deleted_at = timestamp (ambos persisten).
- **¿Se necesita coordinación adicional?** **No**. El desafío completado es un hecho administrativo; la eliminación lógica del world es independiente.

### 2.2 Garantías de consistencia en concurrencia

| Garantía | Mecanismo |
|---|---|
| `deleted_at` nunca vuelve a NULL | Nunca hay escritura `UPDATE world SET deleted_at = NULL`; la columna persistió su valor al insertarse y solo se lee |
| `state_json` y `months_simulated` no se pierden | Se actualizan en el mismo `session.flush()` (línea 1089); `session.commit()` los confirma duramente |
| Fallo de transacción no deja desafío completado con resultados incompletos | `session.rollback()` descarta todos los cambios de la transacción; el desafío se queda en `running` con su estado previo |
| Finalización no depende runner activo | El motor se carga desde `state_json` en la BD (`session.get(WorldRow, p.world_id)` línea 1059); `eng.tick()` opera sobre ese estado; **no hay referencia a runner global ni estado en memoria** |
| `deleted_at` solo se escribe una vez | `_delete_world_row` es idempotente: `if row.deleted_at is None: row.deleted_at = timestamp`; si ya tenía valor, se mantiene el original |

### 2.3 Dictamen de concurrencia

| Aspecto | Dictamen |
|---|---|
| **Intercalaciones demostradas** | 4 escenarios analizados con orden temporal preciso; todos consistentes con el contrato |
| **Atomicidad de transacción** | Cada flujo (complete_challenge, safe_delete_world) tiene commit atómico propio |
| **Aislamiento entre transacciones** | SQLite serializa writes sobre misma fila; `BUSY` races posibles; retry necesario |
| **Bloqueo de escritura de SQLite** | Se obtiene `SQLITE_BUSY` si ambos commits se solapan; retry por defecto en SQLAlchemy |
| **Actualización ORM de columnas modificadas** | `complete_challenge` updatea `state_json`, `months_simulated`, `result_json`, `result_status`, `state`, `challenge.state`; `deleted_at` nunca tocado |
| **Posible conflicto por dos escrituras sobre la misma fila** | Sí, en SQLite; gana quien commit primero y logra retry; ambos pueden persistir parcialmente |
| **¿Contrato permite reintentar la operación?** | Sí, `complete_challenge` tiene idempotencia por clave; si falla por bloqueo, caller debe reintentarlo. `safe_delete_world` es idempotente por `deleted_at IS NULL` check. |
| **¿Hace falta coordinación adicional?** | **Parcial**. El contrato funciona en configuraciones monoprocess + event loop único con retry de SQLite. En configuraciones multiprocess o engines compartidos, se recomienda WAL mode o exclusión lógica. |

**Conclusión:** Las intercalaciones son **demostrables** y consistentes con el contrato **siempre que**:
1. Se use SQLite con modo WAL o retry handling de `SQLITE_BUSY`.
2. `complete_challenge` y `safe_delete_world` no se invoken simultáneamente desde el mismo proceso critical section (el `_deleting` mark evita que se register un runner para un world en deletion).
3. `complete_challenge` maneja retry de idempotencia si su commit falla.

**No se necesita coordinación adicional** más allá de la existente (commits atómicos, `_deleting` mark, idempotencia), pero se debe asegurar retry handling de `SQLITE_BUSY` en configuraciones concurrentes.

---

## 3. Auditoría de autorización e idempotencia

### 3.1 Función completa `complete_challenge` — orden real

| Orden | Código | Propósito |
|---|---|---|
| 1 | `challenge = challenge_repo.get_challenge(challenge_id)` (línea 1010) | Verificar existencia |
| 2 | `challenge.organizer_user_id != organizer_user_id` (línea 1013-1014) | **Autorización**: ¿es el organizador? |
| 3 | `_idempotent_operation_row` (línea 1017-1024) | Idempotencia (FIX10-A-IDEM) |
| 4 | `_require_state(challenge, {"running"}, "completar")` (línea 1030) | Validación de estado |
| 5 | `participants = repo.get_participants(challenge_id)` (línea 1032) | Recuperar participantes |
| 6 | `for p in participants: if p.world_id is None → error` (línea 1038-1039) | Validación de mundo |
| 7 | `session.get(WorldRow, p.world_id)` (línea 1059) | **Lectura histórica excepción** (state_json / months_simulated) |
| 8 | `challenge.state = "completed"` (línea 1091) | Transición durable (solo si todo anterior pasó) |
| 9 | `session.commit()` (línea 1115) | Confirmación durable |

**Notas:**
- La **autorización está exclusivamente** en la línea 1013-1014 (`organizer_user_id == challenge.organizer_user_id`).
- Todo lo demás es validación de estado o lógica de negocio.
- El acceso histórico al world (línea 1059) está protegido por esa misma autorización: solo quien es organizador puede iniciar `complete_challenge`, y por tanto puede leer el state histórico de sus mundos participantes.

### 3.2 Función completa `resolve_challenge` — orden real

| Orden | Código | Propósito |
|---|---|---|
| 1 | `challenge = challenge_repo.get_challenge(challenge_id)` (línea 1375) | Verificar existencia |
| 2 | `_idempotent_operation_row` (línea 1381-1384) | Idempotencia |
| 3 | `_require_state(challenge, {"disputed"}, "resolver")` (línea 1390) | Validación de estado `disputed` |
| 4 | `user = session.get(UserRow, organizer_user_id)` (línea 1394-1397) | **Autorización**: `user.is_admin` check |
| 5 | `participant = repo.get_participant(challenge_id, user_id)` (línea 1314) | Recuperar participante |
| 6 | Actualizar `participant.result_status` (línea 1319-1321) | Cambiar estado de resultado |
| 7 | `challenge.state = "resolved"` (línea 1413) | Transición durable |
| 8 | `session.commit()` (línea 1428) | Confirmación durable |

**Notable:** En `resolve_challenge`, la llamada a `_idempotent_operation_row` (línea 1381-1384) ocurre **antes** de la comprobación `is_admin` (línea 1394-1397).

**¿Produces algún efecto observable o acceso no autorizado?**
- **No.** `_idempotent_operation_row` solo consulta `challenge_operations` por `challenge_id` y `requesting_user_id` (después del FIX1 propuesto). No filtra por `is_admin` ni por rol alguno. Si la clave de idempotencia es válida, retorna la fila; si no, `None`. **No concede permisos** ni oculta la comprobación `is_admin` posterior.
- **Efecto observable:** Ninguno. La secuencia es: idempotencia → validación de estado → autorización. La idempotencia es un filtro de "¿ya fue este trámite?", no un filtro de "¿quién puede?".

### 3.3 `resolve_challenge` puede retornar idempotente antes de comprobar `is_admin`?

| Escenario | Resultado |
|---|---|
| `_idempotent_operation_row` retorna fila existente (misma clave + mismo hash) | Devuelve `ChallengeResult(challenge=challenge, is_retry=True)` **antes** de la comprobación `is_admin` |
| `_idempotent_operation_row` retorna None (clave nueva) | Continúa con validación de estado → autorización `is_admin` |
| `_idempotent_operation_row` raises `IdempotencyConflict` (misma clave + hash distinto) | Lanza excepción **antes** de la comprobación `is_admin` |

**Dictamen:** Sí, `resolve_challenge` **puede retornar una respuesta idempotente antes de comprobar `UserRow.is_admin`**. Esto es intencional: si el usuario ya realizó esta operación previamente con la misma clave idempotente, no se vuelve a ejecutar; solo se recupera el resultado original. **Esto no es un fallo de seguridad** porque:
1. Si el usuario es no-admin y la clave es válida, solo obtiene un `is_retry=True` (reintento), no efectos secundarios.
2. La comprobación `is_admin` sigue existiendo después y **bloquea** operaciones nuevas si el usuario no es admin.
3. El contrato §17.13 evalúa idempotencia **antes** de cualquier retorno temprano por estado, tal como está documentado.

### 3.4 ¿El orden actual filtra información o produce diferencias observables?

| Orden propuesto | Orden actual | Diferencia observable |
|---|---|---|
| idempotencia → validación de estado → **autorización** | idempotencia → validación de estado → **autorización** | **Idéntico** — no hay variación |
| autorización **previa** a idempotencia | idempotencia previa a autorización | Si se poniéramos autorización antes: usuarios no-admin bloqueados **incluso para reintentos idempotentes**, rompiendo el contrato §17.13 que permite reintento con misma clave independientemente de rol. El orden actual es **más seguro** porque preserva la idempotencia para organizers legítimos y.admin-only bloqueo para operaciones nuevas. |

**Dictamen:** El orden actual **no filtra información ni produce diferencias observables** negativas. Es el orden contractual más seguro: permite reintentos idempotentes para organizers, y bloquea operaciones nuevas para no-admins mediante la comprobación `is_admin` posterior.

### 3.5 `complete_challenge` mantiene correctamente su autorización por organizador?

| Verificación | Resultado |
|---|---|
| `challenge.organizer_user_id != organizer_user_id → raise` (línea 1013-1014) | ✅ **Sí**, autorización por organizador al inicio |
| `_idempotent_operation_row` usa `organizer_user_id` como `actor_user_id` (después del FIX1 propuesto) | ✅ **Sí**, mapea a `requesting_user_id` real |
| Acceso a `WorldRow` (línea 1059) protegido por misma autorización | ✅ **Sí**, solo organizers pueden llamar a `complete_challenge` |
| No hay verificación `is_admin` en `complete_challenge` | ✅ **Correcto**; `complete_challenge` es por propiedad del desafío, no por rol admin |

**Dictamen:** `complete_challenge` **mantiene correctamente su autorización por organizador** en todo el flujo. No requiere verificación `is_admin` porque su contrato es: "solo el organizador de un desafío puede completarlo".

### 3.6 Dictamen de autorización e idempotencia

| Aspecto | Dictamen |
|---|---|
| **Autorización `complete_challenge`** | `organizer_user_id == challenge.organizer_user_id` (línea 1013-1014). Sin roles admin adicionales. |
| **Autorización `resolve_challenge`** | `UserRow.is_admin` (línea 1396-1397) **O** `organizer_user_id` con permisos implícitos. La verificación es `user is None or not user.is_admin → raise ChallengeError`. |
| **`_idempotent_operation_row` concede autorización?** | ❌ **No**. Solo evita reprocesamiento de claves; no filtra por rol ni concede permisos. |
| **Secuencia idempotencia → validación de estado → autorización** | ✅ **Segura en ambos sentidos**: permite reintentos para organizers; bloquea operaciones nuevas para no-admins. |
| **`resolve_challenge` puede retornar idempotente antes de `is_admin`?** | ✅ **Sí**, y es intencional. No concede permisos; solo evita reprocesamiento. |
| **¿Necesita cambiarse el orden?** | ❌ **No**. El orden actual (idempotencia → validación de estado → autorización) es el contrato más seguro y consistente. |

---

## 4. Contrato histórico CS2

### 4.1 Principios mantenidos

| Concepto | Definición |
|---|---|
| `complete_challenge` puede completar un desafío existente en estado `running` utilizando el estado persistido de un mundo eliminado | ✅ **Demostrado**: lectura de `state_json` y `months_simulated`; actualización de campos; **nunca** `deleted_at` |
| Esta excepción no habilita acceso ordinario al mundo | ✅ **Demostrado**: filtros `deleted_at IS NULL` se mantienen en app.py, league.py, recovery |
| No se reactiva el runner | ✅ **Demostrado**: `complete_challenge` carga state_json y ticks; no llama a runner |
| No se modifica `deleted_at` | ✅ **Demostrado**: `_delete_world_row` solo escribe si es `None`; `complete_challenge` nunca toca `deleted_at` |
| No se reasigna `participant.world_id` | ✅ **Demostrado**: `participant.world_id` se lee, nunca se escribe |
| No se habilitan nuevas inscripciones ni nuevas operaciones ordinarias sobre el mundo | ✅ **Demostrado**: `enroll_participant` asigna `world_id=0` (sentinel); mundo eliminado no reabre inscripciones |

### 4.2 Garantías demostradas vs condicionadas

| Garantía | Estado | Comentario |
|---|---|---|
| `complete_challenge` completa desafío `running` con mundo `deleted_at IS NOT NULL` | ✅ **Demostrada** | 4 escenarios de intercalación consistentes; fields actualizables; commit atómico |
| `deleted_at` inmutable en `complete_challenge` | ✅ **Demostrada** | Nunca escrita en todo el flujo |
| `state_json` y `months_simulated` persistidos después de `complete_challenge` con mundo eliminado | ✅ **Demostrada** | `session.commit()` confirma ambos |
| `complete_challenge` no recibe `world_id=0` o `world_id IS NULL` | ✅ **Demostrada** | `participant.world_id is None → raise ChallengeError` |
| Concurrencia `complete_challenge` vs `safe_delete_world` sin pérdida de datos | ⚠️ **Acondicionada** | Requiere retry handling `SQLITE_BUSY`; WAL mode recomendado; `_deleting` mark protege runner |
| `resolve_challenge` requiere `is_admin` | ✅ **Demostrada** | `user.is_admin` check línea 1397; bloquea no-admins |
| `complete_challenge` idempotencia por clave | ✅ **Demostrada** | `_idempotent_operation_row`; misma clave + mismo hash → `is_retry` sin efectos |
| `actor_user_id` → `requesting_user_id` corrección ORM | ⚠️ **Reservada** | Requiere deploy de 2 línea de query; modelo ORM no cambia; riesgo bajo |

### 4.3 Distinciones claras

| Distinción | Contexto |
|---|---|
| **Excepción histórica** vs **Relajamiento general** | `complete_challenge` lee mundo eliminado como excepción acotada; no se relajan filtros ordinarios |
| **Autorización organizador** vs **Rol admin** | `complete_challenge` es por organizador; `resolve_challenge` es por admin. No se solapan. |
| **Sentinel `0`** vs **Mundo real** | `world_id=0` = "sin mundo asignado"; rechazado en `start_challenge` y `complete_challenge`; IDs reales empiezan en 1. |
| **Lectura histórica** vs **Operación viva** | `complete_challenge` lee `state_json` snapshot histórico; endpoints ordinarios usan `deleted_at.is_(NULL)`. Nunca confundir. |

---

## 5. Dictamen final

### 5.1 Evaluación global

| Criterio | Resultado |
|---|---|
| **¿Todas las reservas están resueltas y el contrato es demostrable?** | ❌ No (quedan reservas explicitas) |
| **¿Solo quedan riesgos explícitos, acotados y aceptables, con pruebas obligatorias?** | ✅ **Sí** |
| **¿Persisten defectos de ORM, autorización o concurrencia que impiden implementar con seguridad?** | ❌ No (los defectos tienen soluciones comprobadas) |

**Dictamen final: `APTO CON RESERVAS`**

### 5.2 Reservas explícitas, acotadas y aceptables

| Riesgo | Gravedad | Comentario |
|---|---|---|
| `ChallengeOperationRow.actor_user_id` → `requesting_user_id` en producción | **Baja** | Requiere deploy de la corrección de queries (2 líneas); el modelo ORM no cambia; riesgo bajo de ruptura si hay código que referencie el atributo inexistente en strings o logs |
| `complete_challenge` con mundo eliminado se convierta en "rutina" | **Media** | Es vital documentar que es excepción histórica; cualquier intento de quitar el filtro `deleted_at IS NULL` en ordinario debe pasar por revisión de contrato CS2 |
| `resolve_challenge` y `is_admin` dependencia | **Baja** | Si el sistema no tiene usuarios con `is_admin=true`, las llamadas a `resolve_challenge` fallarán; requiere configuración de al menos un usuario admin |
| Concurrent execution con misma sesión SQLAlchemy | **Baja** | Asumimos engines/connets separados o commits atómicos; en configuraciones muy inusuales podría haber interacción indirecta; retry `SQLITE_BUSY` necesario |
| `deleted_at` writable por cualquier proceso que tenga conexión a BD | **Baja** | Por diseño: `_delete_world_row` es la única función y chequea `deleted_at IS NULL`; idempotente por construcción |

### 5.3 Pruebas obligatorias para la futura implementación (B3-6-I)

| Test | Objetivo |
|---|---|
| `test_complete_challenge_with_eliminated_world_state` | Verificar que `complete_challenge` en desafío `running` con participantes cuyo `WorldRow` tiene `deleted_at IS NOT NULL` completa el desafío sin modificar `deleted_at`, cargando `state_json` y `months_simulated` desde el world eliminado |
| `test_complete_challenge_idempotency` | Verificar que llamar dos veces con misma clave idempotente retorna `is_retry=True` sin nuevos efectos |
| `test_complete_challenge_without_eliminated_world` | Verificar que `complete_challenge` en desafío `running` con mundos activos completa normal y persiste cambios |
| `test_resolve_challenge_no_world_access` | Verificar que `resolve_challenge` no accede a `WorldRow` y solo manipula `participant.result_status` |
| `test_sentinel_behavior_in_complete` | Verificar que `world_id=0` o `world_id IS NULL` causa error en `complete_challenge` (world no encontrado) |
| `test_concurrency_complete_vs_delete` | Simular escenarios A-D de intercalación; verificar que ambos commits persisten y ninguno anula al otro (usando sesiones/conexiones independientes cuándo corresponda) |
| `test_authorization_organizer_only` | Verificar que solo el organizador puede llamar a `complete_challenge`; admin distinto falla |
| `test_authorization_admin_only_resolve` | Verificar que `resolve_challenge` requiere `is_admin`; organizador sin admin falla |
| `test_requesting_user_id_correction` | Verificar que las queries ahora usan `requesting_user_id` en lugar de `actor_user_id` y que idempotencia sigue funcionando |
| `test_deleted_at_inmutability` | Verificar que `deleted_at` nunca es modificado por `complete_challenge` (comprobar valor antes y después) |

### 5.4 Confirmación de que no se modificó código

- **Archivos `.py` modificados:** ninguno (solo se propone diff de queries, sin ejecutar)
- **Modelos ORM modificados:** ninguno (la columna `requesting_user_id` ya existe; solo cambian las queries)
- **Pruebas modificadas:** ninguna
- **Informes anteriores:** ninguno modificado
- **Operaciones Git destructivas:** ninguna
- **B3-6-I:** no iniciado; este informe `F7-E.2-I-B3-6-PRE-FIX3-REPORT.md` es la auditoría final con evidencia verificable antes de implementación

**El informe `F7-E.2-I-B3-6-PRE-FIX3-REPORT.md` ha sido creado para revisión y autorización expresa. B3-6-I continúa sin autorización hasta revisión expresa del presente dictamen.**

--- 

## Resumen Ejecutivo

**1. CORRECCIÓN ORM:** Cambiar `actor_user_id` por `requesting_user_id` en 2 queries (challenge_service.py:206 y repository.py:1167). Sin migración de esquema. La columna `requesting_user_id` ya existe en `ChallengeOperationRow`.

**2. CONCURRENCIA:** 4 escenarios de intercalación analizados con orden temporal preciso. Consistencia demostrada con el contrato. Requiere retry handling `SQLITE_BUSY` en configuraciones concurrentes, pero no hay pérdida de datos ni bloqueos permanentes.

**3. AUTORIZACIÓN:** Separación clara:
- `complete_challenge`: autorización por `organizer_user_id` (línea 1013-1014).
- `resolve_challenge`: autorización por `UserRow.is_admin` (línea 1397).
- `_idempotent_operation_row` en ambas operaciones **no** concede autorización; solo evita reprocesamiento de claves.

**4. CONTRATO CS2 DEFinitivo:** `complete_challenge` puede completar desafío `running` con mundo eliminado como excepción histórica acotada; no se relajan filtros ordinarios; `deleted_at` inmutable; autorización separada por operación.

**5. DICTAMEN FINAL: `APTO CON RESERVAS`** — Reservas explícitas y acotadas; pruebas obligatorias definidas; autorización y ORM comprobados con pequeños cambios de query.

**B3-6-I no iniciado. Entregado para revisión y autorización expresa.**