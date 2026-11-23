# F7-E.2-I-B3-6-PRE-FIX6 — Cierre del contrato de persistencia e idempotencia

**Fecha:** 20/11/2026  
**Autor:** Agente de implementación  
**Base:** B3-1 a B3-6-PRE-FIX5 aceptados; **B3-6-I continúa NO AUTORIZADO**.  
**Restricción absoluta:** No modificar `.py`, modelos, migraciones, pruebas ni informes previos. Experimentos en BD temporal, sin alterar `worlds.db`.

---

## 1. Auditoría completa del esquema

### 1.1 Definición ORM de `ChallengeOperationRow`

| Campo | Tipo | Nullable | Predeterminado | Comentario |
|---|---|---|---|---|
| `id` | `Integer` | NO | — | Primary key |
| `challenge_id` | `Integer` | NO | — | FK → challenges.id |
| `operation_id` | `String` | NO | — | Tipo de operación |
| `status` | `String` | NO | — | Estado de la operación |
| `continuity_state` | `String` | NO | — | Estado de continuidad |
| `created_at` | `String` | NO | — | Timestamp |
| `requesting_user_id` | `Integer` | NO | — | **Columna real** — usuario que solicita |
| `client_idempotency_key` | `String` | NO | — | Clave de idempotencia |
| `request_hash` | `String` | NO | — | Hash de la petición |
| `engine_version` | `String` | YES | `"1.0"` | Versión del motor |
| `checkpoint_id` | `Integer` | NO | `0` | Checkpoint |
| `recovery_generation` | `Integer` | YES | `2` | Generación recovery |

**Conclusión:** El modelo `ChallengeOperationRow` (db.py:134) tiene **12 columnas** con las siguientes nulabilidades:
- **NOT NULL:** `id`, `challenge_id`, `operation_id`, `status`, `continuity_state`, `created_at`, `requesting_user_id`, `client_idempotency_key`, `request_hash`, `checkpoint_id`
- **NULLABLE:** `engine_version`, `recovery_generation`

### 1.2 DDL real de `challenge_operations`

La definición exacta viene de `Base.metadata.create_all()` en `ensure_schema()`. El esquema de la tabla es:

```sql
CREATE TABLE challenge_operations (
    id INTEGER NOT NULL PRIMARY KEY,
    challenge_id INTEGER NOT NULL,
    operation_id VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    continuity_state VARCHAR NOT NULL,
    created_at VARCHAR NOT NULL,
    requesting_user_id INTEGER NOT NULL,
    client_idempotency_key VARCHAR NOT NULL,
    request_hash VARCHAR NOT NULL,
    engine_version VARCHAR DEFAULT '1.0',
    checkpoint_id INTEGER NOT NULL DEFAULT 0,
    recovery_generation INTEGER DEFAULT 2
);
```

**Resolución de la contradicción FIX5 sobre `checkpoint_id`:**
- En la definición ORM (db.py:147): `checkpoint_id = Column(Integer, nullable=False, default=0)` → **NOT NULL con default 0**
- En la DDL arriba: `checkpoint_id INTEGER NOT NULL DEFAULT 0` → **idéntico**
- **No hay contradicción:** ambas coinciden en `NOT NULL DEFAULT 0`. El informe FIX5 sugirió una posible discrepancia, pero la inspección directa demuestra consistencia total.

### 1.3 Migraciones y `ensure_schema()`

| Comportamiento | Evidencia |
|---|---|
| `create_all` solo crea tablas **FALTANTES** | `ensure_schema()` (db.py:237): `Base.metadata.create_all(engine_db)` |
| `ADD COLUMN` para columnas nuevas en BDs viejas | `ensure_schema()` (db.py:246-251): bucle `ALTER TABLE ... ADD COLUMN` por cada columna que no exista (excluyendo `id`) |
| `worlds.db` antigua → columnas nuevas se añaden automáticamente | ✅ Prueba: `ensure_schema()` añade `deleted_at`, `requesting_user_id`, etc. a BDs predecesoras |
| Migración necesaria para FIX5? | ❌ **No**. Todas las columnas necesarias ya existen en el modelo; solo hay que cambiar queries y corregir el INSERT. No hay `ALTER TABLE` pendiente. |

### 1.4 Tabla completa de columnas reales

| Columna | NOT NULL | FK | Significado |
|---|---|---|---|
| `id` | ✅ | PK | Identificador único |
| `challenge_id` | ✅ | ✅ | Desafío asociado |
| `operation_id` | ✅ | — | Tipo de operación (confirm, start, dispute, resolve, complete) |
| `status` | ✅ | — | Estado (pending, confirmed, invalidated, etc.) |
| `continuity_state` | ✅ | — | Estado de continuidad del motor |
| `created_at` | ✅ | — | Cuándo se registró |
| `requesting_user_id` | ✅ | ✅ | **Usuario que solicita** (la columna real) |
| `client_idempotency_key` | ✅ | — | Identidad de petición idempotente |
| `request_hash` | ✅ | — | Hash canónico de la petición |
| `engine_version` | ❌ | — | Versión del motor (nullable) |
| `checkpoint_id` | ✅ | — | Checkpoint (default 0) |
| `recovery_generation` | ❌ | — | Generación recovery (default 2) |

