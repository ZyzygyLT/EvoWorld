# F7-E.2-PRE-DECISION: Contrato de eliminación lógica

## Objetivo

Resolver formalmente el prerrequisito de F7-E.1 a partir del informe `F7-E.2-PRE-INSPECTION.md`.

La decisión arquitectónica adoptada es:

**Introducir eliminación lógica mediante `deleted_at`, una columna nullable de tipo timestamp UTC en `WorldRow`.**

No utilizar `is_deleted` como segundo campo equivalente. `deleted_at` será la única fuente persistida de verdad para determinar si un mundo fue eliminado lógicamente.

Esta tarea es exclusivamente contractual y de inspección de impacto.

**No implementar código, ORM, migraciones ni pruebas.**

---

## 1. Modelo contractual

Definir:

- `deleted_at IS NULL`: mundo no eliminado.
- `deleted_at IS NOT NULL`: mundo eliminado lógicamente.
- El valor almacenado representa el instante UTC de eliminación.
- Los mundos existentes reciben semántica de no eliminados mediante el valor NULL.
- La eliminación lógica conserva físicamente la fila `WorldRow`.
- Las referencias históricas permanecen intactas.
- No se introduce un mecanismo de purga física de mundos dentro de F7-E.

No agregar simultáneamente `is_deleted`, `archived_at` ni otros campos equivalentes.

### Justificación

Tras la inspección técnica (`F7-E.2-PRE-INSPECTION.md`), se confirmó que:

1. **No existe mecanismo de eliminación lógica previo** en el modelo `WorldRow`. No hay columnas `deleted_at`, `is_deleted`, `archived_at`, ni ningún campo que indique estado de borrado.
2. **Todas las operaciones de eliminación son físicas** (`session.delete(row)` → DELETE SQL) a través de los caminos identificados:
   - Endpoint `DELETE /api/worlds/{world_id}` → `safe_delete_world()` → `_delete_world_row()`
   - `safe_delete_world()` secuencia DEC10: `begin_delete()` → `runner.close()` → `unregister()` → delete físico en worker thread
3. **Si `world_highlights` existiera con `ON DELETE RESTRICT`**, la eliminación física bloquearía el borrado y rompería la integridad referencial requerida por F7-E.1.
4. **El mecanismo más sólido y minimalista** es introducir `deleted_at` como única fuente de verdad, evitando campos redundantes.

El uso exclusivo de `deleted_at` (sin `is_deleted` paralela) simplifica el modelo, reduce ambigüedades y proporciona una sola columna que cumple la función de "¿está este mundo eliminado lógicamente?".

---

## 2. Contrato de safe_delete_world

Documentar la evolución del flujo actual:

### Flujo contractual de eliminación lógica

La secuencia `safe_delete_world()` (DEC10, §8-§9) experimenta el siguiente cambio conceptual, manteniendo la estructura de garantías:

1. **`registry.begin_delete(world_id)`** — Marca el mundo como "siendo eliminado"; `begin_delete` añade `world_id` a `_deleting` set y incrementa contador de usuarios. Desde este momento, `get()` y `register_existing()` rechazan el world (§9). El mark es sincrónico.

2. **`async with lock`** — Serializa simultáneas eliminaciones del MISMO world mediante lock per-world (FIFO, sin lock global). Otros worlds nunca se bloquean.

3. **`await runner.close()`** — Cierra el runner (drena tareas principales y auxiliares). **Verificación:** Solo procede si el resultado es `CLOSED`. Close incompleto RAISE, mantiene runner registrado, deja datos persistidos intactos → world stays recoverable.

4. **`registry.unregister(world_id)`** — Retira el runner verificado-closed del registry. El registry nunca cierra implícitamente.

5. **`UPDATE worlds SET deleted_at = NOW_UTC WHERE id = :world_id`** — **Paso 5 nuevo**: Actualización transaccional en lugar de DELETE físico. La fila `WorldRow` persiste con `deleted_at` poblando el instante de eliminación. **Importante:** Esta actualización se ejecuta dentro de una transacción de sesión, no en hilo worker shielded, para garantizar atomicidad con la verificación de close.

