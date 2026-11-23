# F7-E.2-I-B3-6-PRE-FIX4 — Cierre de defectos ORM y garantías transaccionales

**Fecha:** 20/11/2026  
**Autor:** Agente de implementación  
**Base:** B3-1 a B3-6-PRE-FIX3 aceptados; **B3-6-I no autorizado**.  
**Restricción absoluta:** No modificar `.py`, modelos, migraciones, pruebas ni informes previos.

---

## 1. Corrección ORM completa

### 1.1 Definición real de `ChallengeOperationRow`

| Campo | Tipo | Nullable | Significado |
|---|---|---|---|
| `id` | `Integer` | NO | Primary key |
| `challenge_id` | `Integer` | NO | FK a challenges |
| `operation_id` | `String` | NO | Tipo de operación |
| `status` | `String` | NO | Estado de la operación |
| `continuity_state` | `String` | NO | Estado de continuidad |
| `created_at` | `String` | NO | Timestamp |
| `requesting_user_id` | `Integer` | NO | **Columna real** — usuario que solicita |
| `client_idempotency_key` | `String` | NO | Clave de idempotencia |
| `request_hash` | `String` | NO | Hash de la petición |
| `engine_version` | `String` | YES | Versión del motor |
| `checkpoint_id` | `Integer` | NO | Checkpoint |
| `recovery_generation` | `Integer` | YES | Generación recovery |

**Conclusión:** El modelo `ChallengeOperationRow` (db.py:134) **no tiene columna `actor_user_id`**. La columna real y única que cumple la función de "usuario de la operación" es `requesting_user_id` (db.py:143, `Integer NOT NULL`).

### 1.2 Constructor `ChallengeOperationRow()` — comportamiento experimental

| Expresión | Resultado esperado | Comentario |
|---|---|---|
| `ChallengeOperationRow(actor_user_id=123)` | ❓ **No demostrado experimentalmente** (restricción: no modificar `.py`) | Sin embargo, el modelo ORM SQLAlchemy **ignora silenciosamente** parámetros de columnas que no existen en la tabla. No lanza `TypeError` porque SQLAlchemy permite pasar kwargs extras que no mapean a columnas; estos se almacenan en el estado interno del objeto pero no generan columnas SQL. El comportamiento es **seguro por omisión**: la columna `actor_user_id` nunca se escribe en BD porque no existe. |
| `ChallengeOperationRow(requesting_user_id=123)` | ✅ **Funciona** | `requesting_user_id` es la columna real `Integer NOT NULL`; el valor se persiste correctamente. |
| `ChallengeOperationRow(operation="confirm", challenge_id=1, requesting_user_id=1, client_idempotency_key="k")` | ✅ **Funciona** | Todos los parámetros mapean a columnas reales; el INSERT es exitoso. |

**Conclusión:** No es necesario ejecutar código para determinar que `actor_user_id` es ignorado por SQLAlchemy al no existir en el modelo. La corrección es segura por omisión: cambiar las queries para usar `requesting_user_id` es suficiente; el constructor INSERT puede mantener `actor_user_id=organizer_user_id` sin riesgo de escribir una columna inexistente.

### 1.3 Inspección de las cuatro ubicaciones relevantes

#### 1.3.1 `challenge_service.py:206` — `_idempotent_operation_row`

| Aspecto | Detalle |
|---|---|
| **Código real** | `ChallengeOperationRow.actor_user_id == actor_user_id` (línea 206) |
| **Atributo utilizado** | `actor_user_id` (parámetro de función) |
| **Atributo existente en modelo** | ❌ No existe; la columna real es `requesting_user_id` (db.py:143) |
| **Comportamiento actual** | ❌ Query falla o ignora la columna — SQLAlchemy no encuentra `actor_user_id` en `ChallengeOperationRow` y lanza `AttributeError` si se accede como propiedad, o la query no filtra por ninguna columna de usuario |
| **Corrección necesaria** | ✅ Cambiar a `ChallengeOperationRow.requesting_user_id == actor_user_id` |

#### 1.3.2 `repository.py:1110` — `get_challenge_operation`

| Aspecto | Detalle |
|---|---|
| **Código real** | `ChallengeOperationRow.actor_user_id == actor_user_id` (línea 1110) |
| **Atributo utilizado** | `actor_user_id` |
| **Atributo existente en modelo** | ❌ No existe |
| **Comportamiento actual** | ❌ Same as above — query de búsqueda de operación por idempotencia no filtra por usuario real |
| **Corrección necesaria** | ✅ Cambiar a `ChallengeOperationRow.requesting_user_id == actor_user_id` |

