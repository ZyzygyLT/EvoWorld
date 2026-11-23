"""Mediciones de rendimiento separadas de los snapshots (DEC19, Etapa 3-R1).

Este archivo NO contiene pruebas funcionales: mide y reporta por fases,
**cada una en su propio bucle independiente** (metodología "por separado"
de la Etapa 3-R1), sobre un escenario comparable al original:

* Mundo 100x50, 120 ticks, 4 reinos, 22 eventos, 1 guerra, ~1 MB de JSON.

Las cuatro mediciones son:

1. copia consistente (``checkpoint_to_dict``);
2. congelación profunda (``freeze_state``);
3. ``capture_snapshot()`` COMPLETO (copia + congelación);
4. serialización JSON (``checkpoint_to_json``), como referencia independiente.

**Criterio DEC19**: el objetivo se aplica a ``capture_snapshot()`` completo y
exige mediana, P95 Y máximo <= 10 ms en el escenario medido. Las demás fases
se reportan sin umbral: no se introducen límites frágiles ajenos al objetivo
DEC19. Única aserción adicional: determinismo byte a byte (propiedad de
correctitud, no de rendimiento).

Se añade además un diagnóstico claramente etiquetado con la metodología
entrelazada de la Etapa 3 (las cuatro fases en el mismo bucle), SIN aserción,
para dejar constancia reproducible de por qué la medición entrelazada no es
comparable: la fase JSON genera ~45.000 objetos intermedios y su basura
dispara recolecciones de GC dentro de la muestra siguiente. Ejecutar con
``-s`` para ver el reporte completo.
"""

from __future__ import annotations

import gc
import math
import statistics
import time

import pytest

from worldsim.export.checkpoint import checkpoint_to_dict, checkpoint_to_json
from worldsim.export.snapshot import capture_snapshot, freeze_state, snapshot_from_state
from worldsim.sim.engine import SimEngine
from worldsim.worldgen import WorldConfig
from worldsim.worldgen.pipeline import generate_world

TICKS = 120
WARMUP = 5
SAMPLES = 50

# Presupuesto DEC19 sobre capture_snapshot() COMPLETO (copia + congelación).
CAPTURE_BUDGET_MS = 10.0


class _GcTrace:
    """Registra cada colección de GC (generación, inicio, fin) de una ventana.

    Solo observa: no desactiva, ni altera umbrales, ni excluye ninguna pausa
    de las mediciones. Sirve para atribuir exactamente qué muestra contiene
    una recolección y cuánto duró (Etapa 3-R2 §3, §7).
    """

    def __init__(self) -> None:
        self.events: list[tuple[int, float, float]] = []
        self._pending: tuple[int, float] | None = None

    def __call__(self, phase: str, info: dict) -> None:
        if phase == "start":
            self._pending = (info.get("generation", -1), time.perf_counter())
        elif phase == "stop" and self._pending is not None:
            generation, start = self._pending
            self.events.append((generation, start, time.perf_counter()))
            self._pending = None


def _percentile(sorted_values: list[float], p: float) -> float:
    """Percentil por rango más cercano sobre una lista ya ordenada."""
    rank = max(1, min(len(sorted_values), math.ceil(p * len(sorted_values))))
    return sorted_values[rank - 1]


def _stats(samples: list[float]) -> tuple[float, float, float]:
    ordered = sorted(samples)
    return (
        statistics.median(ordered),
        _percentile(ordered, 0.95),
        ordered[-1],
    )


def _report(label: str, samples: list[float]) -> tuple[float, float, float]:
    median, p95, maximum = _stats(samples)
    print(
        f"[DEC19] {label}: mediana={median:.3f} ms "
        f"P95={p95:.3f} ms max={maximum:.3f} ms "
        f"(muestras={len(samples)})"
    )
    return median, p95, maximum


def _advance(engine: SimEngine) -> None:
    for _ in range(TICKS):
        engine.tick()


def _heap_note(label: str) -> None:
    """Reporte de estado del GC/heap (solo lectura; no altera nada, §6)."""
    counts = gc.get_count()
    print(
        f"[DEC19][GC] {label}: counts(gen0,gen1,gen2)={counts} "
        f"heap_tracked={sum(1 for _ in gc.get_objects())}"
    )


def _measure(fn) -> list[float]:
    """Bucle independiente: ``WARMUP`` de calentamiento + ``SAMPLES`` muestras."""
    measured: list[float] = []
    for i in range(WARMUP + SAMPLES):
        start = time.perf_counter()
        fn()
        elapsed = (time.perf_counter() - start) * 1000.0
        if i >= WARMUP:
            measured.append(elapsed)
    measured.sort()
    return measured