6. **Lógica de idempotencia:** Si el world ya tiene `deleted_at IS NOT NULL`, la operación es un no-op que retorna éxito sin repetir la actualización. Esto previene ejecuciones duplicadas.

7. **`_forget_recoverable(world_id)`** — Evitar stale 409 si world_id es reutilizado.

### Comportamiento ante escenarios

| Escenario | Comportamiento contractual |
|---|---|
| Mundo sin eliminar (`deleted_at IS NULL`) | Flujo normal de eliminación procede |
| Mundo ya eliminado (`deleted_at IS NOT NULL`) | No-op: retorna éxito inmediatamente, sin repetir close ni update |
| Close del runner falla (CLOSE_INCOMPLETE) | RAISE, datos persistidos intactos, world stays recoverable. `deleted_at` no se actualiza. |
| Cancelación del caller durante el UPDATE | El UPDATE persiste (es transaccional, no worker thread shielded). El estado coherente permanece: o el UPDATE completó o no. |
| Fallo entre close y update | Se considera fallo del flujo; `deleted_at` no se actualiza; world permanece recoverable con runner cerrado pero no eliminado lógicamente. El caller debe reintentarlo. |
| Mundo eliminado, luego recuperación administrativa | `deleted_at` puede limpiarse (ponerse NULL) mediante operación administrativa explícita, con autorización adecuada. |

**Nota:** El diseño deliberadamente mantiene `runner.close()` antes de la actualización de `deleted_at`, preservando las garantías de recoverability de DEC18. La actualización de `deleted_at` es el "punto de no retorno" que confirma la eliminación lógica después de haber cerrado y des-registrado el runner.

---

## 3. Impacto sobre lecturas y ciclo de vida

Para cada ruta/consulta que interactúa con `WorldRow`, la política es:

### Políticas de exclusión

| Ruta / Consulta | Excluir `deleted_at IS NOT NULL` | Permitir acceso histórico | Comentario |
|---|---|---|---|
| `GET /api/worlds` (modo público) | ✅ Sí | — | Los worlds eliminados no aparecen en listados públicos ni en `highlighted=1` |
| `GET /api/worlds` (modo `mine`) | ✅ Sí | — | El propietario tampoco ve mundos eliminados en su lista |
| `GET /api/worlds/{id}` (detalle) | ✅ Sí (404 o ficha pública) | — | El acceso directo a un world eliminado retorna 404 o representación pública vacía |
| `GET /api/worlds/{id}/stats/{kid}` | ✅ Sí | — | Requiere world activo; worlds eliminados no tienen stats |
| `GET /api/worlds/{id}/predictions` | ✅ Sí | — | Los worlds eliminados no son elegibles |
| `GET /api/worlds/{id}/chat` | ✅ Sí | — | No hay chat en worlds eliminados |
| `GET /api/worlds/{id}/leaderboard` | ✅ Sí | — | Sin leaderboard para worlds eliminados |
| `GET /api/worlds/{id}/claims` | ✅ Sí | — | Sin claims para worlds eliminados |
| `GET /api/worlds/{id}/events` | ✅ Sí | — | Sin events para worlds eliminados |
| `GET /api/worlds/{id}` (owner, full state) | ⚠️ Conditional | — | El owner ve ficha completa pero world está "eliminado"; comportamiento dependiendo de política administrativa |
| Recuperación de runners startup | ✅ Sí | — | Worlds con `deleted_at IS NOT NULL` no se reconstruyen runners |
| Recolección de checkpoints | ✅ Sí | — | Checkpoints de worlds eliminados persisten pero no son operativos |

### Políticas de acceso histórico / administrativo

Las consultas que necesiten acceso a worlds eliminados para propósitos administrativos o históricos deben usar filtros explícitos:

- `WHERE deleted_at IS NOT NULL` para seleccionar worlds eliminados
- `WHERE deleted_at IS NULL` para seleccionar worlds activos (política por defecto)
- Los endpoints administrativos pueden tener una bandera `include_deleted` o similar, pero **no es el comportamiento por defecto**

### Política general de consistencia