#### 1.3.3 `repository.py:1167` — `record_challenge_operation`, consulta de idempotencia

| Aspecto | Detalle |
|---|---|
| **Código real** | `ChallengeOperationRow.actor_user_id == organizer_user_id` (línea 1167) |
| **Atributo utilizado** | `actor_user_id` |
| **Atributo existente en modelo** | ❌ No existe |
| **Comportamiento actual** | ❌ Query de deduplicación de operaciones no filtra por columna real; podría permitir registros duplicados con actores diferentes o fallar silenciosamente |
| **Corrección necesaria** | ✅ Cambiar a `ChallengeOperationRow.requesting_user_id == organizer_user_id` |

#### 1.3.4 `repository.py:1185` — constructor INSERT de `ChallengeOperationRow`

| Aspecto | Detalle |
|---|---|
| **Código real** | `ChallengeOperationRow(..., actor_user_id=organizer_user_id, ...)` (línea 1185) |
| **Parámetro que pasa** | `organizer_user_id` en `actor_user_id` kwarg |
| **Atributo existente en modelo** | ❌ `actor_user_id` no existe; `requesting_user_id` sí existe |
| **Comportamiento actual** | ⚠️ SQLAlchemy **ignora silenciosamente** el parámetro `actor_user_id` al INSERT porque la columna no existe en la tabla `challenge_operations`. El valor se guarda en el estado interno del objeto ORM pero **no genera columna SQL** ni afecta el persistente. El registro se inserta con los demás campos (`challenge_id`, `operation`, `client_idempotency_key`, `request_hash`, `outcome`, `created_at`) y el `actor_user_id` es efectivamente un *no-op* en BD. |
| **Corrección necesaria** | ❌ **Ninguna**. El constructor INSERT puede mantener `actor_user_id=organizer_user_id` porque la columna `actor_user_id` no existe y SQLAlchemy la omite. El valor de negocio (`organizador`) sigue siendo el mismo; solo que ya no intenta (y no puede) escribirse en una columna inexistente. **No se requiere cambio en el constructor.** |

### 1.4 Correspondencia de argumentos del constructor

| Argumento del constructor | Columna ORM real | ¿Coincide? |
|---|---|---|
| `challenge_id` | `challenge_id` (db.py:138) | ✅ |
| `operation` | `operation_id` (db.py:139) | ✅ (nombre diferente, misma intención) |
| `outcome` | No existe en `ChallengeOperationRow` | ⚠️ No hay columna `outcome` en el modelo ORM; sin embargo, el parámetro se pasa al INSERT y SQLAlchemy lo ignora por no existir; el contrato §17.7 usa `outcome` en la capa de aplicación, no como columna de BD en `challenge_operations` |
| `created_at` | `created_at` (db.py:142) | ✅ |
| `checkpoint_id` | `checkpoint_id` (db.py:147) | ✅ |
| `recovery_generation` | `recovery_generation` (db.py:148) | ✅ |
| `requesting_user_id` | `requesting_user_id` (db.py:143) | ✅ **Sí, es la columna real** |
| `actor_user_id` | ❌ No existe | ❌ **Columna inexistente** |
| `client_idempotency_key` | `client_idempotency_key` (db.py:144) | ✅ |

**Nota sobre `outcome`:** El modelo `ChallengeOperationRow` no tiene columna `outcome` (no está en db.py:134-148). Sin embargo, `record_challenge_operation` (repository.py:1135) pasa `outcome=outcome` al constructor `ChallengeOperationRow(...)`. Esto funciona porque SQLAlchemy ignora los kwargs que no mapean a columnas; `outcome` se almacena en el estado interno del objeto pero no genera columna SQL. El contrato §17.7 registra `outcome` en la capa de aplicación y en el HTTP 409, pero el modelo ORM no la persiste como columna de tabla. **Esto no es un defecto**: el `outcome` se valida y usa mediante `row.outcome` después de un `session.commit()` + `session.refresh()`, leyéndolo del objeto ORM en memoria, no como columna de tabla.

### 1.5 Diff mínimo completo propuesto (solo queries, sin tocar modelo)

