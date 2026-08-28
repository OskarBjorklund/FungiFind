"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { FungiMap } from "@/components/fungi-map";
import { ResultPanel } from "@/components/result-panel";
import {
  FungiFindApiError,
  requestPointScore,
  type Coordinate,
  type ScoreResponse,
  type Species,
} from "@/lib/fungifind-api";
import { errorMessage, todayForDateInput } from "@/lib/presentation";
import type { OverlayStatus } from "@/lib/viewport-overlay";

type RequestState = "idle" | "loading" | "success" | "error";

export default function Home() {
  const [coordinate, setCoordinate] = useState<Coordinate | null>(null);
  const [species, setSpecies] = useState<Species>("cantharellus_cibarius");
  const [targetDate, setTargetDate] = useState(() => todayForDateInput());
  const [includeDebug, setIncludeDebug] = useState(false);
  const [overlayEnabled, setOverlayEnabled] = useState(true);
  const [overlayOpacity, setOverlayOpacity] = useState(0.62);
  const [overlayStatus, setOverlayStatus] = useState<OverlayStatus>({
    state: "disabled",
    message: "Zooma in för områdesindex.",
  });
  const [requestState, setRequestState] = useState<RequestState>("idle");
  const [result, setResult] = useState<ScoreResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const requestRef = useRef<AbortController | null>(null);

  useEffect(() => () => requestRef.current?.abort(), []);

  const loadScore = useCallback(
    async (
      nextCoordinate: Coordinate,
      nextSpecies: Species,
      nextDate: string,
      nextDebug: boolean,
    ) => {
      requestRef.current?.abort();
      const controller = new AbortController();
      requestRef.current = controller;
      setRequestState("loading");
      setResult(null);
      setError(null);

      try {
        const response = await requestPointScore({
          coordinate: nextCoordinate,
          species: nextSpecies,
          date: nextDate,
          includeDebug: nextDebug,
          signal: controller.signal,
        });
        setResult(response);
        setRequestState("success");
      } catch (caught) {
        if (controller.signal.aborted) return;
        const message =
          caught instanceof FungiFindApiError
            ? errorMessage(caught)
            : "Backendtjänsten går inte att nå. Kontrollera att FastAPI kör på port 8000.";
        setError(message);
        setRequestState("error");
      }
    },
    [],
  );

  const handlePick = useCallback(
    (nextCoordinate: Coordinate) => {
      setCoordinate(nextCoordinate);
      void loadScore(nextCoordinate, species, targetDate, includeDebug);
    },
    [includeDebug, loadScore, species, targetDate],
  );

  const handleSpeciesChange = (nextSpecies: Species) => {
    setSpecies(nextSpecies);
    if (coordinate) void loadScore(coordinate, nextSpecies, targetDate, includeDebug);
  };

  const handleDateChange = (nextDate: string) => {
    setTargetDate(nextDate);
    if (coordinate && nextDate) {
      void loadScore(coordinate, species, nextDate, includeDebug);
    }
  };

  const handleDebugChange = (nextDebug: boolean) => {
    setIncludeDebug(nextDebug);
    if (coordinate) void loadScore(coordinate, species, targetDate, nextDebug);
  };

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand-mark" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <div>
          <p className="eyebrow">Svensk habitat- och vädermodell</p>
          <h1>FungiFind</h1>
        </div>
        <p className="model-badge">Produktion v1</p>
      </header>

      <section className="workspace" aria-label="Kartbaserad svampbedömning">
        <div className="map-region">
          <FungiMap
            onPick={handlePick}
            selected={coordinate}
            species={species}
            targetDate={targetDate}
            overlayEnabled={overlayEnabled}
            overlayOpacity={overlayOpacity}
            onOverlayStatus={setOverlayStatus}
          />
          <div className="map-instruction">
            <span className="instruction-dot" />
            Klicka i kartan för att bedöma en plats
          </div>
          <section className="overlay-controls" aria-label="Områdesindex">
            <label className="overlay-toggle">
              <input
                type="checkbox"
                checked={overlayEnabled}
                onChange={(event) => setOverlayEnabled(event.target.checked)}
              />
              <span>
                <strong>Områdesindex</strong>
                <small>Produktion v1</small>
              </span>
            </label>
            <p
              className={`overlay-status overlay-status-${overlayStatus.state}`}
              aria-live="polite"
            >
              <span aria-hidden="true" />
              {overlayStatus.state === "ready" && overlayStatus.gridCellCount
                ? `${overlayStatus.featureCount ?? 0} visade av ${overlayStatus.gridCellCount} celler · ${overlayStatus.resolutionM} m`
                : overlayStatus.message}
            </p>
            <label className="opacity-control">
              <span>Opacitet</span>
              <input
                type="range"
                min="0.15"
                max="0.9"
                step="0.05"
                value={overlayOpacity}
                disabled={!overlayEnabled}
                onChange={(event) => setOverlayOpacity(Number(event.target.value))}
              />
              <output>{Math.round(overlayOpacity * 100)}%</output>
            </label>
            <div className="overlay-legend" aria-label="Lämplighetsindex från lägre till högre">
              <span>Lägre</span>
              <i aria-hidden="true" />
              <span>Högre</span>
            </div>
            <p className="overlay-note">Lämplighetsindex, inte sannolikhet.</p>
          </section>
        </div>

        <ResultPanel
          coordinate={coordinate}
          species={species}
          targetDate={targetDate}
          includeDebug={includeDebug}
          state={requestState}
          result={result}
          error={error}
          onSpeciesChange={handleSpeciesChange}
          onDateChange={handleDateChange}
          onDebugChange={handleDebugChange}
        />
      </section>
    </main>
  );
}
