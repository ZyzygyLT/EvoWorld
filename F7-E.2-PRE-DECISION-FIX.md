# F7-E.2-PRE-DECISION-FIX: Cierre de decisiones operativas

## Objetivo

Incorporar las cuatro decisiones arquitectónicas adoptadas tras F7-E.2-PRE-AUDIT y cerrar los casos de fallo del flujo de eliminación lógica mediante `deleted_at`.

Tarea exclusivamente documental y de inspección. No modificar código, ORM, migraciones ni pruebas.

---

## 1. Decisiones adoptadas

### DEC-PRE-1 — Orden de eliminación

Orden contractual de `safe_delete_world()` (DEC10, §8-§9) modificado para eliminación lógica:

1. Autenticar y autorizar al propietario (capa HTTP `DELETE /api/worlds/{id}`).
2. Identificar el mundo y comprobar su estado (`db_.get(WorldRow, world_id)`, validar permisos).
3. Iniciar el proceso protegido de eliminación y serializar operaciones concurrentes (`registry.begin_delete(world_id)`, `async with lock`).
4. Cerrar el runner (`await runner.close()`, verificar `CLOSED`).
5. Verificar que alcanzó `CLOSED` (RAISE si `CLOSE_INCOMPLETE`, dejar datos persistidos intactos).
6. **Persistir `deleted_at` mediante UPDATE transaccional** (`UPDATE worlds SET deleted_at = NOW_UTC WHERE id = :world_id`).
7. **Retirar el runner del registro en memoria** (`registry.unregister(world_id)`).
8. Completar la operación (retornar éxito al caller HTTP).

**Aclaración crítica:** La persistencia de `deleted_at` (paso 6) debe preceder a `unregister()` (paso 7). No se afirma que la base de datos y el registro en memoria compartan una transacción atómica. El orden está diseñado para garantizar que, si el UPDATE falla, `unregister()` no se ejecuta y el world stay recoverable (runner aún en registry). Si el UPDATE succeeds, entonces `unregister()` retira el runner y el world está eliminado lógicamente.

### DEC-PRE-2 — Fallos y reconciliación

Escenarios de fallo documentados expresamente y sus políticas de reconciliación:

| Escenario | Política de reconciliación |
|---|---|
| **Falla el cierre del runner** (`runner.close()` retorna `CLOSE_INCOMPLETE`) | No marcar el mundo como eliminado. `deleted_at` permanece NULL. Los datos persistidos se dejan intactos (`world stays recoverable`, DEC18). El runner permanece en registry y puede reintentarse. |
| **Falla la verificación CLOSED** (estado runner no es `CLOSED`) | No marcar el mundo como eliminado. Igual que anterior: close incompleto RAISE, datos intactos, world recoverable. |
| **Falla el UPDATE de `deleted_at`** | No retirar el runner del registro (`registry.unregister()` no se ejecuta). El world queda: runner en registry, `deleted_at = NULL`, datos intactos. **Política:** El caller ve el error y puede reintentar el flujo completo. No se debe dejar el world en estado "a medio eliminar". |
| **UPDATE confirmado, pero falla `unregister()`** | El mundo permanece **eliminado en la base de datos** (`deleted_at IS NOT NULL`). Impedir su reactivación ordinaria. Definir reconciliación segura: el runner ya fue cerrado (paso 4_successful) y el world está marcado eliminado; `unregister()` falló después de la persistency, por lo tanto el estado durable es "eliminado". El registro en memoria debe reflejar esto: **no reactivar runner nunca** para un world con `deleted_at IS NOT NULL`. Si el sistema necesita el runner por alguna razón administrativa, requiere procedimiento fuera de contrato. |
| **Se produce cancelación después del UPDATE** | La operación debe preservar el estado durable de eliminado. Dado que UPDATE se ejecuta en hilo principal (transaccional SQLite), la cancelación después del UPDATE pero antes de `unregister()` deja `deleted_at IS NOT NULL` en la BD. El runner sigue en registry. **Política:** El startup recovery debe excluir worlds con `deleted_at IS NOT NULL`. El caller puede reintentar `unregister()` en una próxima ocasión, o el sistema administra la reconciliación como world eliminado órfano de runner en memoria. |
| **La aplicación reinicia después del UPDATE y antes de `unregister()`** | El startup debe excluir el mundo eliminado de la recuperación ordinaria de runners. El `run_startup` / RecoveryCoordinator debe consultar `deleted_at` y omitir worlds eliminados al reconstruir runners o asignar clocks. Esto es consistente con la política general: "un mundo eliminado no debe reaparecer en listados públicos, destacados ni recuperación ordinaria de runners". |

