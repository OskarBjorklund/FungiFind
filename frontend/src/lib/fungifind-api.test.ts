import { afterEach, describe, expect, it, vi } from "vitest";

import {
  FungiFindApiError,
  requestPointScore,
  type ScoreResponse,
} from "./fungifind-api";

const validResponse: ScoreResponse = {
  location: { latitude: 59.16, longitude: 18.24 },
  species: "cantharellus_cibarius",
  date: "2026-08-27",
  eligibility: { status: "eligible", exclusions: [] },
  production: {
    model_version: "production_v1",
    habitat_score: 0.8,
    fruiting_score: 0.6,
    final_score: 0.72,
    confidence: 0.9,
  },
  experimental: {
    label: "experimental_not_production",
    model_version: "fruiting_v2_experiment_v1",
    status: "experimental_complete",
    fruiting_score_v2: 0.7,
    final_score_v2: 0.75,
  },
  moisture: {
    estimator_version: "current_soil_moisture_heuristic_v1",
    status: "estimated_complete",
    estimated_current_soil_moisture: 0.64,
    confidence: 0.9,
    completeness: 1,
  },
  factors: {
    landcover_class: 111,
    landcover_label: "Tallskog utanför våtmark",
    tree_profile: {
      spruce_fraction: 0.5,
      pine_fraction: 0.3,
      birch_fraction: 0.2,
      other_deciduous_fraction: 0,
    },
    static_wetness_class: 2,
    static_wetness_label: "Frisk mark",
    soil_group: "moraine",
    soil_label: "Sandig morän",
    slope_degrees: 4,
    rain_7d_mm: 20,
    rain_30d_mm: 70,
    temp_mean_7d_c: 15,
    current_moisture: 0.64,
  },
  debug: null,
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("requestPointScore", () => {
  it("sends exactly one coordinate with species and date", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(validResponse), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("NEXT_PUBLIC_FUNGIFIND_API_URL", "http://api.test:8000");

    const response = await requestPointScore({
      coordinate: { latitude: 59.16, longitude: 18.24 },
      species: "cantharellus_cibarius",
      date: "2026-08-27",
      includeDebug: true,
    });

    expect(response.production.final_score).toBe(0.72);
    const requestedUrl = new URL(fetchMock.mock.calls[0][0] as URL);
    expect(requestedUrl.pathname).toBe("/api/score");
    expect(requestedUrl.searchParams.get("latitude")).toBe("59.16");
    expect(requestedUrl.searchParams.get("longitude")).toBe("18.24");
    expect(requestedUrl.searchParams.get("include_debug")).toBe("true");
  });

  it("preserves a machine-readable backend error code", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: {
              code: "weather_history_unavailable",
              message: "Weather unavailable",
              details: [],
            },
          }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    const request = requestPointScore({
      coordinate: { latitude: 59.16, longitude: 18.24 },
      species: "cantharellus_cibarius",
      date: "2026-08-28",
    });

    await expect(request).rejects.toMatchObject({
      name: "FungiFindApiError",
      code: "weather_history_unavailable",
      status: 503,
    } satisfies Partial<FungiFindApiError>);
  });

  it("rejects a successful but malformed response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ production: {} }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(
      requestPointScore({
        coordinate: { latitude: 59.16, longitude: 18.24 },
        species: "cantharellus_cibarius",
        date: "2026-08-27",
      }),
    ).rejects.toMatchObject({ code: "invalid_response" });
  });
});
