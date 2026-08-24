"""Small CLI for exercising the complete mock-data pipeline."""

from __future__ import annotations

import argparse
import json

from fungifind.models import Species
from fungifind.service import get_mushroom_score


def main() -> None:
    parser = argparse.ArgumentParser(description="Calculate a FungiFind v0 suitability index")
    parser.add_argument("--latitude", type=float, required=True)
    parser.add_argument("--longitude", type=float, required=True)
    parser.add_argument("--date", required=True, help="ISO date, for example 2026-08-23")
    parser.add_argument(
        "--species",
        choices=[species.value for species in Species],
        default=Species.CANTHARELLUS_CIBARIUS.value,
    )
    args = parser.parse_args()

    result = get_mushroom_score(
        latitude=args.latitude,
        longitude=args.longitude,
        date=args.date,
        species=args.species,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

