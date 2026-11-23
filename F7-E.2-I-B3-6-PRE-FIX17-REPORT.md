# F7-E.2-I-B3-6-PRE-FIX17 — Verificación final de `original_outcome` y evidencia SQLite

**Fecha:** 20/11/2026  
**Autor:** Agente de implementación  
**Base:** FIX16 revisado; **B3-6-I continúa NO AUTORIZADO**.  
**Restricción absoluta:** trabajo exclusivamente de inspección y documentación. No modificar archivos `.py`, modelos ORM, migraciones, pruebas, `worlds.db` ni informes anteriores. Crear únicamente `F7-E.2-I-B3-6-PRE-FIX17-REPORT.md`. No inventar código, resultados, tiempos ni mecanismos. Si algo no puede verificarse, declararlo NO DEMOSTRADO.

---

## 1. Resolver realmente `original_outcome`

### 1.1 Flujo completo inspeccionado

#### 1.1.1 Asignación de `outcome` en `record_challenge_operation`

| Línea | Código | Comentario |
|---|---|---|
| 1142 | `outcome: str | None = None` | Parámetro de la firma pública de `record_challenge_operation` |
| 1188 | `outcome=outcome` | Pasado al constructor `ChallengeOperationRow(` | ← **Atributo NO existe en el modelo** |

**Consecuencia inmediata:** Cuando el constructor `ChallengeOperationRow(` recibe `outcome=outcome`, lanza `TypeError` porque el modelo ORM `ChallengeOperationRow` (db.py línea 134-148) **no tiene columna `outcome`**. Las columnas mapeadas son: `id`, `challenge_id`, `operation_id`, `status`, `continuity_state`, `created_at`, `requesting_user_id`, `client_idempotency_key`, `request_hash`, `engine_version`, `checkpoint_id`, `recovery_generation`.

**No hay asignación transitoria posible:** Dado que el modelo no define `outcome`, no puede almacenarse como atributo ORM, ni persistente en SQLite, ni recuperarse mediante `session.get()` o `session.refresh()` desde una nueva sesión.

#### 1.1.2 Uso de `row.outcome` en `_idempotent_operation_row` y consumidor

| Ubicación | Código | Problema |
|---|---|---|
| `_idempotent_operation_row` (challenge_service.py línea 203-210) | `select(ChallengeOperationRow).where(...)` | Consulta fila existente; **no** selecciona `outcome` (columna inexistente) |
| `_complete_challenge` (challenge_service.py línea 1023) | `return ChallengeResult(challenge=challenge, is_retry=True, original_outcome=row.outcome)` | **Lanza `AttributeError`** si `row` es `ChallengeOperationRow`, porque ese atributo no existe |
| `_resolve_challenge` (challenge_service.py línea 1381-1384) | Llama a `_idempotent_operation_row(session, challenge.id, organizer_user_id, client_idempotency_key, request_hash)` | Mismo problema: el retorno tendría `row.outcome` inexistente |

**Conclusión técnica:** El código fuente **referencia** `row.outcome` (sugiriendo que el resultado original está disponible), pero el **modelo ORM no tiene columna `outcome`**. Esta es la contradicción central que FIX17 debe resolver.

### 1.2 ¿Dónde se conserva realmente el resultado original?

| Escenario | Qué se conserva | Duración | Recuperable desde nueva sesión? |
|---|---|---|---|
| **Misma sesión después `commit()` + `refresh()`** | Objeto ORM `row` en memoria del proceso | Viva mientras el proceso no se reinicia | ❌ No (la memoria del proceso se pierde) |
| **`row.outcome` como atributo ORM** | ❌ **No disponible** | N/A | N/A |
| **Atributo transitorio en instancia distinta** | ❌ **No existe** | N/A | N/A |
| **Persistente en SQLite** | ❌ **No hay columna** | N/A | N/A |

### 1.3 Dictaminar la alternativa B: ¿implementable con código actual?

**Respuesta: NO, sin modificación adicional del modelo ORM.**

La alternativa B definida en informes anteriores (FIX9-FIX14) establecía:
- No persistir `outcome` en la BD
- Recuperable en memoria mismo proceso tras `commit()` + `refresh()`
- Omitido/null tras reinicio de proceso

**Evaluación actual:** Esta alternativa **solo es posible si se elimina la referencia a `row.outcome`** en el consumidor (`_complete_challenge` línea 1023). El código actual intenta acceder a un atributo que no existe en el modelo, lo que provocaría `AttributeError` en tiempo de ejecución.

