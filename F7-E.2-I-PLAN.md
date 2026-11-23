# F7-E.2-I — Plan de Implementación de Eliminación Lógica

## 1. Estado Inicial de Git

**Branch actual:** feature/engine-checkpoints
**Archivos modificados (trabajo previo, sin tocar):**
- src/worldsim/server/app.py
- src/worldsim/server/db.py
- src/worldsim/server/runner.py
- src/worldsim/sim/engine.py
- src/worldsim/sim/models.py
- tests/conftest.py
- tests/test_auth.py
- tests/test_league.py
- tests/test_multiplayer.py
- tests/test_server.py
- web/src/App.jsx
- web/src/components/RoomPanel.jsx
- web/src/components/StartScreen.jsx

**Archivos nuevos (trabajo previo):**
- tests/test_checkpoint.py
- F7-E.2-PRE-DECISION-FIX.md a FIX9.md

**Sin modificaciones en esta fase de planificación.** Se confirma que ningún archivo `.py` fue alterado durante la inspección de FIX1-FIX9.

---

## 2. Inventario de Archivos y Funciones Afectadas

### 2.1. Modelo ORM
| Archivo | Función/Clase | Cambio necesario |
|---|---|---|
| `src/worldsim/server/db.py:20-38` | `class WorldRow(Base)` | Agregar columna `deleted_at = Column(DateTime, nullable=True)` |

### 2.2. Migración de esquema
| Archivo | Función | Cambio necesario |
|---|---|---|
| `src/worldsim/server/db.py:226-252` | `ensure_schema()` | Ya soporta ADD COLUMN idempotente; `deleted_at` se añadirá automáticamente en BD nuevas. Para BD existentes, el método `ensure_schema` actual necesita lógica explícita para añadir `deleted_at` a tablas existentes (usa `inspect` + `ALTER TABLE ADD COLUMN` — ya presente, solo necesita incluir la nueva columna en la iteración). |

### 2.3. Eliminación lógica (`safe_delete_world`)
| Archivo | Función | Cambio necesario |
|---|---|---|
| `src/worldsim/server/runner.py:1016-1025` | `_delete_world_row()` | Cambiar de `session.delete(row)` + `session.commit()` a `UPDATE worlds SET deleted_at = NOW_UTC` + `session.commit()` |
| `src/worldsim/server/runner.py:1073-1112` | `safe_delete_world()` | El orden ya es correcto (`close → verify → unregister → UPDATE`). Solo cambia la implementación del UPDATE (lógico vs físico). |

### 2.4. Recuperación (`_stage_a`)
| Archivo | Función | Cambio necesario |
|---|---|---|
| `src/worldsim/server/recovery_coordinator.py:461-465` | `_list_world_rows()` | Filtrar `row.deleted_at IS NOT NULL` antes de iterar (o en la query SQL) |
| `src/worldsim/server/recovery_coordinator.py:401-459` | `_stage_a()` | Ya itera sobre todas las rows; añadir condición de elegibilidad `deleted_at IS NULL` |

### 2.5. Consultas ordinarias en app.py
| Archivo/Línea | Contexto | Cambio necesario |
|---|---|---|
| `app.py:_load_row()` (línea 553-558) | Carga de fila para operaciones | El `HTTP 404` ya cubre world no encontrado; `deleted_at IS NOT NULL` debe tratarse como "mundo eliminado" → 404 consistente con DEC20. |
| `app.py:710` (visibility change) | `db_.get(WorldRow, world_id)` | Si `deleted_at IS NOT NULL`, lanzar 404 (mundo eliminado, no visible). |
| `app.py:1294` (`_canonical_month`) | `db_.get(WorldRow, world_id)` | Si `deleted_at IS NOT NULL`, retornar 0 o lanzar error — depende de política. Por seguridad: retornar 0 y dejar que el caller maneje. |
| `app.py:555` (`_load_row` usage) | Varios endpoints | Filtrar `deleted_at IS NOT NULL` → 404. |
| `app.py:698-715` (visibility change) | Ya usa `_load_row` → 404 si row es None. | |
| `app.py:729` (delete_world handler) | Ya valida row existence → el flujo pasa a `safe_delete_world`. | |

