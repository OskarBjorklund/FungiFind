import { FungiFindApiError, type Species } from "./fungifind-api";

export type Bbox = [west: number, south: number, east: number, north: number];

export interface ViewportFeature {
  type: "Feature";
  id: string;
  geometry: {
    type: "Polygon";
    coordinates: number[][][];
  };
  properties: {
    cell_id: string;
    model_version: "production_v1";
    eligibility: "eligible";
    final_index: number;
    habitat_index?: number;
    fruiting_index?: number;
    data_confidence: number;
  };
}

export interface ViewportResponse {
  type: "FeatureCollection";
  features: ViewportFeature[];
  metadata: {
    schema_version: "viewport_geojson_v1";
    model_version: "production_v1";
    config_version: "viewport_overlay_v1";
    species: Species;
    date: string;
    requested_bbox: Bbox;
    coverage_bbox: Bbox;
    grid_crs: "EPSG:3006";
    requested_resolution_m: number;
    actual_resolution_m: number;
    columns: number;
    rows: number;
    grid_cell_count: number;
    feature_count: number;
    eligible_habitat_cell_count: number;
    excluded_cell_count: number;
    no_data_cell_count: number;
    unique_mesan_point_count: number;
    eligibility_policy: "excluded_and_no_data_cells_are_omitted";
    cache_hit: boolean;
    cache_ttl_seconds: number;
  };
}

export type OverlayState = "disabled" | "idle" | "loading" | "ready" | "error";

export interface OverlayStatus {
  state: OverlayState;
  message: string;
  resolutionM?: number;
  gridCellCount?: number;
  featureCount?: number;
  cacheHit?: boolean;
}

export interface OverlayInput {
  enabled: boolean;
  zoom: number;
  bbox: Bbox;
  species: Species;
  date: string;
}

export interface ResolutionBand {
  minimumZoom: number;
  resolutionM: 25 | 50 | 100 | 200;
}

interface OverlayRequest {
  bbox: Bbox;
  species: Species;
  date: string;
  resolution: number;
  signal: AbortSignal;
}

type RequestViewport = (request: OverlayRequest) => Promise<ViewportResponse>;

interface OverlayControllerOptions {
  minimumZoom: number;
  debounceMs: number;
  prefetchFraction: number;
  cacheEntries: number;
  resolutionBands?: readonly ResolutionBand[];
  request?: RequestViewport;
  onData: (data: ViewportResponse | null) => void;
  onStatus: (status: OverlayStatus) => void;
}

interface CacheEntry {
  key: string;
  response: ViewportResponse;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function resolutionForZoom(
  zoom: number,
  bands: readonly ResolutionBand[] = [
    { minimumZoom: 15, resolutionM: 25 },
    { minimumZoom: 14, resolutionM: 50 },
    { minimumZoom: 13, resolutionM: 100 },
    { minimumZoom: 11, resolutionM: 200 },
  ],
): 25 | 50 | 100 | 200 {
  const selected = bands.find((band) => zoom >= band.minimumZoom);
  return selected?.resolutionM ?? 200;
}

export function expandBbox(bbox: Bbox, fraction: number): Bbox {
  const width = bbox[2] - bbox[0];
  const height = bbox[3] - bbox[1];
  return ([
    Math.max(-180, bbox[0] - width * fraction),
    Math.max(-90, bbox[1] - height * fraction),
    Math.min(180, bbox[2] + width * fraction),
    Math.min(90, bbox[3] + height * fraction),
  ] as Bbox).map((value) => Number(value.toFixed(7))) as Bbox;
}

export function bboxContains(container: Bbox, target: Bbox): boolean {
  return (
    container[0] <= target[0] &&
    container[1] <= target[1] &&
    container[2] >= target[2] &&
    container[3] >= target[3]
  );
}

export function normalizeOverlayOpacity(value: number): number {
  if (!Number.isFinite(value)) return 0.62;
  return Math.min(0.9, Math.max(0.15, value));
}

function responseKey(response: ViewportResponse): string {
  const metadata = response.metadata;
  return [
    metadata.species,
    metadata.date,
    metadata.requested_resolution_m,
    ...metadata.coverage_bbox.map((value) => value.toFixed(6)),
    metadata.model_version,
    metadata.config_version,
  ].join("|");
}

export class ViewportLruCache {
  private readonly maxEntries: number;
  private entries: CacheEntry[] = [];

  constructor(maxEntries = 12) {
    if (!Number.isInteger(maxEntries) || maxEntries < 1) {
      throw new Error("maxEntries must be a positive integer");
    }
    this.maxEntries = maxEntries;
  }

  find(
    species: Species,
    date: string,
    requestedResolutionM: number,
    bbox: Bbox,
  ): ViewportResponse | null {
    for (let index = this.entries.length - 1; index >= 0; index -= 1) {
      const entry = this.entries[index];
      const metadata = entry.response.metadata;
      if (
        metadata.species !== species ||
        metadata.date !== date ||
        metadata.requested_resolution_m !== requestedResolutionM ||
        !bboxContains(metadata.coverage_bbox, bbox)
      ) {
        continue;
      }
      this.entries.splice(index, 1);
      this.entries.push(entry);
      return entry.response;
    }
    return null;
  }

  set(response: ViewportResponse): void {
    const key = responseKey(response);
    this.entries = this.entries.filter((entry) => entry.key !== key);
    this.entries.push({ key, response });
    while (this.entries.length > this.maxEntries) this.entries.shift();
  }

