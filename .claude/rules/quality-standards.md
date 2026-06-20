---
description: Quality standards for landscape content
globs: scripts/**/*.py, data/**
---

# Quality Standards

## Loading Screens
Only real photographs of full-size gliders/sailplanes flown in Macedonia by Macedonian aero clubs (Skopje, Bitola, Štip, Riminer/Prilep). No stock photos, no paragliders, no other countries.

## Flight Planner Map
Must match ICAO/topographic cartography standards:
- Roads: red/orange by class (not blue)
- Railways: thin black lines
- Settlements: yellow
- Water: blue
- City labels: bold, readable
- Elevation: proper hypsometric tinting with hillshade

## Forest Maps
- No trees over roads, railways, water, buildings, or runways
- Use OSM + CORINE + DEM elevation data for species classification
- Many small forests with proper conifer/deciduous mixing, not one large blob

## Data Sources
Use best available open data. For Macedonia: cadastre WMS orthophoto (0.28 m/px) as primary, Esri as fallback. Never fabricate geographic data.
