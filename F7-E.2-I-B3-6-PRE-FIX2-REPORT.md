# F7-E.2-I-B3-6-PRE-FIX2 — Conciliación final de CS2

**Fecha:** 20/11/2026  
**Autor:** Agente de implementación  
**Base:** B3-1 a B3-5-FIX4 aceptados; B3-6-PRE-FIX1 aceptado con reservas; **B3-6-I no autorizado**.  
**Restricción absoluta:** No modificar `.py`, modelos, migraciones, pruebas ni informes previos.

## 1. Conciliación de la contradicción entre mundo eliminado y desafío completado

### 1.1 Hecho controvertido

El informe B3-5 estableció que `WorldRow.deleted_at IS NOT NULL` descarta lógicamente un mundo de acceso ordinario (B3-3 filtros A1/A2/A4/A6, B3-4 league filters, B3-5 prepare_worlds y start_challenge). Sin embargo, `complete_challenge` (línea 977, challenge_service.py) necesita cargar ese mismo `WorldRow` para ejecutar ticks pendientes y completar un desafío `running`.

**La contradicción aparente:** ¿Cómo puede `complete_challenge` leer un mundo eliminado mientras las demás operaciones lo excluyen?

**La conciliación:** La excepción es **histórica y acotada**, no un relajamiento general.

| Operación | `deleted_at IS NULL` | `deleted_at IS NOT NULL` | Justificación |
|---|---|---|---|
| `start_challenge` (B3-5 FIX2) | ✅ Permitido | ❌ `ChallengeError "no tiene mundo preparado"` | El desafío no puede iniciarse si no hay mundo asociado |
| `prepare_worlds` (B3-5 FIX1) | ✅ Nuevo mundo | ❌ `WorldNotFoundForParticipant` | El mundo eliminado no se sustituye; error explícito |
| `app.py A1/A2/A4/A6` | ✅ Listado ordinario | ❌ Filtrado excluye | Filtros de acceso público/ordinario |
| `complete_challenge` | ⚠️ Excepción histórica | ✅ **Permitido** | Lectura de `state_json` y `months_simulated` para completar desafío `running`; `deleted_at` **no se modifica** |
| `recovery_coordinator B3-2` | ✅ Listado | ❌ `deleted_at IS NULL` | Recovery no ve mundos eliminados |
| `league.py B3-4` | ✅ Claim/toast/session | ❌ Filtros excluyen | Acceso league, no desafío |

**Conclusión:** El único pathway que lee mundo con `deleted_at IS NOT NULL` es `complete_challenge` para desafíos `running`. No es una puerta abierta; es una necesidad de finalización histórica. La prohibición de operación viva se refiere al **mundo**, no al **estado administrativo del desafío**: un desafío `completed` es administrativamente válido aunque su mundo esté eliminado.

### 1.2 Campos que pueden actualizarse durante la finalización histórica

| Campo | Actualizable | Razón |
|---|---|---|
| `world_row.state_json` | ✅ Sí | Serialización del motor después de `eng.tick()`; necesario para métricas |
| `world_row.months_simulated` | ✅ Sí | Cuenta de ticks ya ejecutados; determina ticks restantes |
| `participant.result_json` | ✅ Sí | Métricas deterministas calculadas en el motor |
| `participant.result_status` | ✅ Sí: `"persisted"` → `"verified"` | Durabilidad del resultado dentro del desafío completado |
| `participant.state` | ✅ Sí: `"finalizado"` | Marca participante como finalizado; no altera asignación del mundo |
| `challenge.state` | ✅ Sí: `"running"` → `"completed"` | Transición durable del desafío; **no toca `world_row.deleted_at`** |
| `challenge.completed_at` | ✅ Sí: `self._now()` | Marca administrativa de cuándo se completó |

| Campo | No actualizable | Razón |
|---|---|---|
| `world_row.deleted_at` | ❌ Nunca | La eliminación lógica es irreversible por diseño (B3-5) |
| `world_row.id` | ❌ Clave primaria | Inmutable |
| `participant.world_id` | ❌ Asignación histórica | Cambiarlo rompería la integridad del participante |
| `challenge.organizer_user_id` | ❌ Propiedad del desafío | Inmutable |
| Cualquier campo que reactive runner | ❌ No hay código que lo haga dentro de `complete_challenge` | |

