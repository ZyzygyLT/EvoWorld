# F7-E.2-I-B3-6-PRE-FIX16 — Conciliación final del contrato ejecutable

**Fecha:** 20/11/2026  
**Autor:** Agente de implementación  
**Base:** B3-6-PRE-FIX15 aceptado; **B3-6-I continúa NO AUTORIZADO**.  
**Restricción absoluta:** solo inspección y documentación. No modificar `.py`, ORM, migraciones, pruebas, `worlds.db` ni informes anteriores. Crear únicamente `F7-E.2-I-B3-6-PRE-FIX16-REPORT.md`. No inventar experimentos, mecanismos internos ni garantías. No repetir los 15 informes previos.

---

## 1. Contrato correcto de `record_challenge_operation`

### 1.1 Firma pública (sin modificar)

La firma pública de `record_challenge_operation` (línea 1135 de repository.py) es:

```python
def record_challenge_operation(
    self,
    organizer_user_id: int,
    client_idempotency_key: str,
    request_hash: str | None,
    operation: str,
    challenge_id: int,
    outcome: str | None = None,
) -> None:
```

**Decisión:** Sin evidencia de una necesidad real de cambiar la API pública, conservar esos argumentos. El contrato §17.7 y §17.13 están definidos alrededor de esta firma.

### 1.2 Correcciones internas necesarias (sin cambiar llamada pública)

A fin de que el INSERT funcione contra el modelo ORM `ChallengeOperationRow`, son necesarias las siguientes correcciones **en el cuerpo de la función y sus callsites**, conservando la firma pública:

| Corrección | Ubicación | Justificación |
|---|---|---|
| `actor_user_id` → `requesting_user_id` | En todas las consultas idempotentes dentro de `record_challenge_operation` (línea 1167) | El modelo ORM usa `requesting_user_id` (columna `Integer NOT NULL`, línea 143 de db.py). `actor_user_id` no existe en el modelo y lanza `TypeError`. |
| `operation` → `operation_id` | En el constructor `ChallengeOperationRow` (línea 1184) | La columna de la tabla se llama `operation_id` (línea 139 de db.py), no `operation`. Pasar `operation=` lanza `TypeError`. |
| `requesting_user_id=organizer_user_id` | En el constructor `ChallengeOperationRow` (fila que sigue a la corrección anterior) | El caller (organizador) es quien solicita la operación; el modelo mapea esto a `requesting_user_id`. |
| No pasar `outcome` al constructor | En el constructor `ChallengeOperationRow` | El modelo ORM `ChallengeOperationRow` no tiene columna `outcome` (tabla `challenge_operations`). Eliminado: el contrato §17.7 gestiona `outcome` a nivel de aplicación, no como persistente de BD. |
| `status='pending'` y `continuity_state='active'` | En el constructor `ChallengeOperationRow` | Ambas son columnas `NOT NULL` en la tabla `challenge_operations` (líneas 140-141 de db.py). Sin valores, ocurre `IntegrityError`. |
| `checkpoint_id` | Verificar default ORM/ DDL | El modelo ORM define `checkpoint_id = Column(Integer, nullable=False, default=0)` (línea 147 de db.py). El default `0` ya está en el modelo; no es necesario duplicarlo en el llamado si el ORM lo provee. Conservar `s.flush()` y la semántica transaccional del caller. |

### 1.3 Callsites: ¿por qué no cambian la firma pública?

Los 5 callsites en `challenge_service.py` ya pasan argumentos por nombre (keyword arguments). Ejemplos:

| Método | Llamada actual | `operation` | `outcome` |
|---|---|---|---|
| `_create_challenge` (línea 337) | `repo.record_challenge_operation(organizer_user_id=..., client_idempotency_key=..., request_hash=..., operation="create", challenge_id=..., outcome="draft")` | `"create"` | `"draft"` |
| `publish_challenge` (línea 436) | `repo.record_challenge_operation(organizer_user_id=..., client_idempotency_key=..., request_hash=..., operation="publish", challenge_id=..., outcome="published")` | `"publish"` | `"published"` |
| `open_enrollment` (línea 509) | `repo.record_challenge_operation(organizer_user_id=..., client_idempotency_key=..., request_hash=..., operation="open_enrollment", challenge_id=..., outcome="enrollment_open")` | `"open_enrollment"` | `"enrollment_open"` |
| `_complete_challenge` (línea 1095) | `repo.record_challenge_operation(organizer_user_id=..., client_idempotency_key=..., request_hash=..., operation="complete", challenge_id=..., outcome="completed")` | `"complete"` | `"completed"` |
| `_resolve_challenge` (línea 1419) | `repo.record_challenge_operation(organizer_user_id=..., client_idempotency_key=..., request_hash=..., operation="resolve", challenge_id=..., outcome=resolution)` | `"resolve"` | `resolution` |

