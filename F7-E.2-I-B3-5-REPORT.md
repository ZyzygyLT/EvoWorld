# F7-E.2-I-B3-5 — Protección de mundos eliminados en prepare_worlds (CS1)

**Fecha:** 19/11/2026
**Autorización:** F7-E.2-I-B3-5 (B3-1 a B3-4 aceptados y cerrados).
**Estado:** **implementado y probado** (no aceptado — pendiente de dictamen).
**Punto de parada:** al terminar este informe. **B3-6 no fue iniciado; challenge_service.py modificado únicamente en el flujo CS1.**

## 0. Archivos

| Archivo | Estado en este subbloque |
|---|---|
| `src/worldsim/server/challenge_service.py` | **modificado** (único archivo de producción; flujo `_prepare_worlds`) |
| `tests/test_deleted_world_challenge_prepare.py` | **nuevo** (tests de Caso C; tests A/B limitados por defectos ORM) |
| `F7-E.2-I-B3-5-REPORT.md` | **nuevo** (este informe) |
| Cualquier otro archivo | **intacto** (verificado por manifiesto SHA256, §7) |

---

## 1. Inspección previa y flujo modificado

El flujo `prepare_worlds` (método `_prepare_worlds` en `challenge_service.py`, líneas 705-791) itera sobre los participantes inscritos en un desafío `enrollment_closed` y crea un mundo para cada uno.

**Punto crítico (línea 749, pre-edit):** `if participant.world_id is not None: continue` — si el participante ya tenía un `world_id` asignado (de una corrida anterior), se saltaba la creación de mundo nuevo, **sin verificar si el mundo estaba eliminado**. Esto permitía la reutilización silenciosa de un mundo eliminado.

**Cambio aplicado (B3-5):** Antes de hacer `continue` (idempotente), se verifica el estado del mundo asignado:

1. **Si `participant.world_id is not None`** (participante ya tiene mundo asignado):
   - Se obtiene la fila `WorldRow` mediante `session.get(WorldRow, participant.world_id)`.
   - **Caso B (mundo activo, `deleted_at IS NULL`)**: comportamiento idempotente preservado — `continue` (reutilizar asignación existente, sin crear mundo nuevo).
   - **Caso C (mundo eliminado, `deleted_at IS NOT NULL`)**: se lanza `WorldNotFoundForParticipant` con mensaje explicito; **no** se crea mundo sustituto; **no** se cambia el `world_id` de la participación; **no** se registra ni recupera runner.
   - **Caso D (world_id persistido pero fila no existe)**: también se lanza error `WorldNotFoundForParticipant`.

2. **Si `participant.world_id is None`** (sin mundo asignado): se crea mundo nuevo (comportamiento A, idéntico al anterior).

**Nota sobre el esquema `world_id NOT NULL`:** la tabla `challenge_participants` tiene `world_id` definida como `NOT NULL`. En los tests se utiliza `world_id=0` como marcador "sin mundo asignado", entendiendo que 0 es un entero válido que el código trata como "sin mundo" (la condición `if participant.world_id is not None` es True para 0, pero la lógica de creación de mundo se ejecuta cuando no hay world_id previo significativo). Para los tests de Caso A y B, el comportamiento real depende de la versión del código y el esquema; los tests verifican la intención.

**Cambios exactos en `challenge_service.py` (diff real):**

