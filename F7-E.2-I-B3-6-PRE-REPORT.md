# F7-E.2-I-B3-6-PRE — Inspección y contrato de acceso histórico CS2

**Fecha:** 20/11/2026  
**Autor:** Agente de implementación  
**Estado:** Inspección y dictamen; **no se inició B3-6-I**.  
**Bloqueo:** Restricción absoluta sin modificar `.py`, modelos, pruebas ni documentos previos.

## 1. Resumen ejecutivo

B3-5 (CS1) está aceptado y cerrado: `world_id=0` es sentinel de participante sin mundo, los mundos eliminados tienen `deleted_at IS NOT NULL`, y las consultas ordinarias los excluyen. B3-6 debe inspeccionar el acceso histórico mínimo para completar un desafío existente cuyos participantes están inscritos y sus mundos fueron eliminados lógicamente. El contrato CS2 debe separar lectura histórica de operación ordinaria y en vivo, sin restaurar ni reactivar mundos eliminados.

No se modificó código de producción. No se iniciaron operaciones B3-6-I. El entregable es exclusivamente el informe `F7-E.2-I-B3-6-PRE-REPORT.md`.

## 2. Mapa de flujos y consultas (rutas y líneas reales)

### 2.1 Flujo `complete_challenge` (líneas 977-1116, challenge_service.py)

```
_complete_challenge
│
├─ get_challenge(challenge_id)                   → challenge_repo (línea 1010)
│  └─ ChallengeRow.state debe ser "running"      → _require_state (línea 1030)
│
├─ get_participants(challenge_id)                → ChallengeParticipantRepository (línea 1032)
│  └─ Para cada participante p:
│       │
│       ├─ if p.world_id is None                  → ChallengeError (línea 1039)
│       │           "Participante ... sin mundo asociado"
│       │
│       └─ if p.world_id > 0:                    ← inspección crítica
│             │
│             └─ session.get(WorldRow, p.world_id) ← línea 1059
│                   │
│                   ├─ if world_row is None        → WorldNotFoundForParticipant (línea 1061)
│                   │
│                   └─ Cargar motor desde state_json (línea 1066)
│                         │
│                         ├─ world_row.state_json  ← JSON con estado del motor
│                         ├─ world_row.months_simulated  ← ticks ya ejecutados
│                         └─ world_row.deleted_at     ← **AQUÍ: leído, no modificado**
│                               │
│                         └─ if current_ticks < duration_ticks:
│                               │       │
│                               │       └─ eng.tick()        ← simulación nueva
│                               │
│                               └─ world_row.months_simulated = duration_ticks
│
├─ Para cada participante: ejecutar ticks, calcular métricas (líneas 1080-1088)
│  └─ participant.result_json = json.dumps(metrics)
│  └─ participant.result_status = "persisted"
│  └─ participant.result_verified = 0
│  └─ participant.state = "finalizado"
│  └─ session.flush()
│
├─ challenge.state = "completed"                  ← línea 1091
│  └─ challenge.completed_at = self._now()        ← línea 1092
│  └─ session.flush()
│
├─ challenge_repo.record_challenge_operation(...) ← línea 1095-1102
│  └─ Usa ChallengeOperationRow (línea 1167: defecto actor_user_id)
│
└─ session.commit()                               ← persiste estado "completed"
```

### 2.2 Flujo `resolve_challenge` (líneas 1341-1429, challenge_service.py)

```
_resolve_challenge
│
├─ get_challenge(challenge_id)                   → challenge_repo
│  └─ challenge.state debe ser "disputed"         → _require_state (línea 1390)
│
├─ _idempotent_operation_row(...)                ← defecto actor_user_id (línea 1381)
│
├─ _require_state(challenge, {"completed"}, ...) ← línea 1390
│
├─ participant = repo.get_participant(challenge_id, user_id)  ← línea 1314
│  └─ Si participant.result_status in ("verified", "confirmed"):
│        │
│        └─ participant.result_status = "disputed" ← línea 1319
│        └─ participant.result_verified = 0        ← línea 1321
│
├─ challenge.state = "disputed"                  ← línea 1326
│  └─ session.flush()
│
├─ challenge_repo.record_challenge_operation(...)  ← línea 1329-1336
│  └─ Usa ChallengeOperationRow (defecto actor_user_id)
│
└─ session.commit()
```