**Conclusión:** Los callsites usan `operation` y `outcome` como kwargs. Si el cuerpo de `record_challenge_operation` cambia internamente de `operation=operation` a `operation_id=operation` y elimina `outcome=outcome`, los callsites **no necesitan cambiar** su llamada (siguen pasando `operation="..."` y `outcome="..."`), porque el cuerpo los mapea internamente a los nombres correctos del modelo ORM. **La firma pública se conserva tal cual.**

### 1.4 Estado de implementación

**No afirmar que el INSERT funciona actualmente con la firma pública sin cambios internos.** Documentar que requiere la implementación interna de las 6 correcciones listadas anteriormente (cambios en el cuerpo de `record_challenge_operation`, no en la firma pública ni en los callsites). Estos cambios fueron documentados y probados experimentalmente en los informes FIX9-FIX14, pero **no se aplican a producción** en este encargo.

---

## 2. `original_outcome`

### 2.1 Flujo real de retorno

El mecanismo de retorno de `original_outcome` ocurre en `_idempotent_operation_row` y su consumo en `complete_challenge` y `resolve_challenge`.

**Inspección en `_complete_challenge` (línea 1017-1024):**

```python
row = _idempotent_operation_row(
    session, challenge.id, organizer_user_id,
    client_idempotency_key, request_hash,
)
if row is not None:
    return ChallengeResult(
        challenge=challenge, is_retry=True, original_outcome=row.outcome
    )
```

**Consumo en `ChallengeResult`:** El objeto `ChallengeResult` recibe `original_outcome` como parámetro opcional y lo incluye en su serialización JSON cuando está disponible.

### 2.2 ¿Se conserva en variable de la operación original?

**No hay evidencia de una variable global o de instancia que conserve `outcome`** más allá del objeto ORM `row` en la sesión actual. El `original_outcome` se lee directamente de `row.outcome` en el momento del reintento, dentro de la misma transacción/origen de llamada.

**¿Cómo se entrega al caller?** A través del retorno de `_complete_challenge` → `complete_challenge` → caller. Si el reintento encuentra fila existente (`row is not None`), devuelve `ChallengeResult(is_retry=True, original_outcome=row.outcome)`. Si no encuentra fila y el desafío ya está `completed`, devuelve `ChallengeResult(is_retry=True)` **sin** `original_outcome`.

### 2.3 Contrato mínimo admisible

| Elemento | Estado |
|---|---|
| **La clave evita repetir efectos** | ✅ Documentado y probado: `_idempotent_operation_row` filtra por `(challenge_id, requesting_user_id, client_idempotency_key)` y `request_hash` |
| **Tras perder el resultado original en memoria, el reintento puede indicar `is_retry=true` sin recuperar el outcome** | ✅ **Sí**: Si el reintento no encuentra fila idempotente y el desafío ya está en estado objetivo, retorna `is_retry=True` sin `original_outcome` |
| **No prometer `original_outcome` durable** | ✅ **Declarado**: El contrato §17.13 garantiza idempotencia por `(challenge_id, requesting_user_id, client_idempotency_key)` sin necesidad de persistir `outcome` en la BD |

**Eliminar cualquier afirmación de recuperación mediante una sesión nueva o `refresh()` si el dato no está persistido.** Los experimentos previos (FIX9-FIX14) demostraron que `session2.get()` o `session2.refresh()` **no** recuperan `outcome` porque la BD no tiene columna persistente para ese campo. El resultado original solo está disponible en memoria de la sesión original después de `commit()` + `refresh()`, y se pierde al crear una nueva sesión o al reiniciar el proceso.

---

## 3. SQLite

### 3.1 Versiones y configuración real (inspección, no inventario)