**"Un mundo eliminado no debe reaparecer en listados públicos, destacados ni recuperación ordinaria de runners."**

La exclusión debe ser **consistente y no depender exclusivamente de que un endpoint recuerde aplicar un filtro**. El filtro `deleted_at IS NULL` debe aplicarse en la capa de datos (repositorio/SQL) o mediante vista/índice, no confiar en que cada endpoint lo recuerde. Esto previene fugas donde un world eliminado podría filtrarse accidentalmente.

**Implementación recomendada:** El repositorio `WorldRepository` y las queries SQL deben incluir `WHERE deleted_at IS NULL` por defecto, a menos que se solicite explícitamente lo contrario. Esto garantiza consistencia en todas las rutas sin depender de memoria de desarrollador.

---

## 4. Integridad histórica y destacados

### Compatibilidad formal

| Regla | Estado |
|---|---|
| `world_highlights.world_id` conserva `ON DELETE RESTRICT` | ✅ Cumplido |
| La eliminación lógica no elimina ni modifica concesiones históricas | ✅ Cumplido |
| Los mundos eliminados no son elegibles para aparecer en `highlighted=1` | ✅ Cumplido |
| Los registros históricos continúan referenciando su `WorldRow` | ✅ Cumplido |
| No se permite borrar manualmente registros históricos para habilitar eliminaciones físicas | ✅ Cumplido |

### Reglas de concesión, expiración, revocación y recuperación

Se mantienen **idénticas** a las definidas en F7-E.1 (secciones H1-H3):

- **Concesión nueva:** `is_active = 1`, `expires_at` se establece. Si el world ya fue eliminado lógicamente (`deleted_at IS NOT NULL`), la concesión se cierra transaccionalmente dentro de la misma operación administrativa (igual que el comportamiento de "cerrar concesión vencida al conceder nueva").
- **Expiración:** `expires_at IS NULL` → vigente; `expires_at > NOW_UTC` → no vencida; `expires_at <= NOW_UTC` → vencida. La condición de elegibilidad se evalúa por consulta, no persistida.
- **Revocación (`unhighlight`):** `is_active = 0`, `revoked_at` y `revoked_by_admin_user_id` se establecen. Igual que hoy, no interactúa con `deleted_at`.
- **Recuperación de elegibilidad:** Si `is_active = 1` y (`expires_at IS NULL` O `expires_at > NOW_UTC`), el world vuelve a aparecer en `highlighted=1` automáticamente. Si `deleted_at IS NOT NULL`, la recuperación de elegibilidad **no** hace aparecer el world hasta que `deleted_at` sea limpiado (ponerse NULL) por operación administrativa.

### Comportamiento cuando un world eliminado intenta concesión

Si un administrador intenta conceder un nuevo destacado a un world que tiene `deleted_at IS NOT NULL`:

1. **Opción A (recomendada):** La concesión falla o es ignorada, ya que el world está marcado como eliminado. El administrador debe limpiar `deleted_at` primero si desea conceder destacado.
2. **Opción B (alternativa):** La transacción administrativa, dentro de la misma operación, establece `deleted_at = NULL` y luego inserta la nueva concesión de destacado. Esto requiere autorización explícita y auditoría.

La **Opción A** es la política más segura y consistente con el concepto de "eliminación lógica". La **Opción B** es un procedimiento administrativo avanzado que requeriría decisiones contractuales adicionales.

---

## 5. Migración y compatibilidad

### Diseño esperado de migración