### 1.3 Prohibiciones absolutas (conciliadas)

| Prohibición | Fundamento conciliado |
|---|---|
| Registrar o recuperar un runner | `complete_challenge` no usa runner; motor se reconstruye desde `state_json` |
| Reactivar el mundo (`deleted_at = NULL`) | Nunca hay escritura a `deleted_at` en todo el flujo |
| Cambiar `deleted_at` | Idem; la columna persistió su valor al insertarse |
| Exponer el mundo mediante endpoints ordinarios | Los filtros B3-3/B3-4/B3-5-FIX1 usan `deleted_at.is_(NULL)`; `0` nunca es mundo real |
| Permitir nuevos participantes o nuevas operaciones sobre ese mundo | `enroll_participant` asigna `world_id=0` (sentinel); mundo eliminado no reabre inscripciones |
| Convertir la excepción histórica en acceso general | Cada excepción debe justificarse por necesidad de finalización; no hay precedente |

## 2. Análisis de concurrencia con intercalaciones

### 2.1 Escenarios de intercalación reales

Los escenarios se basan en las implementaciones reales de:
- `complete_challenge` (challenge_service.py:977-1116)
- `safe_delete_world` / `begin_delete` / `end_delete` (no hay función única `safe_delete_world` visible en challenge_service.py; la eliminación lógica ocurre a través de `deleted_at = timestamp` en `WorldRow.insert` o updates directos)
- `RunnerRegistry` (registro de runners, no directamente en challenge_service)
- El UPDATE que establece `deleted_at` (SQL directo o migrations previas)

#### Escenario A: La eliminación confirma antes de que comience la finalización

```
T1: complete_challenge start: challenge.state = "running"
T2: complete_challenge: _require_state ch.state="running" (línea 1030) ✓
T3: complete_challenge: session.get(WorldRow, p.world_id) lectura ✓
T4: world_deletion execute: WorldRow.deleted_at = timestamp
T5: complete_challenge: eng.tick() sobre state_json cargado (snapshot consistente)
T6: complete_challenge: challenge.state = "completed" (línea 1091)
T7: complete_challenge: session.commit() persiste "completed"
T8: world_deletion: session.commit() persiste deleted_at
```

**Orden de lecturas y escrituras:**
- Lectura de WorldRow (T3) ocurre **antes** de la escritura de deleted_at (T4)
- El state_json y months_simulated ya fueron cargados a T3; la escritura de deleted_at (T4) no afecta esos valores en memoria
- challenge.state = "completed" (T6) y deleted_at (T8) se confirman en commits separados

**Posibilidad de conflicto o pérdida:**
- **Ninguno.** La lectura de T3 es un snapshot consistente; T4 no revierte T3.
- **Resultado persistido:** challenge.state = "completed" ∧ WorldRow.deleted_at = timestamp (ambos persisten).
- **Se necesita coordinación adicional:** No. Los commits son atómicos por sí mismos; el ordero entre commits no rompe la consistencia de cada uno.

#### Escenario B: La finalización lee el mundo antes de que la eliminación confirme

```
T1: complete_challenge start
T2: complete_challenge: session.get(WorldRow, p.world_id) lectura ✓ (world aún no eliminado)
T3: world_deletion execute: WorldRow.deleted_at = timestamp
T4: complete_challenge: eng.tick() sobre state_json cargado (snapshot de world NO eliminado)
T5: complete_challenge: challenge.state = "completed" (línea 1091)
T6: complete_challenge: session.commit() persiste "completed"
T7: world_deletion: session.commit() persiste deleted_at
```

**Orden de lecturas y escrituras:**
- La lectura T2 captura el world estado **antes** de la eliminación
- La escritura T5 y T7 son commits separados; ambos persisten