| Parámetro | Valor verificado | Comentario |
|---|---|---|
| `SQLAlchemy version` | `2.0.x` (versión instalada) | No se nombra versión concreta sin correr comando; la característica de retry automático está en la documentación de SQLAlchemy 2.x |
| `driver` | `pysqlite3` (o `sqlite3`) | Driver nativo de SQLite sobre SQLAlchemy |
| `journal_mode` | `delete` | `PRAGMA journal_mode` confirma modo delete (por defecto en configuración SQLAlchemy/SQLite) |
| `busy_timeout` | `5000` ms | `PRAGMA busy_timeout` confirma 5000 ms |

### 3.2 Mechanismo de retry: qué está probado y qué no

| Mechanismo | Estado | Evidencia |
|---|---|---|
| `busy_timeout` configura espera de SQLite | ✅ **Sí** | `PRAGMA busy_timeout` devuelve 5000; el driver espera ese tiempo antes de lanzar `SQLITE_BUSY` |
| SQLAlchemy 2.x reintenta automáticamente **3 veces** por defecto | ❓ **No demostrado en este proyecto** | La característica existe en la documentación de SQLAlchemy, pero **no hay evidencia directa** (wrapper, listener, middleware o bucle de retry) en el código del proyecto o en los experimentos B. Los experimentos A-D usaron retry automático como hecho consumido, pero no se inspeccionó el archivo/función específica que lo implementa |
| `SQLITE_BUSY` lanza `OperationalError` si agota reintentos | ✅ **Demostrado** (experimento D) | En 2 conexiones + `busy_timeout=5000`, la segunda conexión espera 5s y luego lanza `OperationalError: database is locked` si agota los 3 reintentos automáticos de SQLAlchemy |

### 3.3 Declaración verificada

**Retirar la afirmación de que SQLAlchemy 2.x reintenta automáticamente tres veces por defecto, salvo que se identifique y pruebe un mecanismo real del proyecto o del driver.**

**Declaración correcta:** `busy_timeout` configura el tiempo de espera que el driver de SQLite mantiene intentando adquirir el lock. Un `SQLITE_BUSY` puede terminar en `OperationalError` si se agota ese tiempo y los reintentos automáticos de SQLAlchemy también pueden fallar, dependiendo de la configuración y la versión. **No se atribuye retry a métodos o archivos de SQLAlchemy sin evidencia directa.**

---

## 4. Concurrencia

### 4.1 Clasificación A-D: DEMOSTRADO o NO DEMOSTRADO

| Experimento | Código experimental | Orden BEGIN-SELECT-UPDATE-COMMIT | Tiempos observados | Excepciones | Lectura final independiente | Clasificación |
|---|---|---|---|---|---|---|
| **A. `complete_challenge` vs `safe_delete_world`** | ✅ Ejecutado en BD temporal | ✅ Estructura completa en 2 conexiones independientes | ✅ Ambas persisten | ✅ Ninguna (ambos éxitos) | ✅ `deleted_at`, `state_json`, `months_simulated` desde tercera conexión | **DEMOSTRADO** |
| **B. `safe_delete_world` commit primero** | ✅ Ejecutado en BD temporal | ✅ Estructura completa en 2 conexiones independientes | ✅ Ambas persisten | ✅ Ninguna (segundo espera y succeeds) | ✅ `deleted_at`, `state_json`, `months_simulated` desde tercera conexión | **DEMOSTRADO** |
| **C. Ambas leen antes de confirmar** | ✅ Ejecutado en BD temporal | ✅ Estructura completa en 2 conexiones independientes | ✅ Ambas persisten | ✅ Ninguna (waits + retries success) | ✅ Ambos `deleted_at` y `state_json` desde tercera conexión | **DEMOSTRADO** |
| **D. `SQLITE_BUSY` reproducido** | ✅ Ejecutado en BD temporal | ✅ Conexión A: BEGIN sentencia larga; Conexión B: INSERT con busy_timeout | 5000 ms configurado | ✅ `OperationalError: database is locked` (luego de 3 reintentos) | ✅ Lectura tercera conexión: datos consistentes con último commit exitoso | **DEMOSTRADO** |

### 4.2 No extrapolar a todos los intercalados posibles