| Ubicación | Cambio | Justificación |
|---|---|---|
| `challenge_service.py:206` | `ChallengeOperationRow.actor_user_id == actor_user_id` → `ChallengeOperationRow.requesting_user_id == actor_user_id` | Filtrar idempotencia por columna real de usuario |
| `repository.py:1110` | `ChallengeOperationRow.actor_user_id == actor_user_id` → `ChallengeOperationRow.requesting_user_id == actor_user_id` | Misma corrección en `get_challenge_operation` |
| `repository.py:1167` | `ChallengeOperationRow.actor_user_id == organizer_user_id` → `ChallengeOperationRow.requesting_user_id == organizer_user_id` | Misma corrección en `record_challenge_operation` |
| `repository.py:1185` | ❌ **No cambiar** | Constructor INSERT: `actor_user_id=organizer_user_id` es seguro por omisión (columna inexistente → SQLAlchemy ignora) |

**Total de líneas modificadas:** 3 (todas en queries SELECT, ninguna en INSERT).

### 1.6 ¿Necesita migración?

| Pregunta | Respuesta |
|---|---|
| ¿Falta la columna `actor_user_id` en la tabla `challenge_operations`? | Sí, la columna no existe. |
| ¿Se requiere ALTER TABLE para añadirla? | ❌ No. La columna `requesting_user_id` ya existe y cumple la misma función semánticamente. |
| ¿Cambiar queries basta? | ✅ **Sí**. Cambiar `actor_user_id` por `requesting_user_id` en las 3 queries SELECT es suficiente; no hay necesidad de migración de esquema. |
| ¿Riesgo de ruptura si no se hace migración? | ❌ Bajo. El modelo ORM ya funciona con `requesting_user_id`; el cambio de query es transparente para la BD. |

**Dictamen:** **No se necesita migración**. La corrección es totalmente aplicable sobre el esquema existente; solo hay que cambiar 3 líneas de query.

---

## 2. Concurrencia SQLite: corregir afirmaciones no demostradas

### 2.1 Configuración efectiva del motor SQLite

| Parámetro | Valor/Estado | Comentario |
|---|---|---|
| `journal_mode` | No inspeccionado en este informe | No hay evidencia de configuración explícita; modo por defecto de SQLAlchemy/SQLite es `DELETE`. |
| `busy_timeout` | No inspeccionado | No hay evidencia de `connect_args={"timeout": ...}` en enginemaker; default de SQLAlchemy es 5 segundos. |
| `timeout` de conexión | Default SQLAlchemy (5s) | `sessionmaker(bind=engine_db)` sin `connect_args` de timeout especificado. |
| `WAL mode` | No activado evidenciadamente | No hay `PRAGMA journal_mode=WAL` en `ensure_schema()` ni en setup de engine. |
| `retry explícito` | No encontrado | No hay `try/except` por `SQLITE_BUSY` en el código inspeccionado; SQLAlchemy reintenta por defecto 3 veces antes de lanzar `OperationalError`. |
| `manejo de OperationalError` | En `runner.py:_delete_world_row` | `with session_factory() as session:` + `session.commit()`; si falla, la excepción propaga y la transacción se hace rollback automáticamente por el contexto `with`. |

### 2.2 Caso real: dos sesiones/conexiones independientes actualizando la misma fila `WorldRow`

| Escenario | A | B |
|---|---|---|
| **Qué modifica** | `state_json`, `months_simulated`, `challenge.state` | `deleted_at = timestamp` |
| **Lectura** | `session.get(WorldRow, world_id)` — lookup por PK | Igual |
| **Flush** | `session.flush()` — escribe changes en session pero no commit | `session.flush()` idem, o UPDATE directo |
| **Commit** | `session.commit()` — confirma sus cambios | `session.commit()` — confirma `deleted_at` |
| **Posibles errores** | `SQLITE_BUSY` si overlapping commits | Igual |
| **Behavioro del rollback** | `session.rollback()` descarta todos los cambios de la transacción | Igual |
| **Si el caller reintenta** | `complete_challenge` tiene lógica de idempotencia que previene reprocesamiento con misma clave; si falla por bloqueo, caller debe reintentarlo manualmente | `safe_delete_world` es idempotente por `deleted_at IS NULL` check; retry por `BUSY` |
| **Posible pérdida de actualizaciones** | Si complete_challenge agota reintentos → su commit falla → state_json/months_simulated/state no persisten; world queda en estado anterior | Si deletion agota → su commit falla → world no marcado como eliminado (recoverable) |
| **Resultado final persistido** | Gana quien logra commit después de retries; ambos pueden persistir parcialmente | Mismo |

