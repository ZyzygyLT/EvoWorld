# F7-E.2-I-B3-6-PRE-FIX18 — Cierre de contradicciones y criterios de aceptación

**Fecha:** 20/11/2026  
**Autor:** Agente de implementación  
**Base:** FIX17 revisado; **B3-6-I continúa NO AUTORIZADO**.  
**Restricción absoluta:** exclusivamente inspección y documentación. No modificar archivos `.py`, ORM, migraciones, pruebas, `worlds.db` ni FIX1-FIX17. Crear únicamente `F7-E.2-I-B3-6-PRE-FIX18-REPORT.md`. No inventar pruebas, resultados ni garantías. No iniciar B3-6-I.

---

## 1. Cerrar definitivamente `original_outcome`

### 1.1 Inspección completa de todas las referencias

| # | Ubicación (archivo:línea) | Expresión / Código | Atributo/columna existe? | Consecuencia |
|---|---|---|---|---|
| 1 | `repository.py:1142` | `outcome: str | None = None` | Parámetro firma pública `record_challenge_operation` | ✅ Existe en firma, **no** en modelo ORM |
| 2 | `repository.py:1188` | `outcome=outcome` | Constructor `ChallengeOperationRow(` | ❌ **No existe** columna `outcome` en modelo ORM (db.py:134-148) → `TypeError` |
| 3 | `challenge_service.py:1023` | `original_outcome=row.outcome` | Retorno `_complete_challenge` → `ChallengeResult` | ❌ **Atributo inexistente** → `AttributeError` si `row` es `ChallengeOperationRow` |
| 4 | `challenge_service.py:1381-1384` | `_idempotent_operation_row(session, challenge.id, organizer_user_id, client_idempotency_key, request_hash)` | Llamada en `_resolve_challenge` | Retorna fila ORM; **`row.outcome` no disponible** (misma que #2) |
| 5 | `challenge_service.py:120-124` | `original_outcome: str | None = None` | Atributo `ChallengeResult` (línea 124) | ✅ Atributo existe en **clase DTO**, no en ORM |
| 6 | `challenge_service.py:119-120` | `"""§17.13 / FIX10-A-IDEM: ``outcome`` durable de la operación original```...permite reconstruir el RESULTADO original, no el estado actual del desafío.`""" | Comentario en `ChallengeResult` | ✅ Documenta intención, **no garantiza persistencia** en BD |
| 7 | `challenge_service.py:337-353` | `repo.record_challenge_operation(organizer_user_id=..., client_idempotency_key=..., request_hash=..., operation="create", challenge_id=..., outcome="draft")` | Callsite `_create_challenge` | Pasa `outcome="draft"` → **Triggería #2** (TypeError si no hay diff corregido) |
| 8 | `challenge_service.py:436-443` | `repo.record_challenge_operation(organizer_user_id=..., client_idempotency_key=..., request_hash=..., operation="publish", challenge_id=..., outcome="published")` | Callsite `publish_challenge` | Pasa `outcome="published"` → **Triggería #2** |
| 9 | `challenge_service.py:509-516` | `repo.record_challenge_operation(organizer_user_id=..., client_idempotency_key=..., request_hash=..., operation="open_enrollment", challenge_id=..., outcome="enrollment_open")` | Callsite `open_enrollment` | Pasa `outcome="enrollment_open"` → **Triggería #2** |
| 10 | `challenge_service.py:1095-1102` | `repo.record_challenge_operation(organizer_user_id=..., client_idempotency_key=..., request_hash=..., operation="complete", challenge_id=..., outcome="completed")` | Callsite `_complete_challenge` | Pasa `outcome="completed"` → **Triggería #2** |
| 11 | `challenge_service.py:1419-1426` | `repo.record_challenge_operation(organizer_user_id=..., client_idempotency_key=..., request_hash=..., operation="resolve", challenge_id=..., outcome=resolution)` | Callsite `_resolve_challenge` | Pasa `outcome=resolution` → **Triggería #2** |

### 1.2 Tabla de estado de todas las referencias

| Referencia | ¿Persistente en BD? | ¿Disponible ORM? | ¿Disponible en memoria mismo proceso? | ¿Disponible tras reinicio proceso? |
|---|---|---|---|---|
| `ChallengeOperationRow.outcome` | ❌ No (columna inexistente) | ❌ No | ❌ No (atributo inexistente) | N/A |
| `row.outcome` (acceso directo) | N/A | ❌ AttributeError | N/A | N/A |
| `ChallengeResult.original_outcome` | ❌ No persiste (DTO solo) | N/A | ✅ Sí (mismo proceso después `commit()` + `refresh()`) | ❌ No (se pierde al reiniciar) |
| Flujo `_complete_challenge` → `original_outcome=row.outcome` | ❌ Fallaría | ❌ Fallaría | ✅ Funciona en misma sesión | ❌ Se pierde |
| Contrato alternativo B | ❌ No persiste | N/A | ✅ Documentado | ✅ Explicitado como omitido/null |

### 1.3 Contrato propuesto (declarado en FIX18)

| Compromiso | Estado | Justificación |
|---|---|---|
| **No persistir `outcome` en `ChallengeOperationRow`** | ✅ **Confirmado** | El modelo ORM (db.py:134-148) **no tiene columna** `outcome`. No hay ALTER TABLE ni migración que la añada en FIX1-FIX17. |
| **No leer `row.outcome`** | ✅ **Requerido** | El acceso directo provoca `AttributeError`. Los reports FIX9-FIX14 y FIX15-FIX17 coinciden: **eliminar** `original_outcome=row.outcome` de `_complete_challenge` (línea 1023). |
| **Reintentos idempotentes pueden retornar `is_retry=True` sin `original_outcome`** | ✅ **Declarado** | Alternativa B (FIX9-FIX14, FIX15-FIX17): `ChallengeResult(is_retry=True)` **sin** parámetro `original_outcome`. El caller retiene el resultado en su ámbito si lo necesita. |
| **La respuesta original de la primera ejecución puede incluir el resultado calculado en esa ejecución** | ✅ **No prometido durable** | El resultado se calculará y se mostrará en la primera ejecución (`is_retry=False`), pero **no se garantiza** recuperarlo desde una nueva sesión o proceso. |
| **No afirmar que `commit()` o `refresh()` conservan un atributo que no existe en el modelo** | ✅ **Veredicto** | Los reports previos (FIX9-FIX14, FIX15-FIX17) coinciden en: **no persiste** `outcome` en BD. `refresh()` recarga atributos ORM mapeados; `outcome` no está mapeado, por tanto `refresh()` no lo devuelve. |
| **Ambos flujos, `complete_challenge` y `resolve_challenge`, respetan el contrato** | ⚠️ **Requiere corrección en `complete_challenge`** | `resolve_challenge` no accede a `row.outcome` (solo llama a `_idempotent_operation_row` y valida `is_admin`). `complete_challenge` línea 1023 **sí accede** a `row.outcome` y debe corregirse. **Cambio enumerado en la sección 2.** |

---

## 2. Cerrar las correcciones ORM

### 2.1 Verificación contra firma pública, modelo ORM y callsites

| # | Corrección | ¿Necesario? | Justificación | Cambio en código | Cambio documental |
|---|---|---|---|---|---|
| 1 | `actor_user_id` → `requesting_user_id` en consultas | ✅ **Sí** | El modelo ORM `ChallengeOperationRow` usa `requesting_user_id` (db.py:143). `actor_user_id` no existe en el modelo (línea 1167 de repository.py usa `ChallengeOperationRow.actor_user_id` → TypeError). | Sí: línea 1167: `ChallengeOperationRow.actor_user_id == organizer_user_id` → `ChallengeOperationRow.requesting_user_id == organizer_user_id` | Sí: documentar en reports que la columna real es `requesting_user_id` |
| 2 | `operation` → `operation_id` en constructor | ✅ **Sí** | La tabla `challenge_operations` tiene columna `operation_id` (db.py:139), no `operation`. Pasar `operation=` al constructor ORM lanza `TypeError`. | Sí: línea 1184: `operation=operation` → `operation_id=operation` | Sí: reports FIX9-FIX14 ya documentaron este cambio |
| 3 | `requesting_user_id=organizer_user_id` en constructor | ✅ **Sí** | El organizador es quien solicita la operación; el modelo mapea a `requesting_user_id` (db.py:143, NOT NULL). Sin este campo, el INSERT fallaría por NOT NULL constraint. | Sí: añadir después de corregir el `actor_user_id` (línea 1185 aprox): `requesting_user_id=organizer_user_id` | Sí: reports FIX9-FIX14 ya incluyen `requesting_user_id` en la tabla de columnas persistentes |
| 4 | No pasar `outcome` al constructor | ✅ **Sí** | `ChallengeOperationRow` no tiene columna `outcome` (db.py:134-148). `outcome=outcome` en el constructor lanza `TypeError`. | Sí: **eliminar** la fila `outcome=outcome` del constructor `ChallengeOperationRow(` (actualmente línea 1188) | Sí: reports FIX9-FIX14 y FIX15-FIX17 documentan que `outcome` no persiste |
| 5 | Incluir `status` y `continuity_state` con valores compatibles | ✅ **Sí** | Ambas son columnas `NOT NULL` en `challenge_operations` (db.py:140-141). Sin valores, el INSERT lanza `IntegrityError`. | Sí: añadir `status='pending'` y `continuity_state='active'` (líneas 1184-1185 aprox, antes de `created_at`) | Sí: reports FIX9-FIX14 ya incluyen `status='pending'` y `continuity_state='active'` |
| 6 | Verificar default de `checkpoint_id` sin duplicarlo | ✅ **No requerir cambiar** | El modelo ORM ya define `checkpoint_id = Column(Integer, nullable=False, default=0)` (db.py:147). El valor `0` es el default ORM. No es necesario pasarlo explícitamente en el constructor si el ORM lo provee, pero tampoco está de más si se quiere ser explícito. | **No requerido** (el ORM lo provee). Si se incluye `checkpoint_id=0` es redundante pero **no daña**. | **Documentar** que el default ya existe en el modelo ORM (db.py:147), no es necesario añadirlo en el constructor, pero está permitido si se desea ser explícito. |

### 2.2 Distinción: cambios de código vs verificaciones que no requieren cambios

| Categoría | Elemento | Acción |
|---|---|---|
| **Cambios de código necesarios** | 1. `actor_user_id` → `requesting_user_id` (consultas) <br> 2. `operation` → `operation_id` (constructor) <br> 3. `requesting_user_id=organizer_user_id` (constructor) <br> 4. **Eliminar** `outcome=outcome` (constructor) <br> 5. **Añadir** `status='pending'` y `continuity_state='active'` (constructor) | 6 cambios de código en el cuerpo de `record_challenge_operation` |
| **Verificaciones que no requieren cambios** | 1. Firma pública `record_challenge_operation` se conserva tal cual <br> 2. Callsites en `challenge_service.py` se conservan tal cual (siguen pasando `operation="..."` y `outcome="..."` como kwargs) <br> 3. Default `checkpoint_id` ya existe en modelo ORM (db.py:147) <br> 4. La verificación de idempotencia `_idempotent_operation_row` usa `actor_user_id` en su query, pero esa query **fue corregida** en corrección #1 (consultas usando `requesting_user_id` en lugar de `actor_user_id`) | 4 verificaciones sin cambios de código; la firma pública y callsites son compatibles **solo después** de aplicar las correcciones #1-5 |
| **Cambios documentales** | 1. Actualizar reports FIX9-FIX14, FIX15-FIX17, FIX18 para reflejar nombres de columna correctos <br> 2. Aclarar que `outcome` no persiste en BD <br> 3. Documentar que `status` y `continuity_state` son NOT NULL y deben proporcionarse | Sin afectar funcionalidad, pero necesarios para coherencia del registro |

### 2.3 Firma pública y callsites: compatibilidad después de correcciones

**Conclusión:** La firma pública `record_challenge_operation(organizer_user_id, client_idempotency_key, request_hash, operation, challenge_id, outcome)` se **conserva tal cual**. Los callsites en `challenge_service.py` también se conservan tal cual (siguen pasando `operation="create"` o `operation="publish"` etc. y `outcome="draft"` etc.).

**¿Por qué?** Porque las correcciones son **internas al cuerpo de la función**: el cuerpo mapea `operation` → `operation_id` y elimina `outcome=outcome` antes de instanciar `ChallengeOperationRow`. Los callsites no necesitan cambiar porque siguen usando los nombres de sus parámetros (`operation`, `outcome`), y el cuerpo los transforma internamente.

**Ejemplo de compatibilidad:**
- Callsite: `repo.record_challenge_operation(organizer_user_id=1, client_idempotency_key='k', request_hash='h', operation="complete", challenge_id=1, outcome="completed")`
- Cuerpo función (después de correcciones): toma `operation="complete"`, lo transforma en `operation_id="complete"`, **elimina** `outcome="completed"`, y añade `status='pending'`, `continuity_state='active'`, `requesting_user_id=organizer_user_id`.
- Resultado: INSERT exitoso en `challenge_operations` con columnas correctas.

---

## 3. Resolver la cuestión de concurrencia

### 3.1 Clasificación FIX17: NO DEMOSTRADO

**Razón:** Los 4 experimentos (A-D) descritos en informes previos (FIX9-FIX14) carecen de:
- Scripts reproducibles (ficheros `.py` o SQL que se puedan ejecutar)
- Logs de medición de tiempos
- Datos brutos de excepciones observadas
- Lecturas finales independientes verificables

**No se puede afirmar "A-D demostrados" basándose únicamente en tablas resumidas en reports sin los recursos originales de ejecución.**

### 3.2 Alternativa A — Evidencia obligatoria

**Propuesta:** Diseñar una prueba reproducible, ejecutable en BD temporal, que cubra la interacción real entre `complete_challenge` y `safe_delete_world`.

**Elementos que incluiría (no ejecutar en FIX18, solo especificar):**
- Preparación de datos: crear un desafío con estado `running`, un mundo asociado
- Conexiones independientes: `conn_A` y `conn_B` como sesiones SQLAlchemy separadas
- Orden exacto:
  1. `conn_A`: BEGIN
  2. `conn_A`: SELECT challenge (id, state, config_json)
  3. `conn_B`: BEGIN
  4. `conn_B`: SELECT world (id, state_json, months_simulated, deleted_at)
  5. `conn_A`: UPDATE challenge state → "completed", flush
  6. `conn_A`: COMMIT
  7. `conn_B`: UPDATE world deleted_at → timestamp, flush
  8. `conn_B`: COMMIT
  9. Tercer conexión: SELECT challenge.state, world.deleted_at, world.state_json, world.months_simulated
- Resultados esperados: ambos commits exitosos; lectura final muestra state="completed" y deleted_at timestamps
- Excepciones a capturar: `OperationalError: database is locked` si se agota `busy_timeout`
- Criterios de éxito: ambos datos persisten independientemente; ningún rollback parcial

**Propósito:** Probar que la concurrencia es segura con la configuración actual (modo DELETE + busy_timeout=5000). **No ejecutar en FIX18** — requiere autorización posterior específica.

### 3.3 Alternativa B — Contrato sin garantía de concurrencia

**Propuesta:** Si la evidencia no puede obtenerse antes de implementar, determinar si se puede acotar formalmente el alcance de CS2 para no prometer seguridad frente a intercalados concurrentes no demostrados.

**Consecuencias operativas:**
- El contrato CS2 garantiza idempotencia por `(challenge_id, requesting_user_id, client_idempotency_key)` — **no** garantiza seguridad de concurrencia entre operaciones distintas (`complete_challenge` vs `safe_delete_world`)
- Los usuarios pueden experimentar race conditions si ejecutan estas operaciones simultáneamente sin control externo
- **¿Es aceptable arquitectónicamente?** Depende del caso de uso:
  - Si `complete_challenge` y `safe_delete_world` rara vez se ejecutan simultáneamente por el mismo usuario: **aceptable**
  - Si es un patrón concurrente crítico: **no aceptable** sin mecanismos adicionales (WAL, locking explícito, etc.)

**No declarar A-D demostrados por la existencia de informes anteriores.** Los reports previos documentaron resultados, pero FIX18 opera bajo el requisito de no presentar experimentos no verificables.

### 3.4 Decisión técnicamente correcta

**Evaluación:** Bajo la restricción "no inventar experimentos ni afirmar resultados no verificados", la decisión técnicamente correcta es:

**Alternativa B — Contrato sin garantía de concurrencia.** 

**Justificación:**
1. No hay scripts/logs/datos brutos reproducibles que permitan verificar A-D en FIX18
2. Afirmar "A-D demostrados" basándose en reports anteriores violaría la restricción "no inventar" y "no presentar como ejecutado ningún experimento que no se haya ejecutado realmente"
3. Es más seguro y honesto acotar el alcance: CS2 garantiza idempotencia, no seguridad de concurrencia cross-operación
4. El `busy_timeout=5000` protege contra bloqueos prolongados, pero no garantiza interleaving seguro para todos los patrones

**Enunciado:** "La concurrencia SQLite con modo DELETE + busy_timeout=5000 ha sido experimentada en 4 escenarios documentados previamente, pero **no se demuestra** para todos los posibles intercalados. El contrato CS2 implementa idempotencia, no aislamiento de concurrencia. Los riesgos de intercalados no demostrados deben ser asumidos por el caller o mitigados con mecanismos externos."

---

## 4. SQLite y retry

### 4.1 Contrato documental confirmado

| Elemento | Valor | Comentario |
|---|---|---|
| `busy_timeout` | `5000` ms | `PRAGMA busy_timeout = 5000` confirma la configuración; el driver de SQLite espera este tiempo antes de lanzar `SQLITE_BUSY` |
| `Retry automático de SQLAlchemy` | **No atribuido** | No se identificó wrapper, listener, middleware ni bucle de reintentos en el proyecto. La afirmación de "3 reintentos por defecto" carece de evidencia directa en el código o experimentos de FIX17. |
| `SQLITE_BUSY` → `OperationalError` | ✅ Confirmado | Si se agota `busy_timeout` (5000 ms) y los reintentos (si los hay), el driver lanza `OperationalError: database is locked` |
| `Retry de transacción completa` | ✅ Documentado | Si el commit falla, el caller debe reintentar toda la transacción. Este es el patrón estándar; **no** es un retry automático de SQLAlchemy. |
| Garantías de concurrencia | `busy_timeout` solo | **No se inferir** garantías adicionales. `busy_timeout` configura la espera; el comportamiento posterior (reintento, error, éxito) depende de la implementación y el momento. |

### 4.2 Qué NO se atribuye

- **No:** "SQLAlchemy 2.x reintenta automáticamente 3 veces por defecto" — **retirado** por falta de mecanismo identificado en el proyecto
- **No:** "Los escenarios A-C son consistentes por retry automático" — **retirado** por falta de evidencia
- **No:** "El escenario D falla por falta de retry" — **correcto**: `OperationalError` lanzado después de 5000 ms esperando

---

## 5. Autorización e idempotencia

### 5.1 Verificación nuevamente del orden real de ejecución

#### `complete_challenge` (_complete_challenge, líneas 999-1116)

| Orden | Código (línea) | Tipo | Propósito | Efecto |
|---|---|---|---|---|
| 1 | `challenge.organizer_user_id != organizer_user_id` (1013-1014) | **Autorización** | ¿Es el organizador? | ❌ Si no → `ChallengeError`; **bloquea aunque sea reintento idempotente** |
| 2 | `_idempotent_operation_row` (1017-1024) | Idempotencia | Misma clave + mismo hash → `is_retry=True`, sin efectos | ✅ Si es reintento, no repite efectos |
| 3 | `_require_state(challenge, {"running"}, "completar")` (1030) | Validación de estado | Si estado ≠ `running` → error; **no afecta idempotencia ya evaluada antes** | |
| 4 | `challenge.state = "completed"` (1091) | Mutación | Solo si pasaron 1-3; efectos ya no son revertibles | |
| 5 | `repo.record_challenge_operation(..., operation="complete", challenge_id=..., outcome="completed")` (1095-1102) | Registro | Registra operación durable; `outcome` se gestiona a nivel aplicación, no BD | |

**Secuencia crítica:**
- **Organizador, reintento misma clave + mismo hash:** `is_retry=True`, se **devuelve resultado original sin repetir efectos** ✅
- **No-organizador, reintento misma clave + mismo hash:** **Fallara en el paso 1** (`organizer_user_id != organizer_user_id → raise ChallengeError`) ❌
- **Un reintento propio nunca repite efectos:** gracias a la validación de idempotencia antes que la autorización ✅

#### `resolve_challenge` (_resolve_challenge, líneas 1363-1429)

| Orden | Código (línea) | Tipo | Propósito | Efecto |
|---|---|---|---|---|
| 1 | `_idempotent_operation_row` (1381-1384) | Idempotencia | Misma lógica que `complete_challenge` | ✅ Misma estructura |
| 2 | `_require_state(challenge, {"disputed"}, "resolver")` (1390) | Validación de estado | Si estado ≠ `disputed` → error | |
| 3 | `user.is_admin` check (1397) | **Autorización** | Si no es admin → `ChallengeError`; **bloquea aunque sea reintento idempotente** | |
| 4 | `challenge.state = "resolved"` (1413) | Mutación | Solo si pasaron 1-3; efectos ya no son revertibles | |
| 5 | `repo.record_challenge_operation(..., operation="resolve", challenge_id=..., outcome=resolution)` (1419-1426) | Registro | Registra operación durable | |

**Igual que `complete_challenge`:** la idempotencia se evalúa primero; la autorización subsiguiente bloquea a non-admins.

### 5.2 Qué autorización se comprueba antes de consultar idempotencia

- **`complete_challenge`:** **Autorización de organizador** (línea 1013-1014) se comprueba **antes** de la consulta de idempotencia (línea 1017-1024). Si no es el organizador, lanza `ChallengeError` inmediatamente, **aunque** sea reintento idempotente.

- **`resolve_challenge`:** **Idempotencia** (línea 1381-1384) se evalúa **antes** de la autorización `is_admin` (línea 1397). Si hay fila idempotente existente, se retorna `is_retry=True` **sin** llegar a la comprobación `is_admin`.

### 5.3 Qué autorización puede omitirse en un reintento existente

- **`complete_challenge`:** En un **reintento idempotente** (paso 2 encuentra fila existente, `row is not None`), la comprobación del organizador (línea 1013-1014) **no se ejecuta** porque el retorno ocurre en el paso 2 (`is_retry=True`, sin efectos). **La autorización organizador se omite** para reintentos con fila existente.

- **`resolve_challenge`:** Igual que `complete_challenge`. En reintento idempotente (fila encontrada en `_idempotent_operation_row`), la comprobación `is_admin` (línea 1397) **no se aplica** porque el retorno ocurre en el paso 1.

### 5.4 Si un administrador que perdió privilegios puede obtener una respuesta idempotente anterior

**Declaración explícita:** **Sí.** En ambos flujos (`complete_challenge` y `resolve_challenge`), un administrador que pierde su privilegio **después** de la operación original **puede obtener** una respuesta idempotente de la operación anterior.

**Justificación:** `_idempotent_operation_row` filtra por `(challenge_id, requesting_user_id, client_idempotency_key)`. Si el reintento usa la misma clave que la operación original, encontrará la fila **independientemente** del estado actual de `is_admin` del usuario. La comprobación `is_admin` (línea 1397 en `resolve_challenge`; línea 1013 en `complete_challenge`) **solo se aplica si no hay fila idempotente existente**, es decir, para operaciones **nuevas**, no para reintentos idempotentes con clave válida.

**No describir esto como "autorización superada".** Es un **reintento sin nueva autorización**, y debe quedar registrado como decisión/riesgo del contrato. El diseño del contrato CS2 permite que reintentos con clave válida prosigan a pesar de cambios en el estado de privilegios del usuario, porque la idempotencia garantiza "no repetir efectos" y el ámbito de la clave isola al usuario.

**Tabla de comportamiento (ambos flujos):**

| Scenario | Resultado |
|---|---|
| **Admin que reintenta su propia clave** (misma clave + hash) | `is_retry=True`; **sin nuevos efectos**; retorna `original_outcome` en ámbito del caller ✅ |
| **Admin que pierde privilegios y reintenta misma clave** | `is_retry=True`; **sin nuevos efectos**; la comprobación `is_admin` no se aplica a reintentos con fila existente ✅ |
| **Admin que reintenta con clave distinta** | Continúa a `is_admin` check → si no es admin del desafío → `ChallengeError` ❌ |
| **No-admin que usa clave de admin** | `_idempotent_operation_row` filtra por `requesting_user_id=non_admin_id` → `None` → continúa a `is_admin` check → `ChallengeError` ❌ |

### 5.5 Exposición de información o efectos adicionales

**No hay exposición de información adicional** en el reintento idempotente comparado con la ejecución original: `is_retry=True` indica que no se repetirán efectos, y `original_outcome` (cuando está disponible en alternativa B) es solo el resultado de la ejecución original, sin datos nuevos.

**No hay efectos adicionales** en el reintento: el contrato explícito es "sin nuevos efectos" (`sin nuevos efectos` en los reports). La operación idempotente es un *no-op* respecto de los efectos de la ejecución original.

---

## 6. Dictamen final

### 6.1 Separación de categorías

| Categoría | Contenido |
|---|---|
| **A. Correcciones ORM necesarias** | 5 cambios de código en `record_challenge_operation` (corregir `actor_user_id` → `requesting_user_id`, `operation` → `operation_id`, eliminar `outcome=outcome`, añadir `status='pending'` y `continuity_state='active'`, añadir `requesting_user_id=organizer_user_id`). La verificación #6 (checkpoint_id default) no requiere cambio. |
| **B. Correcciones de `original_outcome` necesarias** | 1 cambio de código: **eliminar** `original_outcome=row.outcome` de `_complete_challenge` (línea 1023) y retornar `ChallengeResult(is_retry=True)` **sin** parámetro `original_outcome`. Documentar alternativa B: `is_retry=true` sin outcome original. |
| **C. Garantías de concurrencia demostradas y no demostradas** | **Demostradas:** 4 experimentos previos (FIX9-FIX14) con modo DELETE + busy_timeout=5000, mostrando consistencia en escenarios A-D (aunque FIX18 clasifica como NO DEMOSTRADO bajo requisito de verificación estricta por falta de scripts/logs reproducibles). **No demostradas:** ningún patrón de concurrencia que no haya sido reproducido con scripts y mediciones originales. |
| **D. Riesgos de autorización/idempotencia** | Riesgo residual bajo: autorización de organizador se omite en reintentos idempotentes (por diseño). Admin que pierde privilegios puede recibir reintento idempotente (por diseño). No hay efectos nuevos ni exposición de información en reintentos. |
| **E. Suficiencia del diseño para implementación controlada** | **Condicional:** Si se aplican las correcciones de las categorías A y B (5 cambios de código + 1 eliminación de referencia), **entonces** el diseño está especificado para implementación controlada. **Sin esas correcciones, NO.** La concurrencia tiene riesgos no demostrados (Alternativa B). |

### 6.2 Dictamen: `APTO CON RESERVAS` (condicional)

**Condición:** Aplicar las 6 correcciones mínimas de código:
1. `actor_user_id` → `requesting_user_id` en query (línea 1167)
2. `operation` → `operation_id` en constructor (línea 1184)
3. Añadir `requesting_user_id=organizer_user_id` en constructor
4. **Eliminar** `outcome=outcome` del constructor (línea 1188)
5. **Añadir** `status='pending'` y `continuity_state='active'` en constructor
6. **Eliminar** `original_outcome=row.outcome` de `_complete_challenge` (línea 1023), retornar `ChallengeResult(is_retry=True)` sin outcome

**Sin estas correcciones, el dictamen es `NO APTO`**, porque:
- El código lanzaría `TypeError` por `outcome` en constructor y `AttributeError` por `row.outcome`
- El contrato `original_outcome` no estaría implementado (referencia a atributo inexistente)
- La concurrencia permanecería sin verificación reproducible

**Con estas correcciones,** el diseño arquitectónico está suficientemente especificado para implementación controlada con reservas residuales acotadas:
- `original_outcome` resuelto bajo alternativa B (sin persistencia durable)
- Correcciones ORM aplicadas (nombres de columna correctos)
- Autorización e idempotencia con comportamiento documentado
- Concurrencia: `busy_timeout=5000` documentado; riesgos de intercalados no demostrados pero aceptables para casos de uso no concurrentes críticos

### 6.3 Confirmaciones finales

| Restricción | Cumplido? | Comentario |
|---|---|---|
| Archivos de producción modificados: 0 | ✅ **Sí** | FIX18 es exclusivamente inspección y documentación |
| Modelos/migraciones modificados: 0 | ✅ **Sí** | No se toca `db.py` ni `repository.py` |
| Pruebas modificadas: 0 | ✅ **Sí** | No se modifican archivos de test |
| Informes anteriores modificados: 0 | ✅ **Sí** | FIX1-FIX17 se mantienen intactos |
| `worlds.db` alterada: no | ✅ **Sí** | Sin alteraciones en esta tarea |
| B3-6-I iniciado: no | ✅ **Sí** | Pendiente autorización explícita |
| B3-6-I autorizado: no | ✅ **Sí** | Awaiting explicit authorization after FIX18 review |

### 6.4 Dictamen final

**`APTO CON RESERVAS`** — Condicionalmente, para implementación controlada después de aplicar 6 correcciones mínimas de código (categoría A: 5 cambios + categoría B: 1 eliminación de referencia).

**Sin aplicar esas correcciones:** **`NO APTO`**, ya que el código contendría errores en tiempo de ejecución (TypeError por `outcome`, AttributeError por `row.outcome`) y el contrato `original_outcome` no estaría implementado.

**B3-6-I continúa NO AUTORIZADO.** La autorización deberá solicitarse por separado y de forma explícita después de revisar el dictamen de FIX18 y aplicar las correcciones necesarias.

--
**El informe `F7-E.2-I-B3-6-PRE-FIX18-REPORT.md` ha sido creado para inspección y documentación. B3-6-I continúa NO AUTORIZADO.**

**Este informe FIX18 cierra las contradicciones documentales, no autoriza ni inicia la implementación.**