import type { FungiFindApiError, ScoreResponse, Species } from "./fungifind-api";

export const speciesLabels: Record<Species, string> = {
  cantharellus_cibarius: "Kantarell",
  craterellus_tubaeformis: "Trattkantarell",
};

export function todayForDateInput(now = new Date()): string {
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function formatIndex(value: number | null, digits = 2): string {
  return value === null ? "Saknas" : value.toFixed(digits);
}

export function formatPercent(value: number | null): string {
  return value === null ? "Saknas" : `${Math.round(value * 100)} %`;
}

export function formatMeasurement(
  value: number | null,
  unit: string,
  digits = 1,
): string {
  return value === null ? "Saknas" : `${value.toFixed(digits)} ${unit}`;
}

export function scoreTone(value: number | null): "low" | "medium" | "high" | "missing" {
  if (value === null) return "missing";
  if (value < 0.4) return "low";
  if (value < 0.7) return "medium";
  return "high";
}

export function scoreSummary(value: number | null): string {
  if (value === null) return "Underlag saknas";
  if (value < 0.4) return "Låg lämplighet";
  if (value < 0.7) return "Måttlig lämplighet";
  return "Hög lämplighet";
}

export function errorMessage(error: FungiFindApiError): string {
  const messages: Record<string, string> = {
    weather_history_unavailable:
      "Väderhistorik saknas för platsen eller datumet. Välj ett tidigare datum och försök igen.",
    weather_history_incomplete:
      "Datumet saknar komplett 30-dygnshistorik från MESAN. Välj ett tidigare datum.",
    source_data_unavailable:
      "En lokal modellkälla saknas. Kontrollera backendens datakonfiguration.",
    point_outside_data_coverage:
      "Platsen ligger utanför modellens nuvarande datatäckning.",
    invalid_coordinates: "Koordinaten är ogiltig.",
    invalid_species: "Den valda arten stöds inte av modellen.",
  };
  return messages[error.code] ?? error.message;
}

export function resultKind(
  result: ScoreResponse | null,
): "empty" | "eligible" | "excluded" {
  if (!result) return "empty";
  return result.eligibility.status;
}
