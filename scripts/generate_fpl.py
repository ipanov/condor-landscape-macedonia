#!/usr/bin/env python3
"""Generate Condor 2 flight plans for the MacedoniaSkopje landscape.

Writes:
  - C:/Condor2/FlightPlans/MacedoniaSkopje Test.fpl
  - C:/Condor2/Pilots/Pilot_New/Flightplan.fpl (the retained last-flight plan)

The retained plan is what Condor loads by default when the user clicks
FREE FLIGHT. If it references an airport that does not exist in the
landscape's .apt file, Condor crashes with "Airport is not installed..".
This script ensures both files use airport names that exactly match
MacedoniaSkopje.apt.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "airports.json"

# Condor landscape coordinate positions for each airport.
# These match the values in the existing MacedoniaSkopje Test.fpl.
AIRPORT_POSITIONS = {
    "Skopje International": {"x": 24538.194133, "y": 14872.391523, "z": 238},
    "Stenkovec": {"x": 43804.616540, "y": 25379.369935, "z": 318},
    "Kumanovo": {"x": 18648.390927, "y": 36459.791770, "z": 371},
}

FLIGHT_PLANS_DIR = Path("C:/Condor2/FlightPlans")
PILOT_PLAN_PATH = Path("C:/Condor2/Pilots/Pilot_New/Flightplan.fpl")


def load_airports():
    with DATA.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data["airports"]


def build_task_section(airports):
    lines = ["[Task]", "Landscape=MacedoniaSkopje", f"Count={len(airports)}"]
    for i, ap in enumerate(airports):
        name = ap["name"]
        pos = AIRPORT_POSITIONS[name]
        is_airport = 1 if i == 0 else 0
        lines.extend(
            [
                f"TPName{i}={name}",
                f"TPPosX{i}={pos['x']:.6f}",
                f"TPPosY{i}={pos['y']:.6f}",
                f"TPPosZ{i}={pos['z']}",
                f"TPAirport{i}={is_airport}",
                f"TPSectorType{i}=0",
                f"TPRadius{i}=3000",
                f"TPAngle{i}=90",
                f"TPAltitude{i}=1500",
                f"TPWidth{i}=0",
                f"TPHeight{i}=5000",
                f"TPAzimuth{i}=0",
            ]
        )
    lines.append("PZCount=0")
    return "\n".join(lines)


def build_minimal_fpl(airports):
    """Public flight plan file (no plane/weather/game options)."""
    return (
        "[Version]\n"
        "Condor version=2002\n\n"
        f"{build_task_section(airports)}\n\n"
        "[Weather]\n"
        "WindDir=180\n"
        "WindSpeed=3.0\n"
        "WindUpperSpeed=0\n"
        "WindDirVariation=3\n"
        "WindSpeedVariation=3\n"
    )


def build_retained_fpl(airports):
    """Pilot retained plan with full sections so Condor can start immediately."""
    task = build_task_section(airports)
    return (
        "[Version]\n"
        "Condor version=2002\n\n"
        f"{task}\n\n"
        "[Weather]\n"
        "WindDir=180\n"
        "WindSpeed=3.0\n"
        "WindUpperSpeed=0\n"
        "WindDirVariation=3\n"
        "WindSpeedVariation=3\n"
        "WindTurbulence=2\n"
        "ThermalsTemp=22\n"
        "ThermalsTempVariation=1\n"
        "ThermalsDew=10\n"
        "ThermalsStrength=2\n"
        "ThermalsStrengthVariation=1\n"
        "ThermalsInversionheight=2200\n"
        "ThermalsWidth=2\n"
        "ThermalsWidthVariation=1\n"
        "ThermalsActivity=3\n"
        "ThermalsTurbulence=2\n"
        "ThermalsFlatsActivity=2\n"
        "ThermalsStreeting=0\n"
        "WavesStability=5\n"
        "WavesMoisture=8\n"
        "HighCloudsCoverage=2\n"
        "Pressure=1018.58740234375\n"
        "WeatherPreset=0\n"
        "RandomizeWeatherOnEachFlight=0\n\n"
        "[Plane]\n"
        "Class=School\n"
        "Name=Blanik\n"
        "Skin=Default\n"
        "Water=0\n"
        "FixedMass=0\n"
        "CGBias=0\n\n"
        "[GameOptions]\n"
        "StartTime=12\n"
        "StartTimeWindow=0\n"
        "RaceStartDelay=0.16666667163372\n"
        "TaskDate=46194\n"
        "IconsVisibleRange=20\n"
        "ThermalHelpersRange=0\n"
        "TurnpointHelpersRange=0\n"
        "AllowPDA=1\n"
        "AllowRealtimeScoring=1\n"
        "AllowExternalView=1\n"
        "AllowPadlockView=1\n"
        "AllowSmoke=1\n"
        "AllowPlaneRecovery=0\n"
        "AllowHeightRecovery=0\n"
        "AllowMidairCollisionRecovery=0\n"
        "PenaltyCloudFlying=100\n"
        "PenaltyPlaneRecovery=100\n"
        "PenaltyHeightRecovery=100\n"
        "PenaltyWrongWindowEnterance=100\n"
        "PenaltyPlaneCollision=100\n"
        "PenaltyWindowCollision=100\n"
        "PenaltyPenaltyZoneEnterance=100\n"
        "PenaltyLostKnuckle=100\n"
        "ThermalHelpersTange=20\n"
        "RandSeed=-721422386\n"
        "StartType=0\n"
        "StartHeight=700\n"
        "BreakProb=0\n"
        "RopeLength=50\n"
        "MaxTowplanes=2\n"
        "TailHunting=0\n"
        "TailKnucklesNum=10\n"
        "TailKnucklesSize=5\n"
        "TailKnucklesDensity=5\n"
        "MaxTeams=0\n"
        "AcroFlight=0\n\n"
        "[Description]\n"
        "Text=MacedoniaSkopje test flight: Skopje International -> Stenkovec -> Kumanovo.\n"
    )


def main():
    airports = load_airports()
    names = [ap["name"] for ap in airports]
    missing = set(names) - set(AIRPORT_POSITIONS)
    if missing:
        print(f"ERROR: no positions for airports: {missing}", file=sys.stderr)
        sys.exit(1)

    FLIGHT_PLANS_DIR.mkdir(parents=True, exist_ok=True)
    PILOT_PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)

    test_plan_path = FLIGHT_PLANS_DIR / "MacedoniaSkopje Test.fpl"
    test_plan_path.write_text(build_minimal_fpl(airports), encoding="utf-8")
    print(f"Wrote {test_plan_path}")

    PILOT_PLAN_PATH.write_text(build_retained_fpl(airports), encoding="utf-8")
    print(f"Wrote {PILOT_PLAN_PATH}")


if __name__ == "__main__":
    main()
