# F7-E.2-PRE: Inspección técnica del mecanismo de eliminación de mundos

## 1. Inspección del estado del repositorio

**Estado Git actual:**
- Rama actual: `feature/engine-checkpoints`
- Hay múltiples archivos modificados y sin rastrear provenientes de trabajo previo (F7-E.1-FIX1 a FIX6, F7-D, etc.)
- **No se realizaron modificaciones** durante esta inspección; solo se leyeron y analizaron archivos existentes.
- El working tree contiene cambios preexistentes en `app.py`, `db.py`, `runner.py`, `repository.py` y otros archivos que fueron modificados en fases anteriores del proyecto. Estos no fueron alterados por esta inspección.

**Estructura del proyecto identificada:**
- `src/worldsim/server/db.py`: Modelos ORM SQLAlchemy (WorldRow, ClaimRow, PredictionRow, ChatRow, UserRow, etc.)
- `src/worldsim/server/runner.py`: WorldRunner, RunnerRegistry, safe_delete_world(), _delete_world_row()
- `src/worldsim/server/app.py`: Endpoints FastAPI incluyendo DELETE /api/worlds/{world_id}
- `src/worldsim/server/repository.py`: WorldRepository y repositorios auxiliares
- `F7-E-R0-DEF.IN.md`: Contrato técnico de destacados (R0)
- Sin tabla `world_highlights` en el código Python actual; el contrato la menciona como concepto futuro

---

## 2. Inspección del modelo WorldRow

**Definición ORM** (`src/worldsim/server/db.py`, líneas 20-38):

```python
class WorldRow(Base):
    __tablename__ = "worlds"

    id = Column(Integer, primary_key=True, autoincrement=True)
    seed = Column(Integer, index=True)
    width = Column(Integer)
    height = Column(Integer)
    months_simulated = Column(Integer)
    land_pct = Column(Float)
    state_json = Column(Text)  # full engine_to_dict snapshot
    created_at = Column(String)
    name = Column(String, default="")
    mode = Column(String, default="single")  # single | league
    season_years = Column(Integer, default=300)
    season_over = Column(Integer, default=0)
    enabled_races_json = Column(String, default="")
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    sea_level = Column(Float, default=0.2)
```

**Columnas relacionadas con estado, visibilidad, propietario y eliminación:**
| Columna | Tipo | Descripción |
|---|---|---|
| `id` | Integer | Primary key |
| `owner_user_id` | Integer (FK nullable) | Propietario, puede ser NULL (mundos legacy) |
| `visibility` | **No existe** en WorldRow | La visibilidad se gestiona pero no como columna del modelo ORM directamente; se consulta y actualiza vía endpoints |
| `state_json` | Text | Snapshot del engine, no indica estado lógico de eliminación |
| Ninguna columna `deleted_at`, `is_deleted`, `archived_at`, `highlighted`, o equivalente de eliminación lógica | | **AUSENTES** |

**Migraciones:**
- `ensure_schema()` (db.py, líneas 226-253): Crea tablas faltantes y añade columnas que faltan en DBs viejos (solo SQLite). No hay migraciones definidas para `worlds` con columnas de eliminación lógica.
- **No hay** columnas de eliminación lógica en el modelo ni en las migraciones.

**Restricciones y relaciones:**
- `owner_user_id` es `ForeignKey("users.id")` y `nullable=True`
- Ninguna relación `ForeignKey` hacia `world_highlights` en el modelo actual
- Las tablas `world_events`, `world_operations`, `world_checkpoints`, `claims`, `predictions`, `chat` tienen `world_id` FK a `worlds.id`, pero **ninguna tiene `ON DELETE RESTRICT` documentado** en el modelo SQLAlchemy (las restricciones dependen de la configuración SQL subyacente)

**Hallazgo clave:** No existe **ningún** campo de eliminación lógica (`deleted_at`, `is_deleted`, `archived_at`, `highlighted`, etc.) en el modelo `WorldRow` o en ningún otro modelo ORM.

---

## 3. Inspección de todos los caminos de eliminación