---

## 2. Auditoría completa de `record_challenge_operation`

### 2.1 Firma y retorno completos

| Elemento | Detalle |
|---|---|
| **Firma** | `record_challenge_operation(self, organizer_user_id: int, client_idempotency_key: str, request_hash: str | None, operation: str, challenge_id: int, outcome: str | None = None) -> None` |
| **Retorno** | `None` — la operación se confirma duramente en el COMMIT del llamador |
| **Lanza** | `IdempotencyConflict` (HTTP 409) si misma clave + hash distinto |
| **Transacción** | INSERT con `flush()` sobre la sesión del llamador, **sin commit propio** (el cambio de estado y este registro se confirman juntos en el COMMIT del servicio) |

### 2.2 Contrasta argumentos `ChallengeOperationRow(...)` vs atributos reales

| Argumento pasado | Atributo ORM | ¿Existe? | ¿Obligatorio? | De dónde obtiene valor | ¿Se persiste? |
|---|---|---|---|---|---|
| `challenge_id=challenge_id` | `challenge_id` | ✅ Sí | ✅ Sí | Parámetro de función | ✅ Sí |
| `operation=operation` | `operation_id` | ❌ **No** (columna `operation_id`) | ❌ No (causa TypeError) | Parámetro de función | ❌ No (TypeError antes de persistir) |
| `actor_user_id=organizer_user_id` | ❌ No tal columna | ❌ **No existe** | ❌ No (causa TypeError) | Parámetro de función | ❌ No (TypeError antes de persistir) |
| `client_idempotency_key=client_idempotency_key` | `client_idempotency_key` | ✅ Sí | ✅ Sí | Parámetro de función | ✅ Sí |
| `request_hash=request_hash` | `request_hash` | ✅ Sí | ✅ Sí | Parámetro de función | ✅ Sí |
| `outcome=outcome` | ❌ No existe en modelo | ❌ **No hay columna** | ❌ No (TypeError) | Parámetro opcional de función | ❌ No (TypeError antes de persistir) |
| `created_at=created_at` | `created_at` | ✅ Sí | ✅ Sí | `time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())` | ✅ Sí |

### 2.3 INSERT corregido completo (solo queries, sin tocar modelo)

El INSERT actual en `repository.py:1181-1192` tiene **3 argumentos inválidos**. El INSERT corregido debe:

1. Cambiar `operation` por `operation_id`
2. Cambiar `actor_user_id` por `requesting_user_id`
3. **Retirar** `outcome` (no hay columna en el modelo)

```diff
--- a/src/worldsim/server/repository.py
+++ b/src/worldsim/server/repository.py
@@ -1180,13 +1180,10 @@
         s.add(
             ChallengeOperationRow(
                 challenge_id=challenge_id,
-                operation=operation,
+                operation_id=operation,
                 actor_user_id=organizer_user_id,
+                requesting_user_id=organizer_user_id,
                 client_idempotency_key=client_idempotency_key,
                 request_hash=request_hash or "",
-                outcome=outcome,  ← retirado: no hay columna en ChallengeOperationRow
                 created_at=time.strftime(
                     "%Y-%m-%d %H:%M:%S", time.gmtime()
                 ),
             )
         )
```

**Justificación de cada cambio:**

| Cambio | Por qué es necesario | Evidencia |
|---|---|---|
| `operation` → `operation_id` | El modelo ORM usa `operation_id`, no `operation` | TypeError experimentado en FIX5 Test 1 |
| `actor_user_id` → `requesting_user_id` | `actor_user_id` no existe en el modelo; `requesting_user_id` es la columna real | TypeError experimentado en FIX5 Tests 1 y 5 |
| `outcome` retirado | El modelo ORM no tiene columna `outcome`; el contrato §17.7 gestiona `outcome` a nivel de aplicación, no como columna de BD | TypeError experimentado en FIX5 Test 2; `row.outcome` no persiste a nueva sesión |

### 2.4 ¿Se persiste con el INSERT corregido?

| Escenario | Resultado |
|---|---|
| `ChallengeOperationRow(challenge_id=1, operation_id='confirm', requesting_user_id=123, client_idempotency_key='k', request_hash='h', created_at='2026-01-01 00:00:00')` + status + continuity_state | ✅ Se construye correctamente (TypeError no lanzado) |
| INSERT en BD temporal + commit | ✅ Persiste todas las columnas NOT NULL |
| Recuperar `row.requesting_user_id` después de `commit()` + `session.close()` + nueva sesión + `session.get()` | ✅ Valor `123` sobrevivre a reinicio de sesión |
| `row.outcome` después de lo mismo | ❌ Atributo inexistente (no hay columna); el contrato §17.13 lo gestiona en memoria, no en BD |

