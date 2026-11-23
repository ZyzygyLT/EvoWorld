"""DEC16 — clasificación explícita de errores HTTP/WS (Etapa 5).

Módulo puro (sin dependencias de FastAPI/Starlette) para que la política
de clasificación sea testeable también con DEC07 cuando FastAPI no está
instalado.

Matriz DEC16:

===============================  ========  ==========================
Situación                        HTTP      Cierre WS (4400 + h-400)
===============================  ========  ==========================
Mundo inexistente                404       4404
Mundo existente ``not_recoverable``
                                 409       4409
Parámetros inválidos             400       4400
Fallo temporal de infraestructura
                                 503       4503
===============================  ========  ==========================

Reglas:

- ``not_recoverable`` es un resultado operativo de ``RecoveryCoordinator``
  por instancia de proceso (memoria), NUNCA un valor de ``WorldRow.status``
  (DEC08/DEC16). Este módulo solo lo LEE; no lo persiste.
- La clasificación distingue ausencia real del mundo (404), mundo
  existente no recuperable (409), validación (400) e infraestructura (503);
  nunca se convierten todos los errores en 404/409/500.
"""

from __future__ import annotations

from typing import Any, Mapping

#: Estados derivados que DEC16 considera "no recuperable" para efectos de
#: clasificación (409). Se mantiene acá para que la política sea una sola
#: fuente; app.py no debe duplicar este conjunto.
NOT_RECOVERABLE_STATUSES = frozenset({"not_recoverable"})


def ws_close_code(status: int) -> int:
    """Cierre WS equivalente a un estado HTTP DEC16: ``4400 + (status-400)``.

    ``404 -> 4404``, ``409 -> 4409``, ``400 -> 4400``, ``503 -> 4503``.
    Estados fuera de la matriz DEC16 se codifican con la misma fórmula.
    """
    return 4400 + (int(status) - 400)


def is_infra_error(exc: BaseException) -> bool:
    """¿``exc`` es un fallo temporal de infraestructura (→ 503)?

    Clasifica por clase, usando duck-typing sobre los nombres de las
    excepciones de la capa de persistencia (``sqlalchemy.exc``) para no
    importar SQLAlchemy en este módulo puro. Cualquier otra excepción NO
    es infraestructura (se conserva y propaga — regla 8).
    """
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    name = type(exc).__name__
    return name in {"OperationalError", "InterfaceError"}


def not_recoverable_reason(coordinator: Any, world_id: int) -> str | None:
    """Motivo ``not_recoverable`` registrado para ``world_id``, o ``None``.

    Lee el registro operativo por instancia del coordinador
    (``coordinator.not_recoverable``). Sin coordinador (p. ej. tests sin
    lifespan) o sin registro → ``None``: no hay resultado que evadir.
    """
    if coordinator is None:
        return None
    table: Mapping[int, Any] | None = getattr(coordinator, "not_recoverable", None)
    if not table:
        return None
    result = table.get(world_id)
    if result is None:
        return None
    reason = getattr(result, "status", None)
    if reason not in NOT_RECOVERABLE_STATUSES:
        # Un registro archivado como otro estado ya no bloquea.
        return None
    detail = getattr(result, "detail", "") or ""
    return str(detail)


def forget_not_recoverable(coordinator: Any, world_id: int) -> None:
    """Descarta el registro operativo tras una eliminación confirmada.

    Permite que un ``world_id`` reutilizado no herede un 409 obsoleto de
    una vida anterior. No toca ``WorldRow.status`` ni los resultados 1A/1B
    (``recovery_results``).
    """
    if coordinator is None:
        return
    table: Mapping[int, Any] | None = getattr(coordinator, "not_recoverable", None)
    if table:
        try:
            table.pop(world_id, None)
        except Exception:  # pragma: no cover - tabla atípica
            pass
