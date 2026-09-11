// biome colors matching worldsim.biomes.Biome
export const BIOME_COLORS = {
    OCEAN: [13, 103, 196],
    SWAMP: [68, 158, 53],
    MARSH: [131, 212, 82],
    GRASSLAND: [131, 212, 82],
    DESERT: [255, 218, 90],
    CONIFER_FOREST: [68, 158, 53],
    BROADLEAF_FOREST: [34, 120, 42],
    BADLANDS: [204, 159, 81],
    MOUNTAIN: [185, 192, 162],
    PEAK: [230, 230, 230],
    ICE: [176, 223, 215],
    SMALL_HILLS: [150, 190, 90],
    SHRUBLAND: [150, 190, 90],
    HILL: [150, 190, 90],
};

export const BIOME_NAMES = [
    'OCEAN', 'SWAMP', 'MARSH', 'GRASSLAND', 'DESERT', 'CONIFER_FOREST',
    'BROADLEAF_FOREST', null /*7 unused*/, 'BADLANDS', 'MOUNTAIN', 'PEAK',
    'ICE', null, null, 'SMALL_HILLS', 'SHRUBLAND', 'HILL',
];

export function biomeColor(id) {
    const name = BIOME_NAMES[id];
    if (name && BIOME_COLORS[name]) return BIOME_COLORS[name];
    return [120, 120, 120];
}