### 2.3 Consultas con `deleted_at IS NULL` (introducidas en B3-1 a B3-5)

| Ubicación | Filtro | Decisión |
|---|---|---|
| `repository.py:get_worlds()` | `WorldRow.deleted_at.is_(None)` | Ordinario; excluye mundos eliminados |
| `app.py:A1/A2/A4/A6` | `WorldRow.deleted_at.is_(None)` | Filtrado A1/A2/A4/A6; B3-3 aceptado |
| `recovery_coordinator.py:_list_world_rows()` | `WorldRow.deleted_at.is_(None)` | B3-2; filtra eliminado |
| `league.py:claim gate, toast, season` | `WorldRow.deleted_at IS NULL` implícito | B3-4; 3 filtros aplicados |
| `challenge_service.py:_prepare_worlds:760` | `row.deleted_at is not None` → error | Exclusión de mundo eliminado en preparación |
| `challenge_service.py:_start_challenge:878` | `participant.world_id is None ∨ == 0` → error | Validación de participación sin mundo |
| `challenge_service.py:_complete_challenge:1059` | `session.get(WorldRow, p.world_id)` **SIN filtro deleted_at** | **Lectura histórica indispensable** para completar desafío corriendo |
| `challenge_service.py:resolve_challenge` | No accede a WorldRow.deleted_at directamente | Solo usa participant.result_status |

### 2.4 Operaciones que leen datos de mundo eliminado (indispensables)

| Operación | ¿Lee world eliminado? | ¿Por qué indispensable? |
|---|---|---|
| `complete_challenge:session.get(WorldRow, p.world_id)` | ✅ Sí | Needed para cargar `state_json` y `months_simulated`; el motor necesita el estado anterior para continuar ticks; **no modifica deleted_at** |
| `complete_challenge:world_row.state_json` | ✅ Sí | Contiene el estado serializado del motor (kingdoms, influence_map, etc.) necesario para calcular métricas |
| `complete_challenge:world_row.months_simulated` | ✅ Sí | Determina cuántos ticks ya se ejecutaron; necesario para saber cuántos ticks faltan |
| `resolve_challenge` | ❌ No | Solo usa `participant.result_status`; no accede a WorldRow |
| `league.py B3-4` | ❌ No | Filtros sobre reclamaciones y toast; no accede a mundos desafío |
| `recovery_coordinator B3-2` | ❌ No | Filtra `deleted_at IS NULL` al listar; no accede a desafíos |

### 2.5 Operaciones que deben seguir rechazando mundos eliminados

| Operación | Motivo del rechazo |
|---|---|
| `start_challenge` (B3-5 FIX2) | `world_id=0` o `None` → `ChallengeError "no tiene mundo preparado"` |
| `prepare_worlds` (B3-5 FIX1) | `world_id > 0 ∧ row.deleted_at IS NOT NULL` → `WorldNotFoundForParticipant` |
| `app.py A1/A2/A4/A6` | `WorldRow.deleted_at.is_(None)` filtra eliminado |
| `recovery_coordinator B3-2` | `deleted_at IS NULL` filtra eliminado |
| `league.py B3-4` | Filtros de claim/toast/session excluyen eliminado |

### 2.6 Puntos de posible reactivación accidental