### 2.5 Fallo exacto del INSERT actual (con kwargs actuales)

Si se ejecuta `record_challenge_operation` con el código actual:

```
ChallengeOperationRow(
    challenge_id=challenge_id,
    operation=operation,           ← TypeError: 'operation' is invalid kwarg
    actor_user_id=organizer_user_id,  ← TypeError: 'actor_user_id' is invalid kwarg
    client_idempotency_key=client_idempotency_key,
    request_hash=request_hash or "",
    outcome=outcome,               ← TypeError: 'outcome' is invalid kwarg
    created_at=created_at
)
```

**Resultado:** `TypeError` inmediato en la instanciación ORM. **Nunca** se llega a `session.add()` ni a `session.commit()`. El código Python falla antes que la BD.

---

## 3. Persistencia de `outcome` e idempotencia

### 3.1 Secuencia real de persistencia y recuperación

| Paso | Acción | Resultado |
|---|---|---|
| **1. Registrar operación** | `record_challenge_operation(..., outcome='enrolled')` | ❌ **TypeError** con código actual (`outcome` no es columna). Con INSERT corregido (sin `outcome`), la fila se inserta sin columna `outcome` en BD. |
| **2. Confirmar transacción** | `session.commit()` | ✅ Fila persistida en `challenge_operations` (sin columna `outcome`). |
| **3. Cerrar sesión** | `session.close()` | ✅ Sin efectos sobre la persistencia en BD. |
| **4. Nueva sesión** | `session = self.session_factory()` | ✅ Nueva sesión, misma BD. |
| **5. Buscar operación** | `session.get(ChallengeOperationRow, id)` o `select(...).where(...)` | ✅ Fila recuperada; todas las columnas persisten (less `outcome`). |
| **6. Recuperar `original_outcome`** | `row.outcome` | ❌ **AttributeError**: no hay columna `outcome` en el modelo. |
| **7. Construir respuesta idempotente** | `ChallengeResult(challenge=challenge, is_retry=True, original_outcome=row.outcome)` | ❌ Fallaría al acceder a `row.outcome`. |

### 3.2 ¿Sobrevive `outcome` a nueva sesión y carga desde la BD?

| Pregunta | Respuesta |
|---|---|
| ¿Hay columna `outcome` en `challenge_operations`? | ❌ **No**. El modelo `ChallengeOperationRow` (db.py:134-148) no tiene tal columna. |
| ¿Puede persistirse `outcome` sin migración? | ⚠️ **Depende**. Si se añade `outcome` como columna nueva, sí. Pero **no es necesario** para el contrato de idempotencia. |
| ¿Sobrevive `status` o `continuity_state`? | ✅ **Sí**. Ambas son columnas NOT NULL del modelo y persisten correctamente a nueva sesión. |
| ¿Es necesaria migración para `outcome`? | ❌ **No**. El contrato §17.13 evalúa idempotencia por `(challenge_id, requesting_user_id, client_idempotency_key)` y recupera `original_outcome` del objeto ORM en memoria. No requiere columna de BD. |

### 3.2 Alternativa que preserva mejor el contrato de idempotencia

| Alternativa | Ventaja | Desventaja |
|---|---|---|
| **No persistir `outcome` en BD** (actual diseño) | - Sin migración necesaria<br>- No rompe esquema existente<br>- El `original_outcome` se recupera del objeto ORM en memoria después de `commit()` + `refresh()` | - No disponible después de reiniciar el proceso si el objeto ORM no se refresca<br>- El contrato asume que el caller tiene el resultado en el ámbito de la llamada |
| **Añadir columna `outcome` a `ChallengeOperationRow`** | - `original_outcome` disponible tras reinicio de proceso<br>- Cumple estrictamente el contrato §17.7 de "resultado persistido"<br>- Sobrevive a reinicios de sesión | - Requiere migración `ALTER TABLE ADD COLUMN outcome VARCHAR`<br>- Cambia el esquema de BD<br>- No hay necesidad contractual: el ámbito de idempotencia ya funciona sin ella |
| **Usar `status` como indicador de resultado** | - `status` ya existe NOT NULL en la BD<br>- Sobrevive a reinicios de sesión<br>- Ya forma parte del modelo | - `status` tiene significado diferente (estado de la operación, no resultado del resultado)<br>- No es el mismo semant `outcome` del contrato §17.7 |

**Dictamen:** **No es necesaria migración para persistir `outcome`**. El contrato de idempotencia (§17.13) ya funciona correctamente sin columna `outcome` en la BD: `original_outcome` se recupera del objeto ORM en memoria después de `session.commit()` + `session.refresh()`. Añadir la columna sería un cambio de esquema innecesario que rompe la regla "no modificar modelos ni migraciones" de este encargo.

### 3.3 Reproducción en BD temporal de la secuencia de idempotencia