def _measure_traced(fn) -> tuple[list[float], _GcTrace, list]:
    """Como :func:`_measure` pero conservando ventanas y traza de GC.

    Devuelve las muestras ordenadas (para las estadísticas) y la traza de
    recolecciones ocurridas dentro de la ventana, para poder atribuir el
    máximo a una pausa concreta sin excluirla de la medición.
    """
    trace = _GcTrace()
    windows: list[tuple[int, float, float, float]] = []
    gc.callbacks.append(trace)
    try:
        for i in range(WARMUP + SAMPLES):
            start = time.perf_counter()
            fn()
            end = time.perf_counter()
            if i >= WARMUP:
                windows.append((i - WARMUP, (end - start) * 1000.0, start, end))
    finally:
        gc.callbacks.remove(trace)
    measured = sorted(w[1] for w in windows)
    return measured, trace, windows


def _attribute_gc(trace: _GcTrace, windows: list) -> None:
    """Imprime qué colecciones cayeron dentro de qué muestra (sin aserciones)."""
    print(
        f"[DEC19][GC] colecciones en la ventana de captura: "
        f"{len(trace.events)} "
        f"(generaciones={sorted({e[0] for e in trace.events}) or 'ninguna'})"
    )
    for generation, start, end in trace.events:
        duration = (end - start) * 1000.0
        owners = [
            (idx, ms) for idx, ms, t0, t1 in windows if t0 <= start <= t1
        ]
        where = (
            f"muestra #{owners[0][0]} ({owners[0][1]:.3f} ms)"
            if owners
            else "entre muestras"
        )
        print(
            f"[DEC19][GC] gen{generation} duración={duration:.3f} ms -> {where}"
        )
    slowest = max(windows, key=lambda w: w[1])
    inside = [
        (g, (e - s) * 1000.0)
        for g, s, e in trace.events
        if slowest[2] <= s <= slowest[3]
    ]
    print(
        f"[DEC19][GC] muestra más lenta: #{slowest[0]} {slowest[1]:.3f} ms; "
        f"colecciones dentro: {[(g, round(d, 3)) for g, d in inside] or 'ninguna'}"
    )