### Camino 1: Endpoint HTTP `DELETE /api/worlds/{world_id}`

**Archivo y función:** `src/worldsim/server/app.py`, líneas 718-750

```python
@app.delete("/api/worlds/{world_id}")
async def delete_world(world_id: int, authorization: str | None = Header(default=None)) -> dict:
    user = _auth_user(authorization)
    with db.SessionLocal() as db_:
        row = db_.get(WorldRow, world_id)
        if row is None:
            raise HTTPException(404, f"world {world_id} not found")
        if row.owner_user_id is not None:
            if user is None:
                raise HTTPException(401, "log in to delete this world")
            if user.id != row.owner_user_id:
                if _visibility(row) == "private":
                    raise HTTPException(404, f"world {world_id} not found")
                raise HTTPException(403, "only the owner may delete this world")
    # Sequence: safe_delete_world
    try:
        await safe_delete_world(world_id, session_factory=db.SessionLocal, registry=registry)
    except WorldNotFoundError:
        raise HTTPException(404, f"world {world_id} not found") from None
    _forget_recoverable(world_id)
    return {"deleted": world_id}
```

**Secuencia de operaciones:**
1. Validar existencia del mundo y permisos del usuario (owner o 401/403/404)
2. Llamar a `safe_delete_world(world_id, session_factory=db.SessionLocal, registry=registry)`

### Camino 2: Función `safe_delete_world`

**Archivo y función:** `src/worldsim/server/runner.py`, líneas 1028-1112

**Secuencia mandatory (DEC10, §8-§9):**

1. **`registry.begin_delete(world_id)`** - Marca el mundo como "siendo eliminado"; `begin_delete` añade `world_id` a `_deleting` set y incrementa contador de usuarios. Desde este momento, `get()` y `register_existing()` rechazan el world (§9). El mark es sincrónico.

2. **`async with lock`** - Serializa simultáneas eliminaciones del MISMO world mediante lock per-world (FIFO, sin lock global). Otros worlds nunca se bloquean.

3. **`await runner.close()`** - Cierra el runner (drena tareas principales y auxiliares). **Verificación:** Solo procede si el resultado es `CLOSED`. Close incompleto RAISE, mantiene runner registrado, deja datos persistidos intactos → world stays recoverable.

4. **`registry.unregister(world_id)`** - Retira el runner verificado-closed del registry. El registry nunca cierra implícitamente.

5. **`asyncio.to_thread(_delete_world_row, session_factory, world_id)`** - **Paso 5**: Elimina la fila persistente en un hilo worker (nunca bloquea el event loop). Este es el delete físico.

6. **`_lingering_delete_work`** y `add_done_callback` - Referencias fuertes para cancelled handling (§9): si el CALLER es cancelado, el delete sigue terminando en su thread; la guarda se transfiere al callback de completion.

**Operación SQL u ORM ejecutada (Paso 5):**
```python
# runner.py línea 1016-1025
def _delete_world_row(session_factory, world_id: int) -> None:
    """Blocking row delete; runs in a WORKER THREAD (never on the loop)."""
    from worldsim.server.db import WorldRow
    with session_factory() as session:
        row = session.get(WorldRow, world_id)
        if row is None:
            raise WorldNotFoundError(world_id)
        session.delete(row)  # <-- DELETE FÍSICO SQL
        session.commit()
```
- **Tipo:** `session.delete(row)` → DELETE SQL físico sobre `worlds` table por primary key.
- **No hay** `WHERE is_deleted = FALSE` ni filtro lógico.
- **No hay** UPDATE que marque un estado; la fila se elimina completamente.

### Camino 3: Función `_delete_world_row` (directa)

**Archivo y función:** `src/worldsim/server/runner.py`, línea 1016

**Mismo comportamiento que el Paso 5 de `safe_delete_world`:** DELETE físico puro sobre `WorldRow` por ID.

### Camino 4: Operaciones administrativas / mantenimiento

