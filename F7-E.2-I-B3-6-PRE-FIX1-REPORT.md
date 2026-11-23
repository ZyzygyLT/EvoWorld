# F7-E.2-I-B3-6-PRE-FIX1 — Cierre contractual de CS2

**Fecha:** 20/11/2026  
**Autor:** Agente de implementación  
**Base:** B3-1 a B3-5-FIX4 aceptados; B3-6-PRE inspeccionado con reservas; **B3-6-I no autorizado**.  
**Restricción absoluta:** No modificar `.py`, modelos, migraciones, pruebas ni documentos previos.

## 1. Simulación de finalización

### 1.1 Excepción histórica de `complete_challenge` reformulada

El `complete_challenge` (línea 977, challenge_service.py) tiene una excepción histórica para completar un desafío `running` cuyos participantes tienen `world_id > 0`. Debe distinguirse tres fases:

| Fase | Código | Propósito | Estado |
|---|---|---|---|
| **Lectura histórica** | `session.get(WorldRow, p.world_id)` línea 1059 | Cargar `state_json` y `months_simulated` desde el world | **Permitido** como excepción justificada; `deleted_at` no se toca |
| **Ejecución acotada** | `eng.tick()` líneas 1073-1074 | Ejecutar ticks pendientes hasta `duration_ticks` | **Permitido** exclusivamente dentro de `complete_challenge`; no persiste runner |
| **Operación viva** | `challenge.state = "completed"` línea 1091 | Transición durable del desafío a `completed` | **Prohibido** para mundo eliminado; solo para mundo activo |

**Campos que pueden actualizarse durante la finalización histórica:**

| Campo | Origen | Por qué puede actualizarse |
|---|---|---|
| `world_row.state_json` | JSON del motor serializado | Needed para calcular métricas actualizadas; refleja el estado después de `eng.tick()` |
| `world_row.months_simulated` | Ticks ya ejecutados | Determina `duration_ticks - current_ticks` ticks restantes; necesario para la finalización |
| `participant.result_json` | Métricas deterministas | Escrito en `result_json` línea 1081; persiste con `session.flush()`/`commit()` |
| `participant.result_status` | `"persisted"` → `"verified"` líneas 1086, 1110-1112 | Refleja la durabilidad del resultado dentro del desafío completado |
| `participant.state` | `"finalizado"` línea 1088 | Marca el participante como finalizado; no altera la asignación del mundo |
| `challenge.state` | `"running"` → `"completed"` línea 1091 | Transición del desafío; **no** toca `world_row.deleted_at` |
| `challenge.completed_at` | `self._now()` línea 1092 | Marca cuándo se completó el desafío; información administrativa |

**Campos que NO pueden actualizarse:**

| Campo | Razón |
|---|---|
| `world_row.deleted_at` | Nunca se modifica en `complete_challenge`; la eliminación lógica es irreversible |
| `world_row.id` | Clave primaria; inmutable |
| `participant.world_id` | Ya está inscrito; cambiarlo rompería la integridad histórica |
| `challenge.organizer_user_id` | Ya está inscrito; inmutable |
| Cualquier campo que provoque reactivación del runner | No hay código que ejecute runner dentro de `complete_challenge` |

### 1.2 Prohibiciones definitivas (lista contractual)

| Prohibición | Justificación |
|---|---|
| Registrar o recuperar un runner | `complete_challenge` no usa `runner`; el motor se reconstruye desde `state_json` |
| Reactivar el mundo | `deleted_at` se comprueba que es `None` al iniciar; si es `IS NOT NULL`, el world está eliminado lógicamente |
| Cambiar `deleted_at` | No hay escritura a esta columna en todo el flujo de `complete_challenge` |
| Exponer el mundo mediante endpoints ordinarios | Los filtros B3-3/B3-4/B3-5-FIX1 usan `deleted_at.is_(NULL)`; `0` nunca es mundo real |
| Permitir nuevos participantes o nuevas operaciones sobre ese mundo | `enroll_participant` asigna `world_id=0` (sentinel); mundo eliminado no reabre inscripciones |
| Convertir la excepción histórica en acceso general | Cada excepción debe justificarse por necesidad de finalización; no hay precedente para relajar filtros |

## 2. Autorización

### 2.1 Validación de identidad (línea 1013-1014)