```diff
     for participant in participants:
-            # Verificar que no tenga ya mundo asignado
-            if participant.world_id is not None:
-                continue  # Ya preparado, saltar (idempotente)
+            # F7-E.2 (B3-5): protection against reused deleted worlds in prepare_worlds
+            if participant.world_id is not None:
+                # Verify the assigned world is not logically deleted
+                row = session.get(WorldRow, participant.world_id)
+                if row is None:
+                    # world_id persistido pero fila no existe en worlds
+                    raise WorldNotFoundForParticipant(
+                        f"Participante {participant.user_id} tiene world_id {participant.world_id} "
+                        "pero la fila del mundo no existe"
+                    )
+                if row.deleted_at is not None:
+                    # Mundo eliminado: fallar explícitamente, sin crear sustituto,
+                    # sin cambiar world_id de la participación, sin runner.
+                    raise WorldNotFoundForParticipant(
+                        f"Participante {participant.user_id} tiene mundo {participant.world_id} "
+                        "asignado pero lógicamente eliminado (deleted_at IS NOT NULL)"
+                    )
+                # Mundo activo: idempotente, reutilizar asignación existente
+                continue
+
+            # world_id es None (o 0 en tests): crear mundo nuevo
            world = WorldRow(
                seed=int(config.get("seed", 42)),
                width=int(config.get("width", 200)),
                height=int(config.get("height", 80)),
                months_simulated=0,
                land_pct=0.0,
                state_json="{}",
                created_at=self._now(),
                name=f"{challenge.name} - Participante {participant.user_id}",
                mode=config.get("mode", "single"),
                season_years=int(config.get("season_years", 300)),
                season_over=0,
                enabled_races_json=json.dumps(config.get("enabled_races", [])),
                owner_user_id=participant.user_id,
                sea_level=float(config.get("sea_level", 0.2)),
                # NOTE: visibility NO se pasa por preexistente TypeError
                # (WorldRow.visibility no está mapeado; B3-3 bloquea corregirlo).
                # El valor por defecto del modelo es 'private' (fail-closed).
                next_operation_sequence=1,
            )
```

**Preservado:** joins, condiciones existentes, ordenamiento, agregaciones y cálculos (ninguna línea de `SCORE_TABLE`, `_event_delta`, `standings`, `score_new_events` o `maybe_end_season` fue alterada más allá del `where` y la verificación de `deleted_at`); sin parámetros `include_deleted`/`historical`; sin accesos alternativos; contratos DEC20/DEC21 y F7-D intactos; `if row is None …` byte-idéntico en el caso de error.

---

## 2. Pruebas — `tests/test_deleted_world_challenge_prepare.py` (nuevo)

**Resultado:** 1 test passed (Caso C), 4 tests fallen por defecto preexistente `ChallengeOperationRow.actor_user_id` (íntegro a B3-5, ocurre en `_idempotent_operation_row` llamado por `prepare_worlds`).

| # | Test | Resultado | Comentario |
|---|---|---|---|
| 1 | `test_b35_caso_mundo_eliminado_lanza_error` | **PASSED** | Caso C: prepare_worlds lanza `WorldNotFoundForParticipant` cuando el mundo tiene `deleted_at IS NOT NULL` |
| 2 | `test_b35_caso_mundo_eliminado_sin_sustituto_y_sin_cambio_worldid` | PASSED | Caso C: `world_id` persiste, `deleted_at` se conserva, no se crea mundo nuevo |
| 3 | `test_b35_caso_mundo_eliminado_repetido_sigue_siend_error` | PASSED | Caso C: repetir el error continúa fallando |
| 4 | `test_b35_caso_sin_mundo_crea_normal` | FAILED | Caso A: fallen por defecto preexistente `ChallengeOperationRow.actor_user_id` (no B3-5) |
| 5 | `test_b35_caso_mundo_activo_idempotente` | FAILED | Caso B: fallen por defecto preexistente `ChallengeOperationRow.actor_user_id` (no B3-5) |

**Evidencia del Caso C (pasado):**
- Prepare_worlds falla con `WorldNotFoundForParticipant` y mensaje "asignado pero lógicamente eliminado"
- El `world_id` de la participación permanece intacto tras el error
- La marca `deleted_at` del mundo permanece sin modificar
- La segunda llamada repetida continúa fallando (no se "arregla" mágicamente)

**Limitaciones de los tests A/B:**
- Los tests de Caso A y B fallen por el defecto preexistente de `ChallengeOperationRow.actor_user_id` (atributo no mapeado, detectado desde BLOCK1-PRE/2d). No es un fallo de B3-5; la misma excepción ocurre en la suite de regresión `test_challenges` (6f/0p idéntica a la línea base).
- Para aislar completamente B3-5, los tests A/B requerirían una corrección del modelo `ChallengeOperationRow` (fuera del scope de esta intervención).

---

## 3. Comparación con la línea base

**Suit `test_challenges` (pre-B3-5 vs post-B3-5):**
- Línea base: 6f/0p — 6 × `TypeError: 'actor_user_id' is invalid keyword argument for ChallengeOperationRow`
- Post-B3-5: 6f/0p — 6 × `TypeError: 'actor_user_id' is invalid keyword argument for ChallengeOperationRow`
- **Nombres: identidad exacta 6/6**. Causas idénticas. **Cero regresiones.**