| Acción | Detalle |
|---|---|
| **Añadir columna** | `ALTER TABLE worlds ADD COLUMN deleted_at TIMESTAMP NULL` (nullable timestamp UTC) |
| **Registros existentes** | Todos quedan con `deleted_at = NULL` (semántica: "no eliminado") |
| **Datos históricos** | Sin modificar ni reconstruir. Las filas `WorldRow` existentes se comportan como "no eliminados" desde el inicio. |
| **Índices** | Considerar `IX_worlds_deleted_at` solo si la inspección demuestra necesidad de queries frecuentes `WHERE deleted_at IS NOT NULL`. Por defecto, **no** añadir índice en esta decisión, ya que la exclusión principal es `deleted_at IS NULL` en listados públicos (mayormente `WHERE deleted_at IS NULL` es la norma y el optimizador de SQL suele manejar bien la búsqueda de NULLs sin índice adicional cuando la tabla no es gigante). |
| **Compatibilidad con migraciones existentes** | El mecanismo `ensure_schema()` (`db.py:226-253`) añade columnas faltantes vía `ALTER TABLE`. La nueva columna `deleted_at` será añadida automáticamente en próximo `ensure_schema()` execution. Sin migración manual adicional necesaria para DBs nuevas. Para DBs viejas, `ensure_schema()` la adicionará en la próxima ejecución. |
| **Migración manual no requerida** | **No crear ni ejecutar la migración** como parte de esta decisión. El diseño es conceptual y la migración será gestionada en la fase de implementación F7-E.2-I cuando se autorice. |

### Consideraciones de compatibilidad

- **Bases de datos existentes:** Los registros existentes se comportan como `deleted_at = NULL` de forma retroactiva. No se requiere UPDATE masiva alguna, la columna nullable con default NULL ya cubre el caso.
- **Nuevas instalaciones:** La columna se añade vía `ensure_schema()` o definición del modelo ORM; los nuevos worlds nacen con `deleted_at = NULL`.
- **ORM:** El modelo `WorldRow` debe incluir `deleted_at = Column(String)` (o tipo Timestamp dependiendo del backend SQL, en SQLite es TEXT/STRING). **No modificar código ORM** en esta fase (solo contrato).
- **Backend SQL:** El tipo `TIMESTAMP` o `DATETIME` con precisión UTC. En SQLite, se almacena como texto en formato ISO 8601. El contrato especifica "timestamp UTC" sin atar a proveedor SQL concreto.

---

## 6. Contratos y alcance

### Secciones que requieren actualización

| Documento | Sección | Qué actualizar |
|---|---|---|
| **F7-E.1** | H4 (Retención histórica y eliminación física) | Reemplazar sección por versión actualizada con contrato `deleted_at`. Quitar la frase "si el proyecto no dispone de mecanismo de eliminación lógica compatible, documentarlo como prerrequisito pendiente" — ese prerrequisito ya está resuelto con esta decisión. |
| **F7-E.1** | H5 (Elegibilidad sin requisitos implícitos de operabilidad) | Añadir condición: `deleted_at IS NULL` es implícitamente requerido para elegibilidad pública. Los worlds con `deleted_at IS NOT NULL` no son elegibles. |
| **F7-E.1** | Tabla de escenarios (H8) | Actualizar filas para incluir `deleted_at IS NOT NULL` como condición de "mundo eliminado". |
| **DEC18** | `safe_delete_world()` | Documentar la evolución del flujo hacia la actualización de `deleted_at` en lugar de DELETE físico. No alterar la secuencia DEC18 existente, solo extenderla conceptualmente. |
| **CONTRACTS.md** | Sección F7-E | Actualizar el estado del prerrequisito y el contrato de `deleted_at`. |
| **IMPLEMENTATION_STATUS.md** | Estado F7-E | Marcar el prerrequisito de eliminación lógica como "resuelto (decisión contractual)" y esperar fase F7-E.2-I. |

### Documentos que NO requieren alteración

- **DEC16** — Sin incompatibilidad demostrada. Las reglas de recoverable/non-recoverable permanecen intactas.
- **DEC18** — La secuencia `safe_delete_world()` se extiende conceptualmente; los pasos de close, verify, unregister se mantienen.
- **DEC20** — Las reglas de visibilidad y pública/private se mantienen. Los worlds eliminados simplemente son excluidos por `deleted_at IS NULL`.
- **DEC21** — La identidad de usuario y autorización se mantienen sin cambios.
- **F7-B, F7-C, F7-D** — Sin relación directa con mecanismo de delete de worlds.

### Alcance que NO se amplía

- **No se ampliar el scope a rankings globales, identidad competitiva global ni otras funcionalidades de F7-F.** La decisión es exclusivamente sobre mecanismo de eliminación lógica de worlds individuales.
- **No se tocan decisiones contractuales previas** (DEC16-DEC21) salvo la adición del campo `deleted_at` y sus implicancias de elegibilidad, que es un cambio específico y documentado.