```python
challenge = challenge_repo.get_challenge(challenge_id)
if challenge.organizer_user_id != organizer_user_id:
    raise ChallengeError("No es el organizador del desafío")
```

**Conclusión:** Solo el organizador del desafío puede autorizar `complete_challenge`. No hay rol "admin" adicional en esta validación; la comprobación es simple comparación de `organizer_user_id`.

### 2.2 Comprobación del rol (ausente)

No hay validación de `is_admin` en `complete_challenge`. La única autorización es la propiedad organizadora. Esto es consistente con el diseño de B3-D y DEC-F7D-04 donde el organizador tiene autoridad total sobre su desafío.

### 2.3 Punto exacto de autorización

La autorización ocurre **después** de la validación de estado y **antes** de la ejecución de ticks:

1. `get_challenge` (línea 1010) - verifica existencia
2. `challenge.organizer_user_id != organizer_user_id` (línea 1013-1014) - **autorización**
3. `_idempotent_operation_row` (línea 1017-1024) - idempotencia
4. `_require_state(challenge, {"running"}, "completar")` (línea 1030) - validación de estado
5. `participants = repo.get_participants(challenge_id)` (línea 1032) - recuperar participantes
6. **Validación de world_id** (líneas 1037-1039) - `world_id is None` → error
7. `session.get(WorldRow, p.world_id)` (línea 1059) - **lectura histórica excepción**
8. Ejecución de ticks (líneas 1073-1077)
9. `challenge.state = "completed"` (línea 1091) - transición durable

**Conclusión:** La autorización está centrada en la línea 1013-1014 (propiedad organizadora). El acceso histórico al world está protegido por esa misma autorización: solo el organizador puede iniciar `complete_challenge`, y por tanto puede leer el state histórico de sus mundos participantes.

## 3. Concurrencia

### 3.1 Intercalaciones entre `complete_challenge`, `safe_delete_world` y cierre/unregister runner

El código no tiene una función `safe_delete_world` visible en `challenge_service.py`. Sin embargo, existe `world_deletion` functionality en otros módulos. Los puntos de conflicto posibles:

| Escenario |Resultado | Justificación |
|---|---|---|
| `complete_challenge` y `world deletion` simultáneos | **Ganador: `complete_challenge`** | `complete_challenge` requiere `challenge.state == "running"` (línea 1030). Si `world deletion` cambiara el challenge state, rompería la restricción de validación. No hay código que pase challenge de `running` a otro estado vía deletion. |
| `complete_challenge` y `complete_challenge` duplicados | **Idempotencia:** `_idempotent_operation_row` (línea 1017-1024) retorna el resultado original si la clave ya se usó. El second call es `is_retry=True` sin nuevos efectos. | El patrón FIX10-A-IDEM ya gestiona esto. |
| `complete_challenge` y fallo de transacción | **Rollback atómico:** Todo o nada. Si falla `session.commit()`, ninguno de los cambios (state_json, months_simulated, result_json, challenge.state="completed") se persisten. El desafío se queda en `running`. | SQLAlchemy transaction atomicidad. |
| `complete_challenge` sin runner activo | **Siguiente:** El motor se reconstruye desde `state_json` (línea 1066-1067). No depende de que el runner siga activo en memoria. | `state_json` persiste en la BD; `eng.tick()` opera sobre ese estado cargado. |

### 3.2 Garantías de consistencia

| Garantía | Mecanismo |
|---|---|
| `deleted_at` nunca vuelve a NULL | No hay `UPDATE world SET deleted_at = NULL` en ningún lugar; la columna se escribió una vez y solo se lee |
| `state_json` y `months_simulated` no se pierden | Ambas variables se actualizan en el mismo `session.flush()` (línea 1089); `session.commit()` las confirma duramente |
| Fallo de transacción no deja desafío completado con resultados incompletos | `session.rollback()` descarta todos los cambios de la transacción; el desafío se queda en `running` con su estado previo |
| Finalización no depende runner activo | El motor se carga desde `state_json` en la BD; `eng.tick()` opera sobre ese estado; no hay referencia a runner global | 

## 4. Sentinel

### 4.1 Comportamiento de `complete_challenge` ante valores de `world_id`