No se encontraron puntos de entrada administrativa adicionales para eliminación de mundos fuera de `safe_delete_world` y el endpoint HTTP. El `recovery_coordinator.py` y `persistence_service.py` no contienen operaciones de delete world.

### ¿Qué sucede con runners y snapshots?

- **Runner:** `close()` debe completarse exitosamente (`CLOSED`) antes del delete. Si close falla o es incompleto, se RAISE y los datos persistidos se dejan intactos (world stays recoverable).
- **Snapshots:** No hay operación de delete sobre checkpoints ni events al borrar un mundo. Los datos históricos (events, operations, checkpoints) **permanecen en la base de datos** después de la eliminación del mundo. El contrato DEC10 especifica que "the world stays recoverable" — los datos persisten para posible recuperación futura.

### ¿Qué sucede con relaciones y registros históricos?

- **world_events:** Se dejan intactos (no hay cascade delete).
- **world_operations:** Se dejan intactos.
- **world_checkpoints:** Se dejan intactos.
- **claims:** Se dejan intactos (FK a worlds.id, pero sin ON DELETE RESTRICT documentado en el modelo).
- **predictions:** Se dejan intactos.
- **chat:** Se dejan intactos.
- **world_highlights:** **No implementado en el código actual**. El contrato F7-E-R0-DEF.IN.md lo menciona como concepto pero sin tabla ni columnas en el código Python. Si existiera con `ON DELETE RESTRICT`, el DELETE físico fallaría.

### ¿Qué autorización exige?

- **Endpoint `DELETE /api/worlds/{world_id}`:**
  - 401 si `authorization` es nulo o no comienza con "Bearer " (due owner must login)
  - 404 si el world no existe
  - 401 si owner_user_id no es nulo y user es anónimo
  - 403 si user.id != owner_user_id y world es private (404) o no es owner (403)
  - 403 si user.id != owner_user_id y world es public (solo owner puede delete)
- **`safe_delete_world()`:** No toma parámetros de autorización; asume que el caller ya validó permisos en el endpoint HTTP. El propio `safe_delete_world` no verifica ownership; eso es responsabilidad de la capa HTTP.

### ¿Qué transacciones y garantías de atomicidad aplica?

- **Capa HTTP:** Cada operación usa `db.SessionLocal()` como context manager; commit explícito en visibility changes; para delete, la transacción es manejada internamente por `safe_delete_world`.
- **`safe_delete_world`:** La secuencia (begin_delete → close → unregister → delete en worker thread) no es atómica en un solo transaction. Es una secuencia coordinada:
  - `begin_delete` y el lock marcan la exclusión mutua
  - `runner.close()` es async, idempotente, puede fallar (CLOSE_INCOMPLETE)
  - El delete en worker thread es asíncrono y shielded
  - Si el caller se cancela, el delete continúa en su thread (referencia held)
- **No hay** garantía de atomicidad "todo o nada" en un solo transaction para la secuencia completa. El diseño DEC10 deliberadamente separa close (en event loop) del delete (en worker thread) para evitar bloqueos.

### Operaciones bulk delete

No se encontraron operaciones de bulk delete. Cada eliminación es por world_id individual.

---

## 4. Inspección de las pruebas

**Pruebas existentes sobre eliminación de mundos:**
- `tests/test_runner_foundation.py` - Foundation tests for runner
- `tests/test_runner_close.py` - Tests for close operation
- `tests/test_world_deletion.py` - Tests specifically for world deletion
- `tests/test_shutdown.py` - Shutdown/tests

**Comportamiento probado (según el código):**
- Close del runner transita estados: NOT_STARTED → RUNNING → CLOSING → CLOSED / CLOSE_INCOMPLETE
- `safe_delete_world` secuencia: begin_delete → close → unregister → delete worker thread
- Close incompleto RAISE y deja datos intactos (world recoverable)
- Cancelación del caller: el delete continúa en worker thread, la guarda se transfiere al callback
- WorldNotFoundError se traduce a 404 HTTP

**Comportamiento descrito documentalmente (no necesariamente probado):**
- `ON DELETE RESTRICT` en `world_highlights.world_id → worlds.id` impedirá delete físico
- Retención histórica de destacados después de eliminación de mundo