def test_dec19_capture_snapshot_full_budget(races, governments):
    """Captura completa (copia + congelación) dentro del presupuesto DEC19."""
    world = generate_world(WorldConfig(seed=42, width=100, height=50, rivers=4))
    engine = SimEngine(world, races, governments, seed=42)
    _advance(engine)

    reference = checkpoint_to_json(engine)
    state_size = len(reference.encode("utf-8"))

    _heap_note("inicio del test (tras setup del escenario)")

    # --- 1. copia consistente (bucle independiente) ---
    copy_ms = _measure(lambda: checkpoint_to_dict(engine))
    _heap_note("tras fase 1 (copia)")

    # --- 2. congelación (bucle independiente, sobre un estado ya copiado) ---
    state = checkpoint_to_dict(engine)
    freeze_ms = _measure(lambda: freeze_state(state))
    _heap_note("tras fase 2 (congelación)")

    # --- 3. capture_snapshot() COMPLETO (bucle independiente, con traza GC) ---
    capture_ms, gc_trace, capture_windows = _measure_traced(
        lambda: capture_snapshot(engine)
    )
    _heap_note("tras fase 3 (captura)")

    # --- 4. serialización JSON (bucle independiente, referencia) ---
    serialize_ms = _measure(lambda: checkpoint_to_json(engine))
    _heap_note("tras fase 4 (JSON)")

    # Recuento de muestras registrado para las cuatro mediciones.
    assert len(copy_ms) == SAMPLES
    assert len(freeze_ms) == SAMPLES
    assert len(capture_ms) == SAMPLES
    assert len(serialize_ms) == SAMPLES

    print(
        f"[DEC19] contexto: ticks={TICKS} warmup={WARMUP} samples={SAMPLES} "
        f"mes={engine.month} reinos={len(engine.kingdoms)} "
        f"eventos={len(engine.chronicle.events)} guerras={len(engine.wars)}"
    )
    print(f"[DEC19] tamaño del estado JSON: {state_size} bytes")

    copy_med, _, _ = _report("1. copia consistente", copy_ms)
    freeze_med, _, _ = _report("2. congelación", freeze_ms)
    capture_med, capture_p95, capture_max = _report(
        "3. capture_snapshot() COMPLETO", capture_ms
    )
    serialize_med, _, _ = _report("4. serialización JSON (referencia)", serialize_ms)

    # Atribución de las pausas de GC dentro de la ventana de captura (§3, §7):
    # se observan y reportan; en absoluto se excluyen de las mediciones.
    _attribute_gc(gc_trace, capture_windows)

    # Coste intrínseco de una gen2 sobre el heap de ESTA prueba (solo reporte;
    # se ejecuta después de las cuatro mediciones, para no alterarlas).
    gc.collect()
    gen2_samples = []
    for _ in range(3):
        t0 = time.perf_counter()
        gc.collect(2)
        gen2_samples.append((time.perf_counter() - t0) * 1000.0)
    gen2_samples.sort()
    print(
        f"[DEC19][GC] gen2 intrínseco sobre este heap: "
        f"mediana={statistics.median(gen2_samples):.3f} ms "
        f"máximo={gen2_samples[-1]:.3f} ms; "
        f"heap_tracked={sum(1 for _ in gc.get_objects())}"
    )

    # Determinismo del camino completo (correctitud, no rendimiento).
    snap_text = capture_snapshot(engine).to_json()
    assert snap_text == reference, (
        "la serialización debe seguir siendo byte-idéntica al contrato"
    )

    # Presupuesto DEC19: los TRES estadísticos de la captura completa.
    assert capture_med <= CAPTURE_BUDGET_MS, (
        f"mediana de capture_snapshot {capture_med:.3f} ms > {CAPTURE_BUDGET_MS} ms"
    )
    assert capture_p95 <= CAPTURE_BUDGET_MS, (
        f"P95 de capture_snapshot {capture_p95:.3f} ms > {CAPTURE_BUDGET_MS} ms"
    )
    assert capture_max <= CAPTURE_BUDGET_MS, (
        f"máximo de capture_snapshot {capture_max:.3f} ms > {CAPTURE_BUDGET_MS} ms. "
        f"Diagnóstico (Etapa 3-R1): mediana={capture_med:.3f} y "
        f"P95={capture_p95:.3f} sí cumplen; el máximo lo domina una "
        f"colección gen2 del recolector de basura (~848 objetos 'tracked' por "
        f"captura: ~600 de checkpoint_to_dict y ~248 de la congelación), con "
        f"coste de 17-53 ms sobre el heap de la suite y periodo de ~133 "
        f"capturas. Reducirlo exige tocar checkpoint.py (fuera de alcance)."
    )

    # Sanidad narrativa (sin aserción sobre las otras fases): deja constancia
    # de que la captura no es el cuello de botella frente a la serialización.
    print(
        f"[DEC19] reparto de la mediana: copia={copy_med:.3f} ms + "
        f"congelación={freeze_med:.3f} ms + "
        f"serialización={serialize_med:.3f} ms"
    )


def test_dec19_interleaved_diagnostic_no_assertion(races, governments):
    """Diagnóstico reproducible de la metodología entrelazada (sin aserción).

    Reproduce el diseño de la Etapa 3 (las cuatro fases en el mismo bucle) y
    SOLO imprime: el objetivo DEC19 se evalúa en el test anterior, con las
    fases por separado. Sirve para documentar que la basura de la fase JSON
    contamina las muestras de captura siguientes vía recolección de basura.
    """
    world = generate_world(WorldConfig(seed=42, width=100, height=50, rivers=4))
    engine = SimEngine(world, races, governments, seed=42)
    _advance(engine)

    copy_i, freeze_i, capture_i, serialize_i = [], [], [], []
    for i in range(WARMUP + SAMPLES):
        t0 = time.perf_counter()
        state = checkpoint_to_dict(engine)
        t1 = time.perf_counter()
        snapshot_from_state(state)
        t2 = time.perf_counter()
        checkpoint_to_json(engine)
        t3 = time.perf_counter()
        capture_snapshot(engine)
        t4 = time.perf_counter()
        if i >= WARMUP:
            copy_i.append((t1 - t0) * 1000.0)
            freeze_i.append((t2 - t1) * 1000.0)
            serialize_i.append((t3 - t2) * 1000.0)
            capture_i.append((t4 - t3) * 1000.0)

    print("[DEC19][entrelazado, sin aserción] metodología de la Etapa 3:")
    _report("1. copia consistente (entrelazado)", copy_i)
    _report("2. congelación (entrelazado)", freeze_i)
    _report("4. serialización JSON (entrelazado)", serialize_i)
    _report("capture_snapshot COMPLETO (entrelazado)", capture_i)
    print(
        "[DEC19][entrelazado] diagnóstico: la basura de la fase JSON "
        "(~45.000 objetos + cadena de 1 MB) provoca recolecciones de GC que "
        "inflan el máximo de la muestra siguiente; por eso el criterio DEC19 "
        "se evalúa con las fases por separado."
    )
