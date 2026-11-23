---
description: Prepara un encargo concreto para la siguiente etapa de EvoWorld (solo lectura y planificación)
---

Prepara el trabajo de la siguiente etapa de EvoWorld a partir de la memoria persistente. Este comando **no autoriza** ninguna etapa ni modifica código: produce un encargo que el usuario debe aprobar.

## Fuentes (en este orden, carga mínima)

1. `docs/implementation/IMPLEMENTATION_STATUS.md` — estado real primero.
2. `docs/architecture/DECISIONS.md` — **solo** las decisiones relacionadas con la etapa candidata.
3. `docs/architecture/CONTRACTS.md` — **solo** las secciones pertinentes.
4. `AGENTS.md` — reglas permanentes.

No cargues informes históricos completos; si necesitas evidencia puntual, usa un agente de solo lectura (Explore/architecture-reviewer) para buscar en `docs/implementation/reports/` y en el código.

## Regla de autorización (obligatoria)

- Que una etapa aparezca como "siguiente" en IMPLEMENTATION_STATUS **NO la autoriza**. La única fuente de autorización es una decisión explícita del usuario.
- Si el usuario no ha autorizado la etapa, termina el encargo con: `"Estado: PENDIENTE DE AUTORIZACIÓN"`.

## Entrada

Argumentos del comando: `$ARGUMENTS` (etapa candidata o detalle opcional; si está vacío, toma la primera candidata de IMPLEMENTATION_STATUS).

## Salida: encargo concreto

Emite un documento breve con:

1. **Etapa y alcance** — qué incluye y, sobre todo, qué **excluye** explícitamente.
2. **Objetivo verificable** — resultado demostrable al terminar.
3. **Contratos y decisiones a tocar** — lista exacta de DECxx y secciones de CONTRACTS.md vinculadas (con su contenido relevante resumido, no copiado íntegro).
4. **Archivos previstos** — rutas candidatas a crear/modificar (inspeccionando el código real con `grep`/`glob`/`read`; no supongas).
5. **Pruebas** — qué suites deben ejecutarse, cuáles no podrán (entorno sin `fastapi`/`httpx`, DEC07) y qué resultados constituirían evidencia de cierre.
6. **Riesgos y no-tocar** — contratos que deben permanecer intactos (ver CONTRACTS.md) y limitaciones conocidas del entorno.
7. **Estado**: `AUTORIZADO` (solo si el usuario ya lo autorizó) o `PENDIENTE DE AUTORIZACIÓN`.

No repitas la historia completa del proyecto: el encargo debe ser autocontenido para trabajar la etapa, sin reconstruir la conversación.