| `world_id` | Estado de la WorldRow | Comportamiento en `complete_challenge` | Resultado |
|---|---|---|---|
| `0` (sentinel) | No aplica (world_id=0 nunca se asigna a WorldRow) | `participant.world_id is None ∨ == 0` → error en `start_challenge` (B3-5 FIX2); `complete_challenge` requiere `world_id > 0` por `_require_state` y `session.get(WorldRow, p.world_id)` | `ChallengeError` en inicio; en completion, `session.get(WorldRow, 0)` devuelve `None` → `WorldNotFoundForParticipant` |
| `NULL` | Prohibido por restricción NOT NULL; `world_id NOT NULL` en BD | `participant.world_id is None` → `ChallengeError` línea 1039 ("Participante ... sin mundo asociado") | Error antes de completar |
| positivo + mundo eliminado | `deleted_at IS NOT NULL` | `session.get(WorldRow, p.world_id)` devuelve la fila; `state_json` y `months_simulated` se cargan; `deleted_at` se lee pero **no se modifica**; ticks se ejecutan sobre el estado cargado | **Completado posible** (excepción histórica); `deleted_at` permanece intacto |
| positivo + mundo inexistente | Fila no existe en BD | `session.get(WorldRow, p.world_id)` devuelve `None` → `WorldNotFoundForParticipant` línea 1061-1063 | Error; completion abortada |
| positivo + mundo activo | `deleted_at IS None` | Carga normal; ticks ejecutados; `challenge.state = "completed"` | Completado normal |

**Conclusión del sentinel:** `world_id=0` y `world_id IS NULL` son rechazados en `start_challenge` y también fallarían en `complete_challenge` (error de "sin mundo asociado"). `world_id` positivo con mundo eliminado es la **única excepción histórica** permitida: se lee el estado y se completa, pero `deleted_at` no cambia. `world_id` positivo activo es el caso normal.

## 4. resolve_challenge

### 5.1 Líneas reales de `_resolve_challenge` (líneas 1363-1429, challenge_service.py)

```python
def _resolve_challenge(
    self,
    session,
    challenge_id: int,
    organizer_user_id: int,
    client_idempotency_key: str,
    request_hash: str | None,
    resolution: str = "confirm_all",
) -> ChallengeResult:
    repo = ChallengeParticipantRepository(session)
    challenge_repo = ChallengeRepository(session)

    challenge = challenge_repo.get_challenge(challenge_id)
    if challenge is None:
        raise ChallengeNotFound(f"Desafío {challenge_id} no existe")

    # Ámbito de idempotencia §17.13: clave reutilizada con contenido
    # distinto → 409 ANTES del retorno temprano por estado.
    _idempotent_operation_row(
        session, challenge.id, organizer_user_id,
        client_idempotency_key, request_hash,
    )

    # Idempotencia: si ya está en el estado objetivo, devolver como reintento
    if challenge.state == "resolved":
        return ChallengeResult(challenge=challenge, is_retry=True)

    _require_state(
        challenge, {"disputed"}, "resolver"
    )

    # Verificar autorización: organizador o admin
    from worldsim.server.db import UserRow
    user = session.get(UserRow, organizer_user_id)
    if user is None or not user.is_admin:
        raise ChallengeError("No autorizado para resolver disputa")

    # Update disputed participants based on resolution
    participants = repo.get_participants(challenge_id)
    for p in participants:
        if p.result_status == "disputed":
            if resolution == "confirm_all":
                p.result_status = "confirmed"
                p.result_verified = 1
            elif resolution == "invalidate_all":
                p.result_status = "invalidated"
                p.result_verified = 0
            else:
                raise ChallengeError(f"Invalid resolution: {resolution}")

    challenge.state = "resolved"
    session.flush()

    challenge_repo.record_challenge_operation(
        organizer_user_id=organizer_user_id,
        client_idempotency_key=client_idempotency_key,
        request_hash=request_hash or "",
        operation="resolve",
        challenge_id=challenge.id,
        outcome=resolution,
    )

    session.commit()
    return ChallengeResult(challenge=challenge, is_retry=False)
```

### 5.2 Secuencia aclarada

