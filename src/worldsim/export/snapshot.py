"""Snapshots independientes, profundamente inmutables y canónicos (DEC13/DEC19).

Un *snapshot* copia el estado de un ``SimEngine`` mediante el contrato público
de checkpoints (``checkpoint_to_dict``) y lo congela en estructuras
profundamente inmutables. Garantías implementadas (Etapa 3 §3):

* **Independencia** (§3.1): la congelación reconstruye cada contenedor; el
  snapshot no retiene referencias mutables al motor ni a los datos de origen,
  y no expone referencias mutables a sus consumidores (solo la vista de solo
  lectura ``state`` y las cadenas/bytes inmutables de serialización).
* **Inmutabilidad profunda** (§3.2): cada ``dict`` se sustituye por un
  ``MappingProxyType`` sobre un dict exclusivo del snapshot; cada
  ``list``/``tuple`` por una ``tupla``; los escalares ya son inmutables. La
  conversión de vuelta a JSON es explícita y está documentada en ``_thaw``.
* **Serialización canónica** (§3.3): UTF-8 con las convenciones exactas de
  ``checkpoint_to_json`` — ``ensure_ascii=False``, ``indent=2``,
  ``separators=(",", ": ")``, ``allow_nan=False`` y el orden de inserción de
  ``checkpoint_to_dict`` — de modo que ``snapshot.to_json()`` es byte a byte
  idéntico a ``checkpoint_to_json(engine)`` para el mismo estado. Claves: solo
  ``str`` exactas. Valores no válidos (tipos ajenos a
  ``bool``/``int``/``float``/``str``/``None``/``dict``/``list``/``tuple``,
  claves no ``str`` y floats no finitos): ``SnapshotError`` con la ruta exacta
  del dato; nunca se descartan ni se convierten en silencio.
* **Consistencia** (§3.4): la copia se obtiene en una única llamada síncrona a
  ``checkpoint_to_dict``. Ejecutada en el hilo propietario del motor (el
  contrato de un solo hilo del event loop que ya usa el servidor), ninguna
  mutación puede intercalar con la copia y no se requiere ningún bloqueo.
  Opcionalmente se puede pasar un ``lock`` (p. ej. el patrón de lock por mundo
  de ``PersistenceService``): entonces se sostiene SOLO durante la copia y se
  libera antes de congelar y serializar.

Límite documentado (§3.4): el motor no expone un lock propio; la consistencia
depende de capturar en el hilo propietario o de un lock proporcionado por el
llamador. No se introduce sincronización nueva ni se modifica el motor, el
runner, la persistencia o la recuperación.

El módulo no importa SQLAlchemy ni ``worldsim.server`` y no realiza
operaciones SQLite: la captura es puro cómputo en memoria (§4, §7.8).
"""

from __future__ import annotations

import json
import math
import types
from dataclasses import dataclass
from typing import Any, ContextManager, Mapping

from worldsim.export.checkpoint import checkpoint_to_dict


class SnapshotError(ValueError):
    """Dato inválido o no serializable detectado durante la congelación.

    Subclase de ``ValueError``, consistente con las señales de
    ``checkpoint.py``. El mensaje siempre incluye la ruta exacta del dato
    rechazado (``$.kingdoms[0].name``, ``$.a.b[0]``...): el dato se rechaza
    de forma explícita, nunca se descarta ni se convierte en silencio (§3.3).
    """


def _fmt_path(parent: str, key: Any) -> str:
    """Ruta legible de un nodo. Solo se construye por contenedor o al fallar."""
    if key is None:
        return parent
    if type(key) is int:
        return parent + "[" + str(key) + "]"
    return parent + "." + str(key)


