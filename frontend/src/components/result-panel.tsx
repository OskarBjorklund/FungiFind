import type { ReactNode } from "react";

import type {
  Coordinate,
  ScoreResponse,
  Species,
} from "@/lib/fungifind-api";
import {
  formatIndex,
  formatMeasurement,
  formatPercent,
  scoreSummary,
  scoreTone,
  speciesLabels,
} from "@/lib/presentation";

type RequestState = "idle" | "loading" | "success" | "error";

interface ResultPanelProps {
  coordinate: Coordinate | null;
  species: Species;
  targetDate: string;
  includeDebug: boolean;
  state: RequestState;
  result: ScoreResponse | null;
  error: string | null;
  onSpeciesChange: (species: Species) => void;
  onDateChange: (date: string) => void;
  onDebugChange: (enabled: boolean) => void;
}

function Metric({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="metric-row">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function ScoreCard({ result }: { result: ScoreResponse }) {
  const score = result.production.final_score;
  const tone = scoreTone(score);
  return (
    <>
      <section className={`score-card score-card-${tone}`}>
        <div>
          <p className="section-label">Produktion v1</p>
          <h3>{scoreSummary(score)}</h3>
          <p>{speciesLabels[result.species]} · {result.date}</p>
        </div>
        <div className="score-orb" aria-label={`Slutindex ${formatIndex(score)}`}>
          <strong>{formatIndex(score)}</strong>
          <span>index</span>
        </div>
        <div className="score-bar" aria-hidden="true">
          <span style={{ width: `${(score ?? 0) * 100}%` }} />
        </div>
      </section>

      <section className="result-section">
        <h3>Produktionsbedömning</h3>
        <Metric label="Habitat" value={formatIndex(result.production.habitat_score)} />
        <Metric label="Fruktsättning" value={formatIndex(result.production.fruiting_score)} />
        <Metric label="Underlagets säkerhet" value={formatPercent(result.production.confidence)} />
      </section>

      <section className="moisture-card">
        <div>
          <p className="section-label">Markfuktighet nu</p>
          <h3>{formatIndex(result.moisture.estimated_current_soil_moisture)}</h3>
          <p>Heuristiskt index 0–1</p>
        </div>
        <div className="moisture-meta">
          <span>Säkerhet {formatPercent(result.moisture.confidence)}</span>
          <span>Fullständighet {formatPercent(result.moisture.completeness)}</span>
        </div>
      </section>

      <section className="result-section compact-factors">
        <h3>Viktigaste faktorerna</h3>
        <Metric label="Marktäcke" value={result.factors.landcover_label ?? "Saknas"} />
        <Metric label="Jordart" value={result.factors.soil_label ?? result.factors.soil_group ?? "Saknas"} />
        <Metric label="Statisk markfukt" value={result.factors.static_wetness_label ?? "Saknas"} />
        <Metric label="Regn 7 dygn" value={formatMeasurement(result.factors.rain_7d_mm, "mm")} />
        <Metric label="Regn 30 dygn" value={formatMeasurement(result.factors.rain_30d_mm, "mm")} />
        <Metric label="Medeltemp 7 dygn" value={formatMeasurement(result.factors.temp_mean_7d_c, "°C")} />
      </section>

      <details className="experimental-card">
        <summary>Experimentell v2 · inte produktion</summary>
        <Metric label="Fruktsättning v2" value={formatIndex(result.experimental.fruiting_score_v2)} />
        <Metric label="Slutindex v2" value={formatIndex(result.experimental.final_score_v2)} />
      </details>

      {result.debug && (
        <details className="debug-card">
          <summary>Datakvalitet och provenance</summary>
          <p>{Object.keys(result.debug.feature_provenance).length} sanerade källposter</p>
          <Metric label="Saknade modellfält" value={result.debug.missing_features.length} />
          <Metric label="Saknade fuktfält" value={result.debug.moisture_missing_inputs.length} />
        </details>
      )}
    </>
  );
}

function ExcludedCard({ result }: { result: ScoreResponse }) {
  return (
    <section className="excluded-card">
      <div className="excluded-icon" aria-hidden="true">×</div>
      <p className="section-label">Ingen poäng beräknad</p>
      <h3>Platsen är exkluderad</h3>
      <p>Modellen returnerar medvetet inga produktionspoäng för den här marktypen.</p>
      <ul>
        {result.eligibility.exclusions.map((item) => (
          <li key={`${item.code}-${item.source_feature}`}>{item.label}</li>
        ))}
      </ul>
    </section>
  );
}

export function ResultPanel({
  coordinate,
  species,
  targetDate,
  includeDebug,
  state,
  result,
  error,
  onSpeciesChange,
  onDateChange,
  onDebugChange,
}: ResultPanelProps) {
  return (
    <aside className="result-panel" aria-live="polite" aria-busy={state === "loading"}>
      <div className="panel-heading">
        <p className="eyebrow">Punktanalys</p>
        <h2>Vad växer här?</h2>
        <p>Välj svamp och datum. Ett klick skickar bara den valda koordinaten till modellen.</p>
      </div>

      <div className="control-grid">
        <label>
          Art
          <select
            value={species}
            onChange={(event) => onSpeciesChange(event.target.value as Species)}
          >
            <option value="cantharellus_cibarius">Kantarell</option>
            <option value="craterellus_tubaeformis">Trattkantarell</option>
          </select>
        </label>
        <label>
          Datum
          <input
            type="date"
            value={targetDate}
            onChange={(event) => onDateChange(event.target.value)}
          />
        </label>
      </div>

      <label className="debug-toggle">
        <input
          type="checkbox"
          checked={includeDebug}
          onChange={(event) => onDebugChange(event.target.checked)}
        />
        Visa datakvalitet
      </label>

      <div className="result-content">
        {!coordinate && state === "idle" && (
          <div className="empty-state">
            <div className="empty-icon" aria-hidden="true">⌖</div>
            <h3>Välj en plats</h3>
            <p>Klicka på ett skogsområde för att se modellens bedömning.</p>
          </div>
        )}

        {coordinate && state === "loading" && (
          <div className="loading-state">
            <div className="loading-ring" />
            <h3>Beräknar platsen</h3>
            <p>{coordinate.latitude.toFixed(5)}, {coordinate.longitude.toFixed(5)}</p>
          </div>
        )}

        {state === "error" && (
          <div className="error-state" role="alert">
            <div className="error-icon" aria-hidden="true">!</div>
            <h3>Kunde inte bedöma platsen</h3>
            <p>{error}</p>
          </div>
        )}

        {state === "success" && result?.eligibility.status === "excluded" && (
          <ExcludedCard result={result} />
        )}
        {state === "success" && result?.eligibility.status === "eligible" && (
          <ScoreCard result={result} />
        )}
      </div>

      <footer className="panel-note">
        <span>i</span>
        Resultatet är ett heuristiskt lämplighetsindex, inte en sannolikhet.
      </footer>
    </aside>
  );
}