### 2.6. Filtros a nivel de query
Los siguientes lugares deben excluirse `deleted_at IS NOT NULL` en sus queries SQL:

| Ubicación | Query actual | Filtro necesario |
|---|---|---|
| `recovery_coordinator._list_world_rows()` (línea 463) | `select(WorldRow).order_by(WorldRow.id)` | `select(WorldRow).where(WorldRow.deleted_at.is_(None)).order_by(WorldRow.id)` |
| Listados `/api/worlds` (paginados) | Query SQL en app.py | Añadir `WHERE deleted_at IS NULL` (o equivalente en el ORM). |
| Recovery/startup | `_stage_a()` iteration | Filtrar al iterar (ver punto 2.5). |

---

## 3. Matriz de Cambios y Pruebas

### 3.1. Cambio 1: Agregar `deleted_at` a `WorldRow`
- **Archivo:** `src/worldsim/server/db.py`
- **Cambio:** `deleted_at = Column(DateTime, nullable=True)` en `class WorldRow`
- **Riesgo bajo:** Columna nueva, nullable, sin valor por defecto en filas existentes (queda NULL).
- **Prueba:** `test_world_deletion` ya existente pasa con runner mock; verificar que `ensure_schema()` añade la columna a DB existente.

### 3.2. Cambio 2: `_delete_world_row` usa UPDATE en lugar de DELETE
- **Archivo:** `src/worldsim/server/runner.py:1016-1025`
- **Cambio:**
  ```python
  # Antes:
  with session_factory() as session:
      row = session.get(WorldRow, world_id)
      if row is None:
          raise WorldNotFoundError(world_id)
      session.delete(row)
      session.commit()
  
  # Después:
  with session_factory() as session:
      row = session.get(WorldRow, world_id)
      if row is None:
          raise WorldNotFoundError(world_id)
      row.deleted_at = datetime.utcnow()  # o NOW_UTC equivalente
      session.commit()
  ```
- **Riesgo medio:** Cambia el comportamiento de persistence; las FK con `ON DELETE RESTRICT` ya no se activan (bueno, pero el world row no se borra, así que no hay problema). Los runners ya fueron `unregister()`eados antes, así que no hay runner activo sobre una row "eliminada".
- **Prueba:** `test_del_01_delete_without_runner`, `test_del_02_delete_with_active_runner`, `test_del_03_close_happens_before_persisted_delete` — estos usan `_FakeRow` y `h.store` simulación; deben adaptarse o crear pruebas nuevas con `deleted_at`.

### 3.3. Cambio 3: `_stage_a()` filtra `deleted_at IS NOT NULL`
- **Archivo:** `src/worldsim/server/recovery_coordinator.py:461-465`
- **Cambio:** En `_list_world_rows()`, modificar la query para `WHERE deleted_at IS NULL`.
- **Riesgo bajo:** Ya se documentó en FIX3/FIX8; previene recuperación de runners para worlds eliminados.
- **Prueba:** Verificar que worlds con `deleted_at IS NOT NULL` no son recuperados en startup.

### 3.4. Cambio 4: Filtros en queries de app.py
- **Archivo:** `src/worldsim/server/app.py`
- **Cambios múltiples:** Añadir validación `deleted_at IS NOT NULL` → 404 en endpoints de lectura.
- **Riesgo bajo:** Agrega gates de seguridad consistentes con el contrato.
- **Prueba:** Tests de visibilidad (`test_visibility.py`) y deletion (`test_world_deletion.py`) con world que tenga `deleted_at` poblado.

