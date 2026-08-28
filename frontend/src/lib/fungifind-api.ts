export type Species =
  | "cantharellus_cibarius"
  | "craterellus_tubaeformis";

export interface Coordinate {
  latitude: number;
  longitude: number;
}

export interface Exclusion {
  code: string;
  label: string;
  source_feature: string;
}

export interface ScoreResponse {
  location: Coordinate;
  species: Species;
  date: string;
  eligibility: {
    status: "eligible" | "excluded";
    exclusions: Exclusion[];
  };
  production: {
    model_version: "production_v1";
    habitat_score: number | null;
    fruiting_score: number | null;
    final_score: number | null;
    confidence: number;
  };
  experimental: {
    label: "experimental_not_production";
    model_version: "fruiting_v2_experiment_v1";
    status: string | null;
    fruiting_score_v2: number | null;
    final_score_v2: number | null;
  };
  moisture: {
    estimator_version: "current_soil_moisture_heuristic_v1";
    status: string | null;
    estimated_current_soil_moisture: number | null;
    confidence: number | null;
    completeness: number | null;
  };
  factors: {
    landcover_class: number | null;
    landcover_label: string | null;
    tree_profile: {
      spruce_fraction: number | null;
      pine_fraction: number | null;
      birch_fraction: number | null;
      other_deciduous_fraction: number | null;
    };
    static_wetness_class: number | null;
    static_wetness_label: string | null;
    soil_group: string | null;
    soil_label: string | null;
    slope_degrees: number | null;
    rain_7d_mm: number | null;
    rain_30d_mm: number | null;
    temp_mean_7d_c: number | null;
    current_moisture: number | null;
  };
  debug: {
    missing_features: string[];
    weather_completeness: Record<string, string>;
    moisture_missing_inputs: string[];
    fruiting_v2_missing_inputs: string[];
    feature_provenance: Record<
      string,
      {
        source_name: string;
        semantic_status: string;
        quality: number;
        is_mock: boolean;
        is_nodata: boolean;
        raw_value: number | null;
        interpreted_value: number | null;
      }
    >;
  } | null;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details: Array<{ field: string | null; message: string }>;
  };
}

export class FungiFindApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = "FungiFindApiError";
    this.code = code;
    this.status = status;
  }
}

interface ScoreRequest {
  coordinate: Coordinate;
  species: Species;
  date: string;
  includeDebug?: boolean;
  signal?: AbortSignal;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function isScoreResponse(value: unknown): value is ScoreResponse {
  if (!isObject(value) || !isObject(value.location)) return false;
  if (!isObject(value.eligibility) || !isObject(value.production)) return false;
  if (!isObject(value.experimental) || !isObject(value.moisture)) return false;
  if (!isObject(value.factors)) return false;
  return (
    typeof value.location.latitude === "number" &&
    typeof value.location.longitude === "number" &&
    typeof value.species === "string" &&
    typeof value.date === "string" &&
    (value.eligibility.status === "eligible" ||
      value.eligibility.status === "excluded")
  );
}

function parseError(value: unknown): ApiErrorBody["error"] | null {
  if (!isObject(value) || !isObject(value.error)) return null;
  if (typeof value.error.code !== "string" || typeof value.error.message !== "string") {
    return null;
  }
  return {
    code: value.error.code,
    message: value.error.message,
    details: Array.isArray(value.error.details) ? value.error.details : [],
  };
}

export async function requestPointScore({
  coordinate,
  species,
  date,
  includeDebug = false,
  signal,
}: ScoreRequest): Promise<ScoreResponse> {
  const baseUrl =
    process.env.NEXT_PUBLIC_FUNGIFIND_API_URL ?? "http://localhost:8000";
  const url = new URL("/api/score", baseUrl);
  url.searchParams.set("latitude", coordinate.latitude.toString());
  url.searchParams.set("longitude", coordinate.longitude.toString());
  url.searchParams.set("species", species);
  url.searchParams.set("date", date);
  if (includeDebug) url.searchParams.set("include_debug", "true");

  const response = await fetch(url, {
    method: "GET",
    headers: { Accept: "application/json" },
    signal,
  });
  const payload: unknown = await response.json().catch(() => null);

  if (!response.ok) {
    const parsed = parseError(payload);
    throw new FungiFindApiError(
      parsed?.code ?? "request_failed",
      parsed?.message ?? "Modelltjänsten kunde inte svara.",
      response.status,
    );
  }
  if (!isScoreResponse(payload)) {
    throw new FungiFindApiError(
      "invalid_response",
      "Modelltjänsten returnerade ett oväntat svar.",
      response.status,
    );
  }
  return payload;
}