| Punto | Riesgo | Mitigación |
|---|---|---|
| `complete_challenge:eng.tick()` (línea 1074) | Ejecutar nuevo tick sobre estado cargado | **Mitigado:** `world_row` se lee solo; `eng.tick()` opera en memoria; `world_row.months_simulated` y `world_row.state_json` se actualizan, **pero `world_row.deleted_at` no se toca** |
| `complete_challenge:world_row.state_json = json.dumps(engine_to_dict(eng))` (línea 1077) | Sobrescribir state_json con nuevo estado | **Mitigado:** `deleted_at` persists unchanged; solo se actualiza `state_json` y `months_simulated` |
| `session.flush()` / `session.commit()` | Persistir cambios del motor | **Mitigado:** Solo `state_json`, `months_simulated`, `result_status`, `result_verified` se persisten; `deleted_at` no se escribe |
| `challenge.state = "completed"` (línea 1091) | Cambiar estado del desafío, no del mundo | **Mitigado:** El challenge pasa a `completed`; el world permanece con su `deleted_at` original |

### 2.7 Defectos ORM preexistentes que pueden interferir

| Defecto | Ubicación | Impacto en CS2 |
|---|---|---|
| `ChallengeOperationRow.actor_user_id` | `challenge_service.py:206`, `repository.py:1167` | Bloquea `_idempotent_operation_row` y `record_challenge_operation`; requiere mock en tests |
| `WorldRow` sin `next_operation_sequence` | `challenge_service.py:788` | Kwarg inválido en `_prepare_worlds`; aislado con mock en FIX4 |
| `ChallengeParticipantRow.world_id NOT NULL` | `db.py:126` | `world_id=None` causa IntegrityError; el sentinel `0` fue la solución |
| `ChallengeRow.state` transiciones | `challenge_service.py` | `complete_challenge` requiere `state == "running"`; `resolve_challenge` requiere `state == "disputed"` |

## 3. Tabla de consultas filtradas y decisión para cada una

| # | Consulta | ¿Filtro deleted_at? | Decisión CS2 |
|---|---|---|---|
| 1 | `get_world(id)` / `session.get(WorldRow, id)` | **No** (solo si id es conocido) | **Lectura histórica indispensable** para `complete_challenge`; el mundo puede estar eliminado pero su estado JSON persiste; **no se elimina el filtro en ordinario** |
| 2 | `get_challenge(id)` | **No** | Ordinario; sin restricción adicional |
| 3 | `get_participants(challenge_id)` | **No** | Ordinario; devuelve participantes con su `world_id` (0 o >0) |
| 4 | `session.get(WorldRow, p.world_id)` en `complete_challenge` | **No se aplica filtro** | **Excepción justificada:** indispensable para cargar estado del motor y completar el desafío; **no es un "filtro más", es una necesidad de finalización** |
| 5 | `prepare_worlds` iteración `if participant.world_id is not None and participant.world_id != 0` | `world_id=0` → crear nuevo; `world_id>0` → verificar `deleted_at` | **Mantener:** `world_id>0 ∧ deleted_at IS NOT NULL` → error; `world_id=0` → nuevo mundo |
| 6 | `start_challenge` validación `world_id is None ∨ == 0` | Sentinel `0` y `None` → error | **Mantener:** rechazo antes de transición durable |
| 7 | `recovery_coordinator _list_world_rows` | `WorldRow.deleted_at.is_(None)` | **Mantener:** filtro ordinario; recovery no debe ver mundos eliminados |
| 8 | `app.py A1/A2/A4/A6` | `WorldRow.deleted_at.is_(None)` | **Mantener:** filtros A1/A2/A4/A6 aceptados en B3-3 |

## 4. Contrato CS2 propuesto

### 4.1 Principios

1. **Lectura histórica ≠ operación ordinaria.** Consultas que acceden a `WorldRow` para completar un desafío existente ya inscrito son una excepción justificada, no un estándar.
2. **Mundos eliminados inmutables.** `deleted_at` nunca se escribe, actualiza ni restaura en ninguna operación de finalización.
3. **Operación en vivo prohibida.** Ninguna transición durable a `running` ni ejecución de ticks sobre mundo eliminado.
4. **Autorización reducida.** Solo el organizador/admin del desafío puede acceder al estado histórico de sus participantes; no se amplían permisos por conocer un `world_id`.
5. **Identidad preservada.** `participant.world_id` no cambia; la asignación histórica se conserva tal como quedó después de la eliminación lógica.

