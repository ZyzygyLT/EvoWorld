# EvoWorld — F7-C-R1-H-B: Parte 3

**Ciclo Real, Identidad Lógica, Paginación, Cobertura, Restauración, A/B/C**

---

## 1. Frontera y Extracción Incremental del Delta del Chronicle (DEC-F7C-02, DEC-F7C-04, DEC-F7C-05)

### 1.1 ¿Cómo se conserva y restaura `Chronicle.events`?

**SERIALIZACIÓN (checkpoint_to_dict, src/worldsim/export/checkpoint.py):**

```python
event_dicts = [_serialize_event(e, checkpoint_version) for e in engine.chronicle.events]  # línea 866
```

**SERIALIZACIÓN DE EVENTO (checkpoint_to_dict, líneas 779-796):**

```python
return {
    "id": e.id,               # <-- Event.id serializado
    "month": e.month,
    "type": e.type.name,
    "text": e.text,
    "kingdom_ids": list(e.kingdom_ids),
    "params": params_encoded,
    "msg_key": e.msg_key,
}
```

**DESERIALIZACIÓN (engine_from_checkpoint, líneas 1033-1039):**

```python
chronicle_state = state.get("chronicle", {})
for d in chronicle_state.get("events", []):
    e = _deserialize_event(d, checkpoint_version)   # Event con id serializado
    engine.chronicle.events.append(e)
    for kid in e.kingdom_ids:
        engine.chronicle._by_kingdom.setdefault(kid, []).append(e)
```

**CONTRATO ACTUAL (DEC-F7C-02):** "La frontera detecta eventos nuevos por su **secuencia dentro del mundo** (identificador persistente del acontecimiento), no los escanea completos en cada operación. La transferencia ocurre **después** de que el estado del mundo se confirma."

**REALIDAD:** El código de `checkpoint_to_dict` ya **serializa la lista COMPLETA** de `chronicle.events` (cumulativa). No hay **comparación delta** contra el checkpoint previo. No hay **búsqueda de eventos nuevos** más allá de “serializar todo lo acumulado”.

### 1.2 Cómo se extraería exclusivamente el delta nuevo de cada operación sin reinsertar E1-E3, etc.

**PROPUESTO (requeriría cambio):**

1. Al generar evento N en `Chronicle.log()`, se asigna `event_id` persistente: `(world_id, operation_seq, posición_en_la_operación)`.
2. `checkpoint_to_dict` serializa la lista completa, pero el **consumed** `operation_seq` y la posición de la operación permiten **filtrar** el delta.

**ACTUAL (no implementado):** Los eventos son serializados todos, pero el contrato DECLARA que se necesita la identidad de operación.

### 1.3 Comparación de identidades

| Identidad | Forma | Estado en código | Evaluación |
|---|---|---|---|
| **(world_id, checkpoint_id, posición)** | DEC-F7C-04 | **Definida contractualmente**, **NO implementada**. | Candidata recomendada (identidad lógica estable y derivable del checkpoint existente). |
| **(world_id, operation_id, posición)** | Alternativa (no formalizada) | **NO existe** (no hay tabla operations). | Mejor para operaciones idempotentes. |
| **(world_id, event_id_global)** | `Event.id` | **NO existe** — Event.id es por-proceso. | **Rechazada** — no es durable. |

### 1.4 ¿Cómo sobrevive a reinicios, reintentos y restauraciones?

- **Reinicios:** `checkpoint_to_dict` serializa `_next_event_id` (checkpoint.py:872-877, 899-903). `engine_from_checkpoint` lo restaura (checkpoint.py:1048-1058). **Pero `Event.id` no es persistente** si no se genera con el contador del checkpoint.
- **Restauraciones:** Al restaurar un checkpoint, se reensambla `chronicle.events` y se restablece `_next_event_id`. **No se reutilizan IDs confirmados** porque los eventos ya están en el payload. **Pero** no hay dedup por identidad de evento.
- **Reintentos:** No hay reintentos de operaciones. No hay batch token.

---

## 2. Paginación Demostrada (con 20 acontecimientos, limit=10)

### 2.1 Setup hipotético del requerimiento: OP1: E1-E7, OP2: E8-E13, OP3: E14-E20.

### 2.2 Orden total elegido (por contrato DEC-F7C-05): `checkpoint_id ASC, posición_dentro_del_checkpoint ASC` (orden cronológico).

### 2.3 Página 1 (limit=10, primer mundo, primer checkpoint, OP1: E1-E7 + E8-E9 de OP2, etc.)