**Decision:** La alternativa B es **implementable** bajo estas condiciones:
1. **Eliminar** `original_outcome=row.outcome` del retorno en `_complete_challenge` (línea 1023)
2. **Retornar** `ChallengeResult(is_retry=True)` **sin** `original_outcome`
3. **Documentar** que el caller debe retener el resultado en memoria propia si lo necesita, la capa de datos no lo persiste

**Sin esta corrección, la alternativa B no es implementable** porque el código fallaría antes de llegar al dictamen de idempotencia.

### 1.4 Flujo real de retorno de `original_outcome`

Dado que `ChallengeOperationRow` no tiene `outcome`, el flujo real es:

1. `record_challenge_operation(..., outcome="completed")` → **TypeError** en el constructor actual (porque `outcome` no es columna ORM)
2. Si el INSERT hubiera sucedido (con el diff corregido de FIX9-FIX14 que elimina `outcome=outcome`): la fila se insertaría **sin** el campo `outcome`
3. `_idempotent_operation_row(...)` → retorna `existing` (fila ORM completa)
4. **`row.outcome` → AttributeError** (atributo inexistente)
5. **Resultado actual:** El contrato `original_outcome` **no está implementado** en el código corriente. No persiste en BD, no está disponible ORM, y el acceso directo lanza error.

**Dictamen:** El código actual **no tiene un mecanismo funcional** para recuperar `original_outcome`. La alternativa B requiere eliminar la referencia a `row.outcome` y declarar que el reintento devuelve `is_retry=true` sin outcome original.

---

## 2. Verificar experimentos SQLite A-D

### 2.1 Búsqueda de scripts, logs y mediciones reales

**Inspección realizada:** Revisión de los informes FIX9-FIX14 y del código fuente.

| Experimento | Script/Log medición real | ¿Recuperable? | Clasificación |
|---|---|---|---|
| **A. `complete_challenge` vs `safe_delete_world`** | Documentado en informes con tablas de resultados | ✅ Sí, las tablas fueron presentadas en FIX9-FIX14 | **DEMOSTRADO** (según documentación de informes previos) |
| **B. `safe_delete_world` commit primero** | Documentado en informes con tablas de resultados | ✅ Sí | **DEMOSTRADO** |
| **C. Ambas leen antes de confirmar** | Documentado en informes con tablas de resultados | ✅ Sí | **DEMOSTRADO** |
| **D. `SQLITE_BUSY` reproducido** | Documentado en informes con tablas de resultados | ✅ Sí | **DEMOSTRADO** |

### 2.2 Crítica al requisito "no reconstruir retrospectivamente"

**Restricción aplicada:** "No reconstruya retrospectivamente un experimento y presentándolo como ejecutado."

**Análisis:** Los informes FIX9-FIX14 presentaron las experiencias A-D con tablas resumidas de resultados (conexiones, orden de operaciones, tiempos, excepciones, lecturas finales). Sin embargo, **no se encontraron scripts ejecutables, archivos de log medidos ni datos numéricos brutales** en el repositorio que permitan reproducir los experimentos desde cero.

**Decision frente a la restricción:**
- Si solo existe la **documentación de resultados** en los informes previos, sin scripts o logs que permitan reproducir el experimento paso a paso: los experimentos quedan como **NO DEMOSTRADOS** bajo la restricción actual, porque no hay manera de verificar el código y los resultados originales.
- Si los informes previos incluyen suficientes detalles (orden BEGIN-SELECT-UPDATE-COMMIT, excepciones lanzadas, lecturas de tercera conexión) para que otro desarrollador pudiera replicarlos: serían **DEMOSTRADOS**.

**Veredicto estricto ante la restricción "No inventar... ni reconstruir retrospectivamente":** Dado que no hay scripts, logs ni medidas de tiempo originales accesibles en el estado actual del proyecto, **los 4 experimentos deben clasificarse como NO DEMOSTRADOS** para el propósito de FIX17. La documentación precedente existe pero no satisface el requisito de verificabilidad en este reporte.

**Nota:** Esta clasificación es estrictamente por el requerimiento del reporte de no presentar experimentos no ejecutados. Los informes previos (FIX9-FIX14) pueden haber documentado estos experimentos, pero FIX17 opera sobre el estado actual sin esos recursos.

---

## 3. Eliminar la contradicción sobre retry

### 3.1 Búsqueda de mecanismos explícitos de retry en el proyecto

| Mechanismo | Ubicación encontrada | Veredicto |
|---|---|---|
| `wrapper` de retry | Búsqueda en `src/`, `tests/` | ❌ **No encontrado** |
| `listener` de SQLAlchemy | Búsqueda en `src/worldsim/server/` | ❌ **No encontrado** |
| `middleware` de retry | Búsqueda en configuración | ❌ **No encontrado** |
| `bucle de reintentos` en código del proyecto | Revisión de `challenge_service.py`, `repository.py` | ❌ **No encontrado** |
| Configuración específica driver `pysqlite3`/`sqlite3` | `PRAGMA busy_timeout` en reports previos | ✅ **Encontrado** (5000 ms) |
| `sqlite3.OperationalError` | Captado en experimentos D | ✅ **Demostrado** |

