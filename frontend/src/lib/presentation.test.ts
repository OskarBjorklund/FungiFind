import { describe, expect, it } from "vitest";

import { FungiFindApiError, type ScoreResponse } from "./fungifind-api";
import {
  errorMessage,
  formatIndex,
  formatMeasurement,
  formatPercent,
  resultKind,
  scoreSummary,
  todayForDateInput,
} from "./presentation";

describe("presentation helpers", () => {
  it("builds the default date from the local calendar day", () => {
    expect(todayForDateInput(new Date(2026, 7, 28, 0, 30))).toBe("2026-08-28");
  });

  it("renders missing values explicitly", () => {
    expect(formatIndex(null)).toBe("Saknas");
    expect(formatPercent(null)).toBe("Saknas");
    expect(formatMeasurement(null, "mm")).toBe("Saknas");
    expect(scoreSummary(null)).toBe("Underlag saknas");
  });

  it("distinguishes excluded responses from eligible scores", () => {
    const eligible = { eligibility: { status: "eligible" } } as ScoreResponse;
    const excluded = { eligibility: { status: "excluded" } } as ScoreResponse;

    expect(resultKind(null)).toBe("empty");
    expect(resultKind(eligible)).toBe("eligible");
    expect(resultKind(excluded)).toBe("excluded");
  });

  it("translates missing weather into an actionable message", () => {
    const error = new FungiFindApiError(
      "weather_history_incomplete",
      "backend detail",
      503,
    );

    expect(errorMessage(error)).toContain("30-dygnshistorik");
  });
});
