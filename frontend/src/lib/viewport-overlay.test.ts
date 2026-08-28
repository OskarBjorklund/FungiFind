import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ViewportLruCache,
  ViewportOverlayController,
  bboxContains,
  expandBbox,
  normalizeOverlayOpacity,
  requestViewport,
  resolutionForZoom,
  type Bbox,
  type ViewportResponse,
} from "./viewport-overlay";

function response(
  coverage: Bbox = [17, 58, 19, 60],
  overrides: Partial<ViewportResponse["metadata"]> = {},
): ViewportResponse {
  return {
    type: "FeatureCollection",
    features: [],
    metadata: {
      schema_version: "viewport_geojson_v1",
      model_version: "production_v1",
      config_version: "viewport_overlay_v1",
      species: "cantharellus_cibarius",
      date: "2026-08-27",
      requested_bbox: coverage,
      coverage_bbox: coverage,
      grid_crs: "EPSG:3006",
      requested_resolution_m: 200,
      actual_resolution_m: 200,
      columns: 4,
      rows: 5,
      grid_cell_count: 20,
      feature_count: 0,
      eligible_habitat_cell_count: 0,
      excluded_cell_count: 20,
      no_data_cell_count: 0,
      unique_mesan_point_count: 0,
      eligibility_policy: "excluded_and_no_data_cells_are_omitted",
      cache_hit: false,
      cache_ttl_seconds: 3600,
      ...overrides,
    },
  };
}

function input(bbox: Bbox = [18, 59, 18.01, 59.01]) {
  return {
    enabled: true,
    zoom: 11.5,
    bbox,
    species: "cantharellus_cibarius" as const,
    date: "2026-08-27",
  };
}

describe("viewport overlay helpers", () => {
  it("selects deterministic zoom resolutions", () => {
    expect(resolutionForZoom(11)).toBe(200);
    expect(resolutionForZoom(12.99)).toBe(200);
    expect(resolutionForZoom(13)).toBe(100);
    expect(resolutionForZoom(14)).toBe(50);
    expect(resolutionForZoom(15)).toBe(25);
    expect(resolutionForZoom(18)).toBe(25);
  });

  it("expands bounds symmetrically and detects containment", () => {
    const expanded = expandBbox([10, 20, 12, 24], 0.25);
    expect(expanded).toEqual([9.5, 19, 12.5, 25]);
    expect(bboxContains(expanded, [10, 20, 12, 24])).toBe(true);
    expect(bboxContains([10, 20, 12, 24], expanded)).toBe(false);
  });

  it("clamps overlay opacity to the UI range", () => {
    expect(normalizeOverlayOpacity(-1)).toBe(0.15);
    expect(normalizeOverlayOpacity(0.55)).toBe(0.55);
    expect(normalizeOverlayOpacity(2)).toBe(0.9);
  });
});

describe("ViewportLruCache", () => {
  it("requires matching species, date and requested resolution", () => {
    const cache = new ViewportLruCache(2);
    cache.set(response());

    expect(
      cache.find("cantharellus_cibarius", "2026-08-27", 200, [18, 59, 18.1, 59.1]),
    ).not.toBeNull();
    expect(
      cache.find("craterellus_tubaeformis", "2026-08-27", 200, [18, 59, 18.1, 59.1]),
    ).toBeNull();
    expect(
      cache.find("cantharellus_cibarius", "2026-08-26", 200, [18, 59, 18.1, 59.1]),
    ).toBeNull();
    expect(
      cache.find("cantharellus_cibarius", "2026-08-27", 100, [18, 59, 18.1, 59.1]),
    ).toBeNull();
  });

  it("evicts the least recently used coverage", () => {
    const cache = new ViewportLruCache(2);
    cache.set(response([10, 10, 11, 11]));
    cache.set(response([20, 20, 21, 21]));
    cache.find("cantharellus_cibarius", "2026-08-27", 200, [10.2, 10.2, 10.8, 10.8]);
    cache.set(response([30, 30, 31, 31]));

    expect(
      cache.find("cantharellus_cibarius", "2026-08-27", 200, [20.2, 20.2, 20.8, 20.8]),
    ).toBeNull();
    expect(
      cache.find("cantharellus_cibarius", "2026-08-27", 200, [10.2, 10.2, 10.8, 10.8]),
    ).not.toBeNull();
  });
});

