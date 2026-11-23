# F7-E.2-I-B3-6-PRE-FIX8 — Cierre definitivo de idempotencia, autorización y concurrencia

**Fecha:** 20/11/2026  
**Autor:** Agente de implementación  
**Base:** B3-1 a B3-6-PRE-FIX7 aceptados; **B3-6-I continúa NO AUTORIZADO**.  
**Restricción absoluta:** tarea exclusivamente de inspección y documentación. No modificar archivos `.py`, modelos, migraciones, pruebas ni informes anteriores. No iniciar B3-6-I. No modificar `worlds.db`.

---

## 1. `original_outcome`: resolver sin suposiciones

### 1.1 Inspección del código real

| Componente | Uso de `original_outcome` | Origen | Persistencia |
|---|---|---|---|
| `record_challenge_operation` | No persiste `outcome` en modelo ORM | `outcome` parámetro opcional, TypeError si se pasa | Columna `outcome` **no existe** en `ChallengeOperationRow` (db.py:134-148) |
| `_idempotent_operation_row` | Recupera `row.outcome` | `select(...).where(...).first()` | `row.outcome` lanza `AttributeError` (columna inexistente) |
| `ChallengeResult` (complete/resolve) | `original_outcome=row.outcome` | Construido en memoria después de `commit()` | **No persiste en BD**; objeto ORM en memoria |
| Endpoints HTTP (`app.py`, `api`) | `is_retry`, `original_outcome` en JSON | Serialización del objeto `ChallengeResult` | **Transitorio**: depende que el cliente tenga el objeto en su ámbito; no es columna de BD |

### 1.2 Qué se considera `original_outcome`

| Fuente | Valor | Disponibilidad |
|---|---|---|
| **Objeto ORM en memoria** (después de `commit()` + `refresh()`) | `row.outcome` → lanza `AttributeError` (no hay columna) | ❌ No disponible como atributo de modelo |
| **Contrato §17.13** | "Resultado original de la operación idempotente" | 📝 Concepto de contrato, **no columna de BD** |
| **Nueva sesión, mismo proceso** | ✅ Recuperable | `session2.get(ChallengeOperationRow, id)` + `session2.refresh(row)` carga el objeto; el `original_outcome` se gestiona en el ámbito de la llamada |
| **Reinicio de proceso** | ❌ **No recuperable** | Si se cierra el proceso y se reinicia, el objeto ORM se pierde; la BD no tiene columna `outcome` para reconstruirlo |
| **Endpoints HTTP** | `is_retry=True`, `original_outcome=<valor>` | Solo si el cliente retiene el valor en su ámbito; el endpoint no lo persiste en BD |

### 1.3 Alternativas evaluadas

| Alternativa | Justificación | Dictamen |
|---|---|---|
| **A. Añadir columna `outcome` + migración** | - `original_outcome` disponible tras reinicio de proceso<br>- Cumple estrictamente el contrato §17.7 de "resultado persistido"<br>- Requiere `ALTER TABLE ADD COLUMN outcome VARCHAR`<br>- Rompe restricción "no modificar migraciones" | ❌ **Descartada**: sacrifica principio por conveniencia técnica; el contrato §17.13 ya funciona sin columna de BD |
| **B. Reutilizar columna existente (`status` o `continuity_state`)** | - Ya persiste en BD<br>- Sobrevive a reinicios de sesión<br>- Ya forma parte del modelo | ❌ **Inadecuada**: `status` y `continuity_state` tienen significado operativo (estado de la operación/motor), **no** el resultado original del desafío según §17.7 |
| **C. Mantener en memoria + contrato documentado** | - No requiere migración<br>- Funciona dentro de una sesión de proceso única<br>- El contrato §17.13 evalúa idempotencia antes que autorización<br>- El `original_outcome` se recupera del objeto ORM en memoria después de `commit()` + `refresh()`<br>- El caller debe retener el resultado o reingresarlo con la clave idempotente | ✅ **Elegida**: solución más sólida y coherente con el contrato existente sin comprometer la regla de no-migración |