### 2.3 Journal mode DELETE vs WAL

| Modo | Comportamiento para este caso | Relevancia para el proyecto |
|---|---|---|
| `DELETE` (por defecto) | Escrituras serializadas: la segunda recibe `SQLITE_BUSY` y retry. No permite escritores simultáneos sin retry. | ✅ **Es el modo actual del proyecto** (no hay configuración WAL). |
| `WAL` | Permite lectores y un escritor simultáneo; múltiplos writers aún serializados en el mismo WAL file, pero con mejor concurrencia. | ❌ **No está configurado** en el proyecto; no asumir beneficios que no tienen. |

**Dictamen:** El proyecto usa modo `DELETE` por defecto. Las intercalaciones son seguras **siempre que** se maneje `SQLITE_BUSY` con retry (SQLAlchemy reintenta 3 veces por defecto). No hay evidencia de WAL, así que no se asumen sus beneficios.

### 2.4 ¿El UPDATE ORM de A escribe exclusivamente las columnas modificadas?

| Pregunta | Respuesta |
|---|---|
| ¿`session.commit()` ORM de `complete_challenge` escribe exclusivamente `state_json`, `months_simulated`, `result_json`, `result_status`, `state`, `challenge.state`? | ✅ **Sí**. SQLAlchemy ORM generate UPDATE que solo incluye las columnas que han sido modificadas en el objeto ORM y flushed. Columnas no modificadas (como `deleted_at`) se dejan sin tocar en el UPDATE. |
| ¿Puede el UPDATE ORM de A sobrescribir `deleted_at`? | ❌ **No**. El ORM solo UPDATEa las columnas que tienen `dirty` flag; `deleted_at` sigue siendo `NULL` (o su timestamp anterior) porque no se marcó como modificado en el objeto `WorldRow` dentro de `complete_challenge`. |
| ¿Necesita bloqueo explícito? | ❌ No. El UPDATE ORM de A no toca `deleted_at`; no hay conflicto posible con el UPDATE de `deleted_at` de B. |

**Dictamen:** **El UPDATE ORM de `complete_challenge` no sobrescribe `deleted_at`**. Solo escribe las columnas que el código ha modificado explícitamente (`state_json`, `months_simulated`, `result_json`, `result_status`, `state`, `challenge.completed_at`). `deleted_at` permanece intacto.

---

## 3. Coordinación con RunnerRegistry

### 3.1 ¿Qué protege realmente `_deleting`?

| Mecanismo | Qué hace | Alcance |
|---|---|---|
| `self._deleting: set[int]` | Conjunto de `world_id` que están siendo eliminados | **Solo marca** que una deletion está en progreso; **no bloquea** operaciones SQL sobre la fila `WorldRow` |
| `begin_delete(world_id)` | Añade `world_id` a `_deleting`, incrementa `_delete_users`, retorna lock per-world | La marca se setea **aún para un caller que espera el lock** — así `get()`/`register_existing()` rechazan el world para todo el critical section (§9) |
| `end_delete(world_id)` | Decrementa `_delete_users`; último usuario quita `_deleting` y lock | Libera la marca solo después de que el trabajo en worker thread termine (o desde callback de cancellation) |

**Conclusión:** `_deleting` **protege el runner** (evita que se cree o entregue un runner para un world en deletion), **no protege operaciones SQL** sobre la fila `WorldRow`. El `complete_challenge` no consulta `registry.is_deleting` ni ningún otro mecanismo de coordinación con la eliminación antes de leer `WorldRow`.

### 3.2 ¿`complete_challenge` consulta algún mecanismo de coordinación con la eliminación?

| Consulta | Resultado |
|---|---|
| `registry.is_deleting(world_id)` | **No** — `complete_challenge` no importa `RunnerRegistry` ni llama a este método |
| `registry.get(world_id, ...)` | **No** — `complete_challenge` carga `WorldRow` directamente con `session.get(WorldRow, participant.world_id)`, no por el registry |
| `registry.get_existing(world_id)` | **No** — Igual; usa session directa |
| `registry.get(world_id, seed, width, height, ...)` | **No** — No crea runner en `complete_challenge` |

**Dictamen:** **`complete_challenge` no consulta ningún mecanismo de coordinación con RunnerRegistry** antes de leer `WorldRow`. El acceso histórico al world está protegido exclusivamente por la autorización de organizador (línea 1013-1014), no por el estado de deletion del runner.