describe("ViewportOverlayController", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("does not request or render below the configured zoom", () => {
    const request = vi.fn();
    const onData = vi.fn();
    const onStatus = vi.fn();
    const controller = new ViewportOverlayController({
      minimumZoom: 11,
      debounceMs: 300,
      prefetchFraction: 0.25,
      cacheEntries: 4,
      request,
      onData,
      onStatus,
    });

    controller.update({ ...input(), zoom: 10.99 });
    vi.runAllTimers();

    expect(request).not.toHaveBeenCalled();
    expect(onData).toHaveBeenLastCalledWith(null);
    expect(onStatus).toHaveBeenLastCalledWith(
      expect.objectContaining({ state: "disabled" }),
    );
  });

  it("toggle-off cancels requests and toggle-on schedules a new load", async () => {
    const request = vi.fn().mockResolvedValue(response());
    const onStatus = vi.fn();
    const controller = new ViewportOverlayController({
      minimumZoom: 11,
      debounceMs: 10,
      prefetchFraction: 0.25,
      cacheEntries: 4,
      request,
      onData: vi.fn(),
      onStatus,
    });

    controller.update({ ...input(), enabled: false });
    await vi.runAllTimersAsync();
    expect(request).not.toHaveBeenCalled();
    expect(onStatus).toHaveBeenLastCalledWith(
      expect.objectContaining({ state: "disabled" }),
    );

    controller.update(input());
    await vi.runAllTimersAsync();
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("debounces rapid viewport changes into one request", async () => {
    const request = vi.fn().mockResolvedValue(response());
    const onData = vi.fn();
    const controller = new ViewportOverlayController({
      minimumZoom: 11,
      debounceMs: 320,
      prefetchFraction: 0.25,
      cacheEntries: 4,
      request,
      onData,
      onStatus: vi.fn(),
    });

    controller.update(input([18, 59, 18.01, 59.01]));
    await vi.advanceTimersByTimeAsync(150);
    controller.update(input([18.01, 59.01, 18.02, 59.02]));
    await vi.advanceTimersByTimeAsync(320);

    expect(request).toHaveBeenCalledTimes(1);
    expect(request.mock.calls[0][0].bbox).toEqual([
      18.0075, 59.0075, 18.0225, 59.0225,
    ]);
    expect(onData).toHaveBeenLastCalledWith(response());
  });

  it("aborts and generation-guards stale responses", async () => {
    const pending: Array<{
      resolve: (value: ViewportResponse) => void;
      signal: AbortSignal;
    }> = [];
    const request = vi.fn(({ signal }: { signal: AbortSignal }) =>
      new Promise<ViewportResponse>((resolve) => pending.push({ resolve, signal })),
    );
    const onData = vi.fn();
    const controller = new ViewportOverlayController({
      minimumZoom: 11,
      debounceMs: 0,
      prefetchFraction: 0,
      cacheEntries: 4,
      request,
      onData,
      onStatus: vi.fn(),
    });

    controller.update(input([18, 59, 18.01, 59.01]));
    await vi.runOnlyPendingTimersAsync();
    controller.update(input([18.02, 59.02, 18.03, 59.03]));
    await vi.runOnlyPendingTimersAsync();
    expect(pending[0].signal.aborted).toBe(true);

    const fresh = response([18.02, 59.02, 18.03, 59.03]);
    pending[1].resolve(fresh);
    await Promise.resolve();
    await Promise.resolve();
    pending[0].resolve(response([18, 59, 18.01, 59.01]));
    await Promise.resolve();

    expect(onData).toHaveBeenLastCalledWith(fresh);
    expect(onData).not.toHaveBeenLastCalledWith(
      response([18, 59, 18.01, 59.01]),
    );
  });

  it("uses cached coverage when panning back inside prefetched bounds", async () => {
    const cached = response([9, 9, 12, 12]);
    const request = vi.fn().mockResolvedValue(cached);
    const onData = vi.fn();
    const onStatus = vi.fn();
    const controller = new ViewportOverlayController({
      minimumZoom: 11,
      debounceMs: 10,
      prefetchFraction: 0.25,
      cacheEntries: 4,
      request,
      onData,
      onStatus,
    });

    controller.update(input([10, 10, 11, 11]));
    await vi.runAllTimersAsync();
    controller.update(input([10.2, 10.2, 10.8, 10.8]));

    expect(request).toHaveBeenCalledTimes(1);
    expect(onData).toHaveBeenLastCalledWith(cached);
    expect(onStatus).toHaveBeenLastCalledWith(
      expect.objectContaining({ state: "ready", cacheHit: true }),
    );
  });

  it("reloads when species or date changes", async () => {
    const request = vi.fn((requestInput) =>
      Promise.resolve(
        response([17, 58, 19, 60], {
          species: requestInput.species,
          date: requestInput.date,
        }),
      ),
    );
    const controller = new ViewportOverlayController({
      minimumZoom: 11,
      debounceMs: 10,
      prefetchFraction: 0.25,
      cacheEntries: 4,
      request,
      onData: vi.fn(),
      onStatus: vi.fn(),
    });

    controller.update(input());
    await vi.runAllTimersAsync();
    controller.update({
      ...input(),
      species: "craterellus_tubaeformis",
    });
    await vi.runAllTimersAsync();
    controller.update({
      ...input(),
      species: "craterellus_tubaeformis",
      date: "2026-08-26",
    });
    await vi.runAllTimersAsync();

    expect(request).toHaveBeenCalledTimes(3);
  });

  it("surfaces request errors without keeping stale cells", async () => {
    const onData = vi.fn();
    const onStatus = vi.fn();
    const controller = new ViewportOverlayController({
      minimumZoom: 11,
      debounceMs: 10,
      prefetchFraction: 0.25,
      cacheEntries: 4,
      request: vi.fn().mockRejectedValue(new Error("offline")),
      onData,
      onStatus,
    });

    controller.update(input());
    await vi.runAllTimersAsync();

    expect(onData).toHaveBeenLastCalledWith(null);
    expect(onStatus).toHaveBeenLastCalledWith(
      expect.objectContaining({ state: "error" }),
    );
  });
});

describe("requestViewport", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends explicit MapLibre bounds and resolution_m", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(response()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await requestViewport({
      bbox: [18.1, 59.15, 18.2, 59.22],
      species: "cantharellus_cibarius",
      date: "2026-08-27",
      resolution: 50,
      signal: new AbortController().signal,
    });

    const url = new URL(String(fetchMock.mock.calls[0][0]));
    expect(url.searchParams.get("west")).toBe("18.1");
    expect(url.searchParams.get("south")).toBe("59.15");
    expect(url.searchParams.get("east")).toBe("18.2");
    expect(url.searchParams.get("north")).toBe("59.22");
    expect(url.searchParams.get("resolution_m")).toBe("50");
    expect(url.searchParams.has("bbox")).toBe(false);
  });
});