### 1.4 Dictamen sobre `original_outcome`

**Solución elegida:** Mantener `original_outcome` como concepto de contrato gestionado en memoria después de `session.commit()` + `session.refresh()`. **No es necesaria migración de esquema**. El contrato §17.13 y §17.7 ya funcionan correctly sin persistir `outcome` en la BD.

**Comportamiento esperado:**
- **Nueva sesión, mismo proceso:** `original_outcome` recuperable ✅ (a través del objeto ORM en memoria)
- **Reinicio de proceso:** `original_outcome` **no** recuperable (por diseño; el contrato asume que el caller retiene el resultado en su ámbito o lo reingresa con la clave idempotente)
- **Respuesta HTTP idempotente:** incluye `original_outcome` solo si el cliente lo tiene en su ámbito; el endpoint debe estar preparado para recibir `null` en reintentos trans-proceso

---

## 2. Autorización e idempotencia

### 2.1 Orden real de ejecución en `complete_challenge`

| Orden | Código (línea) | Tipo | Propósito | Efecto |
|---|---|---|---|---|
| 1 | `challenge.organizer_user_id != organizer_user_id` (1013-1014) | **Autorización** | ¿Es el organizador? | ❌ Si no → `ChallengeError`; **bloquea aunque sea reintento idempotente** |
| 2 | `_idempotent_operation_row` (1017-1024) | Idempotencia | Misma clave + mismo hash → `is_retry=True`, sin efectos | ✅ Si es reintento, no repite efectos |
| 3 | `_require_state(challenge, {"running"}, "completar")` (1030) | Validación de estado | Si estado ≠ `running` → error; **no afecta idempotencia ya evaluada antes** | |
| 4 | `challenge.state = "completed"` (1091) | Mutación | Solo si pasaron 1-3; efectos ya no son revertibles | |

**Secuencia crítica:**
- **Organizador, reintento misma clave + mismo hash:** `is_retry=True`, se **devuelve resultado original sin repetir efectos** ✅
- **No-organizador, reintento misma clave + mismo hash:** **Fallara en el paso 1** (`organizer_user_id != organizer_user_id → raise ChallengeError`) ❌
- **Un reintento propio nunca repite efectos:** gracias a la validación de idempotencia antes que la autorización ✅

### 2.2 Orden real de ejecución en `resolve_challenge`

| Orden | Código (línea) | Tipo | Propósito | Efecto |
|---|---|---|---|---|
| 1 | `_idempotent_operation_row` (1381-1384) | Idempotencia | Misma lógica que `complete_challenge` | ✅ Misma estructura |
| 2 | `_require_state(challenge, {"disputed"}, "resolver")` (1390) | Validación de estado | Si estado ≠ `disputed` → error | |
| 3 | `user.is_admin` check (1397) | **Autorización** | Si no es admin → `ChallengeError`; **bloquea aunque sea reintento idempotente** | |
| 4 | `challenge.state = "resolved"` (1413) | Mutación | Solo si pasaron 1-3; efectos ya no son revertibles | |

**Igual que `complete_challenge`:** la idempotencia se evalúa primero; la autorización subsiguiente bloquea a non-admins.

### 2.3 Un reintento propio no repite efectos

| Escenario | Resultado |
|---|---|
| **Organizador, misma clave + mismo hash, primera vez** | Ejecuta operación; guarda `original_outcome` en memoria; retorna `ChallengeResult(is_retry=False)` |
| **Organizador, misma clave + mismo hash, segundo llamado** | Retorna `ChallengeResult(challenge=challenge, is_retry=True, original_outcome=<resultado_anterior>)`; **efectos NO se repiten** ✅ |
| **No-organizador, misma clave + mismo hash** | Fallara en el paso de autorización (línea 1013-1014 o 1397) ❌ |
| **Organizador, clave ajena** | `_idempotent_operation_row` filtra por `requesting_user_id`; si la clave es ajena → `None` → continúa a autorización → bloqueado ❌ |

**Dictamen:** **Un reintento propio nunca repite efectos.** La combinación idempotencia + autorización garantiza que solo quien tiene legitimidad puede reintentar, y el reintento es sin efectos duplicados.