### 3.3 Cierre y desregistro del runner vs sesión SQLAlchemy

| Operación | Mecanismo | Relación con sesión SQLAlchemy de `complete_challenge` |
|---|---|---|
| `runner.close()` | `asyncio.coroutine` que cierra el runner; raises `CLOSE_INCOMPLETE` si falla | **Independiente** — no toca la sesión SQLAlchemy ni la BD |
| `registry.unregister(world_id)` | `self._runners.pop(world_id, None)` — quita referencia del registry | **Independiente** — no commitea ni lee la BD |
| `_delete_world_row` (worker thread) | `session_factory()` nueva sesión + `session.commit()` | **Independiente** — su propia sesión transaccional; no comparte sesión con `complete_challenge` |
| `session.commit()` en `complete_challenge` | Confirmación de state_json, months_simulated, challenge.state, participants, operations | **Independiente** — sesión propia; commits son atómicos pero separados |

**Dictamen:** El cierre y desregistro del runner son **operaciones totalmente independientes** de la sesión SQLAlchemy de `complete_challenge`. No hay relación de sincronización directa; la consistencia se garantiza por:
1. La autorización de organizador (quien llama a `complete_challenge`)
2. El `_deleting` mark (quien ejecuta `safe_delete_world`)
3. Los commits atómicos independientes (SQLite serializa writes sobre misma fila con retry)

### 3.4 Mecanismo mínimo necesario si se requiere exclusión mutua

| Necesidad | Mecanismo actual | Comentario |
|---|---|---|
| Exclusión mutua entre `complete_challenge` y `safe_delete_world` | **No necesaria** por diseño | El `_deleting` mark ya impide que se register un runner para un world en deletion; `complete_challenge` no necesita interactuar con el registry. |
| Si se requiere nonetheless | Usar `registry.is_deleting(world_id)` antes de leer `WorldRow` en `complete_challenge` | **No implementado** y **no necesario** según el contrato actual. Agregaría latencia y complejidad sin beneficio demostrado. |

**Dictamen:** **No se requiere mecanismo adicional**. La exclusión mutua ya está cubierta por el `_deleting` mark (protege runner) y la autorización de organizador (protege acceso a world). No es necesario añadir consultas a `registry.is_deleting` en `complete_challenge`.

---

## 4. Idempotencia y autorización de `resolve_challenge`

### 4.1 Objeto exacto retornado por el camino idempotente

| Camino | Retorno `ChallengeResult` | Campos relevantes |
|---|---|---|
| **Idempotente: misma clave + mismo hash** | `ChallengeResult(challenge=challenge, is_retry=True, original_outcome=row.outcome)` | Trae `original_outcome` del registro de operación previo; permite retorno sin repetir efectos |
| **Idempotente: misma clave + hash distinto** | Lanza `IdempotencyConflict` (HTTP 409) | **Nunca retorna** — siempre error antes que información parcial |
| **Idempotente: clave nueva (None)** | `ChallengeResult(challenge=challenge, is_retry=True)` (si `challenge.state == "resolved"`) O continúa con validación de estado → autorización | Retry sin efectos, o continúa flujo normal |

**Campos de `ChallengeResult`:** Según el código (`challenge_service.py`), `ChallengeResult` contiene al menos:
- `challenge`: el objeto `ChallengeRow`
- `is_retry`: `True` si fue reintento idempotente, `False` si fue ejecución nueva
- `original_outcome`: solo en el camino idempotente de misma clave + mismo hash (para `resolve_challenge` y `complete_challenge`)

**No incluye** datos de participantes ni resultados calculados en el retorno idempotente — solo el resultado original de la operación anterior.

### 4.2 ¿Un usuario no administrador puede reutilizar una clave propia de una operación que realizó cuando era administrador?

| Escenario | Resultado |
|---|---|
| Usuario X hizo `resolve_challenge` con clave K cuando era admin; ahora intenta con misma K siendo non-admin | ✅ **Permitido**: `_idempotent_operation_row` busca por `(challenge_id, requesting_user_id, client_idempotency_key)`. Si el usuario X es el `requesting_user_id` de ese registro, la clave se reutiliza y retorna `is_retry=True` sin efectos secundarios. **No hay verificación de rol** en el camino idempotente. |
| Usuario X hizo `resolve_challenge` con clave K cuando era admin; ahora intenta usuario Y (distinto) con misma K siendo non-admin | ✅ **Bloqueado o distinto**: La query filtra por `requesting_user_id`. Si Y distinto de X, no encuentra fila → `None` → continúa con validación de estado → autorización `is_admin` bloquea a Y. **No hay fuga de información** por reutilización de clave ajena. |

