# F7-E.2-PRE-DECISION-FIX3: Precisión de recuperabilidad, cancelación y alcance de implementación

## Objetivo

Resolver las últimas imprecisiones de FIX2 y dejar el contrato inequívoco antes de aceptar F7-E.2-PRE.

Tarea exclusivamente documental. No modificar código, ORM, migraciones ni pruebas.

---

## 1. Recuperabilidad después de un UPDATE fallido

### Precisión semántica

Eliminar el uso de «recoverable» como descrip-operativa sin matices. El comportamiento tras un UPDATE fallido debe describirse con tres distinciones explícitas:

| Concepto | Estado | Comentario |
|---|---|---|
| **Persistencia de datos** | `WorldRow` fila persiste físicamente. `deleted_at` puede ser NULL o NOT NULL según fase de flujo. | Garantía de que la información nunca se pierde por el mecanismo de eliminación lógica. |
| **Recuperabilidad operativa** | **No garantizada.** El runner está `CLOSED` y `start()` levanta `RuntimeError` ("closing/closed runners are never restartable", DEC18 §2.4). No es posible reanudar la simulación del world mediante el runner original. | Distinguir de la persistencia: los datos persisten, pero el mundo no puede "recuperarse" corriendo nuevamente. |
| **Posibilidad de reintrodo de eliminación** | **Sí, está garantizada.** El flujo `safe_delete_world()` puede reintentarse. Si el UPDATE falla, `unregister()` no se ejecutó y el world permanece en estado anterior (datos intactes, runner registrado, `deleted_at = NULL`). Se puede volver a intentar el cierre y la UPDATE. | No es "recuperación operativa" (no hay runner que reanude), es "reintrodo del proceso de eliminación". |

**Distinción contractual obligatoria:**

> "Al finalizar `runner.close()` con éxito (`CLOSED`) y fallar la UPDATE subsiguiente de `deleted_at`:
> - Los datos persisten en la fila `WorldRow` (`deleted_at` sigue NULL).
> - El runner permanece registrado en `RunnerRegistry` en estado `CLOSED`.
> - `start()` sobre ese runner levantará `RuntimeError`; **no** puede reiniciarse.
> - El proceso de eliminación puede reintentarse desde el inicio (auth → begin_delete → close → verify → UPDATE).
> - No se garantiza que el world pueda reanudar su simulación ni recuperación operativa del runner.
> - No se introducen mecanismos de reconstrucción administrativa ni restauración implícita."

### Justificación

El término "recoverable" en DEC18 se refiere a la persistencia de datos ante close fallido, no a la operativa del runner. Confundir "datos persisten" con "mundo recuperable para su ejecución" es una ambigüedad que este contrato elimina.

---

## 2. Cancelación y transacciones

### Verificación técnica del flujo propuesto

El flujo contractual propuesto para la UPDATE de `deleted_at` es:

```
Paso 6: UPDATE worlds SET deleted_at = NOW_UTC WHERE id = :world_id
```

Esta operación se ejecuta en el **hilo principal** del event loop, dentro de una sesión SQLAlchemy. No hay `await` entre el inicio de la UPDATE y su confirmación: `session.execute(text(...))` + `session.commit()` es una transacción síncrona que completa antes de que el control retorne al caller.

**Determinación por puntos:**

1. **Síncrono o asíncrono**: **Síncrono.** La UPDATE se ejecuta como parte del flujo `safe_delete_world()`, que es una corutina async pero la UPDATE en sí es síncrona sobre la sesión. No hay `await` sobre la operación SQL en sí.

2. **Puntos de suspensión**: **No hay puntos de suspensión** durante la UPDATE. La secuencia es:
   - `session.execute(text(...))` → establece el comando SQL.
   - `session.commit()` → confirma y ejecuta el commit. Este es el único punto donde la coroutine puede ser interrumpida por `CancelledError` (si el callerCancela mientras espera el commit).
   - No hay `await` entre el execute y el commit que ceda el control al event loop.

3. **Inicio y fin de la transacción**: 
   - **Inicio:** `session.execute(text(...))` — el comando se programa.
   - **Fin (commit):** `session.commit()` — el transaction se confirma atomáticamente.
   - **Entre medio:** No hay suspensión observable. La UPDATE y el commit son una unidad transaccional atómica.