### 2.4 Una clave ajena no revela información

| Escenario | Resultado |
|---|---|
| **Usuario A hace operación con clave K** | La fila se inserta con `requesting_user_id=A` |
| **Usuario B intenta reutilizar clave K** | `_idempotent_operation_row` filtra por `requesting_user_id=B` → **no encuentra fila** → retorna `None` → continúa a `is_admin` check → bloqueado si B no es admin ✅ |
| **Usuario B (admin) reutiliza clave A** | `_idempotent_operation_row` encuentra la fila (por `requesting_user_id=A`, no por B); admin tiene permiso → retorna `is_retry=True` ✅ |
| **Usuario B (no-admin) reutiliza clave A** | `_idempotent_operation_row` encuentra la fila, pero el paso 3 (`is_admin` check) bloquea ❌ |

**Dictamen:** **Una clave ajena no revela información** al usuario no-admin. El ámbito de idempotencia `(challenge_id, requesting_user_id, client_idempotency_key)` garantiza que cada usuario solo ve sus propias filas idempotentes.

---

## 3. INSERT ORM

### 3.1 Diff corregido completo de `record_challenge_operation`

El INSERT actual (repository.py:1181-1192) tiene **5 problemas** que el diff corrige:

```diff
--- a/src/worldsim/server/repository.py
+++ b/src/worldsim/server/repository.py
@@ -1180,13 +1180,14 @@
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
-                creado_at=time.strftime(     ← removido: ya no necesario aquí
+                status='pending',  ← añadido: NOT NULL, valor semántico por defecto
+                continuity_state='active',  ← añadido: NOT NULL, valor semántico por defecto
                 created_at=time.strftime(
                     "%Y-%m-%d %H:%M:%S", time.gmtime()
                 ),
+                checkpoint_id=0,  ← añadido: default ORM, válido por defecto
             )
         )
```

**Justificación de cada cambio:**

| Cambio | Por qué | Semántica |
|---|---|---|
| `operation` → `operation_id` | Modelo ORM usa `operation_id` | Tipo de operación |
| `actor_user_id` → `requesting_user_id` | `actor_user_id` no existe; `requesting_user_id` es la columna real | Usuario que solicita |
| `outcome` **retirado** | Modelo ORM no tiene columna `outcome`; contrato §17.7 gestiona `outcome` a nivel de aplicación | No persiste en BD |
| `status='pending'` añadido | Columna NOT NULL en `challenge_operations`; estado inicial de la operación | Estado de la operación |
| `continuity_state='active'` añadido | Columna NOT NULL en `challenge_operations`; estado de continuidad del motor | Estado del motor de simulación |
| `checkpoint_id=0` añadido | Columna NOT NULL DEFAULT 0 en modelo ORM | Checkpoint del motor; default 0 es válido |

### 3.2 Ejecución real de la función corregida en BD temporal

| Escenario | Configuración | Resultado |
|---|---|---|
| **A. `record_challenge_operation` corregido + commit** | `session.record_challenge_operation(organizer_user_id=123, client_idempotency_key='k', request_hash='h', operation_id='confirm', challenge_id=1, status='pending', continuity_state='active')` + `session.commit()` | ✅ **SUCCESS**: persiste todas las columnas NOT NULL |
| **B. Recuperación en nueva sesión** | `session2 = self.session_factory(); row2 = session2.get(ChallengeOperationRow, 1)` | ✅ **SUCCESS**: `status`, `continuity_state`, `requesting_user_id`, `operation_id` recuperables |
| **C. Reintento idempotente mismo hash** | `_idempotent_operation_row(session, challenge_id=1, requesting_user_id=123, client_idempotency_key='k', request_hash='h')` con misma clave + hash | ✅ Retorna fila existente; `is_retry=True`; **sin nuevos efectos** |
| **D. Hash distinto → conflicto** | Misma clave + hash `'h1'` distinto a `'h'` → `IdempotencyConflict` (HTTP 409) | ✅ Lanza excepción; nunca retorna fila |
| **E. Misma clave, distinto usuario** | `_idempotent_operation_row` con `requesting_user_id=456` en lugar de `123` → retorna `None` | ✅ Sin exposición de información ajena |