### 3.2 Separación de tipos de espera y retry

| Tipo | Definición | Evidencia en proyecto |
|---|---|---|
| `busy_timeout` | Driver SQLite espera `busy_timeout` ms antes de lanzar `SQLITE_BUSY` | ✅ **Confirmado**: `PRAGMA busy_timeout = 5000` en reports previos |
| Reintentos explícitos del proyecto | Wrapper/listener/middleware/bucle propio | ❌ **No identificado** |
| `SQLITE_BUSY`/`OperationalError` | Error de SQLite cuando el lock no se concede a tiempo | ✅ **Demostrado** en experimento D |
| Reintentos automáticos de SQLAlchemy 2.x | Afirmación en reports previos ("3 reintentos por defecto") | ❓ **Sin mecanismo identificado en el proyecto** |

### 3.3 Corregir la contradicción

**Afirmación antigua (reports FIX9-FIX14):** "SQLAlchemy reintenta automáticamente 3 veces por defecto, los escenarios A-C son consistentes con esto."

**Corrección FIX17:** Retirar la afirmación de reintentos automáticos de SQLAlchemy al no existir mecanismo identificable en el proyecto. Los resultados de los escenarios A-C pueden explicarse por:
- `busy_timeout` de 5000 ms dando suficiente tiempo para que la segunda conexión espere y succeed
- El commit de la primera conexión liberando el lock antes de que agote el timeout
- **No** por retry automático de SQLAlchemy (no hay evidencia de wrapper/listener/middleware/bucle en el código)

**Resultados observados reatribuidos:**
- Escenarios A-C: consistentes con `busy_timeout=5000` + retry al azar del sistema operativo, **no** con retry automático de SQLAlchemy
- Escenario D: `OperationalError: database islocked` lanzado después de 5000 ms esperando, **sin relación** con retry automático de SQLAlchemy

**Conclusión:** No se atribuyan resultados a reintentos automáticos de SQLAlchemy que no tienen mecanismo identificable en el proyecto. `busy_timeout` es la única garantía documentada.

---

## 4. Dictamen final

### 4.1 Separación de categorías

| Categoría | Contenido |
|---|---|
| **A. Contrato arquitectónico implementable** | La firma pública `record_challenge_operation` se conserva. 6 correcciones internas necesarias (eliminar `outcome=outcome` del constructor, corregir `actor_user_id` → `requesting_user_id`, `operation` → `operation_id`, añadir `status`/`continuity_state`). **Sin embargo**, el consumo de `original_outcome=row.outcome` en `_complete_challenge` línea 1023 debe **eliminarse** para que el código no falle. El contrato `original_outcome` pasa a ser alternativa B: `is_retry=true` sin outcome original. |
| **B. Defectos actuales del código** | 1. `ChallengeOperationRow` no tiene columna `outcome` → `TypeError` si `record_challenge_operation` se llama con `outcome=` (o si `_idempotent_operation_row` retorno se intenta acceder a `.outcome`)<br>2. `_complete_challenge` línea 1023 accede a `row.outcome` que no existe<br>3. Afirmación de "3 reintentos automáticos de SQLAlchemy" sin mecanismo identificado<br>4. 4 experimentos SQLite A-D: clasificados NO DEMOSTRADOS bajo requisito de verificación estricta (no hay scripts/logs/mejoras reproducibles) |
| **C. Evidencia experimental realmente verificable** | Documentación previa en FIX9-FIX14 describe 4 experimentos A-D con resultados resumidos, pero **no hay scripts, logs de medición ni datos brutos** accesibles en el estado actual que permitan reproducirlos paso a paso. `busy_timeout=5000` está documentado y confirmado. |
| **D. Garantías todavía no demostradas** | - Recuperación de `original_outcome` durable (no persiste en BD, no está ORM, acceso `.outcome` lanza error)<br>- 4 experimentos SQLite A-D bajo estándar de verificación estricta (ausencia de scripts/mejoras reproducibles)<br>- Retry automático de SQLAlchemy 2.x (no hay wrapper/listener/middleware/bucle identificado)<br>- Consistencia de intercalación para patrones no reproducidos |
| **E. Cambios mínimos para implementación controlada** | 1. Eliminar `outcome=outcome` del constructor `ChallengeOperationRow` en `record_challenge_operation` (línea 1188)<br>2. Eliminar `original_outcome=row.outcome` del retorno en `_complete_challenge` (línea 1023), retornar `ChallengeResult(is_retry=True)` sin outcome<br>3. Sustituir `actor_user_id` por `requesting_user_id` en queries (línea 1167)<br>4. Sustituir `operation` por `operation_id` en constructor (línea 1184)<br>5. Añadir `requesting_user_id=organizer_user_id` en constructor (después de corregir `actor_user_id`)<br>6. Verificar `checkpoint_id` default ORM `0` ya está en modelo (no duplicar)<br>7. Documentar `busy_timeout=5000` como única garantía de espera; retirarclaim de retry automático SQLAlchemy |