**PENDIENTE DE IMPLEMENTAR:** No existe endpoint. No existe lógica de extracción delta. No existe cursor compuesto. No existe lógica de `10 eventos por página`.

### 2.4 Comparador de cursores: **NO IMPLEMENTADO**.

### 2.5 Resultado al insertar nuevos eventos entre ambas solicitudes: **N/A** — no hay inserción.

---

## 3. Estados de Cobertura Histórica

### 3.1 Definiciones (DECs y contrato):

- `confirmed`: existe al menos un checkpoint confirmado (`is_valid=1` + checksum OK) y **todos** los acontecimientos entre `first_checkpoint_month` y `last_checkpoint_month` están presentes y recuperables.
- `potentially_incomplete`: existe al menos un checkpoint confirmado, pero puede haber acontecimientos entre checkpoints que no se persistieron (viven solo en memoria).
- `unknown`: no existe ningún checkpoint confirmado.

### 3.2 ¿Qué evidencia durable permite afirmar que no faltan acontecimientos?

**ACTUAL:** La persistencia existe solo en el payload del checkpoint. No hay `batch_token`, no hay `is_valid` distinto del checksum (aunque existe, no se evalúa la cobertura). No hay forma de saber si entre dos checkpoints se perdieron eventos.

### 3.3 Cobertura histórica F7-C

- DEC-F7C-08: Los acontecimientos generados **antes de F7-C** no se importan; la historia comienza en la fecha de habilitación de F7-C.

---

## 4. Semántica de Restauración

### 4.1 Alternativas comparadas

| Alternativa | Descripción | Evaluación |
|---|---|---|
| 1. Rechazar restauraciones que alteren una historia publicada. | El motor no debe reescribir eventos ya confirmados. | **No implementada.** El `RecoveryManager` restaura el primer checkpoint válido, sin verificar si ese checkpoint reescribe historia confirmada. |
| 2. Crear nueva rama histórica. | `world_id` diferente. | **No implementada.** |
| 3. Crear nuevo `world_id`. | Mismo. | **No implementado.** |

### 4.2 Elegir una semántica mínima segura para F7-C.

**RECOMENDADA (del contrato):** Alternativa 1 + 3 combinados. **No reescribir historia confirmada; crear un mundo nuevo (`world_id`) para ramas alternativas.**

### 4.3 Consecuencias si se implementara:

- Los acontecimientos posteriores al checkpoint restaurado: se continúan desde el estado del checkpoint.
- Los cursores previos: deben invalidarse (ya no válidos contra el nuevo contexto).
- Las identidades de eventos: no reutilizar confirmados; nuevos eventos del mundo nuevo.

---

## 5. Comparación Arquitectónica A / B / C

### 5.1 A: Historia derivada de checkpoints.

**Evaluación:**
- ✅ **Pros:** Sin tabla extra; usa lo existente.
- ❌ **Contras:** Falla si la historia necesita eventos **entre** checkpoints (los eventos en memoria se pierden). No cumple el caso de operación por operación.
- ❌ No hay batch_token. No hay idempotencia.
- **Conclusión:** **Insuficiente** — no cumple la garantía de cobertura.

### 5.2 B: Registro incremental persistente de acontecimientos.

**Evaluación:**
- ✅ **Pros:** Permite extracción de delta, idempotencia por operación, cobertura.
- ✅ Requiere tabla `world_events` con `UNIQUE(world_id, operation_id, posición)` o `event_id_global`.
- ✅ Necesita transacciones T1/T2 (operación journal + ejecución + persistencia).
- ❌ Requiere nueva estructura de base de datos (migración) y cambios en modelo (no autorizados).
- **Conclusión:** **Recomendada por el contrato**.

### 5.3 C: Arquitectura híbrida.

**Evaluación:** Combina B con checkpoints como base de recuperación. **No recomendada** en este punto.

### 5.4 Recomendación final (F7-C)

**RECOMENDADA:** **Alternativa B** (registro incremental persistente de acontecimientos) **más** el esquema `world_events` (con claves únicas) **más** el protocolo de operaciones en dos transacciones (T1: journal de operación con `operation_id` estable; T2: confirmación de checkpoint + delta en una transacción).

---

## 6. Resumen y Criterio de Aceptación

*(Este apartado se completará en la Parte 4: Entrega, Dictamen final, Git final y STOP.)*

---

**Fin de Parte 3.**