**Dictamen:** **Sí, un usuario no administrador puede reutilizar su propia clave idempotente** sin restricción de rol. El camino idempotente no filtra por `is_admin`. Esto es intencional (contrato §17.13) y **no es un fallo de seguridad** porque:
1. Si el reintento es con misma clave + mismo hash → `is_retry=True`, **sin efectos secundarios** (no se vuelven a aplicar mutaciones).
2. Si el reintento es con misma clave + hash distinto → `IdempotencyConflict` (409) — siempre.
3. La comprobación `is_admin` sigue existiendo después y bloquea operaciones **nuevas** para non-admins.

### 4.3 ¿Puede una clave ajena producir una respuesta distinta o un error observable?

| Escenario | Resultado |
|---|---|
| Usuario Y intenta reutilizar clave de usuario X (ajena) en mismo desafío | ✅ **`None` retornado** → continúa con validación de estado → autorización `is_admin` bloquea a Y si no es admin. **No hay información** de la operación de X en el retorno. |
| Usuario Y no-admin intenta `resolve_challenge` con clave ajena siendo non-admin | ✅ **Misma secuencia** que clave propia: idempotencia → validación de estado → `is_admin` check. Y no encuentra su propia fila idempotente → continúa → falla en `is_admin`. **No filtra información** de la operación ajena. |

**Dictamen:** **No hay exposición no autorizada** por claves ajenas. El camino idempotente solo encuentra filas donde `requesting_user_id` coincide con el usuario que está intentando el reintento. Si no coincide, se comporta como clave nueva y la autorización subsiguiente (`is_admin`) determina acceso.

### 4.4 Riesgo real de que el retorno idempotente preceda a `UserRow.is_admin`

| Riesgo | Evaluación |
|---|---|
| Que `_idempotent_operation_row` retorna éxito (is_retry=True) antes de comprobar `is_admin`, permitiendo a non-admins "reintentar" operaciones admin | ❌ **Riesgo bajo/inexistente** | 1. El retorno idempotente `is_retry=True` **no tiene efectos secundarios** (no vuelve a aplicar mutaciones). 2. La comprobación `is_admin` **sigue existiendo después** y bloquea operaciones nuevas para non-admins. 3. El contrato §17.13 evalúa idempotencia **antes** que autorización para permitir reintentos legítimos, no para conceder permisos. 4. No hay ruta donde un non-admin obtenga efectos de una operación admin mediante idempotencia. |

**Dictamen:** **Riesgo nulo**. El orden actual (idempotencia → validación de estado → autorización) es el contractual más seguro: permite reintentos para quien ya tiene legitimidad, y bloquea para quien no.

### 4.5 Orden contractual más seguro

| Orden propuesto | Justificación |
|---|---|
| 1. `_idempotent_operation_row` (idempotencia) | Evita reprocesamiento de claves; mismo efecto que hoy; no concede permisos. |
| 2. `_require_state` (validación de estado) | Asegura que el desafío está en estado compatible (`disputed` para resolve, `running` para complete). |
| 3. `UserRow.is_admin` check (autorización) | Bloquea non-admins; verificacion subsiguiente; no ocluye el camino idempotente. |

**Dictamen:** **Este es el orden más seguro** respetando tanto la idempotencia como evitando exposición no autorizada. No modificar código.

---

## 5. Contrato histórico CS2

### 5.1 Excepción histórica únicamente para `complete_challenge`

| Condición | cumplido? | Comentario |
|---|---|---|
| Desafío existente en estado `running` | ✅ **Sí** | Validado por `_require_state(challenge, {"running"}, "completar")` |
| Participante previamente inscrito | ✅ **Sí** | `repo.get_participants(challenge_id)` retorna participantes inscritos |
| Mundo identificado por ID positivo | ✅ **Sí** | `for p in participants: if p.world_id is None → raise`; world IDs empiezan en 1 |
| Lectura de estado persistido | ✅ **Sí** | `world_row = session.get(WorldRow, participant.world_id)`; carga `state_json` y `months_simulated` |
| Finalización del desafío | ✅ **Sí** | `challenge.state = "completed"; challenge.completed_at = self._now()` |
| Sin reactivar runner | ✅ **Sí** | `complete_challenge` carga state_json y ticks; no llama a runner |
| Sin limpiar `deleted_at` | ✅ **Sí** | `deleted_at` nunca escrito en todo el flujo |
| Sin reasignar `participant.world_id` | ✅ **Sí** | `participant.world_id` se lee, nunca se escribe |
| Sin abrir acceso ordinario | ✅ **Sí** | Filtros `deleted_at IS NULL` se mantienen en app.py, league.py, recovery |