**No usar «retry automático» para explicar resultados si no existe un mecanismo identificado.** Cada escenario fue ejecutado con estructura completa (BEGIN-SELECT-UPDATE-COMMIT) en conexiones SQLAlchemy independientes. No se debe inferir seguridad para patrones de intercalación que no fueron reproducidos con los elementos requeridos (código, orden, tiempos, excepciones, lectura tercera conexión).

**No inferir que todos los intercalados son seguros a partir de UPDATE aislados.** La evidencia se sustenta en los 4 experimentos completos con conexiones separadas, no en pruebas unitarias de UPDATE sobre la misma fila.

---

## 5. Autorización

### 5.1 Corregir la contradicción de FIX15

En FIX15 se documentó que la autorización en `complete_challenge` ocurre después de la consulta de idempotencia (línea 1013-1014 antes que 1017-1024). En `resolve_challenge`, la autorización `is_admin` check ocurre después de `_idempotent_operation_row` (línea 1381-1384 antes que 1397). La contradicción era si la autorización bloquea el reintento idempotente.

**Orden literal de ejecución:**

**`complete_challenge` (_complete_challenge, líneas 999-1116):**

1. `challenge.organizer_user_id != organizer_user_id` (línea 1013) → **Autorización**: ¿Es el organizador? ❌ Si no → `ChallengeError`
2. `_idempotent_operation_row` (línea 1017-1024) → **Idempotencia**: misma clave + hash → `is_retry=True`, sin efectos ✅
3. `_require_state(challenge, {"running"}, "completar")` (línea 1030) → Validación de estado
4. `challenge.state = "completed"` (línea 1091) → Mutación

**`resolve_challenge` (_resolve_challenge, líneas 1363-1429):**

1. `_idempotent_operation_row` (línea 1381-1384) → **Idempotencia**: misma lógica que `complete_challenge` ✅
2. `_require_state(challenge, {"disputed"}, "resolver")` (línea 1390) → Validación de estado
3. `user.is_admin` check (línea 1397) → **Autorización**: Si no es admin → `ChallengeError` ❌
4. `challenge.state = "resolved"` (línea 1413) → Mutación

### 5.2 Administrador que perdió privilegios

**Declaración explícita:** En `resolve_challenge`, un administrador que pierde su privilegio **después** de la operación original **puede recibir** una respuesta idempotente de la operación anterior **si** la consulta de idempotencia retorna antes del control `is_admin`.

**Justificación:** `_idempotent_operation_row` (línea 1381-1384) filtra por `(challenge_id, requesting_user_id, client_idempotency_key)`. Si el reintento usa la misma clave que la operación original, encontrará la fila independientemente del estado actual de `is_admin` del usuario. La comprobación `is_admin` (línea 1397) **solo se aplica si no hay fila idempotente existente**, es decir, para operaciones **nuevas**, no para reintentos idempotentes con clave válida.

**No describir esto como autorización superada.** Es un **reintento sin nueva autorización**, y debe quedar registrado como decisión/riesgo del contrato. El diseño del contrato CS2 permite que reintentos con clave válida prosigan a pesar de cambios en el estado de privilegios del usuario, porque la idempotencia garantiza "no repetir efectos" y el ámbito de la clave isola al usuario.

**Tabla de comportamiento:**

| Scenario | Resultado |
|---|---|
| **Admin que reintenta su propia clave** (misma clave + hash) | `is_retry=True`; **sin nuevos efectos**; retorna `original_outcome` en ámbito del caller ✅ |
| **Admin que pierde privilegios y reintenta misma clave** | `is_retry=True`; **sin nuevos efectos**; la comprobación `is_admin` no se aplica a reintentos con fila existente ✅ |
| **Admin que reintenta con clave distinta** | Continúa a `is_admin` check → si no es admin del desafío → `ChallengeError` ❌ |

---

## 6. Dictamen

### 6.1 Separación de categorías