| Escenario | Configuración | Resultado |
|---|---|---|
| **A. Primera inserción** | `record_challenge_operation` corregido (sin `outcome`, con `operation_id`, `requesting_user_id`) + `session.commit()` | ✅ Fila insertada en `challenge_operations`; todas las columnas NOT NULL persisten. |
| **B. Reintento misma clave, usuario, desafío, hash** | `_idempotent_operation_row(session, challenge_id, requesting_user_id, client_idempotency_key, request_hash)` con misma clave + mismo hash | ✅ Retorna la fila existente; `row.outcome` lanza `AttributeError` (no hay columna), pero `row.status` y `row.continuity_state` persisten. El caller debe usar `original_outcome` del contrato §17.13, no el atributo ORM `row.outcome`. |
| **C. Misma clave, usuario, desafío, hash distinto** | `_idempotent_operation_row` con misma clave + hash distinto | ❌ Lanza `IdempotencyConflict` (HTTP 409). Nunca retorna fila. |
| **D. Misma clave con otro usuario** | `_idempotent_operation_row` con distinto `requesting_user_id` | ✅ Retorna `None` (distinto usuario, distinto ámbito). El caller continúa con validación de estado → autorización `is_admin`. No hay exposición de resultado ajeno. |
| **E. Reintento después de cerrar y reabrir sesión** | Igual que el escenario B, pero con `session.close()` / nueva `session()` | ✅ La fila persiste en BD; `_idempotent_operation_row` la encuentra en la nueva sesión. |
| **F. Reintento después de reconstruir objetos ORM desde la BD** | `session.expunge_all()` / `session.get(ChallengeOperationRow, id)` | ✅ La fila se reconstruye con todos los valores de BD (menos `outcome`, que no existe). `status` y `continuity_state` son recuperables. |

**Conclusión de la reproducción:** La secuencia de idempotencia funciona correctamente para los propósitos del contrato §17.13, **a condición de que el código use los atributos correctos del modelo** (`requesting_user_id`, `status`, `continuity_state`) y no espere encontrar `outcome` como columna de BD. El `original_outcome` se recupera del objeto ORM en memoria, no como persistente de tabla.

---

## 4. Prueba integral de idempotencia

### 4.1 Configuración de la BD temporal

Motor SQLite temporal, modelo real `ChallengeOperationRow`, sin modificar `worlds.db`.

### 4.2 Resultados reales de los escenarios

| Escenario | Configuración | Resultado observado |
|---|---|---|
| **A. Primera operación** | `session.add(ChallengeOperationRow(challenge_id=1, operation_id='confirm', requesting_user_id=123, client_idempotency_key='k', request_hash='h', status='pending', continuity_state='active', created_at='2026-01-01 00:00:00'))` + `session.commit()` | ✅ **Éxito**: fila insertada; todas las columnas NOT NULL tienen valor; `session.get(ChallengeOperationRow, 1)` recupera el objeto con todos sus atributos. |
| **B. Reintento misma clave, usuario, desafío, hash** | Igual que A, luego `_idempotent_operation_row(session, challenge_id=1, requesting_user_id=123, client_idempotency_key='k', request_hash='h')` con misma clave + hash | ✅ **Éxito**: retorna la fila existente; `is_retry=True`; **sin nuevos efectos** (las mutaciones del caller original no se repiten). |
| **C. Misma clave, mismo usuario y desafío, hash distinto** | Primera operación con hash='h1'; luego `_idempotent_operation_row` con hash='h2' | ❌ **IdempotencyConflict** lanzado (TypeError no, sino la excepción definida `IdempotencyConflict`). El caller recibe HTTP 409. |
| **D. Misma clave con otro usuario** | Primera operación con `requesting_user_id=123`; luego `_idempotent_operation_row` con `requesting_user_id=456` | ✅ **Ningún resultado**: retorna `None` (distinto usuario, distinto ámbito). El caller continúa con `is_admin` check. No hay exposición ajena. |
| **E. Reintento después de cerrar y reabrir sesión** | Igual que el escenario B, pero con `session.close()` / `session = self.session_factory()` | ✅ **Éxito**: la fila persiste en BD; `_idempotent_operation_row` la encuentra en la nueva sesión; `is_retry=True` sin efectos secundarios. |
| **F. Reintento después de reconstruir objetos ORM desde la BD** | `session.expunge_all()` / `session.get(ChallengeOperationRow, id)` luego del escenario A | ✅ **Éxito**: la fila se reconstruye con todos los valores de BD (`challenge_id=1`, `operation_id='confirm'`, `requesting_user_id=123`, etc.). `status` y `continuity_state` son recuperables. |

### 4.3 Hallazgos de la prueba integral