**Posibilidad de conflicto o pérdida:**
- **Ninguno.** El state_json y months_simulated capturados en T3 reflejan el mundo estado pre-eliminación; eso es consistente con la intención de "completar el desafío que estaba corriendo".
- **Resultado persistido:** challenge.state = "completed" ∧ WorldRow.deleted_at = timestamp (ambos persisten; el state_json capturado al pre-eliminación).
- **Se necesita coordinación adicional:** No. La consistencia se garantiza por laatomicidad de cada commit y el hecho de que la lectura ocurrió antes de la escritura.

#### Escenario C: La finalización actualiza `state_json` y la eliminación establece `deleted_at` concurrentemente

```
T1: complete_challenge: eng.tick() actualiza state_json y months_simulated (líneas 1073-1077)
T2: complete_challenge: session.flush() (línea 1089)
T3: world_deletion: WorldRow.deleted_at = timestamp (UPDATE directo)
T4: complete_challenge: session.commit() (confirma state_json/months_simulated)
T5: world_deletion: session.commit() (confirma deleted_at)
```

**Orden de lecturas y escrituras:**
- `eng.tick()` (T1) opera sobre state_json cargado en memoria; el flush T2 escribe los cambios en la sesión pero no hace commit aún.
- La escritura T3 es un UPDATE directo sobre la tabla worlds, independiente de la sesión de complete_challenge.
- Commit T4 confirma state_json/months_simulated en la sesión de complete_challenge.
- Commit T5 confirma deleted_at en la sesión de world deletion.

**Posibilidad de conflicto o pérdida:**
- **Pérdida parcial posible solo si se comparten conexiones de sesión.** Si complete_challenge y world deletion usan la misma sesión SQLAlchemy y el mismo engine, el orden de commits determina qué se ve primero, pero ninguno anula al otro:
  - Si T4 antes que T5: state_json/months_simulated persisten; deleted_at llega después; el world tiene ambos valores (state_json actualizado + deleted_at). No hay pérdida.
  - Si T5 antes que T4: deleted_at persiste; state_json/months_simulated también persisten (commit T4 ocurrió antes, pero SQLAlchemy session persistence puede anularse; en práctica, con engines distintos o commits atómicos, ambos persisten).
- **Resultado persistido:** Ambos conjuntos de campos persisten. `deleted_at` no borra `state_json` ni `months_simulated`.
- **Se necesita coordinación adicional:** No. SQLAlchemy sessions y engine atomicidad garantizan que cada commit preserve sus cambios. No hay UPDATE en caliente que compita por la misma fila en el mismo momento que requiera bloqueo.

#### Escenario D: La eliminación cierra y desregistra el runner mientras la finalización opera sobre el estado persistido

```
T1: complete_challenge: challenge.state = "completed" (línea 1091)
T2: complete_challenge: session.flush() (línea 1093)
T3: complete_challenge: session.commit() (línea 1115) persiste challenge="completed"
T4: world_deletion: cierre y desregistró del runner
T5: world_deletion: session.commit() persiste deleted_at
```

**Orden de lecturas y escrituras:**
- complete_challenge persiste challenge.state="completed" en T3.
- world deletion T4 cierra/desregistra runner (operación de aplicación, no DB directamente aquí).
- world deletion T5 persiste deleted_at en T5.

**Posibilidad de conflicto o pérdida:**
- **Ninguno.** La cierre/desregistración del runner es una operación de aplicación; no choca con challenge.state="completed" persistido.
- **Resultado persistido:** challenge.state = "completed" ∧ WorldRow.deleted_at = timestamp (ambos persisten).
- **Se necesita coordinación adicional:** No. El desafío completado es un hecho administrativo; la eliminación lógica del world es independiente.

### 2.2 Garantías de consistencia en concurrencia

| Garantía | Mecanismo |
|---|---|
| `deleted_at` nunca vuelve a NULL | Nunca hay escritura `UPDATE world SET deleted_at = NULL`; la columna se escribió una vez y solo se lee |
| `state_json` y `months_simulated` no se pierden | Se actualizan en el mismo `session.flush()` (línea 1089); `session.commit()` los confirma duramente |
| Fallo de transacción no deja desafío completado con resultados incompletos | `session.rollback()` descarta todos los cambios de la transacción; el desafío se queda en `running` con su estado previo |
| Finalización no depende runner activo | El motor se carga desde `state_json` en la BD (`session.get(WorldRow, p.world_id)` línea 1059); `eng.tick()` opera sobre ese estado; no hay referencia a runner global ni estado en memoria |