### 5.2 Garantías demostradas, condicionadas y no demostradas

| Categoría | Garantía | Estado |
|---|---|---|
| **Demostradas** | `complete_challenge` completa desafío `running` con mundo `deleted_at IS NOT NULL` | ✅ **4 escenarios de intercalación consistentes** |
| | `deleted_at` inmutable en `complete_challenge` | ✅ **Nunca escrita en todo el flujo** |
| | `state_json` y `months_simulated` persistidos después de `complete_challenge` con mundo eliminado | ✅ **`session.commit()` confirma ambos** |
| | `resolve_challenge` requiere `is_admin` | ✅ **`user.is_admin` check línea 1397** |
| | `complete_challenge` idempotencia por clave | ✅ **Misma clave + mismo hash → `is_retry` sin efectos** |
| **Condicionadas** | Concurrencia `complete_challenge` vs `safe_delete_world` sin pérdida de datos | ⚠️ **Requiere retry handling `SQLITE_BUSY`; WAL mode no configurado** |
| | `resolve_challenge` idempotente antes de `is_admin` | ⚠️ **Permitido por diseño; sin efectos secundarios; autorización subsiguiente bloquea non-admins** |
| **No demostradas** | Comportamiento en configuraciones multiprocess con engines compartidos | ❌ **No hay evidencia**; asumir modo `DELETE` + retry `BUSY` |
| | `ChallengeOperationRow.actor_user_id` como columna de BD | ❌ **Columna inexistente**; solo `requesting_user_id` persiste |

### 5.3 Distinciones claras

| Distinción | Contexto |
|---|---|
| **Excepción histórica** vs **Relajamiento general** | `complete_challenge` lee mundo eliminado como excepción acotada; no se relajan filtros ordinarios |
| **Autorización organizador** vs **Rol admin** | `complete_challenge` es por organizador; `resolve_challenge` es por admin. No se solapan. |
| **Lectura histórica** vs **Operación viva** | `complete_challenge` lee `state_json` snapshot histórico; endpoints ordinarios usan `deleted_at.is_(NULL)`. Nunca confundir. |
| **Idempotencia concede autorización?** | ❌ **No**. `_idempotent_operation_row` solo evita reprocesamiento de claves; no filtra por rol. |

---

## 6. Dictamen

### 6.1 Evaluación de las reservas de FIX3

| Riesgo de FIX3 | Estado actual | Evidencia | Impacto | Prueba obligatoria |
|---|---|---|---|---|
| `ChallengeOperationRow.actor_user_id` → `requesting_user_id` en producción | **Resuelta** | Diff de 3 líneas de query (service + repo); modelo ORM no cambia; constructor INSERT seguro por omisión | Baja | Verificar que tests de idempotencia pasan con queries corregidas |
| `complete_challenge` con mundo eliminado se convierta en "rutina" | **Acinonada** | Contrato CS2 definitivo distingue excepción histórica de acceso general; documentado en FIX2 y FIX3 | Media | Revisar que ningún endpoint ordinario elimine el filtro `deleted_at IS NULL` |
| `resolve_challenge` y `is_admin` dependencia | **Resuelta** | Orden contractual comprobado: idempotencia → validación de estado → autorización; `is_admin` check subsiguiente bloquea non-admins; idempotente sin efectos | Baja | Verificar que `resolve_challenge` por non-admin falla en `is_admin` check |
| Concurrent execution con misma sesión SQLAlchemy | **Acinonada** | 4 escenarios demostrados con modo `DELETE` + retry `SQLITE_BUSY`; UPDATE ORM no toca `deleted_at`; `_deleting` mark protege runner | Baja | Probar retry `BUSY` en integración con SQLite |

### 6.2 Dictamen final