---

## 7. Entregable

**Archivo creado:** `F7-E.2-PRE-DECISION.md`

El documento contiene:

1. ✅ Decisión arquitectónica adoptada: `deleted_at` como única columna de eliminación lógica.
2. ✅ Semántica exacta: `deleted_at IS NULL` = no eliminado, `deleted_at IS NOT NULL` = eliminado lógicamente, valor = timestamp UTC.
3. ✅ Flujo contractual de eliminación lógica: evolución de `safe_delete_world()` con UPDATE en lugar de DELETE físico.
4. ✅ Matriz de impacto sobre lecturas y ciclo de vida: políticas de exclusión por ruta, acceso histórico/Admin, consistencia.
5. ✅ Compatibilidad con runners y recuperación: garantías mantenidas, worlds eliminados no se reconstruyen.
6. ✅ Compatibilidad con `world_highlights` y retención histórica: `ON DELETE RESTRICT` preservado, highlights históricos intactos.
7. ✅ Diseño conceptual de migración: `ALTER TABLE worlds ADD COLUMN deleted_at TIMESTAMP NULL`, retrocompatibilidad.
8. ✅ Contratos y documentos que deben actualizarse: F7-E.1, DEC18 (conceptual), CONTRACTS.md, IMPLEMENTATION_STATUS.md.
9. ✅ Riesgos, casos límite y garantías: idempotencia, fallos entre close y update, comportamiento administrativo, consistencia de filtrado.
10. ✅ Dictamen sobre el cierre del prerrequisito de F7-E.1: **RESUELTO** mediante decisión contractual de introducir `deleted_at`.

---

## 8. Restricciones

- ✅ **No modificar código fuente** — decisiones exclusivamente contractuales.
- ✅ **No modificar ORM** — el modelo `WorldRow` se actualizará en fase de implementación F7-E.2-I.
- ✅ **No crear ni ejecutar migraciones** — diseño conceptual únicamente.
- ✅ **No modificar pruebas** — suites de pruebas existentes no se tocan.
- ✅ **No alterar datos** — el `ensure_schema()` añadirá la columna NULL default en ejecución futura; datos existentes quedan intactos.
- ✅ **No crear commits ni ramas** — state Git preservado.
- ✅ **No iniciar F7-E.2-I (implementación)** — esta decisión es fase de contrato/inspección.

**Próximo paso:** La siguiente revisión arquitectónica decidirá si el contrato está suficientemente definido para autorizar la implementación (F7-E.2-I). Hasta entonces, el prerrequisito de F7-E.1 queda **resuelto** desde el punto de vista contractual.

---

## Resumen del prerrequisito resuelto

**Antes de esta decisión:** Prerrequisito pendiente — "verificar el mecanismo de eliminación lógica compatible con la retención de `world_highlights` antes de F7-E.2" — sin solución documentada.

**Después de esta decisión:** Prerrequisito **RESUELTO**. Se ha definido contractualmente:

1. **Columna `deleted_at`** en `WorldRow` como única fuente de verdad de eliminación lógica.
2. **Flujo `safe_delete_world()`** evolucionado: close→verify→unregister→UPDATE `deleted_at` (en lugar de DELETE físico).
3. **Compatibilidad con `world_highlights`**: `ON DELETE RESTRICT` preservado, retención histórica garantizada.
4. **Políticas de exclusión** consistentes en todas las lecturas (no depende de memoria de endpoint).
5. **Migración conceptual:** `ALTER TABLE worlds ADD COLUMN deleted_at TIMESTAMP NULL`, retrocompatible.
6. **Actualizaciones contractuales** identificadas en F7-E.1, DEC18 conceptual, CONTRACTS.md.

**El prerrequisito de F7-E.1 fue resuelto mediante decisión contractual** sin necesidad de modificar código, ORM, migraciones ni pruebas. La fase F7-E.2-I (implementación) quedará autorizada cuando la revisión arquitectónica confirme que el contrato está suficientemente definido.