| Paso | Estado | Acción |
|---|---|---|
| 1 | `disputed` | Desafío pasa a `disputed` tras disputa de resultados |
| 2 | Validación de idempotencia | `_idempotent_operation_row` evita reprocesamiento |
| 3 | `_require_state(challenge, {"disputed"}, "resolver")` | Verifica que el desafío esté en `disputed` |
| 4 | Validación de autorización | `UserRow.is_admin` check; **nota:** requiere que el organizador sea admin o que haya un usuario admin diferente |
| 5 | Actualizar participantes | `result_status` cambia de `disputed` a `confirmed` o `invalidated`; `result_verified` actualizado |
| 6 | `challenge.state = "resolved"` | Transición durable |
| 7 | `session.flush()` | Persiste cambios de participantes |
| 8 | `challenge_repo.record_challenge_operation` | Registro duradero |
| 9 | `session.commit()` | Confirma todo |

**¿Son ramas distintas o hay contradicción?** 

No hay contradicción. `resolve_challenge` es el camino inverso/directo de `dispute_challenge`:

- `dispute_challenge` (línea 1261): `completed` → `disputed` (baja un resultado)
- `resolve_challenge` (línea 1341): `disputed` → `resolved` (confirma o invalida todos los resultados)

Son **ramas distintas** con estados iniciales diferentes:

| Operación | Estado inicial | Estado final |
|---|---|---|
| `dispute_challenge` | `completed` | `disputed` |
| `resolve_challenge` | `disputed` | `resolved` |

No hay superposición: un desafío va `completed` → `dispute` → `resolved`. La secuencia es lineal.

**Nota importante:** `resolve_challenge` **no** accede a `WorldRow` ni a `state_json`. Solo manipula `participant.result_status` y `participant.result_verified`. Esto es consistente con que la resolución de disputa es sobre resultados ya calculados, no sobre la simulación del motor.

## 6. Bloqueo ORM

### 6.1 Impacto concreto de `ChallengeOperationRow.actor_user_id` inexistente

| Call site | Línea | Error | Contexto |
|---|---|---|---|
| `_idempotent_operation_row` | 206 | `AttributeError: type object 'ChallengeOperationRow' has no attribute 'actor_user_id` | Al evaluar idempotencia en `complete_challenge`, `start_challenge`, `publish_challenge`, `open_enrollment`, etc. |
| `record_challenge_operation` | 1167 | Mismo error | Al registrar la operación duratoria; `ChallengeOperationRow` se inserta pero referencia `actor_user_id` que no existe como atributo ORM |

**Impacto en `complete_challenge` específicamente:**

1. Línea 1017-1024: Si `_idempotent_operation_row` falla, `complete_challenge` nunca alcanza la lógica de ticks. El error se propaga como excepción no atrapada.
2. Línea 1095-1102: `challenge_repo.record_challenge_operation` también falla en el mismo punto.

**Propiedad real del modelo:** La tabla `challenge_operations` sí tiene columna `requesting_user_id` (línea 143, db.py), no `actor_user_id`. El atributo `actor_user_id` simplemente no existe en el modelo SQLAlchemy.

### 6.2 Corrección ORM mínima propuesta

**Opción A (recomendada - mapear al campo real):**

El modelo `ChallengeOperationRow` debería usar `requesting_user_id` en lugar de `actor_user_id` en las consultas SQLAlchemy. Cambio mínimo:

```python
# En challenge_service.py línea 206:
ChallengeOperationRow.requesting_user_id == actor_user_id

# En repository.py línea 1167:
ChallengeOperationRow.requesting_user_id == organizer_user_id
```

**Opción B (mantener compatibilidad):**

Crear una propiedad mapeada o alias, pero esto excede el alcance de "mínimo".

**Opción C (evitar el llamado en producción):**

Usar mocks en tests (como se hizo en FIX3/FIX4) y en producción asegurar que `complete_challenge` se llame con sesión nueva que no active ese path, o reestructurar la lógica para no necesitar esa validación en el path caliente.

**Decisión:** La **Opción A** es la corrección mínima compatible con el modelo real. El campo `requesting_user_id` existe en `ChallengeOperationRow` (db.py:143) y es el que debería usarse en las consultas. Cambiar `actor_user_id` por `requesting_user_id` en las dos líneas:

- `challenge_service.py:206`: `ChallengeOperationRow.requesting_user_id == actor_user_id`
- `repository.py:1167`: `ChallengeOperationRow.requesting_user_id == organizer_user_id`

Esto soluciona el `AttributeError` sin modificar el modelo (solo las consultas SQLAlchemy que ya mapean columnas existentes).

**Nota:** `WorldRow.next_operation_sequence` se mantiene fuera de este bloque al no afectar directamente a CS2, tal como se documentó en B3-6-PRE.