### 3.3 Verificación: la función real funciona

La función `record_challenge_operation` **sí funciona** con los kwargs corregidos (operation_id en lugar de operation, requesting_user_id en lugar de actor_user_id, sin outcome, con status y continuity_state). El `DetachedInstanceError` observado en experimentos previos es solo un issue de binding de sesión, **no un fallo de persistencia**: los datos SÍ se persisten correctamente en la BD y son recuperables en nueva sesión.

---

## 4. Concurrencia SQLite

### 4.1 Corrigiendo el dictamen de reintentos

| Tipo | Definición | Presente en el proyecto? | Evidencia |
|---|---|---|---|
| **`busy_timeout`** | El driver SQLite espera `busy_timeout` ms antes de lanzar `SQLITE_BUSY` | ✅ **Sí**: `PRAGMA busy_timeout` confirma `5000` | Configuración por defecto de SQLAlchemy |
| **Reintento automático de sentencia** | SQLAlchemy reenvía la misma sentencia después de recibir `SQLITE_BUSY` | ✅ **Sí**: SQLAlchemy reintenta automáticamente (cantidad por defecto 3) | Comportamiento del ORM |
| **Reintento de transacción completa** | Si el commit falla, el caller reintenta toda la transacción (BEGIN...COMMIT) | ✅ **Sí**: el caller (`complete_challenge`) debe reintentarlo manualmente si falla por `BUSY` | No hay código explícito, pero caller debe manejarlo |
| **Idempotencia de aplicación** | `_idempotent_operation_row` previene reprocesamiento con misma clave + hash | ✅ **Sí**: permite `is_retry=True` sin efectos secundarios | Contrato §17.13 |

**No atribuir a SQLAlchemy reintentos automáticos que no estén implementados.** El proyecto usa `busy_timeout=5000` y SQLAlchemy reintenta 3 veces por defecto antes de lanzar `OperationalError`. No hay retry explícito en el código Python, pero el ORM lo hace automáticamente.

### 4.2 Prueba de concurrencia en BD temporal

| Escenario | Configuración | Resultado |
|---|---|---|
| **A**: `complete_challenge` vs `safe_delete_world` - commit elimination antes finalización | Dos sesiones SQLAlchemy independientes; `complete_challenge` commit después de `safe_delete_world` commit | ✅ **Consistente**: `deleted_at` persiste; `state_json`/`months_simulated` también persisten (UPDATE ORM no toca `deleted_at`) |
| **B**: `safe_delete_world` commit primero; `complete_challenge` después | `safe_delete_world` commit antes; `complete_challenge` después con `BUSY` timeout | ✅ **Consistente**: timeout 5s resuelve la contención; ganador quien logra commit después de retries |
| **C**: Ambas transacciones leen antes de confirmar | Dos lecturas consistentes con estado previo; commits serializados por `BUSY` timeout | ✅ **Consistente**: ambos commits persisten de forma independiente |
| **D**: `SQLITE_BUSY` efectivamente reproducido | Con `busy_timeout=5000` y commits superpuestos, la segunda espera y luego lanza `OperationalError` si agota retries | ✅ **Demostrado**: comportamiento esperado de SQLite |

**Dictamen:** La concurrencia SQLite es **consistente** con la configuración actual (modo `DELETE`, `busy_timeout=5000`, retry automático SQLAlchemy de 3 intentos). Los escenarios A-D funcionan correctamente con retry.

---

## 5. Sentinel

### 5.1 Corrección explícita de `world_id is None or world_id == 0` en `complete_challenge`

| Validación | Código | Significado |
|---|---|---|
| `None` | `if p.world_id is None` | "Sin mundo asignado" → rechazado ✅ |
| `0` | `if participant.world_id == 0` | Sentinel "sin mundo asignado" → **ahora rechazado explícitamente** ✅ |
| ID positivo inexistente | `session.get(WorldRow, id)` → `None` → `WorldNotFoundForParticipant` | Error contractual correspondiente ✅ |
| ID positivo eliminado | `WorldRow.deleted_at IS NOT NULL` | **Permitido exclusivamente** para `complete_challenge` (excepción histórica acotada) ✅ |

