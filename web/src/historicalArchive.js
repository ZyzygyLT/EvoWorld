export const HISTORICAL_ARCHIVE_FORMAT = 'evoworld-historical-archive';
export const HISTORICAL_ARCHIVE_VERSION = 1;

function isInteger(value) {
    return Number.isInteger(value) && !Number.isNaN(value);
}

function validateMatrix(matrix, width, height, label) {
    if (!Array.isArray(matrix) || matrix.length !== height ||
        matrix.some(row => !Array.isArray(row) || row.length !== width ||
            row.some(value => !Number.isFinite(value)))) {
        throw new Error(`Formato inválido: ${label} no coincide con las dimensiones del mapa.`);
    }
}

function validateWorldState(state, label) {
    if (!state || typeof state !== 'object' || Array.isArray(state)) {
        throw new Error(`Formato inválido: estado ausente en ${label}.`);
    }
    const world = state.world;
    const width = world?.width;
    const height = world?.height;
    if (!isInteger(width) || !isInteger(height) || width < 1 || height < 1 ||
        width > 2048 || height > 1024 || width * height > 2_000_000) {
        throw new Error(`Formato inválido: dimensiones no permitidas en ${label}.`);
    }
    validateMatrix(world.biome, width, height, `${label}.world.biome`);
    validateMatrix(world.rivers, width, height, `${label}.world.rivers`);
    // Legacy snapshots can omit elevation; WorldMap renders a neutral fallback.
    // If the field is present, it must still be a complete finite numeric array.
    if (world.elevation !== undefined && world.elevation !== null &&
        (!Array.isArray(world.elevation) || world.elevation.length !== width * height ||
            world.elevation.some(value => !Number.isFinite(value)))) {
        throw new Error(`Formato inválido: elevación no válida en ${label}.`);
    }

    if (!Array.isArray(state.kingdoms)) {
        throw new Error(`Formato inválido: lista de reinos ausente en ${label}.`);
    }
    for (const [index, kingdom] of state.kingdoms.entries()) {
        if (!kingdom || typeof kingdom.name !== 'string' ||
            !Array.isArray(kingdom.color) || kingdom.color.length !== 3 ||
            kingdom.color.some(value => !Number.isFinite(value) || value < 0 || value > 255) ||
            !Array.isArray(kingdom.sites)) {
            throw new Error(`Formato inválido: reino ${index} en ${label}.`);
        }
        for (const site of kingdom.sites) {
            if (!site || !isInteger(site.x) || !isInteger(site.y) ||
                site.x < 0 || site.x >= width || site.y < 0 || site.y >= height) {
                throw new Error(`Formato inválido: asentamiento fuera del mapa en ${label}.`);
            }
        }
    }

    for (const key of ['beasts', 'wars', 'chronicle']) {
        if (state[key] !== undefined && !Array.isArray(state[key])) {
            throw new Error(`Formato inválido: ${key} no es una lista en ${label}.`);
        }
    }
    for (const beast of state.beasts ?? []) {
        if (!beast || typeof beast.name !== 'string' ||
            !isInteger(beast.x) || !isInteger(beast.y) ||
            beast.x < 0 || beast.x >= width || beast.y < 0 || beast.y >= height ||
            typeof beast.alive !== 'boolean') {
            throw new Error(`Formato inválido: bestia fuera del mapa o incompleta en ${label}.`);
        }
    }
    for (const war of state.wars ?? []) {
        if (!war || typeof war.a !== 'string' || typeof war.b !== 'string' ||
            (war.end_month != null && !isInteger(war.end_month))) {
            throw new Error(`Formato inválido: guerra incompleta en ${label}.`);
        }
    }
    for (const event of state.chronicle ?? []) {
        if (!event || !isInteger(event.month) ||
            typeof event.type !== 'string' || typeof event.text !== 'string') {
            throw new Error(`Formato inválido: evento de crónica incompleto en ${label}.`);
        }
    }
    if (state.influence !== undefined) {
        validateMatrix(state.influence, width, height, `${label}.influence`);
    }
    if (state.month !== undefined && !isInteger(state.month)) {
        throw new Error(`Formato inválido: mes no válido en ${label}.`);
    }
}

export function validateHistoricalArchive(archive) {
    if (!archive || archive.format !== HISTORICAL_ARCHIVE_FORMAT ||
        archive.format_version !== HISTORICAL_ARCHIVE_VERSION ||
        !Array.isArray(archive.sources) || archive.sources.length < 1) {
        throw new Error('El archivo no es un paquete histórico EvoWorld compatible (versión 1).');
    }

    let totalWorlds = 0;
    const worlds = [];
    for (const source of archive.sources) {
        if (!source || typeof source.snapshot_name !== 'string' ||
            !/^[a-f0-9]{64}$/i.test(source.sha256 || '') ||
            !Array.isArray(source.worlds) || source.world_count !== source.worlds.length) {
            throw new Error('El paquete contiene metadatos de snapshot inválidos.');
        }
        for (const record of source.worlds) {
            const label = `${source.snapshot_name} · mundo ${record?.source_world_id}`;
            if (!record || !isInteger(record.source_world_id) ||
                !record.state || typeof record.state !== 'object') {
                throw new Error(`Registro de mundo inválido: ${label}.`);
            }
            validateWorldState(record.state, label);
            worlds.push({
                archiveId: `${source.sha256}:${record.source_world_id}`,
                sourceName: source.snapshot_name,
                sourceHash: source.sha256,
                record,
            });
            totalWorlds += 1;
        }
    }
    if (totalWorlds < 1) {
        throw new Error('El paquete no contiene mundos históricos.');
    }
    return worlds;
}

export function adaptHistoricalWorld(entry) {
    const original = entry.record.state;
    const kingdoms = original.kingdoms.map((kingdom, index) => ({
        ...kingdom,
        id: kingdom.id ?? index,
        sites: kingdom.sites.map(site => ({ ...site })),
    }));
    const wars = (original.wars ?? []).filter(war => war && war.end_month == null);
    const tick = {
        month: original.month ?? entry.record.months_simulated ?? 0,
        kingdoms,
        beasts: original.beasts ?? [],
        wars_ongoing: wars,
        events: original.chronicle ?? [],
    };
    if (Array.isArray(original.influence)) tick.influence = original.influence;

    return {
        state: {
            seed: original.seed ?? entry.record.seed ?? 0,
            world: original.world,
            kingdoms,
        },
        tick,
        mapModes: tick.influence ? ['biome', 'realms', 'political'] : ['biome'],
    };
}
