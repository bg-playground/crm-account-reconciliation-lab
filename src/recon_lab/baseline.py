"""Splink (DuckDB backend) probabilistic baseline, trained on the dev seed only."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from .blocking import BLOCKING_RULES, Blocker, Pair

FIELDS = ["unique_id", "source_dataset", "name_norm", "domain_norm", "phone10", "postcode5",
          "street_norm", "city_norm", "state_norm", "name4", "soundex_first"]


class SplinkBaseline:
    @staticmethod
    def _frame(records: list[dict[str, object]], org: str) -> pd.DataFrame:
        rows = [{k: r.get(k) for k in FIELDS} for r in records if r["source_dataset"] == org]
        return pd.DataFrame(rows, columns=FIELDS).astype(object)

    @staticmethod
    def settings():
        import splink.comparison_library as cl
        from splink import SettingsCreator, block_on

        return SettingsCreator(
            link_type="link_and_dedupe",
            unique_id_column_name="unique_id",
            comparisons=[
                cl.JaroWinklerAtThresholds("name_norm", [0.95, 0.88, 0.75]),
                cl.ExactMatch("domain_norm"),
                cl.ExactMatch("phone10"),
                cl.ExactMatch("postcode5"),
                cl.ExactMatch("city_norm"),
                cl.LevenshteinAtThresholds("street_norm", [2, 6]),
            ],
            blocking_rules_to_generate_predictions=[block_on(*fields) for _, fields in BLOCKING_RULES],
            retain_intermediate_calculation_columns=False,
        )

    @staticmethod
    def _linker(records: list[dict[str, object]], settings):
        from splink import DuckDBAPI, Linker

        logging.getLogger("splink").setLevel(logging.ERROR)
        frames = [SplinkBaseline._frame(records, "org_a"), SplinkBaseline._frame(records, "org_b")]
        return Linker(frames, settings, db_api=DuckDBAPI(), input_table_aliases=["org_a", "org_b"])

    @staticmethod
    def train(records: list[dict[str, object]], seed: int = 101) -> dict:
        from splink import block_on

        linker = SplinkBaseline._linker(records, SplinkBaseline.settings())
        linker.training.estimate_probability_two_random_records_match([block_on("domain_norm")], recall=0.8)
        linker.training.estimate_u_using_random_sampling(max_pairs=2_000_000, seed=seed)
        linker.training.estimate_parameters_using_expectation_maximisation(block_on("domain_norm"))
        linker.training.estimate_parameters_using_expectation_maximisation(block_on("phone10"))
        return linker.misc.save_model_to_json()

    @staticmethod
    def predict(records: list[dict[str, object]], model: dict) -> dict[Pair, dict[str, float]]:
        """Score every blocked pair; returns {pair: {"p": match_probability, "mw": match_weight}}."""

        linker = SplinkBaseline._linker(records, model)
        frame = linker.inference.predict().as_pandas_dataframe()
        out: dict[Pair, dict[str, float]] = {}
        for left, right, prob, weight in zip(frame["unique_id_l"], frame["unique_id_r"],
                                             frame["match_probability"], frame["match_weight"]):
            out[Blocker.pair(str(left), str(right))] = {"p": float(prob), "mw": float(weight)}
        return out

    @staticmethod
    def save(model: dict, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(model, indent=2, sort_keys=True) + "\n", encoding="utf-8")