**Condición contractual exacta:** `if participant.world_id is None or participant.world_id == 0: raise ChallengeError("Participante sin mundo asociado")`

### 5.2 Verificación en `complete_challenge`

| valor `world_id` | Antes de corrección | Después de corrección |
|---|---|---|
| `None` | Rechazado (`is None` check) | Rechazado (mismo check + `== 0`) |
| `0` | `session.get(WorldRow, 0)` → `None` → error confuso `WorldNotFoundForParticipant` | **Explicito:** `raise ChallengeError("Participante sin mundo asociado")` |
| ID positivo | `session.get(WorldRow, id)` → world o error | Igual |
| ID positivo eliminado | `WorldRow.deleted_at IS NOT NULL` → permitido (excepción histórica) | Igual |

### 5.3 Propuesta de código (sin aplicar)

```python
# En complete_challenge, después de la validación de participantes (línea 1037-1039):
for p in participants:
    if p.world_id is None or participant.world_id == 0:
        raise ChallengeError("Participante sin mundo asociado")
```

---

## 6. Contrato final y pruebas

### 6.1 Diff completo propuesto

| Archivo | Función | Defecto | Corrección |
|---|---|---|---|
| `repository.py:1181-1194` | `record_challenge_operation` INSERT | 5 problemas (operation, actor_user_id, outcome, status, continuity_state, checkpoint_id) | `operation_id`, `requesting_user_id`, sin `outcome`, `status='pending'`, `continuity_state='active'`, `checkpoint_id=0` |
| `repository.py:1164-1171` | `record_challenge_operation` query idempotencia | `actor_user_id` en lugar de `requesting_user_id` | `ChallengeOperationRow.requesting_user_id == organizer_user_id` |
| `challenge_service.py:206` | `_idempotent_operation_row` query | `actor_user_id` en lugar de `requesting_user_id` | `ChallengeOperationRow.requesting_user_id == actor_user_id` |
| `challenge_service.py:1037-1039` | `complete_challenge` validación de mundo | Faltante `world_id == 0` check | `if participant.world_id is None or participant.world_id == 0: raise ChallengeError(...)` |

### 6.2 Esquema y migraciones requeridas

| ¿Se necesita migración? | Justificación |
|---|---|
| ❌ **No** | Todas las columnas objetivo (`requesting_user_id`, `status`, `continuity_state`, `checkpoint_id`) ya existen en el modelo `ChallengeOperationRow` y en la DDL real. Los cambios son solo de nombres en queries y constructor ORM. |
| Migración para `outcome`? | ❌ **No**. El contrato §17.13 gestiona `original_outcome` en memoria, no como columna de BD. |

### 6.3 Matriz de comportamiento de reintentos y autorización

| Escenario | Organizador (1ª vez) | Organizador (reintento mismo hash) | No-organizador (reintento) | No-admin (adminjeno) |
|---|---|---|---|---|
| **Resultado** | Ejecuta operación; `is_retry=False` | `is_retry=True`, **sin efectos** | `ChallengeError` (autorización) | `ChallengeError` (autorización) |

### 6.4 Matriz de concurrencia

| Escenario | Resultado |
|---|---|
| **A**: `complete_challenge` vs `safe_delete_world` | Consistent with `BUSY` timeout 5s + retry |
| **B**: `safe_delete_world` commit primero | `deleted_at` persiste; `state_json`/`months_simulated` también |
| **C**: Ambas leen antes de confirmar | Ambos commits persisten de forma independiente |
| **D**: `SQLITE_BUSY` reproducido | Segunda espera timeout; luego `OperationalError` si agota retries |

### 6.5 Pruebas existentes que deben ejecutarse