**No se ejecutaron suites completas** innecesariamente; solo se inspeccionó el código fuente.

**Determinación:** El código no tiene pruebas que verifiquen un mecanismo de eliminación lógica (soft-delete). Las pruebas se enfocan en el flujo físico de close→unregister→delete.

---

## 5. Evaluación de compatibilidad con F7-E.1

**Escenarios evaluados:**

| Escenario | Mecanismo encontrado | Compatibilidad con `ON DELETE RESTRICT` en `world_highlights.world_id` |
|---|---|---|
| Mundo sin destacados históricos | DELETE físico sobre `WorldRow` | **Bloqueado** si `world_highlights` existe con FK RESTRICT |
| Mundo con concesión activa | Mismo DELETE físico | **Bloqueado** si `world_highlights` existe con FK RESTRICT |
| Mundo con concesión revocada | Mismo DELETE físico | **Bloqueado** si `world_highlights` existe con FK RESTRICT |
| Mundo con concesión vencida | Mismo DELETE físico | **Bloqueado** si `world_highlights` existe con FK RESTRICT |
| Mundo con varias concesiones históricas | Mismo DELETE físico | **Bloqueado** si `world_highlights` existe con FK RESTRICT |
| Mundo privado | Mismo DELETE físico | **Bloqueado** si `world_highlights` existe con FK RESTRICT |
| Mundo cuyo propietario deja de estar vinculado a cuenta válida | Mismo DELETE físico | **Bloqueado** si `world_highlights` existe con FK RESTRICT |

**Compatibilidad con `ON DELETE RESTRICT`:**
- **NULO COMPATIBILIDAD VERIFICABLE.** El mecanismo de eliminación actual es 100% físico (`session.delete(row)`). Si `world_highlights` tiene `world_id` con `ON DELETE RESTRICT` hacia `worlds.id`, la eliminación física **fallaría** con error de restricción de integridad referencial.
- Como `world_highlights` **no existe** como tabla/columnas en el código Python actual (solo se menciona en el contrato F7-E-R0-DEF.IN.md), no hay restricción activa hoy. Pero el contrato la contempla como requisito futuro.

**Hallazgo crítico:** El proyecto **no dispone** de un mecanismo de eliminación lógica (soft-delete) compatible con `ON DELETE RESTRICT`. La única alternativa es la eliminación física, que estaría sujeta a la restricción FK.

---

## 6. Dictamen arquitectónico

### Resultado: **C. No existe mecanismo lógico verificable**

**Justificación:**

1. **Ausencia de campos de eliminación lógica:** El modelo `WorldRow` no tiene columnas `deleted_at`, `is_deleted`, `archived_at`, o cualquier campo que indique un estado lógico de eliminación. La única columnas relevantes son `owner_user_id` (FK nullable) y no hay columna de estado de borrado.

2. **Eliminación física pura:** Todos los caminos de eliminación (`_delete_world_row`, el Paso 5 de `safe_delete_world`, el endpoint `DELETE /api/worlds/{world_id}`) ejecutan `session.delete(row)` → DELETE SQL físico sobre la fila de `worlds`. No hay UPDATE que marque `is_deleted = TRUE` ni `deleted_at = NOW()`.

3. **`world_highlights` no implementado:** El contrato F7-E-R0-DEF.IN.md menciona `world_highlights` como un concepto para el destacado (`highlighted`), pero:
   - No hay tabla `world_highlights` en el código Python
   - No hay columnas en `WorldRow` relacionadas
   - El contrato indica: "En R0, sin tabla nueva, el campo `highlighted` simplemente deja de ser consultable"
   - Si `world_highlights` existiera con `ON DELETE RESTRICT`, el delete físico fallaría

4. **Secuencia DEC10 no proporciona eliminación lógica:** `safe_delete_world` cierra el runner, verifica el close, des-registra y luego elimina la fila físicamente. No hay paso que convierta el DELETE físico en un UPDATE lógico. El diseño deliberadamente separa close del delete para world recoverability, pero ambos operan sobre la eliminación física física.