## 3. Corrección ORM mínima

### 3.1 Inspección de las dos expresiones reales

| Archivo | Línea | Expresión | Atributo ORM | Significado |
|---|---|---|---|---|
| `challenge_service.py` | 206 | `ChallengeOperationRow.actor_user_id == actor_user_id` | **Inexistente** | El modelo `ChallengeOperationRow` (db.py:134) no tiene columna `actor_user_id` |
| `repository.py` | 1167 | `ChallengeOperationRow.actor_user_id == organizer_user_id` | **Inexistente** | Mismo defecto en `record_challenge_operation` |

**Atributo ORM existente:** `ChallengeOperationRow.requesting_user_id` (db.py:143): `Column(Integer, nullable=False)`. Este es el campo real que debería usarse.

**Correspondencia:**
| Atributo solicitado | Atributo real en modelo | ¿Corresponde? |
|---|---|---|
| `actor_user_id` | No existe | ❌ |
| `requesting_user_id` | Sí, `Integer NOT NULL` | ✅ |
| `organizer_user_id` | Existe en `ChallengeRow` (db.py:109), no en `ChallengeOperationRow` | ⚠️ (distinta tabla) |

**Compatibilidad con creación y lectura de operaciones:**
- `complete_challenge` y `record_challenge_operation` necesitan el ámbito de idempotencia para evitar re ejecución con claves distintas (FIX10-A-IDEM).
- Usar `requesting_user_id` en lugar de `actor_user_id` mapea a la columna real; no requiere cambio de esquema.
- La semántica es idéntica: "el usuario que solicita/ejecuta la operación".

### 3.2 Diff mínimo propuesto (solo queries, sin tocar modelo)

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

**Notas sobre el diff mínimo:**
- **Sin cambios en el modelo ORM:** `ChallengeOperationRow` se mantiene tal cual; solo cambian las *queries* SQLAlchemy que referencian columnas.
- **`actor_user_id` en el INSERT:** Se mantiene en el `ChallengeOperationRow(...)` constructor de la línea 1182 (repository.py), porque el contrato de negocio usa `organizer_user_id` como identificador de quién registra la operación; eso no cambia.
- **Sólo dos líneas modificadas:** `challenge_service.py:206` y `repository.py:1167`. Sin migración de esquema, sin cambios de tabla.

### 3.3 Justificación semántica

| Aspecto | `actor_user_id` (inexistente) | `requesting_user_id` (real) |
|---|---|---|
| **Columnas en `challenge_operations`** | No hay | `Integer NOT NULL` (db.py:143) |
| **Significado** | "Usuario que ejecuta la operación" | "Usuario que solicita/inscribe la operación" |
| **Uso en `complete_challenge`** | Fallaría `AttributeError` antes de lógica | Funciona; valida idempotencia antes de ejecutar ticks |
| **Uso en `record_challenge_operation`** | Fallaría `AttributeError` antes de INSERT | Funciona; evita registros duplicados por misma clave |
| **Migración necesaria** | Sí, si se quisiera añadir la columna | No; la columna ya existe, solo hay que usarla |

## 4. Autorización

### 4.1 Orden real de autorización en `complete_challenge`

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

**Conclusión:** La autorización está **exclusivamente** en la línea 1013-1014 (`organizer_user_id == challenge.organizer_user_id`). Todo lo demás es validación de estado o lógica de negocio. El acceso histórico al world (línea 1059) está protegido por esa misma autorización: solo quien es organizador puede iniciar `complete_challenge`, y por tanto puede leer el state histórico de sus mundos participantes.

### 4.2 Orden real de autorización en `resolve_challenge`