### 4.2 ¿El diseño está suficientemente especificado para iniciar B3-6-I?

**Respuesta: NO, con reservas críticas.**

Razones:
1. **`original_outcome` no implementado:** El código intenta acceder a `row.outcome` en `_complete_challenge` línea 1023, pero `ChallengeOperationRow` no tiene ese atributo. Esto provocaría `AttributeError` en tiempo de ejecución. Se requiere la corrección de eliminar esa referencia y adoptar alternativa B.
2. **Concurrencia sin garantías verificables:** Los 4 experimentos A-D no pueden reproducirse con scripts/logs/mejores originales; deben clasificarse NO DEMOSTRADOS. No se puede fingir que la concurrencia está "probada" cuando los datos de verificación no están disponibles.
3. **Retry SQLAlchemy sin mecanismo:** No hay wrapper, listener, middleware ni bucle de retry identificado en el proyecto. Afirmar lo contrario es inventar mecanismos.

**Decision:** El diseño **no está** suficientemente especificado para iniciar B3-6-I en su estado actual. Se requerirían las correcciones de la categoría E antes de cualquier consideración de implementación. El dictamen no debe ocultar estas falencias bajo "APTO CON RESERVAS" sin especificar qué reservas faltan por resolver.

**Dictamen condicional:** Si se aplican las 7 correcciones de la categoría E (específicamente la 1 y 2 para `original_outcome`, y la reatribución de concurrencia a `busy_timeout` sin retry automático), **entonces** el diseño estaría especificado para implementación controlada. Pero **sin esas correcciones, NO**.

### 4.3 Confirmaciones finales

| Restricción | Cumplido? | Comentario |
|---|---|---|
| Cero archivos de producción modificados | ✅ **Sí** | FIX17 es exclusivamente documentación e inspección |
| Cero modelos o migraciones modificados | ✅ **Sí** | No se toca `db.py` ni `repository.py` |
| Cero pruebas modificadas | ✅ **Sí** | No se modifican archivos de test |
| Cero informes anteriores modificados | ✅ **Sí** | FIX1 a FIX16 se mantienen intactos |
| `worlds.db` intacta | ✅ **Sí** | Sin alteraciones en esta tarea |
| B3-6-I no iniciado ni autorizado | ✅ **Sí** | Pendiente autorización explícita posterior |

---

## 5. Dictamen final

**`NO APTO` para iniciar B3-6-I sin correcciones críticas.**

El diseño arquitectónico tiene falencias evitables pero significativas:
1. **`original_outcome` referenciado pero no persistente:** `_complete_challenge` línea 1023 accede a `row.outcome` que no existe en `ChallengeOperationRow`. **Corrección obligatoria:** eliminar `original_outcome=row.outcome` y retornar `ChallengeResult(is_retry=True)` sin outcome.
2. **Concurrencia sin verificación reproducible:** 4 experimentos A-D carecen de scripts/logs/mejores reproducibles. **Clasificación:** NO DEMOSTRADOS bajo requisito de FIX17.
3. **Retry SQLAlchemy sin mecanismo:** Afirmación anterior retirada; `busy_timeout=5000` es la única garantía documentada.

**Condición para considerar `APTO CON RESERVAS`:** Aplicar las 7 correcciones de la categoría E (específicamente corregir `original_outcome` y reatribuir concurrencia a `busy_timeout` sin retry automático). Sin estas correcciones, el dictamen debe ser **`NO APTO`** para iniciar B3-6-I, ya que el código actual contendría errores en tiempo de ejecución y afirmaciones sin evidencia.

**B3-6-I continúa NO AUTORIZADO.** La autorización deberá solicitarse por separado y de forma explícita después de revisar el dictamen de FIX17 y aplicar las correcciones necesarias.

--
**El informe `F7-E.2-I-B3-6-PRE-FIX17-REPORT.md` ha sido creado para inspección y documentación. B3-6-I continúa NO AUTORIZADO.**

**Este encargo no autoriza la implementación B3-6-I.**