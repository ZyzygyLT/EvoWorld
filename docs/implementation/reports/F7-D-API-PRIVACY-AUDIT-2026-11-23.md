# F7-D API Privacy Audit — 2026-11-23

## Estado

Corrección de privacidad implementada en la rama `fix/f7d-challenge-privacy` y sometida a validación específica.
El PR #4 fue fusionado en `feature/evoworld-review` el 2026-11-24.

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

Cobertura (8 tests):

1. Listado público y ocultamiento de desafíos no publicados.
2. Visibilidad del detalle y privacidad del organizador.
3. Autenticación y limitación de filas de participantes.
4. Acceso administrativo basado en el rol persistido.
5. Resultados públicos anónimos, solo confirmados y con allowlist estricta de métricas; incluye una regresión que inyecta `internal_score` y `private_note` en el JSON persistido.
6. Acceso de un participante a su resultado antes de completar el desafío.
7. Acceso del participante a su resultado verificado antes de la confirmación pública.
8. Acceso del organizador y administrador a todos los resultados.

Validación ejecutada en HEAD `251d8011a8529e6a5347b03897d3acc173199457`:

- `python -m pytest -q tests/test_f7d_api_privacy.py`: **8 passed, 1 warning**.
- `python -m compileall -q src/worldsim/server/app.py tests/test_f7d_api_privacy.py`: completado sin errores.
- `git diff origin/feature/evoworld-review...HEAD --check`: completado sin errores.

La advertencia corresponde a la integración `httpx`/Starlette TestClient y no
representa un fallo de estos tests.

## Limitaciones de la regresión histórica

La ejecución más reciente de la regresión ampliada en HEAD
`251d8011a8529e6a5347b03897d3acc173199457`, excluyendo
`tests/test_f7d_constraints_atomicity.py` porque no recopila por importar
`_ensure_challenge_participants_constraints`, produjo:

- 793 tests aprobados.
- 93 fallos.
- 8 skipped, 1 xfailed, 1 xpassed y 1 warning.

Los fallos se concentran en incompatibilidades de los tests históricos con
el esquema/modelos presentes en esta rama, entre ellas referencias a
`ChallengeOperationRow.actor_user_id` y fixtures que no proporcionan
`client_idempotency_key`.

Además:

- `test_f7d_constraints_atomicity.py` no pudo recopilarse porque importa
  `_ensure_challenge_participants_constraints`, función ausente en la rama.
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

Los cambios de esta corrección se registraron en commits de la rama `fix/f7d-challenge-privacy` mediante la API de GitHub. El PR #4 se integró mediante merge commit `f80156813dc2027d1bfab0079042ad097dc2271b`. Esta actualización documental se realiza después del merge; no modifica código funcional ni otros worktrees.

## Dictamen

La corrección de privacidad de los cuatro endpoints tiene cobertura específica
aprobada: **8/8 tests**. La suite específica se ejecutó en el HEAD indicado y
obtuvo 8 aprobados y 1 warning. Las comprobaciones estáticas también se
ejecutaron sin errores. La regresión histórica ampliada no está completamente
verde: registró 793 aprobados y 93 fallos, además de los resultados omitidos
indicados; sus incompatibilidades quedan documentadas y no se atribuyen
automáticamente a esta corrección.

**Estado: corrección de privacidad y allowlist implementadas y fusionadas en `feature/evoworld-review` mediante `f80156813dc2027d1bfab0079042ad097dc2271b`; suite específica y comprobaciones estáticas aprobadas. Regresión histórica amplia no verde por incompatibilidades documentadas.**

## Revisión adicional — resultados privados por participante

La ruta ya seleccionaba la fila propia del participante antes de filtrar el ranking público; el filtro `confirmed` se aplica únicamente a la vista pública. La prueba anterior no representaba el ciclo real porque creaba un participante `confirmed` dentro de un desafío `running`. Se reemplazó por cobertura de una fila `persisted` sin resultado durante `running` y una fila `verified` en `completed` antes de la confirmación pública. La respuesta privada incluye ahora `result_status`, mientras que la vista pública sigue limitada a `confirmed`.

## Corrección adicional — allowlist de métricas públicas

La serialización del ranking público ya no devuelve el diccionario completo de `result_json`. Proyecta únicamente `total_population`, `wars_won`, `tech_level` e `influence_max`; los resultados privados del participante/organizador/admin mantienen su respuesta independiente. Se añadió una regresión dentro del test de resultados públicos que inyecta `internal_score` y `private_note` en el JSON persistido y exige que no aparezcan en el ranking. La suite específica y las comprobaciones estáticas quedaron reejecutadas y aprobadas en el HEAD indicado.