5. **Contrastación con los contratos:**
   - **DEC16:** No tiene disposiciones sobre mecanismo de delete de worlds; trata sobre recoverable/non-recoverable guard state.
   - **DEC18:** Define `WorldRunner.close()` y `safe_delete_world()` secuencia, pero ambas operan sobre el runner y datos físicos; no introduce soft-delete.
   - **DEC20:** Define visibilidad (private/public) y endpoints, pero no mecanismos de delete lógico.
   - **DEC21:** Define identidad de usuario para operaciones; no tiene disposiciones de delete lógico.

**No se encontraron** mecanismos documentados o de código que permitan la eliminación lógica compatible con retención de `world_highlights`.

### Hallazgos de riesgo:

| Riesgo | Gravedad | Comentario |
|---|---|---|
| Eliminación física rompe integridad si `world_highlights` existe con RESTRICT | Alta | Si se crea la tabla `world_highlights` con FK RESTRICT, los deletes actuales fallarán |
| Datos históricos (events, operations, checkpoints) se pierden conceptualmenteaa aa | Media | Aunque no se borran filas, el mundo ya no es recuperable como "mundo completo" (runner cerrado, datos persistidos pero inaccesibles sin runner) |
| World queda en estado "recoverable" pero sin runner | Alta | Los datos persisten pero no hay clock activo; recuperación requiere intervención administrativa |
| No hay camino de "soft-delete → recuperación" | Alta | Si se requiere preservar el mundo en vivo y después retirarlo, no existe mecanismo |

---

## 7. Alternativa arquitectónica recomendada

### Escenario C: No existe mecanismo lógico verificable

**Recomendación arquitectónica:**

Dado que el proyecto actualmente no tiene mecanismo de eliminación lógica, la alternativa más sólida considerando los requisitos de F7-E.1 es:

#### Opción C1: Implementar eliminación lógica antes de F7-E.2

Añadir campos de eliminación lógica a `WorldRow` y modificar el flujo de eliminación:

1. **Añadir columna `is_deleted`** (Boolean, default `FALSE`) a `WorldRow`
   - `ALTER TABLE worlds ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE`
   - Index para queries frecuentes

2. **Modificar `_delete_world_row`** para UPDATE en lugar de DELETE físico:
   ```python
   def _delete_world_row(session_factory, world_id: int) -> None:
       with session_factory() as session:
           row = session.get(WorldRow, world_id)
           if row is None:
               raise WorldNotFoundError(world_id)
           row.is_deleted = True
           # Opcional: row.deleted_at = datetime.utcnow()
           session.commit()
   ```
   - **Beneficio:** Permite `ON DELETE RESTRICT` en `world_highlights.world_id → worlds.id` porque la fila física nunca se borra.
   - **Cambio de comportamiento:** Las queries deben filtrar `WHERE is_deleted = FALSE` o el ORM debe excluir rows soft-deleteados.

3. **Actualizar `safe_delete_world`** para reflejar la nueva secuencia:
   - La secuencia seguiría siendo: begin_delete → close → unregister → UPDATE is_deleted (en lugar de DELETE)
   - Garantías de recoverability se mantienen, pero el world físico persiste marcado como deleted.

4. **Considerar `deleted_at`** para auditoría:
   - `deleted_at = Column(DateTime, nullable=True)`
   - Se pobla al momento del soft-delete.
   - Permite queries de "worlds eliminados recientemente" para fines administrativos.

5. **Actualizar endpoints y queries:**
   - `list_worlds`: Añadir `WHERE visibility = 'public' AND is_deleted = FALSE` (modo público)
   - `get_world`: Si `is_deleted = TRUE`, comportamiento dependiendo de requisito (404 o world "fantasma")
   - Queries sobre `world_highlights`: Ahora compatibles con `ON DELETE RESTRICT` ya que rows físicos persisten.

**Compatibilidad con requisitos:**

