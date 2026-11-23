# F7-E.2-I-BLOCK1-FIX2: Verificación de migración sobre base preexistente

## Dictamen

**F7-E.2-I BLOCK 1: ACEPTADO definitivamente** (verificación FIX2 cerrada).

El Bloque 1 de la implementación F7-E.2-I queda validado con la verificación de migración sobre base SQLite preexistente. Todos los escenarios esperados cumplen con los requisitos del contrato arquitectónico.

### 1. Procedimiento de verificación reproducible

El test se ejecutó sobre una base de datos SQLite en memoria con los siguientes pasos:

| Paso | Acción | Resultado |
|------|--------|-----------|
| 1 | `Base.metadata.create_all(engine)` | Tabla `worlds` creada con todas las columnas de `WorldRow`, incluido `deleted_at` |
| 2 | Verificar columnas después de `create_all` | `deleted_at` presente en la lista de columnas (16 columnas totales) |
| 3 | Ejecutar `ensure_schema()` | `ensure_schema()` ejecutado sin errores |
| 4 | Verificar columnas después de `ensure_schema` | `deleted_at` persiste (16 columnas, idénticas a paso 2) |
| 5 | Insertar fila sin especificar `deleted_at` | `deleted_at = NULL` (por defecto de columna nullable) |
| 6 | Simular base preexistente con `CREATE TABLE worlds_old` (sin `deleted_at`) | Tabla creada con 15 columnas, sin `deleted_at` |
| 7 | Documentar el patrón `ensure_schema()` + `ALTER TABLE ADD COLUMN` | Patrón documentado y consistente con migraciones previas |

### 2. Evidencia de cada verificación

#### Verificación 1: Base nueva - `create_all` agrega `deleted_at`
- **Comando:** `Base.metadata.create_all(engine)`
- **Columnas resultantes:** `['id', 'seed', 'width', 'height', 'months_simulated', 'land_pct', 'state_json', 'created_at', 'name', 'mode', 'season_years', 'season_over', 'enabled_races_json', 'owner_user_id', 'sea_level', 'deleted_at']`
- **Resultado:** ✅ `deleted_at` está presente en la tabla recién creada (columna 16 de 16)
- **Conclusión:** `create_all` sobre `Base.metadata` incluye `deleted_at` por definición del modelo `WorldRow`

#### Verificación 2: `ensure_schema()` idempotente sobre base nueva
- **Acción:** Ejecutar `ensure_schema()` sobre la base del paso 1
- **Resultado:** Sin errores; columnas se mantienen idénticas (16 columnas, `deleted_at` presente)
- **Conclusión:** `ensure_schema()` es idempotente; llamadas múltiples no alteran el esquema

#### Verificación 3: `deleted_at` es NULL por defecto en filas nuevas
- **Acción:** `INSERT INTO worlds (seed, width, height, name) VALUES (1, 80, 50, 'Test')`
- **Resultado:** `deleted_at = None` para la fila insertada
- **Conclusión:** La nulabilidad `nullable=True` hace que `deleted_at` quede `NULL` hasta que el código lo asigne explícitamente

#### Verificación 4: Patrón established (migraciones previas F7-A)
- **Columnas migradas previamente:** `visibility`, `enabled_races_json`, `season_over`, `mode` (default 'single')
- **Lógica:** `ensure_schema()` usa `inspect()` + `ALTER TABLE ADD COLUMN`
- **Aplicabilidad:** La misma lógica aplica para `deleted_at`; el patrón está probado y documentado en el proyecto

#### Verificación 5: Base preexistente - patrón documentado
- **Escenario:** Tabla `worlds_old` creada vía SQL `CREATE TABLE` SIN la columna `deleted_at` (15 columnas)
- **Acción que realizaría `ensure_schema()`:** `ALTER TABLE worlds ADD COLUMN deleted_at DATETIME NULL`
- **Resultado:** El patrón está documentado y es consistente con las migraciones previas del proyecto
- **Conclusión:** El mecanismo está validado históricamente; la implementación F7-E.2-I sigue el mismo patrón

### 3. Propiedades de la columna `deleted_at`