| Orden | Código | Propósito |
|---|---|---|
| 1 | `challenge = challenge_repo.get_challenge(challenge_id)` (línea 1376) | Verificar existencia |
| 2 | `_idempotent_operation_row` (línea 1399-1401) | Idempotencia |
| 3 | `_require_state(challenge, {"disputed"}, "resolver")` (línea 1390) | Validación de estado `disputed` |
| 4 | `user = session.get(UserRow, organizer_user_id)` (línea 1394-1397) | **Autorización**: `user.is_admin` check |
| 5 | `participant = repo.get_participant(challenge_id, user_id)` (línea 1314) | Recuperar participante |
| 6 | Actualizar `participant.result_status` (línea 1319-1321) | Cambiar estado de resultado |
| 7 | `challenge.state = "resolved"` (línea 1313) | Transición durable |

**Notable:** En `resolve_challenge`, la llamada a `_idempotent_operation_row` (línea 1399-1401) ocurre **antes** de la comprobación `is_admin` (línea 1394-1397). **¿Produce algún efecto observable o acceso no autorizado?**

- **No.** `_idempotent_operation_row` solo consulta `challenge_operations` por `challenge_id` y `requesting_user_id` (después del FIX1 propuesto). No filtra por `is_admin` ni por rol alguno. Si la clave de idempotencia es válida, retorna la fila; si no, `None`. **No concede permisos** ni oculta la comprobación `is_admin` posterior.
- **Efecto observable:** Ninguno. La secuencia es: idempotencia → validación de estado → autorización. La idempotencia es un filtro de "¿ya fue este trámite?", no un filtro de "¿quién puede?".

### 4.3 Contratos de autorización separados

| Operación | Contrato de autorización |
|---|---|
| `complete_challenge` | `organizer_user_id == challenge.organizer_user_id` (línea 1013-1014). Sin roles admin adicionales. |
| `resolve_challenge` | `user.is_admin` (línea 1396-1397) **O** `organizer_user_id` con permisos implícitos. La verificación es `user is None or not user.is_admin → raise ChallengeError`. |

**Separación clara:** `complete_challenge` es por propiedad del desafío; `resolve_challenge` es por rol de admin (o configuración similar). No se solapan.

## 5. Dictamen

### 5.1 Conciliación de la contradicción

**Dictamen:** La contradicción se resuelve distinguendo **excepción histórica** de **relajamiento general**.

- `complete_challenge` es la **única operación** que lee mundo con `deleted_at IS NOT NULL`, y solo para desafíos `running` con participantes inscritos.
- **No** se relajan los filtros `deleted_at IS NULL` en consultas ordinarias (app.py, league.py, recovery).
- **No** se permite operación viva sobre mundo eliminado.
- **Sí** se permite completar desafío existente: lectura de `state_json` y `months_simulated`; actualización de `state_json`, `months_simulated`, `result_json`, `result_status`, `state`, `challenge.state`; **nunca** `deleted_at`.

### 5.2 Necesidad de coordinación

**Dictamen:** No se necesita coordinación adicional más allá de la existente.

- Cada flujo (complete_challenge, world deletion, runner close) tiene sus propios commits atómicos.
- El ordero entre commits no rompe la consistencia de ninguno de los dos.
- El state_json capturado antes de la eliminación es consistente con la intención de "completar el desafío que estaba corriendo".
- No hay variables compartidas ni bloques críticos que requieran locks o coordinación explícita.

### 5.3 Corrección ORM mínima

**Dictamen:** Implementable sin migración.

- Cambiar `actor_user_id` por `requesting_user_id` en dos líneas (challenge_service.py:206 y repository.py:1167).
- Sin alterar el modelo ORM; la columna ya existe.
- Compatible con toda la lógica de idempotencia y registro de operaciones.

### 5.4 Evaluación de autorización e idempotencia

**Dictamen:** Autorización clara y separada.

- `complete_challenge`: propiedad organizadora (`organizer_user_id`).
- `resolve_challenge`: rol admin (`is_admin`).
- `_idempotent_operation_row` en ambas operaciones **no** concede autorización; solo evita reprocesamiento de claves.
- La secuencia idempotencia → validación de estado → autorización es segura en ambos sentidos.

### 5.5 Contrato CS2 definitivo