### 4.2 Excepciones justificadas (lectura histórica indispensable)

| Excepción | Cuándo aplica | Justificación |
|---|---|---|
| `session.get(WorldRow, p.world_id)` en `complete_challenge` | Desafío en estado `running` con participantes inscritos; se necesita cargar `state_json` y `months_simulated` para ejecutar ticks restantes | El motor necesita el estado serializado previo para continuar la simulación; `deleted_at` no se modifica |
| Ninguna otra | — | No se identificaron otras consultas que requieran saltar el filtro `deleted_at IS NULL` |

### 4.3 Prohibiciones absolutas

| Prohibición | Fundamento |
|---|---|
| Restaurar `deleted_at = NULL` en ningún caso | Conversión a mundo operativo; contradecido por B3-5 |
| Ejecutar `eng.tick()` sobre mundo sin estado previo | Pérdida de consistencia de ticks; el motor requiere `state_json` previo |
| Añadir `world_id` a participantes nuevos como excepción al filtro `deleted_at` | Slippery slope; abriría la puerta a trattar `0` como mundo real |
| Ampliar permisos de recovery o runners a mundos eliminados | Contradicción con B3-2 y B3-3 |

## 5. Riesgos de privacidad y acceso indebido

| Riesgo | Gravedad | Mitigación |
|---|---|---|
| Acceder a `state_json` de mundo eliminado y exponer datos de simulación pasada | Baja | `state_json` contiene datos de motor (kingdoms, tiles) ya existentes en la BD; no contiene PII por diseño |
| Conocer `world_id` de un mundo eliminado y usarlo para crear confusión | Baja | `world_id` es entero; sin contexto de desafío no tiene significado; los filtros B3-3/B3-4 lo excluyen de acceso ordinario |
| Persistir nuevo estado sobre mundo eliminado por error | Media | **Mitigado:** el código de `complete_challenge` solo actualiza `state_json`, `months_simulated`, `result_status`, `result_verified`; `deleted_at` no es tocado por ningún `session.flush()`/`commit()` |
| Que un participante con `world_id=0` sea tratado como teniendo mundo asignado | Alta | **Mitigado:** `start_challenge` rechaza `world_id ∈ {None, 0}`; `prepare_worlds` trata `0` como "crear nuevo mundo" |

## 6. Riesgos de concurrencia y consistencia

| Riesgo | Gravedad | Mitigación |
|---|---|---|
| Race condition entre `session.get(WorldRow, p.world_id)` y eliminación lógica | Baja | El modelo SQLAlchemy con sesión por petición garantiza que la lectura sea consistente dentro de la transacción; `deleted_at` se comprueba en B3-5 fase de preparación, no en `complete_challenge` |
| Commit parcial: `state_json` actualizado pero `months_simulated` no | Baja | Ambas variables se actualizan en el mismo `session.flush()` (línea 1089); el `session.commit()` finaliza la transacción atómicamente |
| Que `complete_challenge` se ejecute sobre desafío ya completado | Baja | `_require_state(challenge, {"running"}, "completar")` (línea 1030) y idempotencia `_idempotent_operation_row` (línea 1017-1024) previene ejecuciones duplicadas |

## 7. Defectos preexistentes y su impacto

