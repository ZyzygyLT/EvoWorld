# F7-D API Privacy Audit — 2026-11-23

## Estado

Corrección de privacidad implementada en la rama `fix/f7d-challenge-privacy` y sometida a validación específica.
El PR #4 permanece abierto y sin fusionar.

## Alcance

Se limitaron los cambios funcionales a los siguientes endpoints en
`src/worldsim/server/app.py`:

- `GET /api/challenges`
- `GET /api/challenges/{challenge_id}`
- `GET /api/challenges/{challenge_id}/participants`
- `GET /api/challenges/{challenge_id}/results`

No se modificaron modelos, migraciones, dependencias, servicios de desafíos,
DEC20/DEC21, F7-C ni el alcance F7-D Level 2.

## Contrato de privacidad aplicado

### Listado público

- No requiere autenticación.
- Solo devuelve desafíos en estado `published`.
- No expone `organizer_user_id`.

### Detalle

- Los desafíos publicados son consultables públicamente.
- Los desafíos no publicados solo son visibles para su organizador.
- Los demás usuarios reciben `404`.
- No se expone `organizer_user_id`.

### Participantes

- Requiere autenticación (`401` si falta).
- Desafío inexistente: `404`.
- Organizador y administrador consultan todos los participantes.
- El administrador se identifica mediante `UserRow.is_admin` en la base de datos.
- Un participante inscrito consulta únicamente su propia fila.
- Un usuario ajeno recibe `404`.
- No se exponen `state_json` ni `joined_at`.

### Resultados

- El organizador y el administrador pueden consultar todas las filas.
- Un participante inscrito puede consultar su propia fila incluso antes de completar el desafío.
- Visitantes y usuarios ajenos solo pueden consultar resultados cuando el desafío está `completed`.
- La vista pública incluye exclusivamente filas con `result_status == "confirmed"`.
- La vista pública utiliza alias, estado, métricas, verificación y posición.
- Las métricas públicas se filtran mediante allowlist estricta: `total_population`, `wars_won`, `tech_level`, `influence_max`; otros campos presentes en `result_json` no se serializan.
- No expone `user_id`, `world_id`, `result_json` ni `state_json`.
- Usuarios ajenos autenticados reciben el mismo ranking anonimizado que visitantes anónimos.
- `result_verified` refleja el valor almacenado, no un valor fijo.
- El orden del ranking mantiene el criterio de métrica principal descendente,
  desempates por `tech_level`, `wars_won`, `total_population` descendentes
  y `user_id` ascendente.

## Tests específicos

Archivo agregado:

`tests/test_f7d_api_privacy.py`

Cobertura:

1. Listado público y ocultamiento de desafíos no publicados.
2. Visibilidad del detalle y privacidad del organizador.
3. Autenticación y limitación de filas de participantes.
4. Acceso administrativo basado en el rol persistido.
5. Resultados públicos anónimos y solo confirmados.
6. Acceso de un participante a su resultado antes de completar el desafío.
7. Acceso del participante a su resultado verificado antes de la confirmación pública.
8. Acceso del organizador y administrador a todos los resultados.
9. Filtrado estricto de métricas públicas ante campos internos adicionales en `result_json`.

Validación ejecutada:

- `python -m pytest -q tests/test_f7d_api_privacy.py`
- Validación original antes de ampliar los casos: **7 passed, 1 warning**.
- La suite específica previamente ejecutada obtuvo **8 passed, 1 warning** antes de añadir el caso de allowlist; la nueva versión requiere reejecución local.
- La compilación y `diff --check` deben repetirse tras esta revisión.

La advertencia corresponde a la integración `httpx`/Starlette TestClient y no
representa un fallo de estos tests.

## Limitaciones de la regresión histórica

La ejecución más reciente de la regresión ampliada, excluyendo el archivo que no recopila por importar `_ensure_challenge_participants_constraints`, produjo:

- 794 tests aprobados.
- 92 fallos.
- 8 skipped, 1 xfailed, 1 xpassed y 1 warning.

Los fallos se concentran en incompatibilidades de los tests históricos con
el esquema/modelos presentes en esta rama, entre ellas referencias a
`ChallengeOperationRow.actor_user_id` y fixtures que no proporcionan
`client_idempotency_key`.

Además:

- `test_f7d_constraints_atomicity.py` y `test_f7d_ddl_injection.py` no pudieron
  recopilarse porque importan `_ensure_challenge_participants_constraints`,
  función ausente en la rama.
- `test_f7d_idempotency_http.py` presenta una línea base conocida de 22 fallos
  relacionados con `ChallengeOperationRow.actor_user_id`.

Estos resultados se documentan como limitaciones de compatibilidad de la suite.
No se atribuyen automáticamente a esta corrección de privacidad ni se modifican
servicios, modelos o migraciones para resolverlos dentro de este alcance.

Los registros de ejecución se conservan en:

- `pytest-f7d-baseline.txt`
- `pytest-f7d-regression.txt`

## Archivos de trabajo

- Modificado: `src/worldsim/server/app.py`
- Agregado: `tests/test_f7d_api_privacy.py`
- Agregado: este informe.

Los cambios de esta corrección se registraron en commits de la rama `fix/f7d-challenge-privacy` mediante la API de GitHub. No se realizó merge ni se modificaron otros worktrees.

## Dictamen

La corrección original de privacidad de los cuatro endpoints tuvo cobertura específica
aprobada (7/7); la versión ampliada previa obtuvo 8/8 antes del nuevo caso de allowlist. La revisión amplía la cobertura con un noveno test para la allowlist; la versión actual requiere ejecutar de nuevo la suite específica. La regresión histórica ampliada no está completamente verde y
sus incompatibilidades quedan documentadas, sin ampliar el alcance de esta tarea.

**Estado: allowlist de métricas públicas implementada; suite específica y validaciones estáticas pendientes de reejecución local. Regresión histórica amplia no verde por incompatibilidades documentadas.**

## Revisión adicional — resultados privados por participante

La ruta ya seleccionaba la fila propia del participante antes de filtrar el ranking público; el filtro `confirmed` se aplica únicamente a la vista pública. La prueba anterior no representaba el ciclo real porque creaba un participante `confirmed` dentro de un desafío `running`. Se reemplazó por cobertura de una fila `persisted` sin resultado durante `running` y una fila `verified` en `completed` antes de la confirmación pública. La respuesta privada incluye ahora `result_status`, mientras que la vista pública sigue limitada a `confirmed`.


## Corrección adicional — allowlist de métricas públicas

La serialización del ranking público ya no devuelve el diccionario completo de `result_json`. Proyecta únicamente `total_population`, `wars_won`, `tech_level` e `influence_max`; los resultados privados del participante/organizador/admin mantienen su respuesta independiente. Se añadió un test que inyecta `internal_score` y `private_note` en el JSON persistido y exige que no aparezcan en el ranking. Esta corrección requiere ejecutar de nuevo la suite específica y las comprobaciones estáticas en el worktree local.