  clear(): void {
    this.entries = [];
  }
}

export function isViewportResponse(value: unknown): value is ViewportResponse {
  if (!isObject(value) || value.type !== "FeatureCollection") return false;
  if (!Array.isArray(value.features) || !isObject(value.metadata)) return false;
  return (
    value.metadata.schema_version === "viewport_geojson_v1" &&
    value.metadata.model_version === "production_v1" &&
    typeof value.metadata.species === "string" &&
    typeof value.metadata.date === "string" &&
    Array.isArray(value.metadata.coverage_bbox) &&
    typeof value.metadata.actual_resolution_m === "number" &&
    typeof value.metadata.grid_cell_count === "number"
  );
}

export async function requestViewport({
  bbox,
  species,
  date,
  resolution,
  signal,
}: OverlayRequest): Promise<ViewportResponse> {
  const baseUrl =
    process.env.NEXT_PUBLIC_FUNGIFIND_API_URL ?? "http://localhost:8000";
  const url = new URL("/api/viewport", baseUrl);
  url.searchParams.set("west", bbox[0].toString());
  url.searchParams.set("south", bbox[1].toString());
  url.searchParams.set("east", bbox[2].toString());
  url.searchParams.set("north", bbox[3].toString());
  url.searchParams.set("species", species);
  url.searchParams.set("date", date);
  url.searchParams.set("resolution_m", resolution.toString());

  const response = await fetch(url, {
    method: "GET",
    headers: { Accept: "application/geo+json, application/json" },
    signal,
  });
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const error =
      isObject(payload) && isObject(payload.error) ? payload.error : null;
    throw new FungiFindApiError(
      error && typeof error.code === "string" ? error.code : "viewport_failed",
      error && typeof error.message === "string"
        ? error.message
        : "Kartlagret kunde inte hämtas.",
      response.status,
    );
  }
  if (!isViewportResponse(payload)) {
    throw new FungiFindApiError(
      "invalid_viewport_response",
      "Kartlagret returnerade ett oväntat svar.",
      response.status,
    );
  }
  return payload;
}

export class ViewportOverlayController {
  private readonly options: OverlayControllerOptions;
  private readonly cache: ViewportLruCache;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private controller: AbortController | null = null;
  private generation = 0;

  constructor(options: OverlayControllerOptions) {
    this.options = options;
    this.cache = new ViewportLruCache(options.cacheEntries);
  }

  private cancelPending(): number {
    this.generation += 1;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.controller?.abort();
    this.controller = null;
    return this.generation;
  }

  cancelForMovement(): void {
    this.cancelPending();
    this.options.onData(null);
    this.options.onStatus({ state: "idle", message: "Kartan flyttas…" });
  }

  update(input: OverlayInput): void {
    const generation = this.cancelPending();
    if (!input.enabled) {
      this.options.onData(null);
      this.options.onStatus({ state: "disabled", message: "Områdeslagret är avstängt." });
      return;
    }
    if (input.zoom < this.options.minimumZoom) {
      this.options.onData(null);
      this.options.onStatus({
        state: "disabled",
        message: `Zooma in till nivå ${this.options.minimumZoom} för områdesindex.`,
      });
      return;
    }

    const resolution = resolutionForZoom(
      input.zoom,
      this.options.resolutionBands,
    );
    const expanded = expandBbox(input.bbox, this.options.prefetchFraction);
    const cached = this.cache.find(input.species, input.date, resolution, expanded);
    if (cached) {
      this.options.onData(cached);
      this.options.onStatus({
        state: "ready",
        message: "Områdesindex från lokal cache.",
        resolutionM: cached.metadata.actual_resolution_m,
        gridCellCount: cached.metadata.grid_cell_count,
        featureCount: cached.metadata.feature_count,
        cacheHit: true,
      });
      return;
    }

    this.options.onData(null);
    this.options.onStatus({
      state: "loading",
      message: `Beräknar områdesindex i ${resolution} m-celler…`,
      resolutionM: resolution,
    });
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.load(
        {
          bbox: expanded,
          species: input.species,
          date: input.date,
          resolution,
        },
        generation,
      );
    }, this.options.debounceMs);
  }

  private async load(
    request: Omit<OverlayRequest, "signal">,
    generation: number,
  ): Promise<void> {
    const controller = new AbortController();
    this.controller = controller;
    try {
      const response = await (this.options.request ?? requestViewport)({
        ...request,
        signal: controller.signal,
      });
      if (controller.signal.aborted || generation !== this.generation) return;
      this.cache.set(response);
      this.options.onData(response);
      this.options.onStatus({
        state: "ready",
        message: "Områdesindex uppdaterat.",
        resolutionM: response.metadata.actual_resolution_m,
        gridCellCount: response.metadata.grid_cell_count,
        featureCount: response.metadata.feature_count,
        cacheHit: response.metadata.cache_hit,
      });
    } catch (error) {
      if (controller.signal.aborted || generation !== this.generation) return;
      this.options.onData(null);
      this.options.onStatus({
        state: "error",
        message:
          error instanceof FungiFindApiError
            ? error.message
            : "Områdeslagret kunde inte uppdateras.",
      });
    } finally {
      if (generation === this.generation) this.controller = null;
    }
  }

  dispose(): void {
    this.cancelPending();
    this.cache.clear();
  }
}