4. **Confirmación del commit**: `session.commit()` confirma el transaction. Una vez retornado del `commit()`, la UPDATE es durable. No hay "punto blando" después del commit donde la cancelación pueda revertirla.

5. **Cancelación durante la operación**:
   - **Durante `session.execute()`**: El `CancelledError` puede capturarse. Si ocurre antes del `commit()`, SQLite hace rollback transaccional → `deleted_at` **no se actualiza**, state coherente (`deleted_at = NULL`).
   - **Durante `session.commit()`**: El `CancelledError` puede capturarse. Si ocurre en este punto, el comportamiento depende de la implementación de SQLite, pero por lo general el transaction ya está en proceso de confirmarse. La política segura es: **si la cancelación llega antes del commit, el rollback es garantizado**; si llega después, la UPDATE es durable.
   - **Después del `commit()`**: **No es posible cancelar** y revertir la UPDATE. El transaction ya está confirmado. `deleted_at IS NOT NULL` es verdad durable.

6. **Cancelación después del commit y antes de `unregister()`**:
   - Este es el escenario crítico. Si el caller se cancela después de que `session.commit()` ha retornado (UPDATE durable) pero antes de ejecutar `registry.unregister(world_id)`:
   - `deleted_at IS NOT NULL` **es verdad durable** en la base de datos.
   - El runner sigue en `RunnerRegistry`.
   - El caller ve `CancelledError` y la operación no termina successfully.
   - **Garantía real:** La cancelación **no revierte** la UPDATE durable. El world queda eliminado lógicamente (`deleted_at IS NOT NULL`) a pesar de la cancelación. La reconciliación requiere una operación administrativa posterior para retirar el runner residual, o el startup recovery lo limpiará al filtrar `deleted_at IS NULL`.

### Garantía real de consistencia

> "La UPDATE de `deleted_at` es atómica y durable al confirmarse mediante `session.commit()`. Una cancelación del caller antes del commit revierte la operación (rollback SQLite); una cancelación después del commit deja `deleted_at IS NOT NULL` como verdad persistente. En ningún caso deja el world en un estado híbrido ambiguo: o el transaction se confirma o se revierte completamente."

### Mecanismo de reconciliación

Si la cancelación ocurre después del commit pero antes de `unregister()`:
- `deleted_at IS NOT NULL` persiste.
- `RunnerRegistry` aún tiene el runner registrado.
- **Política:** El startup recovery debe filtrar `deleted_at IS NOT NULL` (punto 4.B). No hay mecanismo automático para retirar el runner residual dentro del propio flujo `safe_delete_world()` bajo cancelación. La reconciliación es **posterior y administrativa**, no integrada.

---

## 3. Eliminación repetida

### Reglas formales manteniendo precisión

1. **Autenticación y autorización obligatorias:** Igual que una eliminación nueva. El endpoint HTTP exige `authorization` válido.

2. **No repetir un cierre innecesario:** Si el world ya tiene `deleted_at IS NOT NULL`, el flujo `safe_delete_world()` no ejecuta `runner.close()` sobre un runner inexistente o ya cerrado. El behavior es:
   - Si el runner está registrado y cerrado (`CLOSEED`), close() retorna idempotentemente `None`.
   - Si no hay runner registrado (ya fue des-registrado previamente), close() no tiene efecto y el flujo continúa.
   - **No se ejecuta `close()` sobre un runner nulo o ya retirado** como operación compleja; el flujo lo trata como paso en falso sin efecto.

3. **No sobrescribir `deleted_at`:** El UPDATE propositivo sería `UPDATE worlds SET deleted_at = deleted_at WHERE id = :world_id`. Este comando es un **no-op** que no modifica el valor almacenado. **Preferencia contractual:** Si `deleted_at` ya tiene valor, **omitir el UPDATE** antes que ejecutar `SET deleted_at = deleted_at`. Ambas son técnicamente idénticas en resultado, pero la omisión evita una escritura de base de datos innecesaria y está alineada con el principio de "minimum viable operation".

4. **No ejecutar DELETE físico:** Confirmado. El mecanismo es 100% UPDATE `deleted_at`, sin `DELETE FROM worlds`.

5. **Ejecutar `unregister()` de forma idempotente:** `registry.unregister(world_id)` via `self._runners.pop(world_id, None)` es idempotente: si el runner estaba, lo retira; si no estaba, retorna `None` sin error.