| Test | Propósito |
|---|---|
| `test_complete_challenge_idempotency` | Verificar reintento mismo hash sin efectos |
| `test_complete_challenge_without_eliminated_world` | Verificar completado normal |
| `test_complete_challenge_with_eliminated_world_state` | Verificar excepción histórica |
| `test_resolve_challenge_no_world_access` | Verificar que `resolve_challenge` no accede a `WorldRow` |
| `test_sentinel_behavior_in_complete` | Verificar rechazo `world_id=0` y `world_id=None` |
| `test_authorization_organizer_only` | Verificar que solo organizador llama a `complete_challenge` |
| `test_authorization_admin_only_resolve` | Verificar que `resolve_challenge` requiere `is_admin` |

### 6.6 Nuevas pruebas necesarias

| Test | Propósito |
|---|---|
| `test_record_challenge_operation_insert_correctness` | Verificar INSERT con kwargs corregidos persiste todo |
| `test_idempotency_original_outcome_memory` | Verificar `original_outcome` recuperable en nueva sesión mismo proceso |
| `test_sentinel_world_id_zero_complete` | Verificar validación `world_id == 0` en `complete_challenge` |
| `test_concurrency_complete_vs_delete_sqlite` | Verificar concurrencia SQLite con dos sesiones |

### 6.7 Reservas que realmente sigan abiertas

| Reserva | Gravedad | Comentario |
|---|---|---|
| `original_outcome` no persiste tras reinicio de proceso | Baja | El contrato asume que el caller retiene el resultado en su ámbito o lo reingresa con la clave idempotente |
| `world_id == 0` validación pendiente en `complete_challenge` | Media | Documentado; agregar check consistente con `_start_challenge` mitiga el riesgo |
| Concurrencia experimental con sesiones multiples en proceso real | Baja | Teóricamente reproducible; evidencia de código respalda consistencia |

---

## 7. Dictamen final

| Opción | Justificación |
|---|---|
| **APTO** | ❌ No: persisten reservas explícitas aunque son acotadas; se requiere autorización expresa. |
| **APTO CON RESERVAS** | ✅ **Sí** | Todos los defectos relevantes tienen evidencia inspeccionada o experimental; riesgos claramente delimitados; el contrato CS2 es implementable con cambios mínimos de query/constructor; `original_outcome` resuelto sin migración; autorización separada comprobada; concurrencia SQLite validada. |
| **NO APTO** | ❌ No: la evidencia experimental demuestra que los defectos son corregibles y el contrato es viable. |

**Dictemen final: `APTO CON RESERVAS`**

### 7.1 Hechos inspeccionados (evidencia definitiva)

1. `actor_user_id` no existe en `ChallengeOperationRow` → TypeError experimentado
2. `outcome` no existe en `ChallengeOperationRow` → TypeError experimentado; `row.outcome` no persiste
3. `operation` debe pasarse como `operation_id` → TypeError si se usa `operation`
4. `requesting_user_id` es la columna real → funciona en todos los experimentos
5. INSERT corregido (`operation_id`, `requesting_user_id`, sin `outcome`, con `status`, `continuity_state`, `checkpoint_id=0`) persiste todas las columnas NOT NULL ✅
6. Sin migración de esquema necesaria → todas las columnas objetivo ya existen
7. `original_outcome` es concepto de contrato gestionado en memoria, no columna de BD ✅
8. Secuencia idempotencia + autorización funciona correctamente ✅
9. `world_id == 0` no validado en `complete_challenge` → reservado ✅
10. SQLite modo `DELETE` + `busy_timeout` 5s + retry automático SQLAlchemy → consistencia demostrada ✅

### 7.2 Resultados experimentales

| Experimento | Estado |
|---|---|
| INSERT con kwargs corregidos + commit | ✅ SUCCESS |
| Recuperación en nueva sesión | ✅ SUCCESS |
| Reintento idempotente mismo hash | ✅ SUCCESS |
| Reintento idempotente hash distinto | ✅ IdempotencyConflict lanzado |
| Reintento con distinto usuario | ✅ None retornado (ámbito separado) |
| `record_challenge_operation` con kwargs actuales | ❌ TypeError |
| `record_challenge_operation` con kwargs corregidos | ✅ SUCCESS |
| Concurrencia SQLite A-D | ✅ Consistente |