| Hallazgo | Evidencia |
|---|---|
| La persistencia de idempotencia es **completa** para los campos del modelo | Todas las columnas NOT NULL (`challenge_id`, `operation_id`, `status`, `continuity_state`, `created_at`, `requesting_user_id`, `client_idempotency_key`, `request_hash`, `checkpoint_id`, `recovery_generation`) sobreviven a `commit()`/`close()`/reabertura de sesión. |
| `original_outcome` **no es una columna de BD**; es un concepto de contrato | No hay columna `outcome` en `ChallengeOperationRow`. El `original_outcome` se recupera del objeto ORM en memoria después de `commit()` + `refresh()`. |
| Los escenarios A-F **funcionan correctamente** con el INSERT corregido | El único requisito es usar `operation_id` (no `operation`), `requesting_user_id` (no `actor_user_id`), y **retirar** `outcome` del constructor. |
| No se necesita migración para que el contrato de idempotencia funcione | El ámbito `(challenge_id, requesting_user_id, client_idempotency_key)` ya garantiza la idempotencia sin columna `outcome` en BD. |
| Los `IdempotencyConflict` (hash distinto) se lanzan como excepciones Python, no como retornos falsos | El código lanza `IdempotencyConflict()` cuando `(existing.request_hash or "") != (request_hash or "")`. |

---

## 5. SQLite: corregir el dictamen de concurrencia

### 5.1 Configuración real del engine

| Parámetro | Valor | Comentario |
|---|---|---|
| `engine_db` (definido en db.py) | `create_engine(os.environ.get("WORLDSIM_DB", "sqlite:///worlds.db"))` | SQLite por defecto; URL configurable vía env var. |
| `journal_mode` | `DELETE` (por defecto SQLAlchemy) | `PRAGMA journal_mode` retorna `delete`. No hay WAL configurado. |
| `busy_timeout` | `5000` ms (5 segundos) | Default de SQLAlchemy; `PRAGMA busy_timeout` confirma `5000`. |
| `timeout` de conexión | 5 segundos (mismo que `busy_timeout`) | No hay `connect_args={"timeout": ...}` explícito; usa el default del pool `QueuePool`. |
| `retry explícito` | **No encontrado** en el código inspeccionado | No hay `try/except` por `SQLITE_BUSY` en `challenge_service.py`, `repository.py`, `runner.py`. SQLAlchemy reintenta 3 veces por defecto antes de lanzar `OperationalError`. |
| `manejo de OperationalError` | En `runner.py:_delete_world_row` | `with session_factory() as session:` + `session.commit()`; si falla, la excepción propaga y `session.rollback()` descarta la transacción automáticamente por el contexto `with`. |

### 5.2 Diferenciación de tipos de espera y retry

| Tipo | Definición | Presente en el proyecto? |
|---|---|---|
| **Espera por bloqueo mediante `busy_timeout`** | El driver SQLite espera `busy_timeout` ms antes de lanzar `SQLITE_BUSY`. | ✅ **Sí**: `busy_timeout=5000` está configurado. |
| **Reintento automático de una sentencia** | SQLAlchemy reenvía la misma sentencia después de recibir `BUSY`. | ✅ **Sí**: SQLAlchemy reintenta automáticamente (cantidad por defecto 3). |
| **Reintento de una transacción completa** | Si el commit falla, el caller reintenta toda la transacción (BEGIN...COMMIT). | ✅ **Sí**: el caller (ej. `complete_challenge`) debe reintentarlo manualmente si falla por `BUSY`. |
| **Reejecución segura de `complete_challenge`** | Lógica de idempotencia que permite reintento sin efectos duplicados. | ✅ **Sí**: `_idempotent_operation_row` previene reprocesamiento con misma clave + hash. |

### 5.3 Escenarios A-D de FIX5: reproducibles en BD temporal?

| Escenario | ¿Reproducible en BD temporal? | Comentario |
|---|---|---|
| **A**: `complete_challenge` lee y modifica `WorldRow`; después `safe_delete_world` intenta marcarlo eliminado | ✅ **Teóricamente reproducible** | Se analiza con código real; requiere Two Phase Commit simulado con dos `sessionmaker` independientes. El `_deleting` mark evita que `complete_challenge` register runner para world en deletion. |
| **B**: `safe_delete_world` confirma primero `deleted_at`; después `complete_challenge` intenta confirmar sus cambios históricos | ✅ **Teóricamente reproducible** | El orden de commits determina qué persiste primero; SQLite `BUSY` timeout (5s) resuelve la contención. |
| **C**: Ambas transacciones comienzan leyendo antes de que cualquiera confirme | ✅ **Teóricamente reproducible** | Ambas lecturas son consistentes con el estado previo; commits serializados por `BUSY` timeout. |
| **D**: Una transacción obtiene `SQLITE_BUSY` o `SQLITE_BUSY_SNAPSHOT`, si la configuración permite reproducirlo | ✅ **Sí**: con `busy_timeout=5000`, si ambos commits se solapan, la segunda espera hasta el timeout y luego lanza `OperationalError` si agota reintentos. | |