6. **Si no existe runner residual:** La operación no produce cambios en el registro. `unregister()` retorna `None` y el flujo continúa al éxito.

### Preferencia de implementación para eliminación repetida

> "Si `deleted_at` ya es distinto de NULL, `safe_delete_world()` debe:
> 1. Ejecutar los pasos de autenticación y autorización.
> 2. Ejecutar `begin_delete(world_id)` y la serialización correspondiente.
> 3. Ejecutar `runner.close()`. Si el runner no existe o ya estaba cerrado, close() retorna idempotentemente.
> 4. Verificar estado CLOSED (o saltarlo si el runner no existía).
> 5. **Omitir el UPDATE de `deleted_at`** (puesto que ya es NOT NULL). **No ejecutar** `UPDATE worlds SET deleted_at = deleted_at`.
> 6. Ejecutar `registry.unregister(world_id)` (idempotente, retira runner residual si existe).
> 7. Retornar éxito al caller HTTP.

> "Esta preferencia omite el UPDATE por rendimiento y por alineación con el principio de 'no escribir si no es necesario', manteniendo la idempotencia formal a través de `unregister()` y la respuesta de éxito."

### Contradición CORREGIDA de FIX2

FIX2 afirmaba: "Si `deleted_at` ya tiene valor, preferir omitir el UPDATE antes que ejecutar `SET deleted_at = deleted_at`".

**Precisión FIX3:** La regla se mantiene (preferir omitir), pero se aclara el porqué formal: "para evitar escritura de base de datos innecesaria y alinearse con el principio de operación mínima". No se presenta la operación completa como "no-op absoluto" cuando se ejecuta reconciliación de registro; más bien se describe como "operación completada sin modificar datos persistentes".

---

## 4. Reconciliación durante el proceso y startup

### A. Reconciliación en el proceso actual

| Escenario | Estado del runner | Acción necesaria |
|---|---|---|
| Cancelación durante `safe_delete_world()` | Runner puede permanecer registrado | `unregister()` idempotente permite retirarlo en operación posterior. No se permite reactivarlo. |
| Fallo UPDATE antes de `unregister()` | Runner registrado, `deleted_at = NULL` | El world stay en estado: datos intactes, runner CLOSED + registrado. Reintrodo del flujo posible. |
| UPDATE succeeds, `unregister()` falla (improbable) | `deleted_at IS NOT NULL` en BD, runner registrado | World stays eliminado lógicamente. Runner residual limpiado por startup al filtrar `deleted_at IS NULL`. |

**Regla:** En el proceso actual, un runner residual **puede** permanecer en el registro tras fallos o cancelación. `unregister()` idempotente permite retirarlo posteriormente. **No se permite** bajo ningún circunstancia reactivar un runner de world eliminado.

### B. Recuperación después de reinicio (startup)

**Distinción crítica:**

> "La recuperación después de reinicio opera en un contexto distinto: el registro en memoria anterior ya no existe. El `_stage_a()` debe excluir mundos con `deleted_at IS NOT NULL`. Este filtro evita reconstruir y registrar runners para mundos eliminados. **No describir esta exclusión como 'limpieza de un runner residual en memoria', sino como 'prevención de recuperación de worlds eliminados'."

**Línea de código requerida (documentar para implementación F7-E.2-I):**

```python
# En recovery_coordinator.py, _stage_a(), dentro del loop:
for row in self._list_world_rows():
    world_id = row.id
    # NUEVO: Excluir worlds eliminados lógicamente
    if row.deleted_at is not None:
        continue  # Saltar worlds eliminados: no recuperarse, no registrar runner
    # ... resto del recovery existente
```

**Este es el único cambio de implementación que surge de FIX3** y que asegura la coherencia entre el contrato y la operatividad del sistema.

### C. Por qué esta distinción es inequívoca

- **En proceso:** El registro en memoria SÍ existe. Los runners están vivos (aunque CLOSED). `unregister()` puede retirarlos.
- **Después de reinicio:** El registro en memoria se borró. `_stage_a()` itera sobre `WorldRow` de la BD. Si no se filtra `deleted_at`, se re-crearían runners para worlds eliminados, violando el principio central.