def _freeze(value: Any, parent: str, key: Any) -> Any:
    """Congela ``value`` reconstruyendo cada contenedor (§3.1, §3.2).

    * Escalares de tipo exacto ``bool``/``int``/``float``/``str``/``None``:
      se conservan tal cual (ya inmutables); los ``float`` deben ser finitos.
    * ``dict`` (o ``MappingProxyType``): nuevo dict exclusivo envuelto en
      ``MappingProxyType``; las claves deben ser ``str`` exactas.
    * ``list``/``tuple``: nueva ``tupla`` con cada elemento congelado.
    * Cualquier otro tipo: ``SnapshotError`` con la ruta (rechazo explícito).

    Ruta rápida (Etapa 3-R1): si TODOS los elementos son escalares exactos
    (``None``/``bool``/``int``/``str``/``float`` finito), la validación se hace
    en un único barrido y el contenedor se copia con ``tuple(value)`` a nivel C,
    sin lista intermedia ni recursión. Esto elimina el segundo recorrido y las
    ~45.000 asignaciones intermedias del caso real (payloads ``.tolist()`` de
    los ndarray) sin relajar ninguna garantía:

    * sigue exigiendo tipos **exactos** (un ``np.float64`` no supera la ruta
      rápida: cae al camino lento y se rechaza con su ruta);
    * sigue rechazando floats no finitos con su ruta: la ruta rápida se aborta
      y el camino lento vuelve a recorrer y lanza ``SnapshotError`` con el
      índice exacto (doble recorrido SOLO en el caso de error);
    * ``tuple(value)`` sobre una ``list`` crea una tupla nueva, así que la
      independencia se conserva; los elementos compartidos son escalares
      inmutables por definición.

    Al reconstruir cada contenedor, el resultado no comparte NINGUNA
    referencia mutable con la entrada: por eso un snapshot posteriormente
    aislado de mutaciones del motor y de los datos de origen (§3.1).
    """
    t = type(value)
    if value is None or t is bool or t is int or t is str:
        return value
    if t is float:
        if not math.isfinite(value):
            raise SnapshotError(
                f"float no finito en {_fmt_path(parent, key)}: {value!r}"
            )
        return value
    if t is dict or t is types.MappingProxyType:
        path = _fmt_path(parent, key)
        out: dict[str, Any] = {}
        for k, v in value.items():
            if type(k) is not str:
                raise SnapshotError(
                    f"clave no str en {path}: {k!r} ({type(k).__name__})"
                )
            out[k] = _freeze(v, path, k)
        return types.MappingProxyType(out)
    if t is list or t is tuple:
        # Ruta rápida: un solo barrido de validación + copia en C.
        # Los floats se comprueban primero (son el tipo mayoritario del
        # estado) y ``math.isfinite`` se enlaza a local para el bucle.
        isfinite = math.isfinite
        for v in value:
            vt = type(v)
            if vt is float:
                if not isfinite(v):
                    break
            elif vt is not int and vt is not str and vt is not bool and v is not None:
                break
        else:
            return tuple(value)

        # Camino lento: contiene contenedores, tipos ajenos o un float no
        # finito. Aquí se construye la ruta y se rechaza con precisión.
        path = _fmt_path(parent, key)
        items: list[Any] = []
        for i, v in enumerate(value):
            vt = type(v)
            if v is None or vt is bool or vt is int or vt is str:
                items.append(v)
            elif vt is float:
                if not math.isfinite(v):
                    raise SnapshotError(f"float no finito en {path}[{i}]: {v!r}")
                items.append(v)
            else:
                items.append(_freeze(v, path, i))
        return tuple(items)
    raise SnapshotError(
        f"tipo no serializable en {_fmt_path(parent, key)}: {t.__name__}"
    )


def freeze_state(state: Any) -> Mapping[str, Any]:
    """Congela una estructura de checkpoint en su forma inmutable (§3.2).

    Entrada: la representación plana producida por ``checkpoint_to_dict``
    (``dict``/``list``/``str``/``int``/``float``/``bool``/``None`` con claves
    ``str``). Salida: árbol profundamente inmutable
    (``MappingProxyType`` + ``tupla``) apto para :class:`Snapshot`.

    Cualquier dato inválido o no serializable se rechaza con
    :class:`SnapshotError` indicando la ruta (§3.3).
    """
    return _freeze(state, "$", None)