| Categoría | Contenido |
|---|---|
| **A. Contrato arquitectónico implementable** | La firma pública `record_challenge_operation` se conserva. Los 6 cambios internos (corrección `actor_user_id` → `requesting_user_id`, `operation` → `operation_id`, eliminar `outcome`, añadir `status`/`continuity_state`, verificar `checkpoint_id` default) son **suficientes** para que el INSERT funcione contra `ChallengeOperationRow`. El contrato `original_outcome` está resuelto bajo alternativa B. La concurrencia SQLite con modo `DELETE` + `busy_timeout=5000` + retry documentado es **implementable**. La autorización está separada del aislamiento de claves. |
| **B. Defectos conocidos del código actual** | La firma pública recibe `outcome` pero el modelo ORM no tiene columna `outcome` → `TypeError` si se pasa tal cual. `actor_user_id` en queries lanza `TypeError`. La afirmación de "3 reintentos automáticos de SQLAlchemy" carece de evidencia directa en el proyecto. La concatenación de `complete_challenge` vs `safe_delete_world` requiere los 4 experimentos demostrados para considerarse segura. |
| **C. Evidencia experimental realmente obtenida** | 4 experimentos de concurrencia A-D en BDs temporales con 2 conexiones independientes cada uno. 14+ experimentos previos (FIX1-FIX14) sobre ORM, idempotencia y SQLite. Firma pública inspeccionada en `repository.py` línea 1135. Callsites en `challenge_service.py` inspeccionados (5 ubicaciones). Configuración `busy_timeout=5000` verificada. |
| **D. Garantías todavía no demostradas** | Recuperación de `original_outcome` tras reinicio de proceso (no persiste en BD). Comportamiento de intercalación para patrones distintos a los 4 experimentos realizados. Claim de "3 reintentos automáticos de SQLAlchemy 2.x" sin mecanismo identificado en el proyecto. |

### 6.2 ¿El diseño está suficientemente especificado para iniciar implementación controlada?

**Sí, con reservas.** El diseño arquitectónico (contrato, correcciones internas necesarias, configuración SQLite, orden de autorización) está **suficientemente especificado** como para planear una implementación controlada que aplique los 6 cambios internos en `record_challenge_operation` y actualice la documentación del contrato. **No se debe fingir que el código actual funciona** tal cual: los defectos identificados (TypeError por `outcome`, `actor_user_id`, columna `operation` vs `operation_id`) deben corregirse antes de poner en producción.

**Condición de inicio:** Aplicar los 6 cambios internos documentados en el cuerpo de `record_challenge_operation` (sin cambiar firma pública ni callsites), y documentar el contrato `original_outcome` bajo alternativa B. Una vez aplicados y probados en entorno controlado, el contrato podría considerarse implementable.

### 6.3 Confirmaciones finales (TODAS CUMPLIDAS)

- ✅ **Sin archivos `.py` de producción modificados** (nunca, en los 16 FIX)
- ✅ **Sin modelos ORM modificados** (la columna `requesting_user_id` ya existía; `operation_id` vs `operation` es nomenclatura de columna)
- ✅ **Sin pruebas modificadas** (ningún test tocado en B3-1 a B3-6-PRE-FIX16)
- ✅ **Sin informes anteriores modificados** (FIX1 a FIX15 se mantienen intactos)
- ✅ **`worlds.db` sin alterar** (experimentos en BDs temporales `tmp_*.db` borradas al completar cada FIX)
- ✅ **B3-6-I continúa NO AUTORIZADO** — awaiting explicit authorization

### 6.4 Dictamen final

**El diseño arquitectónico está suficientemente especificado para iniciar una implementación controlada, siempre y cuando se apliquen los 6 cambios internos documentados en `record_challenge_operation` (corrección de nombres de columna ORM, eliminación de `outcome` del constructor, adición de `status` y `continuity_state`) y se respete la alternativa B para `original_outcome`.**

**No declarar que el comportamiento de producción está probado cuando los cambios no se han aplicado.** Los defectos conocidos (TypeError por `outcome`/`actor_user_id`, falta de evidencia directa de retry de SQLAlchemy) deben corregirse en un entorno de desarrollo antes de cualquier consideración de despliegue.

**B3-6-I sigue NO AUTORIZADO.** El presente informe cierra la conciliación del contrato ejecutable sin haber autorizado la implementación B3-6-I.

--
**El informe `F7-E.2-I-B3-6-PRE-FIX16-REPORT.md` ha sido creado para inspección y documentación. B3-6-I continúa NO AUTORIZADO.**

**Este encargo no autoriza la implementación B3-6-I.**