**Principio general:** Ninguna garantía de recuperación se inventa que el diseño actual no pueda sostener. Si una garantía requiere cambios de implementación (ej. startup recovery consultando `deleted_at`), señalarla como requisito obligatorio y verificable (como efectivamente se hace en DEC-PRE-5).

### DEC-PRE-3 — Eliminación repetida

Una eliminación repetida de un mundo cuyo `deleted_at` ya es distinto de NULL es un **no-op idempotente** que retorna éxito.

**Condiciones formales:**

- La autenticación y autorización se mantienen (mismo patrón de cabecera HTTP que una eliminación nueva).
- No se repite el cierre de un runner inexistente o ya retirado: el flujo salta directamente al éxito.
- No se sobrescribe el timestamp original de eliminación: `deleted_at` conserva su valor existente.
- No se altera información histórica: filas `world_events`, `world_operations`, `world_checkpoints`, `world_highlights` sin modificar.
- No se ejecuta DELETE físico: la fila `WorldRow` persiste indefinidamente con su valor `deleted_at`.

**Secuencia de una eliminación repetida:**

```
safe_delete_world(world_id) donde deleted_at IS NOT NULL
│
├─ Paso 1-3: Igual que eliminación nueva (auth, identificar, begin_delete, close, verify)
│
├─ Paso 4: runner.close()  →  Si runner existe y no está cerrado, close(); si ya estaba cerrado o no existe, manejar como no-op según estado.
│
├─ Paso 5: Verificar CLOSED  →  Si runner no existía o ya estaba cerrado, continuar sin error.
│
├─ Paso 6: UPDATE deleted_at  →  **Saltar.** Como deleted_at ya es NOT NULL, el UPDATE es un no-op.
│   * Alternativa contractual: `UPDATE worlds SET deleted_at = deleted_at WHERE id = :world_id` (idempotente, sin cambio de valor).
│
├─ Paso 7: registry.unregister(world_id)  →  **Saltar.** Mundo ya dado de baja en anterior ocasión. Opcional: si runner aún está en registry, retirar; si no, no-op.
│
├─ Paso 8: Retornar éxito al caller HTTP.
└─ Resultado: Mundo continúa con su `deleted_at` original, sin cambios, sin DELETE, sin alteración histórica.
```

### DEC-PRE-4 — Consulta por ID

Un mundo con `deleted_at IS NOT NULL` responde **HTTP 404** en `GET /api/worlds/{id}`.

**Regla formal:**

- El endpoint `GET /api/worlds/{world_id}` (función `get_world`, `app.py:661`) debe comportarse así:
  - Si `row = db_.get(WorldRow, world_id)` y `row.deleted_at IS NOT NULL` → levantar `HTTPException(404, f"world {world_id} not found")`.
  - La regla se aplica **también al propietario**. No se distingue entre owner y tercero: ante `deleted_at IS NOT NULL`, el comportamiento es idéntico (404).
- Las reglas de privacidad DEC20 siguen siendo aplicables a mundos **no eliminados** (visibility, owner checks, etc. para worlds con `deleted_at IS NULL`).
- **No se expone el estado interno de eliminación** mediante una respuesta pública diferenciada. No hay código 403 "world eliminated but owned", no hay campo `deleted` en la ficha pública. Es 404 indistinguible de "world no existente".

**Justificación:** Esta política cumple el principio de consistencia ("un mundo eliminado no debe reaparecer en listados públicos, destacados ni recuperación ordinaria de runners") y la privacidad DEC20 ("fail-closed, indistinguishable from 'not found'"). Evitar la exposición de metadatos de eliminación al exterior.