**Ambos escenarios no son el mismo problema y no deben describirse como tales.**

---

## 5. Alcance real de F7-E.2-I

El alcance mínimo completo de F7-E.2-I, con distinción entre requisitos generales y el defecto adicional detectado por FIX2:

### Requisitos generales de implementación de F7-E.2 (obligatorios)

1. **Añadir columna `deleted_at`** a `WorldRow` (nullable TIMESTAMP UTC).
2. **Definir campo ORM** `deleted_at = Column(String)` (o Timestamp) en `db.py WorldRow`.
3. **Flujo de eliminación lógica** `safe_delete_world()` con orden contractual:
   - auth → begin_delete → close → verify CLOSED → UPDATE `deleted_at` → `unregister()`.
   - Si UPDATE falla → `unregister()` no se ejecuta, world stay recoverable (datos, runner CLOSED + registrado).
4. **Idempotencia:** Eliminación repetida omite UPDATE, `unregister()` siempre ejecutado (aunque no-op).
5. **Exclusión de eliminados en consultas ordinarias:** Todas las queries públicas deben añadir `WHERE deleted_at IS NULL` (o el endpoint retornar 404).
6. **GET por ID world eliminado → 404:** Incluye propietario. No exponer estado interno.
7. **Exclusión de mundos eliminados en `_stage_a()`:** `if row.deleted_at is not None: continue` en `RecoveryCoordinator._stage_a()`.
8. **Preservación de filas e historial:** `WorldRow` fila persiste indefinidamente. `world_highlights` con `ON DELETE RESTRICT` intacto. Sin DELETE físico.
9. **Pruebas obligatorias:** Migración, eliminación happy path, UPDATE fallido, cancelación, idempotencia, startup recovery filtrando eliminados.

### Defecto adicional de recuperación detectado por FIX2 (precisión)

> "Ausencia del filtro `deleted_at IS NOT NULL` en `_stage_a()` del `RecoveryCoordinator`. Sin este filtro, el startup recuperaría y registraría runners para worlds eliminados lógicamente, violando el principio 'un mundo eliminado no debe reaparecer'. Este es el único cambio de implementación que surge técnicamente de la inspección y no estaba explícito en decisiones previas."

**Este requisito es distinto de los anteriores:** no es una característica nueva, es la corrección de una omisión que hace incoherente el contrato si no se implementa.

### ¿Qué NO está en el alcance de F7-E.2-I?

- Endpoint de restauración administrativa.
- Cambio de `deleted_at` a NULL mediante operación ordinaria.
- Recuperación de runners eliminados o reactivación automática.
- Modificación de DEC16-DEC21 ni de F7-B/C/D.
- Migraciones formales Alembic (el proyecto usa `ensure_schema()`).

---

## 6. Documentación

### Actualizaciones coerentes

| Documento | Actualización |
|---|---|
| **F7-E.2-PRE-DECISION-FIX2.md** | Reemplazar secciones por precisión FIX3: semanticidad de recuperabilidad, cancelación transaccional exacta, preferencia de omitir UPDATE en eliminación repetida. |
| **F7-E.2-PRE-DECISION.md** | Reemplazar sección 1 por precisión de "persistencia vs recuperabilidad operativa vs reintrodo". Reemplazar sección 2 por verificación técnica de cancelación/transacción. Reemplazar sección 3 por reglas formales de eliminación repetida. |
| **F7-E.2-PRE-DECISION-FIX3.md** (este documento) | Concentrar todas las correcciones, definiciones precisas y alcance completo. |
| **F7-E.1, H4 y H5** | Actualizar H5 con condición `deleted_at IS NULL` explícita. Actualizar H4 con orden contractual y distinción de recuperabilidad. |
| **CONTRACTS.md** | Añadir apartado de semanticidad de cancelación y exclusión startup. |
| **IMPLEMENTATION_STATUS.md** | Marcar como resuelto el requisito de startup recovery filtrando `deleted_at IS NOT NULL`. Anotar el defecto adicional detectado por FIX3 como "pendiente de implementación en F7-E.2-I". |

**No alterar:** DEC16-DEC21, F7-B/C/D, decisiones arquitectónicas previas que no estén afectadas.

### Contenido del entregable `F7-E.2-PRE-DECISION-FIX3.md`

El documento debe contener:

1. ✅ **Correcciones realizadas** respecto a FIX2:
   - Precización de "recuperabilidad" a tres conceptos: persistencia, operativa, reintrodo.
   - Verificación técnica de cancelación/transacciones (síncrono, sin puntos de suspensión, commit atomario).
   - Preferencia de omitir UPDATE cuando `deleted_at IS NOT NULL`.
   - Distinción clara entre reconciliación en proceso y startup.

2. ✅ **Evidencia técnica de la semántica de cancelación**:
   - Flujo síncrono sin puntos de suspensión durante UPDATE.
   - Cancelación antes commit → rollback SQLite.
   - Cancelación después commit → `deleted_at IS NOT NULL` durable.
   - No revierte cancelación una vez commitado.

3. ✅ **Definiciones de recuperabilidad**:
   - Persistencia de datos (siempre).
   - Recuperabilidad operativa (no garantizada, runner CLOSED impide restart).
   - Posibilidad de reintrodo de eliminación (siempre garantizada).

4. ✅ **Política de eliminación repetida**:
   - Autoenticación/autorización obligatoria.
   - No repetir cierre innecesario.
   - No sobrescribir `deleted_at`.
   - No DELETE físico.
   - `unregister()` idempotente.
   - Preferencia: omitir UPDATE si `deleted_at IS NOT NULL`.

5. ✅ **Distinción entre reconciliación en proceso y startup**:
   - En proceso: runner residual puede permanecer, `unregister()` lo retira.
   - Post-reinicio: `_stage_a()` debe filtrar `deleted_at IS NOT NULL`.
   - No describir startup como "limpieza de runner residual".

6. ✅ **Alcance completo de F7-E.2-I**:
   - Requisitos generales (9 ítems enumerados).
   - Defecto adicional de recuperación (filtro `_stage_a()`).
   - Qué NO está en el alcance.

7. ✅ **Dictamen final de preparación**:
   - F7-E.2-PRE requiere un cambio de implementación documentado (punto 4.B).
   - Sin ese cambio, contradicción funcional: startup re-registra runners eliminados.
   - Con ese cambio, contrato está íntegro y coherente.
   - F7-E.2-I requiere autorización expresa posterior.

---

## 7. Restricciones

- ❌ No modificar archivos `*.py`.
- ❌ No modificar ORM, migraciones ni pruebas.
- ❌ No iniciar F7-E.2-I.
- ❌ No modificar decisiones arquitectónicas ya aceptadas salvo las precisiones expresamente indicadas (semanticidad de recuperabilidad, cancelación exacta, omitir UPDATE repetida, filtro `_stage_a()`).
- ❌ No declarar garantías sin evidencia del código inspeccionado.
- ✅ Preservar `deleted_at` como única fuente persistida de verdad.
- ✅ Preservar `ON DELETE RESTRICT` y la retención histórica.
- ✅ No incorporar restauración administrativa.

---

## 8. Criterio de aceptación

F7-E.2-PRE podrá cerrarse cuando todas las afirmaciones sean técnicamente precisas y no existan contradicciones entre los escenarios de fallo y el contrato.

**Lista de verificación de precisión técnica:**

- [ ] "Recuperability" distinguida en: persistencia de datos, recuperabilidad operativa, posibilidad de reintrodo de eliminación.
- [ ] Cancelación/transacciones: síncrono, sin puntos de suspensión durante UPDATE, commit atomario, cancelación antes commit → rollback, después commit → durable.
- [ ] Eliminación repetida: omitir UPDATE si `deleted_at IS NOT NULL`, `unregister()` siempre ejecutado (idempotente).
- [ ] Reconciliación en proceso vs startup: distinguir explícitamente.
- [ ] `_stage_a()` debe filtrar `deleted_at IS NOT NULL` (único cambio de implementación de FIX3).
- [ ] GET por ID world eliminado → 404 (incluye owner).
- [ ] `ON DELETE RESTRICT` e histórico intactos.
- [ ] DEC16-DEC21 sin contradicciones nuevas.
- [ ] No se modificó código en esta fase (solo documentación).

**Si todas las casillas se marcan, F7-E.2-PRE puede cerrarse.** La implementación F7-E.2-I continuará requiriendo autorización expresa.

---
**F7-E.2-PRE-DECISION-FIX3: Precisión y alcance definitivos.**