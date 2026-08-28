function numberFromEnvironment(value: string | undefined, fallback: number): number {
  if (!value) return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export const mapConfig = {
  styleUrl:
    process.env.NEXT_PUBLIC_BASEMAP_STYLE_URL ??
    "https://tiles.openfreemap.org/styles/liberty",
  centerLatitude: numberFromEnvironment(
    process.env.NEXT_PUBLIC_MAP_CENTER_LATITUDE,
    59.160136,
  ),
  centerLongitude: numberFromEnvironment(
    process.env.NEXT_PUBLIC_MAP_CENTER_LONGITUDE,
    18.247348,
  ),
  zoom: numberFromEnvironment(process.env.NEXT_PUBLIC_MAP_ZOOM, 10.5),
} as const;

export const overlayConfig = {
  minimumZoom: numberFromEnvironment(
    process.env.NEXT_PUBLIC_VIEWPORT_MIN_ZOOM,
    11,
  ),
  debounceMs: numberFromEnvironment(
    process.env.NEXT_PUBLIC_VIEWPORT_DEBOUNCE_MS,
    320,
  ),
  prefetchFraction: numberFromEnvironment(
    process.env.NEXT_PUBLIC_VIEWPORT_PREFETCH_FRACTION,
    0.25,
  ),
  cacheEntries: numberFromEnvironment(
    process.env.NEXT_PUBLIC_VIEWPORT_CACHE_ENTRIES,
    12,
  ),
  resolutionBands: [
    { minimumZoom: 15, resolutionM: 25 },
    { minimumZoom: 14, resolutionM: 50 },
    { minimumZoom: 13, resolutionM: 100 },
    { minimumZoom: 11, resolutionM: 200 },
  ],
} as const;