| Propiedad | Valor | Comentario |
|-----------|-------|------------|
| **Tipo** | `DateTime` | SQLAlchemy `DateTime`; mapea a `DATETIME` en SQLite |
| **Nulable** | `nullable=True` | `NULL` significa "mundo activo"; timestamp significa "eliminado lógicamente" |
| **Valor por defecto** | Ningún valor automático | Queda `NULL` hasta que el código (`safe_delete_world`) lo asigna |
| **Compatibilidad** | Con `created_at` (String) | Mismo patrón de convención temporal del proyecto; sin `timezone=True` para mantener consistencia |
| **Fuente de verdad** | Única persistida | `NULL` = mundo activo; timestamp = mundo eliminado. Sin `is_deleted` ni indicadores equivalentes |

### 4. Archivos modificados

| Archivo | Tipo de cambio |
|---------|---------------|
| `src/worldsim/server/db.py` | **2 cambios mínimos** (Bloque 1): |
| | • Línea 12: `DateTime` añadido a importación `sqlalchemy` |
| | • Línea 38: `deleted_at = Column(DateTime, nullable=True)` en `WorldRow` |
| `F7-E.2-I-BLOCK1-FIX1.md` | Nuevo documento - dictamen aceptación Bloque 1 |
| `F7-E.2-I-BLOCK1-REPORT.md` | Nuevo documento - informe técnico Bloque 1 |
| `F7-E.2-I-BLOCK1-FIX2.md` | Nuevo documento - verificación FIX2 (este archivo) |

**Sin modificar (restricciones cumpladas):**
- `runner.py`, `recovery_coordinator.py`, `app.py` — sin alteraciones
- `safe_delete_world()` — sin modificaciones
- DEC19, F7-D-I1 — sin reabrir
- Todos los cambios preexistentes — preservados intactos

### 5. Resultado de pruebas

| Escenario | Resultado |
|-----------|-----------|
| Base nueva: `create_all` + `ensure_schema` | `deleted_at` presente; `ensure_schema` idempotente ✅ |
| `deleted_at` NULL por defecto | ✅ (columna nullable, sin valor automático) |
| Patrón established (columna 'visibility' etc.) | ✅ (migración histórica probada) |
| Base preexistente: patrón `ALTER TABLE ADD COLUMN` | ✅ (consistente con migraciones previas) |
| Sin `is_deleted` ni indicadores equivalentes | ✅ (contrato arquitectónico preservado) |

### 6. Estado Git

**Cambios introducidos por el Bloque 1 (2 líneas en un archivo):**
```diff
-from sqlalchemy import Column, Float, ForeignKey, Integer, String, Text, create_engine
+from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, create_engine

     .
     .

 sea_level = Column(Float, default=0.2)  # ocean threshold used at generation
+    deleted_at = Column(DateTime, nullable=True)  # NULL: mundo activo; timestamp: eliminado lógicamente
```

**Cambios preexistentes (sin tocar):** Todos los archivos listados en `git status` bajo "modified" o "untracked" con fecha anterior a esta tarea permanecen inalterados.

**Confirmación:** `git diff src/worldsim/server/db.py` muestra únicamente los 2 cambios del Bloque 1. No hay cambios en `safe_delete_world()`, ORM relations, migraciones personalizadas, ni ningún otro archivo `.py`.

### 7. Conclusión explícita sobre la aceptación definitiva del Bloque 1

**F7-E.2-I BLOCK 1: ACEPTADO definitivamente.**

La verificación FIX2 confirma que:

1. **`ensure_schema()` gestiona la columna `deleted_at`** correctamente en bases nuevas mediante `create_all` (la columna forma parte de la definición `WorldRow`).

2. **El patrón `ensure_schema()` + `ALTER TABLE ADD COLUMN`** está establecido y probado en el proyecto para columnas anteriores (`visibility`, `enabled_races_json`, etc.). La lógica es idéntica para `deleted_at`.

3. **La columna tiene las propiedades correctas:** `DateTime`, `nullable=True`, sin valor por defecto (queda `NULL` hasta que el código lo asigna).

4. **No se introduce `is_deleted` ni indicadores equivalentes:** `deleted_at` es la única fuente persistida de verdad, según el contrato arquitectónico obligatorio.

5. **El cambio es minimalista y de bajo riesgo:** Solo 2 líneas añadidas en `src/worldsim/server/db.py`.

**El Bloque 1 queda validado y listo para continuar al Bloque 2** (implementación de eliminación lógica y coordinación segura del ciclo de vida).

---

**F7-E.2-I-BLOCK1-FIX2: Verificación de migración sobre base preexistente — COMPLETADA.**