### 3.5. Cambio 5: `ensure_schema` actualizado (si es necesario)
- **Archivo:** `src/worldsim/server/db.py:226-252`
- **Cambio:** La lógica existente de `inspect` + `ALTER TABLE ADD COLUMN` ya funciona para cualquier columna nueva; solo asegurarse de que `WorldRow.deleted_at` sea reconocido. No requiere cambio si se usa `Base.metadata.create_all` + `ensure_schema` flow existente.
- **Riesgo bajo:** Es el pattern ya probado para columnas `visibility`, `enabled_races_json`, etc.
- **Prueba:** `ensure_schema()` sobre BD existente añade `deleted_at` columna.

---

## 4. Riesgos Técnicos Detectados

| Riesgo | Gravedad | Mitigación |
|---|---|---|
| **R1:** World con `deleted_at NULL` pero runner ya `unregister()`eado crea inconsistencia temporal. | Media | El flujo `safe_delete_world` ya garantiza: `close → verify CLOSED → unregister → UPDATE`. El UPDATE es el último paso; entre `unregister` y `UPDATE`, world está "en eliminación" (`_deleting` set) pero `deleted_at` aún NULL. Esto es intencional y Documentado en FIX8. |
| **R2:** Reintento de `safe_delete_world` después de UPDATE fallido deja `deleted_at=NULL` y runner removido. | Baja | El reintento ejecuta el flujo completo nuevamente; `begin_delete` añade world a `_deleting`; `unregister` es idempotente; `register_existing` levanta `RuntimeError` si se intenta registrar mientras `_deleting`. El reintento es seguro. |
| **R3:** `_stage_a()` recupera runners de worlds eliminados si no filtra `deleted_at`. | Alta | **Esta es la razón principal del cambio.** Añadir filtro `WHERE deleted_at IS NULL` en `_list_world_rows()`. Documentado en FIX3/FIX8. |
| **R4:** Callsites externos (`get_existing()`) pueden acceder runner mientras world está `_deleting`. | Media | `get_existing()` no chequea `_deleting`; `get()` sí. El contrato (FIX9) establece explícitamente esta distinción. No se corrige en esta implementación para evitar cambios amplios no evaluados. |
| **R5:** Migración en BDs existentes: `ensure_schema` debe añadir `deleted_at` a tabla `worlds` ya existente. | Media | El pattern `inspect` + `ALTER TABLE ADD COLUMN` ya está probado para columnas `visibility`, etc. Probar con SQLite existente. |
| **R6:** `ON DELETE RESTRICT` en FK a `worlds.id`: No debería dispararse puesto que el row ya no se `DELETE` físicamente, solo se `UPDATE` lógicamente. | Baja | Las constraints FK siguen vigentes; si alguna tabla tiene `ON DELETE RESTRICT` a `worlds`, el `UPDATE deleted_at` no lo afecta (no borra la row). Los runners `unregister()` antes de UPDATE, así que no hay referencia activa. |

---

## 5. Secuencia Concreta de Implementación

### Fase 1: Modelo y Migración (Bloque 1)
1. **Modificar `src/worldsim/server/db.py`**:
   - Agregar `deleted_at = Column(DateTime, nullable=True)` a `class WorldRow` (después de `sea_level`).
   - Verificar que `ensure_schema()` ya incluye lógica `inspect` + `ALTER TABLE ADD COLUMN` (ya probado con otras columnas). Si es necesario, añadir `deleted_at` explícito en la iteración de migración.
2. **Ejecutar `ensure_schema()`** sobre BD de desarrollo para confirmar que la columna se añade.
3. **Ejecutar pruebas de migración** (`tests/test_schema_migrations.py` si existe, o `test_*` de visibilidad).

### Fase 2: Eliminación Lógica y Coordinación (Bloque 2)
1. **Modificar `src/worldsim/server/runner.py:_delete_world_row()`**:
   - Cambiar `session.delete(row)` + `session.commit()` por `row.deleted_at = datetime.utcnow()` + `session.commit()`.
   - **No tocar** `safe_delete_world()` flow; el orden `close → verify → unregister → UPDATE` se mantiene.
2. **Verificar que `safe_delete_world()` tests aún pasan** con el nuevo `_delete_world_row` (usando `_FakeRow` o mock). Los tests usan `h.store` simulación; pueden necesitar pequeña actualización para chequear `deleted_at` en lugar de `h.store` pop.