| Requisito | Satisfacción |
|---|---|
| `ON DELETE RESTRICT` en `world_highlights.world_id → worlds.id` | ✅ Cumplido (fila física persiste) |
| Retención permanente del historial de destacados | ✅ Cumplido (rows de `world_highlights` nunca bloqueadas por RESTRICT) |
| Integridad referencial | ✅ Cumplido (fila WorldRow persiste con `is_deleted=TRUE`) |
| Ciclo de vida y recuperación de mundos | ⚠️ Parcial: world persiste marcado deleted; "recovery" requeriría restablecer `is_deleted = FALSE` |
| Privacidad DEC20 | ✅ Cumplido (queries filtran `is_deleted = FALSE`) |
| Simplicidad operativa | ✅ Cumplido (UPDATE en lugar de DELETE, cambio minimalista) |
| Impacto sobre mundos y datos existentes | ⚠️ Requiere filtrado en queries existentes |

#### Opción C2: Conservar eliminación física pero documentar procedimiento administrativo

Mantener el delete físico actual, pero documentar que:

1. **Antes de eliminar un world físicamente**, si se desea preservar `world_highlights`:
   - Revocar/conceder highlights admin manually (`unhighlight` operations)
   - Borrar filas `world_highlights` asociadas manualmente
   - Proceder luego al delete físico de `WorldRow`

2. **Esto está fuera del contrato R0** y requeriría decisiones sobre:
   - Quién tiene permiso para borrar `world_highlights` manualmente
   - Si el sistema debe automatizar este proceso
   - Behavior cuando `world_highlights` existe pero no hay mecanismo de crear/borrar en el código actual

3. **Compatibilidad:** `ON DELETE RESTRICT` impediría el delete hasta que se borren las filas de `world_highlights` manualmente.

**Rechazo:** Esta opción no satisface el requisito de "retención permanente del historial de destacados" ya que requiere borrado manual de filas históricas. No es una solución arquitectónica limpia.

#### Opción C3: Vista/índice que oculte worlds eliminados pero no modifique mecanismo

Añadir un índice o vista que filtre `is_deleted = FALSE` sin modificar el mecanismo de delete:

- **Limitación:** No resuelve el problema de `ON DELETE RESTRICT`; la fila física aún se borra y la restricción FK sigue fallando.
- **No recomendado** como solución para el problema planteado.

---

### Conclusión del dictamen

**Resulta C: No existe mecanismo lógico verificable.**

El proyecto actualmente:
- No tiene campos de eliminación lógica en `WorldRow` (`deleted_at`, `is_deleted`, `archived_at`, etc.)
- Ejecuta eliminación física pura (`session.delete(row)`) en todos los caminos
- No tiene tabla `world_highlights` implementada (solo mencionada en contrato conceptual)
- El `safe_delete_world` (DEC10) secuencia close→verify→unregister→delete físico, sin conversión a soft-delete

**Para satisfacer el contrato F7-E.1 y compatibilizar con `ON DELETE RESTRICT` en `world_highlights`,** la alternativa arquitectónica más sólida es la **Opción C1**: implementar una columna `is_deleted` (y opcionalmente `deleted_at`) en `WorldRow` y modificar `_delete_world_row` para hacer UPDATE en lugar de DELETE físico. Esto permitiría:

1. `ON DELETE RESTRICT` en `world_highlights.world_id → worlds.id` sin interferencia
2. Retención indefinida de registros de `world_highlights` (filas físicas siempre disponibles)
3. Integridad referencial preservada
4. Mundo marcado como eliminado pero aún presente en BD para posible recuperación administrativa

**Cambios que requerirían decisión contractual adicional:**
- Añadir columna `is_deleted` a migración SQL (ALTER TABLE) — requiere autorización de migración
- Modificar `_delete_world_row` de DELETE a UPDATE — cambia comportamiento de delete a nivel de sistema
- Actualizar queries y endpoints para filtrar `is_deleted = FALSE` — alcance en código existente
- Decidir comportamiento de worlds "soft-deleted" en API pública (¿404? ¿visible como world fantasma? ¿sin exposición?)

