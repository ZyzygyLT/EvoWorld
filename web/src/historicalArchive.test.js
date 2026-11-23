import test from 'node:test';
import assert from 'node:assert/strict';
import {
    adaptHistoricalWorld,
    validateHistoricalArchive,
} from './historicalArchive.js';

function makeArchive() {
    const state = {
        month: 120,
        seed: 7,
        world: {
            width: 2,
            height: 2,
            biome: [[3, 3], [4, 4]],
            rivers: [[0, 0], [0, 1]],
            elevation: [10, 20, 30, 40],
        },
        kingdoms: [{
            id: 4,
            name: 'Aster',
            color: [10, 20, 30],
            population: 123,
            sites: [{ name: 'Capital', x: 0, y: 1, is_capital: true }],
        }],
        wars: [{ a: 'Aster', b: 'Boreal', end_month: null }],
        beasts: [{ name: 'Drake', x: 1, y: 1, alive: true }],
        chronicle: [{ id: 1, month: 4, type: 'MILESTONE', text: 'Aster founded' }],
        influence: [[4, -1], [4, -1]],
    };
    return {
        format: 'evoworld-historical-archive',
        format_version: 1,
        sources: [{
            snapshot_name: 'snapshot.db',
            sha256: 'a'.repeat(64),
            world_count: 1,
            worlds: [{
                source_world_id: 1,
                seed: 7,
                months_simulated: 120,
                land_pct: 55,
                created_at: '2026-01-01',
                name: 'Historic',
                state,
            }],
        }],
    };
}

test('validates and adapts a historical package without inventing state', () => {
    const [entry] = validateHistoricalArchive(makeArchive());
    const adapted = adaptHistoricalWorld(entry);
    assert.equal(adapted.tick.month, 120);
    assert.equal(adapted.tick.kingdoms[0].name, 'Aster');
    assert.equal(adapted.tick.beasts[0].name, 'Drake');
    assert.equal(adapted.tick.wars_ongoing.length, 1);
    assert.deepEqual(adapted.mapModes, ['biome', 'realms', 'political']);
    assert.equal(adapted.state.world.width, 2);
});

test('accepts legacy maps without elevation and preserves absence', () => {
    const archive = makeArchive();
    delete archive.sources[0].worlds[0].state.world.elevation;
    const [entry] = validateHistoricalArchive(archive);
    const adapted = adaptHistoricalWorld(entry);
    assert.equal('elevation' in adapted.state.world, false);
});

test('rejects malformed elevation when supplied', () => {
    const archive = makeArchive();
    archive.sources[0].worlds[0].state.world.elevation = [1];
    assert.throws(() => validateHistoricalArchive(archive), /elevación/);
});

test('rejects unsupported package versions', () => {
    const archive = makeArchive();
    archive.format_version = 99;
    assert.throws(() => validateHistoricalArchive(archive), /versión 1/);
});

test('rejects malformed map dimensions', () => {
    const archive = makeArchive();
    archive.sources[0].worlds[0].state.world.biome = [[3]];
    assert.throws(() => validateHistoricalArchive(archive), /biome/);
});

test('rejects a settlement outside the map', () => {
    const archive = makeArchive();
    archive.sources[0].worlds[0].state.kingdoms[0].sites[0].x = 9;
    assert.throws(() => validateHistoricalArchive(archive), /fuera del mapa/);
});

test('rejects malformed chronicle records before rendering', () => {
    const archive = makeArchive();
    archive.sources[0].worlds[0].state.chronicle = [null];
    assert.throws(() => validateHistoricalArchive(archive), /evento de crónica/);
});

test('rejects beasts outside the map', () => {
    const archive = makeArchive();
    archive.sources[0].worlds[0].state.beasts[0].x = 8;
    assert.throws(() => validateHistoricalArchive(archive), /bestia fuera del mapa/);
});