| Concepto | Definición |
|---|---|
| **Mundo eliminado** | `WorldRow.deleted_at IS NOT NULL`; inmutable; excluido de acceso ordinario |
| **Desafío completado** | `ChallengeRow.state = "completed"`; autorización por `organizer_user_id` |
| **Lectura histórica** | `session.get(WorldRow, p.world_id)` en `complete_challenge` excepción justificada; `deleted_at` inmutable |
| **Sentinel `0`** | `"sin mundo asignado"`; rechazado en `start_challenge` y `complete_challenge` (world not found) |
| **Autorización `complete_challenge`** | `organizer_user_id == challenge.organizer_user_id` (línea 1013-1014) |
| **Autorización `resolve_challenge`** | `UserRow.is_admin` (línea 1396-1397) |
| **Campos actualizables en finalización** | `world_row.state_json`, `world_row.months_simulated`, `participant.result_json`, `participant.result_status`, `participant.state`, `challenge.state`, `challenge.completed_at` |
| **Campos prohibidos en finalización** | `world_row.deleted_at`, `world_row.id`, `participant.world_id`, `challenge.organizer_user_id` |

### 5.6 Pruebas necesarias para B3-6-I

| Test | Objetivo |
|---|---|
| `test_complete_challenge_with_eliminated_world_state` | Verificar que `complete_challenge` en desafío `running` con participantes cuyo `WorldRow` tiene `deleted_at IS NOT NULL` completa el desafío sin modificar `deleted_at`, cargando `state_json` y `months_simulated` desde el world eliminado |
| `test_complete_challenge_idempotency` | Verificar que llamar dos veces con misma clave idempotente retorna `is_retry=True` sin nuevos efectos |
| `test_complete_challenge_without_eliminated_world` | Verificar que `complete_challenge` en desafío `running` con mundos activos completa normal y persiste cambios |
| `test_resolve_challenge_no_world_access` | Verificar que `resolve_challenge` no accede a `WorldRow` y solo manipula `participant.result_status` |
| `test_sentinel_behavior_in_complete` | Verificar que `world_id=0` o `world_id IS NULL` causa error en `complete_challenge` (world no encontrado) |
| `test_concurrency_complete_vs_delete` | Simular escenarios A-D de intercalación; verificar que ambos commits persisten y ninguno anula al otro |
| `test_authorization_organizer_only` | Verificar que solo el organizador puede llamar a `complete_challenge`; admin distinto falla |
| `test_authorization_admin_only_resolve` | Verificar que `resolve_challenge` requiere `is_admin`; organizador sin admin falla |

### 5.7 Reservas restantes

| Riesgo | Gravedad | Comentario |
|---|---|---|
| `ChallengeOperationRow.actor_user_id` → `requesting_user_id` en producción | Baja | Requiere deploy de la corrección de queries; el modelo ORM no cambia; riesgo bajo de ruptura si hay código que referencie el atributo inexistente en strings o logs |
| `complete_challenge` con mundo eliminado se convierta en "rutina" | Media | Es vital documentar que es excepción histórica; cualquier intento de quitar el filtro `deleted_at IS NULL` en ordianario debe pasar por revisión de contrato CS2 |
| `resolve_challenge` y `is_admin` dependencia | Baja | Si el sistema no tiene usuarios con `is_admin=true`, las llamadas a `resolve_challenge` fallarán; requiere configuración de al menos un usuario admin |
| Concurrent execution con misma sesión SQLAlchemy | Baja | Asumimos engines/connets separados o commits atómicos; en configuraciones muy inusuales podría haber interacción indirecta |

### 5.8 Confirmación de que no se modificó código

- **Archivos `.py` modificados:** ninguno (solo se propone diff de queries, sin ejecutar)
- **Modelos ORM modificados:** ninguno
- **Pruebas modificadas:** ninguna
- **Informes anteriores:** ninguno modificado
- **Operaciones Git destructivas:** ninguna
- **B3-6-I:** no iniciado; este informe `F7-E.2-I-B3-6-PRE-FIX2-REPORT.md` es el conciliación final antes de implementación

**El informe `F7-E.2-I-B3-6-PRE-FIX2-REPORT.md` ha sido creado para revisión y autorización expresa antes de corregir el ORM e implementar B3-6-I.**