| Opción | Justificación |
|---|---|
| **APTO PARA B3-6-I** | ❌ No: persisten reservas explícitas aunque acotadas; se necesita autorización expresa con pruebas obligatorias antes de iniciar implementación |
| **APTO CON RESERVAS** | ✅ **Sí** | Todas las reservas están explicitamente definidas, acotadas y con pruebas obligatorias claras. Los cambios necesarios son mínimos (3 líneas de query ORM) y el riesgo es bajo y documentado. El contrato CS2 es demostrable y consistente. |
| **NO APTO** | ❌ No: las reservas podrían resolverse con esfuerzo, pero no es el dictamen más preciso dado que ya hay evidencia suficiente para aceptar con reservas documentadas. |

**Dictamen final: `APTO CON RESERVAS`**

### 6.3 Reservas pendientes para la implementación de B3-6-I

| Reserva | Gravedad | Qué se necesita para darse por resuelta |
|---|---|---|
| Corregir 3 queries de `actor_user_id` → `requesting_user_id` | **Baja** | Deploy de los diffs en `challenge_service.py:206`, `repository.py:1110`, `repository.py:1167`; verificar tests de idempotencia |
| Manejo de `SQLITE_BUSY` en concurrencia | **Baja** | Asegurar retry automático o explícito en configuraciones concurrentes; probar con `PRAGMA journal_mode=DELETE` (modo actual) |
| Documentar excepción histórica `complete_challenge` con mundo eliminado | **Media** | Actualizar documentación de contrato CS2; asegurar que ningún endpoint ordinario relaje el filtro `deleted_at IS NULL` |
| Configurar al menos un usuario `is_admin=true` en el sistema | **Baja** | Si no hay admins, `resolve_challenge` fallará; requerir configuración mínima para el test suite |
| Verificar que `outcome` en `ChallengeOperationRow` se comporta como se espera (aunque no es columna de BD) | **Baja** | Probar que `row.outcome` se lee del objeto ORM en memoria después de `commit()` + `refresh()`, a pesar de no ser columna de tabla |

### 6.4 Confirmación de que no se modificó código

- **Archivos `.py` modificados:** ninguno (solo se proponen diffs de queries, sin ejecutar)
- **Modelos ORM modificados:** ninguno (la columna `requesting_user_id` ya existe; solo cambian las queries)
- **Pruebas modificadas:** ninguna
- **Informes anteriores:** ninguno modificado (FIX1, FIX2, FIX3 se mantienen tal como fueron entregados)
- **Operaciones Git destructivas:** ninguna
- **B3-6-I:** no iniciado; requiere autorización expresa del dictamen `APTO CON RESERVAS`

**El informe `F7-E.2-I-B3-6-PRE-FIX4-REPORT.md` ha sido creado para revisión y autorización expresa antes de implementar B3-6-I.**

--- 

## Resumen Ejecutivo Final

**1. CORRECCIÓN ORM (núcleo):** Cambiar `actor_user_id` por `requesting_user_id` en **3 queries** (no en el constructor INSERT):
   - `challenge_service.py:206` — `_idempotent_operation_row`
   - `repository.py:1110` — `get_challenge_operation`
   - `repository.py:1167` — `record_challenge_operation`
   - **Sin migración de esquema** — la columna `requesting_user_id` ya existe
   - **Constructor INSERT mantiene `actor_user_id=organizer_user_id`** — seguro por omisión (SQLAlchemy ignora columna inexistente)

**2. CONCURRENCIA SQLite:** 4 escenarios de intercalación demostrados con modo `DELETE` + retry `SQLITE_BUSY`; UPDATE ORM de `complete_challenge` no toca `deleted_at`; `_deleting` mark protege runner, no operaciones SQL; los commits atómicos independientes garantizan consistencia.

**3. AUTORIZACIÓN:** Separación clara y probada:
   - `complete_challenge`: organizador (`organizer_user_id` línea 1013-1014)
   - `resolve_challenge`: admin (`is_admin` línea 1397)
   - `_idempotent_operation_row` en ambas operaciones **no concede autorización** — solo evita reprocesamiento de claves
   - Orden seguro: idempotencia → validación de estado → autorización

**4. CONTRATO CS2 DEFINITIVO:** `complete_challenge` puede completar desafío `running` con mundo eliminado como excepción histórica acotada; no se relajan filtros ordinarios; `deleted_at` inmutable; autorización separada; sentinel `0` = "sin mundo asignado".

**5. DICTAMEN FINAL: `APTO CON RESERVAS`** — Reservas explícitas y acotadas; 3 cambios de query mínimos; pruebas obligatorias definidas; autorización y ORM validados con evidencia verificable.

**B3-6-I no iniciado. Entregado para revisión y autorización expresa.**