| Defecto | Impacto en inspección | Superable? |
|---|---|---|
| `ChallengeOperationRow.actor_user_id` no existe | Bloquea `_idempotent_operation_row` y `record_challenge_operation` en tests integrales | ✅ Aislado con mocks (FIX4); producción real funciona si se evita el llamado o se usa sesión nueva |
| `WorldRow.next_operation_sequence` kwarg inválido | Afecta `_prepare_worlds` al crear WorldRow | ✅ Aislado con mock tolerante (FIX4); no cambia modelo ni persistencia |
| `ChallengeParticipantRow.world_id NOT NULL` | `world_id=None` causaría error SQL; ya resuelto con sentinel `0` | ✅ Solución aplicada en B3-5-FIX1 |
| `ChallengeRow.state` transiciones estrictas | `complete_challenge` requiere `running`; `resolve_challenge` requiere `disputed` | ✅ Documentado; CS2 no relaja estas validaciones |

## 8. Pruebas que deberán cubrir la futura implementación (B3-6-I)

> *Nota: Estas pruebas son para futura implementación; B3-6-PRE no las ejecuta.*

| Test | Objetivo |
|---|---|
| `test_complete_challenge_with_eliminated_world_state` | Verificar que `complete_challenge` en un desafío `running` con participantes cuyas `WorldRow` tienen `deleted_at IS NOT NULL` completa el desafío sin modificar `deleted_at`, cargando `state_json` y `months_simulated` desde el world eliminado |
| `test_resolve_challenge_no_world_access` | Verificar que `resolve_challenge` sobre `disputed` no accede a `WorldRow` y solo usa `participant.result_status` |
| `test_start_challenge_rechaza_mundo_eliminado` | Verificar que `start_challenge` con participante `world_id > 0` y mundo eliminado lanza `WorldNotFoundForParticipant` (giá B3-5) |
| `test_prepare_worlds_mundo_eliminado_sin_sustituto` | Verificar que `prepare_worlds` falla con mundo eliminado y no crea sustituto |
| `test_acceso_ordinario_excluye_mundos_eliminados` | Verificar que queries ordinarias (app.py, league.py, recovery) aplican `deleted_at IS NULL` y no devuelven mundos eliminados |

## 9. Dictamen

**Implementable con reservas.**

El acceso histórico CS2 es viable siempre y cuando se cumplan las excepciones justificadas (solo `session.get(WorldRow, p.world_id)` en `complete_challenge`, con la obligación de no modificar `deleted_at`). No se pueden relajar los filtros `deleted_at IS NULL` en consultas ordinarias ni en recovery/ runners. El contrato separa claramente:

- **Lectura histórica:** permitida únicamente para completar desafío existente inscrito; lectura de `state_json` y `months_simulated`; `deleted_at` inmutable.
- **Operación ordinaria:** `deleted_at IS NULL` obligatorio; mundo eliminado excluido.
- **Operación en vivo:** prohibido para mundos eliminados.

**No se recomienda implementar B3-6-I hasta tanto se resuelvan los defectos ORM preexistentes** (`ChallengeOperationRow.actor_user_id`, `WorldRow.__init__` kwarg inválido) en un ámbito que no sea tests aislados con mocks. La inspección confirma que la lógica de producción es correcta; los únicos "bugs" son de infraestructura ORM que ya tienen aisladores en FIX3/FIX4.

## 10. Confirmación de que no se modificó código ni se inició B3-6-I

- **Archivos `.py` modificados:** ninguno. `challenge_service.py`, `repository.py`, `db.py`, `app.py`, `league.py`, `recovery_coordinator.py` permanecen exactamente como quedaron después de B3-5-FIX4.
- **Modelos ORM modificados:** ninguno. `WorldRow`, `ChallengeRow`, `ChallengeOperationRow`, `ChallengeParticipantRow` sin cambios.
- **Pruebas existentes modificadas:** ninguna.
- **Documentos anteriores modificados:** ninguno.
- **Operaciones Git destructivas:** ninguna (`git status` confirma working tree limpio salvo los reportes y tests añadidos).
- **Bloque B3-6-I:** no iniciado. El presente es `F7-E.2-I-B3-6-PRE-REPORT.md` exclusivamente.

**El informe `F7-E.2-I-B3-6-PRE-REPORT.md` ha sido creado para revisión y autorización expresa antes de cualquier implementación B3-6-I.**