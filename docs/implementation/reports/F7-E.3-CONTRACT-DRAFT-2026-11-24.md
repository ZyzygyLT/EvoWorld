# F7-E.3 — Contrato consolidado de autoridad y elegibilidad de destacados

**Fecha:** 2026-11-24  
**Rama de referencia:** `feature/f7-e4-implementation`  
**HEAD inspeccionado:** `89cbbceb279257c234d7e99a453c63f67e2ac7fd`  
**Estado:** borrador de consolidación para revisión y aceptación explícita. No constituye por sí mismo una DEC aprobada.

## 1. Propósito y límites

F7-E.3 define quién puede conceder o retirar un destacado administrativo y qué mundo puede recibirlo. Se limita al ciclo administrativo de `world_highlights`.

No modifica:
- visibilidad, propiedad ni autorización de lectura HTTP/WS;
- la eliminación lógica de F7-E.2-I ni la retención histórica de F7-E.1;
- la proyección pública, orden y cursor de F7-E.4;
- checkpoints, recuperación, motor de simulación o runners;
- rankings globales ni F7-D Nivel 2.

El destacado no vuelve público un mundo, no amplía permisos y no habilita acceso al estado vivo.

## 2. Autoridad administrativa

La identidad del actor se obtiene de la autenticación de servidor mediante `_auth_user(authorization)`. El permiso se determina exclusivamente por `UserRow.is_admin`.

Para las tres rutas administrativas:
- sin sesión válida: HTTP 401;
- usuario autenticado sin `is_admin`: HTTP 403;
- administrador autenticado: puede continuar con las validaciones propias de la operación.

`user_token`, el propietario del mundo y cualquier dato aportado por el cliente no sustituyen la identidad autenticada del actor.

La concesión guarda el identificador del administrador en `WorldHighlight.admin_user_id`. La retirada guarda el actor en `revoked_by_admin_user_id`. La consulta de detalle administrativo devuelve los datos de auditoría del registro.

## 3. Elegibilidad del mundo para conceder un destacado

En cada concesión, el servicio vuelve a evaluar la elegibilidad dentro de la transacción de escritura. Todas estas condiciones son obligatorias:

1. El mundo existe.
2. `deleted_at IS NULL`.
3. `visibility == "public"`.
4. `owner_user_id` no es NULL.
5. `status` es exactamente `running` o `paused`.
6. Existe una cuenta `users.id` correspondiente al propietario.
7. El propietario no es el administrador que concede el destacado.
8. Existe al menos un checkpoint candidato íntegro conforme a §4.

Si alguna condición falla, el servicio lanza `WorldNotEligible`. La ruta de concesión lo traduce a HTTP 400. El mundo inexistente también cae en esta respuesta por la validación común de elegibilidad; no se define aquí una respuesta 404 distinta.

La elegibilidad se revalida en cada concesión, incluso si el mundo tuvo un destacado anteriormente. La retirada no reevalúa la elegibilidad: su función es cerrar el destacado activo existente.

## 4. Validación no destructiva de checkpoints

El conjunto candidato está compuesto exclusivamente por filas de `world_checkpoints` que cumplen:
- `world_id == id del mundo`;
- `is_valid == 1`.

Los candidatos se recorren por `sequence DESC`. Para cada candidato se calcula:

`sha256(bytes(checkpoint.payload)).hexdigest()`

El candidato se considera íntegro cuando el resultado coincide con `str(checkpoint.checksum).lower()`. Se acepta el mundo si al menos un candidato resulta íntegro. Por tanto, un candidato más reciente corrupto no invalida un candidato anterior que siga íntegro y marcado `is_valid=1`.

Esta comprobación es estrictamente de solo lectura:
- no cambia `is_valid`, checksum, payload, secuencia ni versión;
- no elimina, repara ni reescribe checkpoints;
- no llama a `CheckpointRepository.get_latest_valid_checkpoint()` ni a su ruta de mutación ante corrupción;
- no llama a `RecoveryManager.recover()` ni inicia recuperación;
- no crea ni modifica runners o motores.

El contrato no añade filtros por `checkpoint_type` o `checkpoint_version`: no forman parte del predicado implementado para seleccionar candidatos.

## 5. Concesión

Ruta: `POST /api/admin/worlds/{world_id}/highlight`.

Precondiciones:
- administrador autenticado;
- `reason.strip()` no vacío;
- `duration_months`, si se proporciona, debe ser entero positivo;
- mundo elegible según §3 y §4.