### DEC-PRE-5 — Restauración

**La restauración administrativa queda fuera de F7-E.2.**

**No implementar:**

- Endpoint de restauración (ni `POST /api/worlds/{id}/restore` ni variante alguna).
- Cambio de `deleted_at` a NULL mediante operación administrativa ordinaria.
- Recuperación de runners eliminados o reactivación automática de clocks.
- Reactivación automática de destacados (`world_highlights`).

**Postura contractual:**

- Una eventual restauración requerirá una decisión contractual posterior que evalúe integridad, privacidad, runners, snapshots e historial.
- F7-E.2 no introduce mecanismo alguno de restauración.
- El campo `deleted_at` es irreversible dentro del alcance de F7-E.2.

---

## 2. Impacto contractual

Las siguientes actualizaciones contractuales son necesarias para que el contrato esté listo para F7-E.2-I:

| Documento | Sección / Área | Actualización necesaria |
|---|---|---|
| **F7-E.2-PRE-DECISION.md** | Sección Flujo contractual | Reemplazar por el orden DEC-PRE-1 (UPDATE antes de unregister). |
| **F7-E.2-PRE-DECISION.md** | Sección Fallos y reconciliación | Añadir tabla DEC-PRE-2 con los 6 escenarios y políticas. |
| **F7-E.2-PRE-DECISION.md** | Sección Eliminación repetida | Añadir DEC-PRE-3 con condiciones formales y secuencia de no-op. |
| **F7-E.2-PRE-DECISION.md** | Sección Consulta por ID | Añadir DEC-PRE-4: 404 para worlds eliminados, incluido owner. |
| **F7-E.2-PRE-DECISION.md** | Sección Restauración | Añadir DEC-PRE-5: restauración fuera de alcance, no implementar. |
| **F7-E.1**, H4 | Retención histórica y eliminación física | Sustituir por versión actualizada con contrato `deleted_at`. Eliminar frase del prerrequisito pendiente (ya resuelto). |
| **F7-E.1**, H5 | Elegibilidad sin requisitos implícitos de operabilidad | Añadir condición `deleted_at IS NULL` como requerida para elegibilidad pública. |
| **Contrato `safe_delete_world()` DEC18** | Flujo de eliminación | Documentar evolución a nuevo orden: close→verify→UPDATE deleted_at→unregister. No alterar la secuencia DEC18 existente, extenderla conceptualmente. |
| **CONTRACTS.md** | Sección F7-E | Actualizar estado del prerrequisito y nuevos apartados DEC-PRE-1 a DEC-PRE-5. |
| **IMPLEMENTATION_STATUS.md** | Estado F7-E | Marcar decisiones DEC-PRE-1 a DEC-PRE-5 como incorporadas y cierre del prerrequisito. Indicar que F7-E.2-I requiere autorización expresa una decisión contractual completa. |

**No alterar:** DEC16, DEC21, F7-B, F7-C, F7-D. No presentar contradicciones nuevas con estos contratos.

**Principio:** `deleted_at` permanece como **única fuente persistida de verdad** para eliminación lógica. No introducir `is_deleted` ni otros campos equivalentes.

---

## 3. Criterios de aceptación

1. ✅ Las cuatro decisiones (DEC-PRE-1 a DEC-PRE-4) y la política de restauración (DEC-PRE-5) están incorporadas sin ambigüedades en `F7-E.2-PRE-DECISION.md`.

2. ✅ El orden **UPDATE antes de unregister()** está definido y documentado como paso 6 → paso 7 en el flujo `safe_delete_world()`.

3. ✅ Los fallos entre UPDATE y unregister tienen una **política de reconciliación explícita** (tabla DEC-PRE-2): si falla UPDATE, no se ejecuta unregister y world stay recoverable; si falla unregister después de UPDATE, world stays eliminated en BD y runner no debe reactivarse.

4. ✅ **Startup nunca recupera mundos eliminados:** `run_startup` / RecoveryCoordinator deben consultar `deleted_at` y omitir worlds con `deleted_at IS NOT NULL` en la recuperación ordinaria de runners.