### Fase 3: Exclusión en Recuperación y Consultas (Bloque 3)
1. **Modificar `src/worldsim/server/recovery_coordinator.py:_list_world_rows()`**:
   - Cambiar `select(WorldRow).order_by(WorldRow.id)` por `select(WorldRow).where(WorldRow.deleted_at.is_(None)).order_by(WorldRow.id)`.
2. **Actualizar `ensure_schema()`** (si es necesario) para incluir `deleted_at` en la iteración de migración.
3. **Revisar endpoints en `app.py`** que hacen `db_.get(WorldRow, world_id)`:
   - `_load_row()` (línea 553-558): Ya lanza 404 si row es None. No requiere cambio adicional siempre y cuando la aplicación no cree rows con `deleted_at` poblado manualmente.
   - `app.py:710` (visibility change): El `db_.get(WorldRow, world_id)` + 404 Already cubre el caso. Si row existe pero `deleted_at IS NOT NULL`, el comportamiento actual de "mundo no encontrado" (404) es correcto según el contrato.
   - `app.py:1294` (`_canonical_month`): Agregar validación temprana: `if row.deleted_at is not None: return 0` o lanzar error personalizado. Por seguridad, retornar 0 (mes 0 = "nunca simuló") y dejar que el caller decida.

### Fase 4: Pruebas (Bloque 4)
1. **Ejecutar tests de eliminación** (`tests/test_world_deletion.py`): Verificar que el flujo `close → verify → unregister → UPDATE lógico` funciona correctamente.
2. **Ejecutar tests de recuperación** (`tests/test_recovery_coordinator.py`): Verificar que `_stage_a()` no recupera worlds con `deleted_at IS NOT NULL`.
3. **Ejecutar tests de visibilidad** (`tests/test_visibility.py`): Verificar que worlds eliminados no aparecen en listados públicos.
4. **Tests de concurrencia** (`tests/test_runner_foundation.py`, `tests/test_cancellation.py`): Verificar comportamiento con `asyncio.CancelledError`, `shield`, `guard_transferred`.
5. **Tests de migración** (`tests/test_schema_migrations.py` o similar): Confirmar que `ensure_schema()` añade `deleted_at` a BD existentes.

### Fase 5: Conciliación y Entregable (Bloque 5)
1. **Generar reporte de implementación** confirmando:
   - Modelo ORM actualizado.
   - Migración de esquema exitosa.
   - Flujo de eliminación lógica operativo.
   - Filtros en recuperación y consultas.
2. **Actualizar documento `F7-E.2-PRE-DECISION-FIX9.md`** con el dictamen de implementación (opcional, depende si se continúa la documentación posterior).
3. **Confirmar que no se reabrieron decisiones anteriores** (DEC19, F7-D-I1) y que todos los cambios están alineados con el contrato F7-E.2-PRE aprobado.

---

## 6. Confirmación de Inspección Sin Modificaciones

**Confirmación explícita:** Durante la fase de inspección y planificación presentada en este documento, **ningún archivo `.py`, modelo ORM, migración, prueba ni documentación contractual previa fue modificado.**

Los únicos archivos nuevos generados fueron los documentos de decisión FIX1-FIX9 (`F7-E.2-PRE-DECISION-FIX*.md`), todos ellos de naturaleza exclusivamente documental. El estado actual de los archivos `.py` permanece idéntico al verificado en `git status` al inicio de esta tarea.

**Firma de inspección:** Todos los métodos inspeccionados, los callsites de `get_existing()`, los escenarios de cancelación y el contrato operativo definitivo fueron documentados con evidencia de código (`runner.py:835-1112`, `db.py:20-38`, `recovery_coordinator.py:401-465`, `app.py` callsites 7 totales).

---

**Fin del plan de implementación F7-E.2-I.** 

Próximo paso: Revisión y aprobación del presente plan antes de ejecutar el Bloque 1 (modelo ORM y migración).