La concesión:
1. abre una transacción de escritura;
2. serializa la operación sobre la fila del mundo;
3. vuelve a comprobar elegibilidad y checkpoint íntegro;
4. consulta el destacado activo;
5. si existe uno sin expiración o cuya expiración sea posterior al instante de concesión, rechaza con HTTP 409;
6. si existe uno activo ya vencido, lo cierra con `is_active=0`, sin simular una revocación: `revoked_at` y `revoked_by_admin_user_id` permanecen NULL;
7. inserta el nuevo registro activo, con actor, motivo, instante de concesión y expiración calculada;
8. confirma ambas modificaciones en una única transacción.

Respuestas documentadas:
- HTTP 201: concesión creada; devuelve `world_highlight_id`;
- HTTP 400: motivo vacío, duración inválida o mundo no elegible;
- HTTP 401/403: autenticación/autoridad insuficiente;
- HTTP 409: ya existe un destacado activo no vencido.

Si la transacción falla, se revierte tanto el cierre de un registro vencido como la inserción nueva.

## 6. Retirada

Ruta: `POST /api/admin/worlds/{world_id}/unhighlight`.

Requiere administrador autenticado. Dentro de una transacción, bloquea/serializa sobre la fila del mundo y busca el destacado activo. Si lo encuentra:
- establece `is_active=0`;
- establece `revoked_at`;
- establece `revoked_by_admin_user_id`;
- conserva la fila y todos sus datos históricos.

Si no existe el mundo o no hay destacado activo, responde HTTP 404 (`no active highlight`). Una segunda retirada no borra ni altera el historial.

## 7. Consulta de detalle administrativo

Ruta: `GET /api/admin/world_highlights/{highlight_id}`.

Requiere administrador autenticado. Devuelve los campos administrativos del registro:
`id`, `world_id`, `admin_user_id`, `granted_at`, `expires_at`, `reason`, `is_active`, `revoked_at` y `revoked_by_admin_user_id`.

Si el registro no existe, responde HTTP 404. La ruta no concede permisos adicionales sobre el mundo ni devuelve `state_json).

## 8. Serialización y persistencia

La implementación inspeccionada usa:
- SQLite: `BEGIN IMMEDIATE` antes de comprobar elegibilidad y estado activo, serializando escritores de concesión/retirada;
- PostgreSQL: bloqueo de fila mediante `SELECT ... FOR UPDATE` sobre `WorldRow`; concesión y retirada bloquean primero la fila del mundo para serializar operaciones del mismo mundo;
- índice único parcial sobre `world_id` para `is_active = 1`, como última defensa de integridad.

La concesión traduce un conflicto de integridad a `ActiveHighlightExists` y HTTP 409. La política de retención `ON DELETE RESTRICT` de F7-E.1 se mantiene: el historial no se elimina mediante el ciclo ordinario de destacados.

La cobertura automatizada concurrente inspeccionada demuestra serialización de concesiones en SQLite. No se presenta como evidencia de una prueba equivalente contra PostgreSQL.

## 9. Relación con F7-E.1, F7-E.2-I y F7-E.4

- F7-E.1 define la tabla histórica, expiración, retirada y retención.
- F7-E.2-I define `deleted_at`; un mundo eliminado no puede recibir un destacado nuevo.
- F7-E.4 consume el estado vigente del destacado como booleano público derivado. No redefine autoridad ni elegibilidad.
- La condición de destacado no cambia `visibility`, la allowlist pública, las autorizaciones HTTP/WS ni la privacidad.

## 10. Evidencia de implementación inspeccionada

- Servicio: `src/worldsim/server/highlight_service.py`.
- Rutas: `src/worldsim/server/app.py`, helper `_require_highlight_admin`.
- Modelo e índices: `src/worldsim/server/db.py`, `WorldRow`, `WorldCheckpoint`, `WorldHighlight`.
- Pruebas: `tests/test_world_highlight_service.py`, `tests/test_world_highlight_api.py`, `tests/test_world_highlights_schema.py`.
- Contrato consumidor: `docs/architecture/CONTRACTS.md`, §18 (F7-E.4).

Este documento consolida el comportamiento observado y no afirma que se hayan ejecutado pruebas nuevas durante su preparación.

## 11. Estado propuesto

El contrato queda preparado para revisión. La aceptación de F7-E.3 requiere:
1. revisar y aceptar explícitamente este contrato;
2. cerrar los casos pendientes de la matriz `F7-E.3-TEST-MATRIX-2026-11-24.md), o aceptar expresamente sus límites;
3. registrar entonces la decisión y el acta de aceptación correspondientes.

Hasta ese momento, el estado correcto es **implementación evidenciada; aceptación formal independiente pendiente**.