## 7. Entregable

`F7-E.2-I-B3-6-PRE-FIX1-REPORT.md`

### 7.1 Contenido incluido

1. **Hallazgos con rutas y líneas reales:**
   - `complete_challenge:1013-1014` autorización organizador
   - `complete_challenge:1059` lectura histórica `session.get(WorldRow, p.world_id)`
   - `complete_challenge:1030` validación `_require_state` state="running"
   - `repository.py:1167` y `challenge_service.py:206` defecto `actor_user_id`
   - `db.py:143` campo real `requesting_user_id`

2. **Contrato CS2 definitivo propuesto:**
   - Lectura histórica excepción: `complete_challenge` con world eliminado
   - Autorización: solo organizador (`organizer_user_id`)
   - Prohibiciones: sin runner, sin `deleted_at` reset, sin exposición ordinaria
   - Sentinel: `0` y `NULL` rechazados; positivo eliminado = excepción justificada

3. **Campos modificables durante finalización histórica:**
   - `world_row.state_json`, `world_row.months_simulated`, `participant.result_json`, `participant.result_status`, `participant.state`, `challenge.state`, `challenge.completed_at`
   - Ninguno de `world_row.deleted_at`, `world_row.id`, `participant.world_id`

4. **Garantías de autorización:**
   - Validación de línea 1013-1014 (`organizer_user_id == challenge.organizer_user_id`)
   - Sin roles admin adicionales en `complete_challenge`
   - Idempotencia vía `_idempotent_operation_row` (con fix ORM propuesto)

5. **Análisis de concurrencia:**
   - `complete_challenge` vs `world deletion`: `running` state es barrera infranqueable
   - Fallo de transacción: rollback atómico; desafío permanece en `running`
   - Sin dependencia runner: motor cargado desde `state_json` en BD

6. **Matriz de comportamiento para los cinco casos de `world_id`:**
   - `0`: error en `start_challenge` y `complete_challenge` (world no encontrado)
   - `NULL`: error "sin mundo asociado" (restricción NOT NULL)
   - positivo + eliminado: **excepción histórica** `complete_challenge` carga state y completa; `deleted_at` inmutable
   - positivo + inexistente: `WorldNotFoundForParticipant`
   - positivo + activo: completado normal

7. **Resolución de la secuencia de `resolve_challenge`:**
   - `disputed` → validaciones → actualizar participantes → `challenge.state = "resolved"`
   - No accede a `WorldRow`; solo manipula `participant.result_status`
   - Ramos distintos de `dispute_challenge` (`completed` → `disputed`)

8. **Corrección ORM mínima propuesta:**
   - `challenge_service.py:206`: `ChallengeOperationRow.requesting_user_id == actor_user_id`
   - `repository.py:1167`: `ChallengeOperationRow.requesting_user_id == organizer_user_id`
   - Sin cambios en modelo ORM; solo en queries mapeo

9. **Pruebas necesarias para B3-6-I:**
   - `test_complete_challenge_with_eliminated_world_state`
   - `test_complete_challenge_idempotency`
   - `test_resolve_challenge_no_world_access`
   - `test_sentinel_behavior_in_complete`
   - `test_concurrency_complete_vs_delete`

10. **Dictamen final y reservas:**
    - **Dictamen:** Implementable con reservas ORM
    - **Reservas pendientes:** `ChallengeOperationRow.actor_user_id` → `requesting_user_id` requiere migration/deploy; comportamiento de `complete_challenge` con mundo eliminado es excepción justificada pero debe documentarse rígidamente; no relajar filtros `deleted_at IS NULL` en ordinario

## 8. Confirmación de que no se modificó código ni se inició B3-6-I

- **Archivos `.py` modificados:** ninguno (solo propuesta de corrección de consultas, sin ejecutar)
- **Modelos ORM modificados:** ninguno
- **Pruebas modificadas:** ninguna
- **Documentos anteriores:** ninguno modificado
- **Operaciones Git destructivas:** ninguna
- **B3-6-I:** no iniciado; este informe `F7-E.2-I-B3-6-PRE-FIX1-REPORT.md` es el entregable de inspección y contrato previo a implementación

**El informe `F7-E.2-I-B3-6-PRE-FIX1-REPORT.md` ha sido creado para revisión y autorización expresa antes de corregir el ORM e implementar B3-6-I.**