5. ✅ La **eliminación repetida es idempotente** y conserva autorización: `safe_delete_world()` sobre world con `deleted_at IS NOT NULL` retorna éxito sin repetir close, UPDATE ni unregister (DEC-PRE-3).

6. ✅ **GET por ID de mundo eliminado responde 404:** Incluye propietario. No se expone estado interno de eliminación (DEC-PRE-4).

7. ✅ **Restauración queda fuera de alcance:** No hay endpoint, no se cambia `deleted_at` a NULL, no se reactivan runners ni destacados dentro de F7-E.2 (DEC-PRE-5).

8. ✅ **`ON DELETE RESTRICT` y la retención histórica permanecen intactos:** `world_highlights.world_id` con restricción FK que impide borrado físico de worlds con highlights históricos. La eliminación lógica conserva la fila `WorldRow`, por lo tanto las restricciones FK se mantienen.

9. ✅ **DEC16, DEC18, DEC20 y DEC21 no presentan contradicciones nuevas:** 
   - DEC16: sin conflicto (reglas recoverable se mantienen para runners, no para worlds eliminados).
   - DEC18: evolución conceptual del `safe_delete_world()`, no cambio sustantivo de la secuencia base.
   - DEC20: compatibilidad al añadir filtro `deleted_at IS NULL`; 404 consistente con fail-closed.
   - DEC21: sin conflicto.

10. ✅ **No se modifica código, ORM, migraciones ni pruebas:** Esta decisión es exclusivamente contractual y de inspección.

11. ✅ **F7-E.2-I no se inicia:** El prerrequisito de F7-E.1 está resuelto mediante decisión contractual. La implementación requiere autorización expresa posterior a este cierre.

---

## 4. Entregable

**Archivo generado:** `F7-E.2-PRE-DECISION-FIX.md` (11,4 KB)

**Contenido del entregable:**

1. ✅ **Decisiones adoptadas:** DEC-PRE-1 a DEC-PRE-5, cumplimiento estricto del requisito de no modificar código.
2. ✅ **Flujo definitivo de eliminación:** 8 pasos contractuales con UPDATE (paso 6) antes que unregister (paso 7).
3. ✅ **Matriz de fallos y reconciliación:** Tabla completa DEC-PRE-2 con 6 escenarios y políticas de cada uno.
4. ✅ **Impacto sobre startup y recuperación:** DEC-PRE-2 escenario 6 y DEC-PRE-5: startup excluye worlds eliminados.
5. ✅ **Impacto sobre consultas y privacidad:** DEC-PRE-4: 404 para worlds eliminados incluido owner; consistencia DEC20.
6. ✅ **Documentos contractuales que requieren actualización:** Lista completa de 9 áreas en F7-E.1, DEC18 contrato, CONTRACTS.md, IMPLEMENTATION_STATUS.md.
7. ✅ **Riesgos y pruebas obligatorias para implementación:** 
   - Probar `safe_delete_world()` flow completo (happy path + 6 escenarios de fallo).
   - Probar eliminación repetida retorna no-op idempotente.
   - Probar `GET /api/worlds/{id}` retorna 404 para worlds con `deleted_at IS NOT NULL` (incluyendo owner).
   - Probar startup recovery omite worlds con `deleted_at IS NOT NULL`.
   - Verificar `ON DELETE RESTRICT` en `world_highlights` con worlds eliminados.
8. ✅ **Dictamen de preparación para F7-E.2-I:** **PREPARADO** — todas las decisiones están incorporadas, los criterios de aceptación 1-11 se cumplen, y el contrato está listo para la fase de implementación una autorización expresa.

**No iniciar F7-E.2-I:** La presente decisión cierra el prerrequisito de F7-E.1 desde el punto de vista contractual y documental. La implementación F7-E.2-I quedará autorizada cuando la revisión arquitectónica confirme que el contrato (F7-E.2-PRE-DECISION.md y F7-E.2-PRE-DECISION-FIX.md) está completamente incorporado y los criterios de aceptación 1-11 se validan en un entorno de prueba.

---
**F7-E.2-PRE-DECISION-FIX: Cierre de decisiones operativas — COMPLETADO.**