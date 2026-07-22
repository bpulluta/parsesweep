#!/usr/bin/env python3
"""
CSV export for consolidated data.

Simple, clean CSV output.
"""

import pandas as pd
from pathlib import Path


class CsvExporter:
    """
    Export DataFrame to CSV file.

    Simple CSV export with automatic directory creation.
    """

    def save(self, df: pd.DataFrame, output_path: Path):
        """
        Save DataFrame to CSV file.

        Args:
            df: DataFrame to export
            output_path: Path for output CSV file

        Example:
            >>> exporter = CsvExporter()
            >>> exporter.save(df, Path("output.csv"))
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_path, index=False)