@dataclass(frozen=True, repr=False)
class Snapshot:
    """Captura independiente y profundamente inmutable del estado del motor.

    No se puede construir con datos mutables: ``state`` debe ser el árbol
    congelado producido por :func:`freeze_state`/:func:`snapshot_from_state`
    (guardia en ``__post_init__``). Los consumidores solo obtienen la vista
    de solo lectura ``state`` y las cadenas/bytes de ``to_json()``/
    ``to_bytes()`` (§3.1); ninguna referencia mutable se expone.
    """

    state: Mapping[str, Any]

    def __post_init__(self) -> None:
        if type(self.state) is not types.MappingProxyType:
            raise SnapshotError(
                "Snapshot.state debe ser una estructura congelada "
                "(MappingProxyType); use snapshot_from_state() o "
                "capture_snapshot() para construir snapshots"
            )

    def __repr__(self) -> str:
        # Sin volcado del estado completo: puede superar el MB.
        return (
            f"Snapshot(month={self.state.get('month')!r}, "
            f"checkpoint_version={self.state.get('_checkpoint_version')!r})"
        )

    def to_json(self) -> str:
        """JSON canónico UTF-8 (convenciones idénticas a ``checkpoint_to_json``)."""
        return snapshot_to_json(self)

    def to_bytes(self) -> bytes:
        """``to_json()`` codificado en UTF-8 (bytes inmutables)."""
        return snapshot_to_bytes(self)


def snapshot_from_state(state: Any) -> Snapshot:
    """Construye un :class:`Snapshot` congelando ``state`` (dict de checkpoint)."""
    if type(state) is not dict:
        raise SnapshotError(
            f"estado de snapshot inválido: se esperaba dict, "
            f"se obtuvo {type(state).__name__}"
        )
    return Snapshot(freeze_state(state))


def capture_snapshot(
    engine: Any, *, lock: ContextManager[Any] | None = None
) -> Snapshot:
    """Captura un snapshot coherente de ``engine`` (§3.1-§3.4).

    Fase 1 (copia consistente): una única llamada síncrona a
    ``checkpoint_to_dict`` —el contrato público de checkpoints— que produce
    una copia plana e independiente del estado. Si se proporciona ``lock``
    (p. ej. un ``threading.Lock`` por mundo como el patrón de
    ``PersistenceService``), se sostiene SOLO durante esta copia y se libera
    antes de congelar y serializar. Sin ``lock``, la consistencia se apoya en
    ejecutar la captura en el hilo propietario del motor (un solo hilo, sin
    puntos de ``await`` durante la copia), que es el contrato existente.

    Fase 2: congelación profunda (:func:`snapshot_from_state`).

    No realiza operaciones SQLite ni escrituras de persistencia (§4): este
    módulo no importa SQLAlchemy ni ``worldsim.server``.
    """
    if lock is None:
        state = checkpoint_to_dict(engine)
    else:
        with lock:
            state = checkpoint_to_dict(engine)
    return snapshot_from_state(state)


def _thaw(value: Any) -> Any:
    """Convierte el árbol congelado a la representación plana para JSON (§3.2).

    Estrategia explícita de conversión a JSON: ``MappingProxyType`` -> nuevo
    ``dict``, ``tupla`` -> ``list``, escalares tal cual. Los contenedores
    resultantes son locales a la llamada: no se expone ninguna referencia
    mutable interna del snapshot.
    """
    if isinstance(value, types.MappingProxyType):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


def snapshot_to_json(snapshot: Snapshot) -> str:
    """Serializa ``snapshot`` a JSON canónico UTF-8 determinista (§3.3).

    Convenciones — idénticas a ``checkpoint_to_json``, para que el snapshot
    sea compatible byte a byte con el contrato de checkpoints de la Fase
    2.2.2:

    * UTF-8 con ``ensure_ascii=False`` (los no ASCII viajan tal cual);
    * ``indent=2`` con ``separators=(",", ": ")`` (separadores explícitos,
      equivalentes a los por defecto con ``indent``);
    * ``allow_nan=False`` (los floats no finitos ya fueron rechazados al
      congelar: doble defensa, nunca se emite JSON inválido);
    * orden de claves: el de inserción de ``checkpoint_to_dict``, que se
      conserva durante la congelación y el ``thaw`` (determinista por
      construcción para un mismo estado).

    Serializar el mismo snapshot siempre produce los mismos bytes (§4, §7.4).
    """
    return json.dumps(
        _thaw(snapshot.state),
        ensure_ascii=False,
        indent=2,
        separators=(",", ": "),
        allow_nan=False,
    )


def snapshot_to_bytes(snapshot: Snapshot) -> bytes:
    """``snapshot_to_json`` codificado en UTF-8 (bytes inmutables)."""
    return snapshot_to_json(snapshot).encode("utf-8")