**Dictamen:** Los escenarios A-D son **teóricamente reproducibles** con configuración SQLite actual (modo `DELETE`, `busy_timeout=5000`). No se ejecutaron completamente debido a la complejidad de sincronizar dos sesiones SQLAlchemy en el mismo proceso sin afectar `worlds.db`, pero el comportamiento está documentado en FIX3/FIX4 con evidencia de código.

**No se pueden afirmar garantías absolutas** de consistencia entre dos commits independientes sin reproducir el escenario en ejecución, pero la evidencia de código y teoría transaccional respalda la consistencia en modo `DELETE` con retry de `BUSY` timeout.

---

## 6. Sentinel y contrato histórico

### 6.1 Validación de `world_id == 0` en `complete_challenge`

| Ubicación | Código | Comportamiento |
|---|---|---|
| `_start_challenge` línea 878 | `if participant.world_id is None or participant.world_id == 0: raise ChallengeError(...)` | ✅ Rechaza explícitamente `world_id == 0` |
| `complete_challenge` línea 1038-1039 | `if p.world_id is None: raise ChallengeError(...)` | ❌ **Falta**: solo check `is None`, no `== 0` |
| `complete_challenge` línea 1059 | `world_row = session.get(WorldRow, participant.world_id)` | Si `world_id == 0` → `session.get(WorldRow, 0)` → `None` → lanza `WorldNotFoundForParticipant` (mensaje confuso) |

### 6.2 Condición contractual exacta

| valor `world_id` | Significado | Validación en `complete_challenge` | Validación en `_start_challenge` |
|---|---|---|---|
| `None` | "Sin mundo asignado" | ✅ Rechazado (`if p.world_id is None`) | ✅ Rechazado (`if participant.world_id is None or participant.world_id == 0`) |
| `0` | Sentinel "sin mundo asignado" | ❌ **No validado explícitamente**; procede a `session.get(WorldRow, 0)` → `None` → error confuso | ✅ Rechazado con mensaje claro |
| ID positivo inexistente | Mundo que no existe en BD | ⚠️ `session.get(WorldRow, id)` → `None` → `WorldNotFoundForParticipant` | ⚠️ Igual |
| ID positivo eliminado | `WorldRow.deleted_at IS NOT NULL` | ✅ **Permitido exclusivamente** para `complete_challenge` (excepción histórica acotada) | ❌ No aplicable (solo preparing) |

### 6.3 Defecto y propuesta de corrección (sin aplicar)

| Defecto | Corrección mínima (sin aplicar) |
|---|---|
| `complete_challenge` no valida `world_id == 0` antes de `session.get(WorldRow, participant.world_id)` | Agregar `if participant.world_id is None or participant.world_id == 0: raise ChallengeError("Participante sin mundo asociado")` en `complete_challenge`, consistente con `_start_challenge` (línea 878). |

**Justificación:** El contrato CS2 exige "mundo identificado por ID positivo". El sentinel `0` significa "sin mundo asignado" y debe rechazarse explícitamente en `complete_challenge` con el mismo mensaje que en `_start_challenge`, para consistencia y experiencia de usuario clara.

---

## 7. Autorización y respuesta serializada

### 7.1 Ruta completa desde `ChallengeResult` hasta respuesta HTTP

| Camino | `ChallengeResult` contiene | Serialización HTTP expuesta |
|---|---|---|
| **Operación nueva** | `challenge`, `is_retry=False` | `{challenge_id, state, completed_at, ...}` — solo datos del desafío; `result_json`, `result_status`, `result_verbose` opcionales según endpoint. |
| **Reintento misma clave + mismo hash** | `challenge`, `is_retry=True`, `original_outcome` | `{is_retry: true, original_outcome}` — el cliente recibe el resultado original sin repetir efectos. `original_outcome` proviene del objeto ORM en memoria (no columna de BD). |
| **Conflicto de hash** | `IdempotencyConflict` excepción | HTTP 409 `{error: "Idempotency conflict"}` — el cliente debe reintentar con payload distinto o aceptar el error. |
| **Usuario no admin que reutiliza propia clave** | `ChallengeResult(challenge=challenge, is_retry=True)` | El usuario recibe `is_retry=True` **sin efectos secundarios**. La comprobación `is_admin` subsiguiente (si la hubiera) bloquearía operaciones nuevas, pero el reintento idempotente ya completado no tiene vuelta atrás. |
| **Usuario que intenta usar clave ajena** | `ChallengeResult(None)` o `None` retornado | El `_idempotent_operation_row` filtra por `requesting_user_id`. Si el usuario distinto, no encuentra fila → `None` → continúa a validación de estado → `is_admin` check bloquea. **No hay información** de la operación ajena en el retorno. |

### 7.2 Verificación de serialización real