**Estos cambios están fuera del alcance de "solo inspección" de F7-E.2-PRE** y requerirían una decisión contractual (F7-E o posterior) antes de implementarlos. La presente inspección confirma que **no existe mecanismo verificable actualmente** y documenta el camino a seguir.

---

## 8. Prerrequisito pendiente: resuelto o todavía pendiente

**Estado: PRERREQUISITO PENDIENTE identificado y documentado.**

La inspección técnica confirma que **no existe mecanismo de eliminación lógica compatible** con `ON DELETE RESTRICT` en `world_highlights.world_id → worlds.id` en el código actual.

**Declaración explícita:**

> **PRERREQUISITO PENDIENTE:** Implementar mecanismo de eliminación lógica compatible con la retención de `world_highlights` antes de iniciar F7-E.2.

Los pasos no resueltos son:
1. **Decidir si añadir columnas `is_deleted`/`deleted_at` a WorldRow** (y qué comportamiento implican)
2. **Modificar `_delete_world_row`** para UPDATE lógico en lugar de DELETE físico
3. **Actualizar queries y endpoints** para filtrar worlds soft-deleted
4. **Definir autoridad y criterios** para concesión/retirada de `world_highlights` registros
5. **Definir esquema SQL** con `ON DELETE RESTRICT` y validar compatibilidad

Ningún estos pasos se han ejecutado y ninguno forma parte de la inspección actual (que es exclusivamente de análisis y reporte).

---

## 9. Confirmación de que no se modificó ningún archivo del proyecto ni el estado Git

**Confirmado:** Esta inspección ha sido **exclusivamente de lectura y análisis**. No se han:

- Modificado archivos de código fuente (`db.py`, `runner.py`, `app.py`, `repository.py`, etc.)
- Alterado modelos ORM, definiciones de columnas o migraciones
- Ejecutado suites de pruebas o modificado datos de prueba
- Creado commits, ramas o alterado el estado Git
- Agregado, movido o eliminado archivos del repositorio

El estado actual del repositorio (ver `git status` arriba) permanece inalterado por esta inspección. Todos los archivos modificados listados en `git status` son producto de trabajo previo (F7-E.1-FIX1 a FIX6, F7-D, etc.) y no de esta inspección.

**Evidencia concreta:**
- `git status` ejecutado después de la inspección muestra la misma lista de archivos modificados/untracked que antes
- No hay archivos nuevos en el directorio de trabajo (aparte de los temporales `tmp_*` que existían previa)
- Solo se llamó a `read()` para inspeccionar archivos, `grep()` para buscar patrones, y `shell` para consultar `git status` — ninguna herramienta de escritura

---

## 10. Resumen ejecutivo

**Dictamen: C — No existe mecanismo lógico verificable.**

**Hallazgos principales:**
1. `WorldRow` tiene **ninguna** columna de eliminación lógica (`deleted_at`, `is_deleted`, `archived_at`, etc.)
2. Todos los caminos de eliminación ejecutan **DELETE físico** (`session.delete(row)`)
3. `world_highlights` **no está implementado** en el código Python actual (solo en contrato conceptual F7-E-R0-DEF.IN.md)
4. Si `world_highlights` existiera con `ON DELETE RESTRICT`, la eliminación física **fallaría**
5. El contrato DEC10 `safe_delete_world` secuencia no incluye conversión a soft-delete

**Recomendación:** Implementar columna `is_deleted` (Boolean, default FALSE) en `WorldRow` y modificar `_delete_world_row` para UPDATE `is_deleted = TRUE` en lugar de DELETE físico. Esto habilitaría `ON DELETE RESTRICT` en `world_highlights` y permitiría retención histórica indefinida. Estos cambios requieren decisión contractual aparte de la presente inspección.

**Cierre:** La inspección está completada con evidencia suficiente para decidir cómo preservar el historial de destacados frente a la eliminación de mundos. No se iniciará F7-E.2-I (implementación) hasta que se resuelva el prerrequisito de mecanismo de eliminación lógica.