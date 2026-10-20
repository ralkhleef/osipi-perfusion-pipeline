from __future__ import annotations

import main


def _scoring() -> dict:
    gm = {"status": "compared", "voxel_count": 4960, "mean_submitted": 0.00067456,
          "mean_reference": 0.00061928, "bias": 5.528e-05, "mae": 8.2565e-05,
          "rmse": 0.00010831, "correlation": 0.694435}
    whole = {**gm, "voxel_count": 1596595, "mean_submitted": 2.42e-06}
    return {
        "maps": [
            {"status": "compared", "detected_map_type": "Ktrans", "units": "min^-1",
             "participant": 2, "site": 1, "repeat": 1, "whole_map": whole,
             "masks": [{"mask_label": "gray matter", "metrics": gm}]},
            {"status": "compared", "detected_map_type": "Vp", "units": "units not provided",
             "participant": 1, "site": 1, "repeat": 1, "whole_map": whole, "masks": []},
            {"status": "reference_not_available", "detected_map_type": "Ktrans",
             "participant": 3, "site": 1, "repeat": 1},
        ],
        "roi_descriptive_statistics": [
            {"participant": 2, "site": 1, "repeat": 1, "map_type": "ktrans",
             "roi_label": "gray matter", "units": "min^-1", "voxel_count": 4960,
             "roi_mean": 0.000674562724, "roi_median": 0.00067650707,
             "roi_within_scan_sd": 0.000129052},
        ],
    }


def test_one_row_per_scan_map_and_region_in_reading_order() -> None:
    rows = main._results_table_rows("Submission 1", _scoring())
    assert [row[1:7] for row in rows] == [
        ["1", "1", "1", "Vp", "", "Whole image"],
        ["2", "1", "1", "Ktrans", "min^-1", "Gray matter"],
        ["2", "1", "1", "Ktrans", "min^-1", "Whole image"],
    ]


def test_roi_statistics_sit_beside_the_reference_comparison() -> None:
    row = main._results_table_rows("Submission 1", _scoring())[1]
    record = dict(zip(main.RESULTS_TABLE_COLUMNS, row))
    assert record["Mean"] == "0.0006746"
    assert record["Median"] == "0.0006765"
    assert record["Reference mean"] == "0.0006193"
    assert record["RMSE"] == "0.0001083"
    assert record["Correlation"] == "0.6944"
    assert record["Voxels"] == "4960"


def test_the_table_names_no_team() -> None:
    assert "Team" not in " ".join(main.RESULTS_TABLE_COLUMNS)
    assert "email" not in " ".join(main.RESULTS_TABLE_COLUMNS).lower()


def test_missing_numbers_are_blank_not_nan() -> None:
    assert main._results_table_number(float("nan")) == ""
    assert main._results_table_number(None) == ""
    assert main._results_table_number(True) == ""
    assert main._results_table_number(7) == "7"