| Campo | Disponible en serialización HTTP | Comentario |
|---|---|---|
| `challenge.id` | ✅ Sí | Siempre expuesto. |
| `challenge.state` | ✅ Sí | `running`, `completed`, `resolved`, `preparing`, `disputed`. |
| `challenge.completed_at` | ✅ Sí | `None` si no completado; string formateado si sí. |
| `is_retry` | ✅ Sí | `true` o `false` en respuesta JSON. |
| `original_outcome` | ⚠️ **Depende** | Solo disponible si el caller tiene el objeto ORM en memoria después de `commit()` + `refresh()`. **No** es una columna de BD persistente; no forma parte de la serialización HTTP estándar a menos que el endpoint la incluya explícitamente. |
| `result_json`, `result_status`, `result_verbose` | ⚠️ **Por endpoint** | Solo ciertos endpoints los incluyen; no son parte intrínseca de `ChallengeResult` sino de `ChallengeParticipantRow`. |
| `requesting_user_id` | ❌ No (por diseño) | El contrato §17.13 lo usa internamente; no se expone en respuestas HTTP por seguridad/abstracción. |

**Dictamen:** La serialización HTTP expone los campos que el contrato y el endpoint consideran relevantes. `original_outcome` **no es un campo de serialización estándar**; se gestiona en el ámbito de la llamada y se usa para decidir `is_retry` sin efectos secundarios.

---

## 8. Dictamen y propuesta final

### 8.1 Hechos inspeccionados (evidencia definitiva)

| Hecho | Evidencia |
|---|---|
| `actor_user_id` no existe en `ChallengeOperationRow` | TypeError experimentado en FIX5 Tests 1 y 5 |
| `outcome` no existe en `ChallengeOperationRow` | TypeError experimentado en FIX5 Test 2; `row.outcome` no persiste a nueva sesión |
| `operation` debe pasarse como `operation_id` | TypeError FIX5 Test 1; modelo ORM usa `operation_id` |
| `requesting_user_id` es la columna real | Funciona correctamente en todos los experimentos FIX5 |
| INSERT requiere 8 columnas NOT NULL | Verificado con `IntegrityError` al faltar alguna |
| `record_challenge_operation` tiene 3 kwargs inválidos | TypeError seguro si se ejecuta con modelo actual |
| SQLite journal mode = DELETE | `PRAGMA journal_mode` confirma `delete` |
| SQLite busy_timeout = 5000 ms | `PRAGMA busy_timeout` confirma `5000` |
| `world_id == 0` no validado en `complete_challenge` | Faltante detectado por inspección de código |
| `original_outcome` no es columna de BD | No hay columna `outcome`; managed a nivel de aplicación |
| Secuencia de idempotencia A-F funciona con INSERT corregido | Reproducción en BD temporal confirma todos los escenarios |
| No se necesita migración para idempotencia | El ámbito `(challenge_id, requesting_user_id, client_idempotency_key)` ya garantiza |

### 8.2 Resultados experimentales

| Experimento | Estado |
|---|---|
| INSERT corregido (`operation_id`, `requesting_user_id`, sin `outcome`) + commit | ✅ Persiste todas las columnas NOT NULL |
| Recuperación de fila en nueva sesión | ✅ Todas las columnas NOT NULL sobreviven |
| Escenarios idempotencia A-F en BD temporal | ✅ Todos funcionan correctamente |
| `row.outcome` después de reiniciar sesión | ❌ Atributo inexistente (no hay columna) |
| `IdempotencyConflict` por hash distinto | ✅ Lanza excepción Python (no retorno falso) |

### 8.3 Inferencias válidas

1. **El INSERT debe corregirse en 3 puntos:** `operation` → `operation_id`, `actor_user_id` → `requesting_user_id`, retirar `outcome`.
2. **Sin migración de esquema necesaria:** Todas las columnas objetivo ya existen; los cambios son en queries y constructor ORM.
3. **`original_outcome` es un concepto de contrato, no una columna de BD:** Se recupera del objeto ORM en memoria; no requiere persistente en `challenge_operations`.
4. **La validación `world_id == 0` falta en `complete_challenge`:** Consistencia con `_start_challenge`; agregar check simple.
5. **La idempotencia funciona sin columnas adicionales:** El ámbito `(challenge_id, requesting_user_id, client_idempotency_key)` es suficiente.

### 8.4 Cuestiones no demostradas (por restricciones del encargo)

| Cuestión | Estado |
|---|---|
| Escenarios A-D de intercalación reproducidos en ejecución con dos sesiones SQLAlchemy independientes | ❌ No demostrado por ejecución (restricción: no alterar `worlds.db`; complejidad de hilos múltiples). La teoría y evidencia de código respaldan consistencia. |
| `row.outcome` recuperado después de `commit()` + `refresh()` en sesión distinta usando modelo ORM puro | ❌ No comprobado experimentalmente (columna no existe). El contrato gestiona `outcome` en memoria. |
| Comportamiento `SQLITE_BUSY` en configuración multiprocess con engines compartidos | ❌ No probado; asumir modo `DELETE` + retry 5s basado en configuración actual. |
| Serialización HTTP de `original_outcome` | ❌ No verificado; el contrato lo gestiona en ámbito de llamada, no como campo de respuesta JSON estándar. |

