"use client";

import {
  Map,
  Marker,
  NavigationControl,
  type GeoJSONSource,
  type MapMouseEvent,
} from "maplibre-gl";
import { useEffect, useRef } from "react";

import { mapConfig, overlayConfig } from "@/lib/config";
import type { Species } from "@/lib/fungifind-api";
import {
  ViewportOverlayController,
  normalizeOverlayOpacity,
  type Bbox,
  type OverlayStatus,
  type ViewportResponse,
} from "@/lib/viewport-overlay";

type Coordinate = { latitude: number; longitude: number };

const OVERLAY_SOURCE_ID = "fungifind-viewport-cells";
const OVERLAY_LAYER_ID = "fungifind-viewport-fill";
const EMPTY_COLLECTION = { type: "FeatureCollection" as const, features: [] };

interface FungiMapProps {
  onPick: (coordinate: Coordinate) => void;
  selected: Coordinate | null;
  species: Species;
  targetDate: string;
  overlayEnabled: boolean;
  overlayOpacity: number;
  onOverlayStatus: (status: OverlayStatus) => void;
}

function mapBbox(map: Map): Bbox {
  const bounds = map.getBounds();
  return [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()];
}

function geoJsonData(data: ViewportResponse | null) {
  return data
    ? { type: "FeatureCollection" as const, features: data.features }
    : EMPTY_COLLECTION;
}

export function FungiMap({
  onPick,
  selected,
  species,
  targetDate,
  overlayEnabled,
  overlayOpacity,
  onOverlayStatus,
}: FungiMapProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<Map | null>(null);
  const markerRef = useRef<Marker | null>(null);
  const overlayControllerRef = useRef<ViewportOverlayController | null>(null);
  const onPickRef = useRef(onPick);
  const speciesRef = useRef(species);
  const dateRef = useRef(targetDate);
  const overlayEnabledRef = useRef(overlayEnabled);
  const overlayOpacityRef = useRef(overlayOpacity);
  const onOverlayStatusRef = useRef(onOverlayStatus);

  useEffect(() => {
    onPickRef.current = onPick;
    speciesRef.current = species;
    dateRef.current = targetDate;
    overlayEnabledRef.current = overlayEnabled;
    overlayOpacityRef.current = overlayOpacity;
    onOverlayStatusRef.current = onOverlayStatus;
  }, [
    onOverlayStatus,
    onPick,
    overlayEnabled,
    overlayOpacity,
    species,
    targetDate,
  ]);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const map = new Map({
      container: containerRef.current,
      style: mapConfig.styleUrl,
      center: [mapConfig.centerLongitude, mapConfig.centerLatitude],
      zoom: mapConfig.zoom,
      attributionControl: { compact: true },
    });
    map.addControl(new NavigationControl({ showCompass: false }), "bottom-left");
    map.on("click", (event: MapMouseEvent) => {
      onPickRef.current({
        latitude: event.lngLat.lat,
        longitude: event.lngLat.lng,
      });
    });
    const controller = new ViewportOverlayController({
      ...overlayConfig,
      onData: (data) => {
        const source = map.getSource(OVERLAY_SOURCE_ID) as GeoJSONSource | undefined;
        if (!source) return;
        source.setData(
          geoJsonData(data) as Parameters<GeoJSONSource["setData"]>[0],
        );
      },
      onStatus: (status) => onOverlayStatusRef.current(status),
    });
    overlayControllerRef.current = controller;

    const scheduleOverlay = () => {
      controller.update({
        enabled: overlayEnabledRef.current,
        zoom: map.getZoom(),
        bbox: mapBbox(map),
        species: speciesRef.current,
        date: dateRef.current,
      });
    };
    const cancelOverlay = () => {
      if (overlayEnabledRef.current) controller.cancelForMovement();
    };
    const handleLoad = () => {
      map.addSource(OVERLAY_SOURCE_ID, {
        type: "geojson",
        data: EMPTY_COLLECTION,
      });
      const firstSymbolLayer = map
        .getStyle()
        .layers?.find((layer) => layer.type === "symbol")?.id;
      map.addLayer(
        {
          id: OVERLAY_LAYER_ID,
          type: "fill",
          source: OVERLAY_SOURCE_ID,
          paint: {
            "fill-color": [
              "interpolate",
              ["linear"],
              ["get", "final_index"],
              0,
              "#7b4d3b",
              0.35,
              "#c07b43",
              0.55,
              "#b7b85b",
              0.72,
              "#5f8b4c",
              1,
              "#24543b",
            ],
            "fill-opacity": normalizeOverlayOpacity(overlayOpacityRef.current),
            "fill-outline-color": "rgba(255, 253, 248, 0.32)",
          },
        },
        firstSymbolLayer,
      );
      scheduleOverlay();
    };
    map.on("load", handleLoad);
    map.on("movestart", cancelOverlay);
    map.on("zoomstart", cancelOverlay);
    map.on("moveend", scheduleOverlay);
    map.on("zoomend", scheduleOverlay);
    mapRef.current = map;

    return () => {
      controller.dispose();
      overlayControllerRef.current = null;
      markerRef.current?.remove();
      markerRef.current = null;
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    const controller = overlayControllerRef.current;
    if (!map || !controller || !map.getSource(OVERLAY_SOURCE_ID)) return;
    controller.update({
      enabled: overlayEnabled,
      zoom: map.getZoom(),
      bbox: mapBbox(map),
      species,
      date: targetDate,
    });
  }, [overlayEnabled, species, targetDate]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map?.getLayer(OVERLAY_LAYER_ID)) return;
    map.setPaintProperty(
      OVERLAY_LAYER_ID,
      "fill-opacity",
      normalizeOverlayOpacity(overlayOpacity),
    );
  }, [overlayOpacity]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !selected) return;

    if (!markerRef.current) {
      const element = document.createElement("div");
      element.className = "selected-marker";
      element.setAttribute("aria-label", "Vald plats");
      markerRef.current = new Marker({ element, anchor: "center" })
        .setLngLat([selected.longitude, selected.latitude])
        .addTo(map);
    }
    markerRef.current.setLngLat([selected.longitude, selected.latitude]);
  }, [selected]);

  return <div ref={containerRef} className="map-canvas" aria-label="Interaktiv karta" />;
}