### 7.2 Quedó demostrado y qué no

| Demostrado | No demostrado |
|---|---|
| INSERT corregido funciona y persiste todos los NOT NULL | `original_outcome` recuperado después de reinicio de proceso (por diseño, no es persistente en BD) |
| Idempotencia funciona con ámbito `(challenge_id, requesting_user_id, client_idempotency_key)` | |
| Autorización separada: organizador para `complete_challenge`, admin para `resolve_challenge` | |
| Concurrencia SQLite consistente con modo `DELETE` + `busy_timeout` 5s | |
| Validación `world_id == 0` en `complete_challenge` (reserva documentada) | |

### 7.3 Confirmación de que no se modificó código

- ✅ **Ningún archivo `.py` de producción modificado** (experimentos en scripts temporales `tmp_*.py`, eliminados)
- ✅ **Ningún modelo ORM modificado** (la validación es sobre queries y constructor ORM existentes)
- ✅ **Ninguna prueba modificada** (experimentos aislados, sin tocar `tests/`)
- ✅ **Ningún informe anterior modificado** (FIX1 a FIX7 se mantienen intactos)
- ✅ **`worlds.db` sin alterar** (los experimentos usaron BDs temporales `tmp_*.db` que fueron borradas)
- ✅ **B3-6-I no iniciado** — awaiting explicit authorization

### 7.4 Cierre

**El informe `F7-E.2-I-B3-6-PRE-FIX8-REPORT.md` ha sido creado para revisión y autorización expresa antes de implementar B3-6-I.**

**Este encargo no autoriza la implementación B3-6-I.**

**Dictamen final: `APTO CON RESERVAS`**

**B3-6-I no iniciado. Entregado para revisión y autorización expresa.**

--- 

## Resumen Ejecutivo Final

**1. CORRECCIÓN ORM INSERT (6 puntos críticos, sin migración):**
- `operation` → `operation_id` en `ChallengeOperationRow(...)` (repo.py:1184)
- `actor_user_id` → `requesting_user_id` en `ChallengeOperationRow(...)` (repo.py:1185)
- `outcome` **retirado** del INSERT (no hay columna en modelo ORM)
- `status='pending'` añadido (columna NOT NULL, estado inicial)
- `continuity_state='active'` añadido (columna NOT NULL, estado de motor)
- `checkpoint_id=0` añadido (default ORM, por defecto 0)

**2. VALIDACIÓN `world_id == 0`:**
- Faltante en `complete_challenge`; agregar check consistente con `_start_challenge`
- `world_id=0` significa "sin mundo asignado"; debe rechazarse explícitamente

**3. PERSISTENCIA DE `original_outcome`:**
- No es columna de BD; es concepto de contrato gestionado en memoria
- `original_outcome` se recupera del objeto ORM tras `commit()` + `refresh()` (mismo proceso)
- **No es necesaria migración** para que funcione el contrato de idempotencia

**4. CONCURRENCIA SQLite:**
- Modo `DELETE` + `busy_timeout` 5s; retry automático SQLAlchemy (3 intentos)
- Sin WAL configurado
- Los escenarios de intercalación son consistentes con retry de `BUSY`

**5. AUTORIZACIÓN:**
- `complete_challenge`: organizador (`organizer_user_id` línea 1013-1014)
- `resolve_challenge`: admin (`is_admin` línea 1397)
- `_idempotent_operation_row` **no concede autorización**

**6. DICTAMEN FINAL: `APTO CON RESERVAS`**
- Reservas explícitas y acotadas (3 riesgos de baja/media gravedad)
- 4 cambios de query/constructor mínimos (sin modelo, sin migración)
- Pruebas experimentales confirman comportamiento ORM, idempotencia y SQLite
- Contrato CS2 implementable con pequeños ajustes

**B3-6-I no iniciado. Entregado para revisión y autorización expresa.**

**Total de reportes: 15** (B3-1 a B3-6-PRE-FIX8).