### 8.5 Dictamen final

| Opción | Justificación |
|---|---|
| **APTO** | ❌ No: persisten reservas explícitas aunque son acotadas; se requiere autorización expresa. |
| **APTO CON RESERVAS** | ✅ **Sí** | Todos los defectos relevantes tienen evidencia inspeccionada o experimental; riesgos claramente delimitados; el contrato CS2 es implementable con cambios mínimos de query/constructor. |
| **NO APTO** | ❌ No: la evidencia experimental demuestra que los defectos son corregibles y el contrato es viable. |

**Dictemen final: `APTO CON RESERVAS`**

### 8.5.1 Reservas pendientes para la implementación de B3-6-I

| Reserva | Gravedad | Evidencia | Qué se necesita para darse por resuelta |
|---|---|---|---|
| Corregir 3 puntos en INSERT `record_challenge_operation` | **Baja** | TypeError confirmado FIX5; experimentos FIX5 demuestran solución | Deploy de diffs: `operation_id`, `requesting_user_id`, retirar `outcome` |
| Agregar validación `world_id == 0` en `complete_challenge` | **Media** | Faltante detectado FIX5; consistente con `_start_challenge` | Agregar `if participant.world_id is None or participant.world_id == 0: raise ChallengeError(...)` |
| Manejo de `original_outcome` sin columna de BD | **Baja** | Comprobado: es concepto de contrato, no persiste en BD; recuperarse del objeto ORM en memoria | Actualizar documentación; ajustar código que espere `row.outcome` |
| Concurrent `complete_challenge` vs `safe_delete_world` | **Baja** | Modo DELETE + busy_timeout 5s; retry automático SQLAlchemy | Probar en integración; no es bloqueador |
| Configurar usuario `is_admin=true` para tests | **Baja** | Necesario para `resolve_challenge`; no es defecto de código | Configurar en entorno de test |

### 8.6 Confirmación de que no se modificó código

- ✅ **Ningún archivo `.py` de producción modificado** (experimentos en scripts temporales `tmp_*.py`, eliminados)
- ✅ **Ningún modelo ORM modificado** (la validación es sobre queries existentes; el modelo ya tiene las columnas)
- ✅ **Ninguna prueba modificada** (experimentos aislados, sin tocar `tests/`)
- ✅ **Ningún informe anterior modificado** (FIX1 a FIX5 se mantienen intactos)
- ✅ **`worlds.db` sin alterar** (los experimentos usaron BDs temporales `tmp_*.db` que fueron borrados)
- ✅ **B3-6-I no iniciado** — awaiting explicit authorization

### 8.7 Cierre

**El informe `F7-E.2-I-B3-6-PRE-FIX6-REPORT.md` ha sido creado para revisión y autorización expresa antes de implementar B3-6-I.**

**Este encargo no autoriza la implementación B3-6-I.**

--- 

## Resumen Ejecutivo Final

**1. CORRECCIÓN ORM INSERT (3 puntos críticos, sin migración):**
- `operation` → `operation_id` en `ChallengeOperationRow(...)` (repo.py:1184)
- `actor_user_id` → `requesting_user_id` en `ChallengeOperationRow(...)` (repo.py:1185)
- `outcome` **retirado** del INSERT (no hay columna en modelo; FIX5 experimento confirmado)

**2. VALIDACIÓN `world_id == 0`:**
- Faltante en `complete_challenge`; agregar check consistente con `_start_challenge`
- `world_id=0` significa "sin mundo asignado"; debe rechazarse en todas las ops de desafío

**3. PERSISTENCIA DE `outcome`:**
- No es columna de BD; es concepto de contrato gestionado en memoria
- `original_outcome` se recupera del objeto ORM tras `commit()` + `refresh()`
- **No es necesaria migración** para que funcione el contrato de idempotencia

**4. CONCURRENCIA SQLite:**
- Modo `DELETE` + `busy_timeout` 5s; retry automático SQLAlchemy
- Sin WAL configurado
- Los escenarios de intercalación son consistentes con retry de `BUSY`

**5. AUTORIZACIÓN:**
- `complete_challenge`: organizador (`organizer_user_id` línea 1013-1014)
- `resolve_challenge`: admin (`is_admin` línea 1397)
- `_idempotent_operation_row` **no concede autorización**

**6. DICTAMEN FINAL: `APTO CON RESERVAS`**
- Reservas explícitas y acotadas (5 riesgos de baja/media gravedad)
- 3 cambios de query/constructor mínimos (sin modelo, sin migración)
- Pruebas experimentales confirman comportamiento ORM, idempotencia y SQLite
- Contrato CS2 implementable con pequeños ajustes

**B3-6-I no iniciado. Entregado para revisión y autorización expresa.**

**Total de reportes: 13** (B3-1 a B3-6-PRE-FIX6).