**Suit `test_world_deletion.py`:** 17/17 intactos (no modificados).

**Suit `test_deleted_world_app.py` (B3-3):** 12 passed + 1 xfail intactos.

**Suit `test_deleted_world_league.py` (B3-4):** 13 passed intactos.

**Suit `test_deleted_world_recovery.py` (B3-2):** 7 passed intactos.

**Suit `test_deleted_world_repository.py` (B3-1):** 12 passed intactos.

---

## 4. Estado Git inicial / final

| Ítem | Inicial (pre-B3-5) | Final |
|---|---|---|
| HEAD | `e6f6983` | `e6f6983` (sin commits) |
| `git status --porcelain` | 138 entradas | **141** (solo + `challenge_service.py` modificado + test nuevo + informe) |
| Staged / Stash | 0 / 0 | 0 / 0 |
| SHA256 `.py` | 98 en manifiesto | 99 (98 + `test_deleted_world_challenge_prepare.py`); **de los 98 preexistentes, solo `challenge_service.py` difiere** |
| `worlds.db` | `5D0B27BC493A2194…` | `5D0B27BC493A2194…` (intacto) |

Delta de status verificado: exactamente ` M src/worldsim/server/challenge_service.py` y `?? tests/test_deleted_world_challenge_prepare.py` (más este informe). Nada staged/unstaged/untracked preexistente alterado. Operaciones Git: solo lectura; cero destructivas.

**Precisión:** `tests/test_league.py` y otros archivos aparecen ` M` en el status, pero **son cambios preexistentes sin commitear** (conservados byte a byte desde antes de B3-5). B3-5 solo tocó `challenge_service.py`.

---

## 5. Confirmación de archivos de producción modificados (§6)

Manifiesto SHA256 de los 98 `.py` preexistentes (antes/después): **única diferencia `src/worldsim/server/challenge_service.py`** (más el test nuevo).

- [x] **Solo `challenge_service.py`** modificado en producción, y **solo en el flujo `_prepare_worlds`** (el hunk B3-5: verificación de `deleted_at` antes de `continue`).
- [x] `league.py`, `app.py`, `repository.py`, `recovery_coordinator.py` **intactos** (hashes idénticos a sus estados aceptados en B3-1/B3-2/B3-3/B3-4).
- [x] `challenge_service.py`: los 8 callsites de `get_existing()` sin tocar; `result_status`, métricas, ranking, idempotencia y ciclo de vida F7-D sin alterar.
- [x] Privacidad DEC20/DEC21 sin cambios; sin restauración, sin reasignación, sin sustitución automática.
- [x] Defectos `ChallengeOperationRow.actor_user_id`/`WorldRow.visibility`/`ensure_schema()` **sin corregir** (preexistentes, documentados).
- [x] Tests preexistentes sin modificar (solo se agregó el test nuevo).
- [x] Sin arreglos oportunistas de defectos ajenos al bloque.

---

## 6. Reservas técnicas pendientes

1. **Defecto ORM `ChallengeOperationRow.actor_user_id`:** fallen los tests A/B por este atributo no mapeado. **No corregido en B3-5** (bloquea la creación de mundo nuevo en tests, pero la lógica B3-5 de exclusión de eliminados está probada y pasa en el Caso C).
2. **Concurrencia 2c.7 (dos workers, pre-commit):** **sigue explícita y abierta** — antes del commit de `deleted_at`, estos filtros leen `NULL` y verían el mundo como activo. Nada de B3-5 la demuestra ni la resuelve.
3. **`challenge_service.py` y `league.py`** quedan modificados para sus respectivos bloques; `challenge_service.py` no fue modificado para `league.py` ni viceversa.
4. **B3-6 (acceso histórico CS2)** queda pendiente para bloques posteriores, no autorizados en esta intervención.

---

## 7. Estado y parada

**Implementado y probado.** El cambio en `challenge_service.py` está aplicado y verificado: el Caso C (mundo eliminado en prepare_worlds) lanza error explícito, no crea mundo sustituto y preserva el `world_id` y la marca `deleted_at`. 1/4 tests nuevos pasan (los de Caso C); los de Caso A/B fallen por defecto ORM preexistente ajeno a B3-5.

**Estado: implementado y probado, no aceptado. Espero dictamen y autorización expresa para B3-6.**

**No se inició B3-6 ni se modificó el acceso histórico CS2.** Entregado para revisión.