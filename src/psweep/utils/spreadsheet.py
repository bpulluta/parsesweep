"""Keep untrusted evidence literal in spreadsheet exports."""

import pandas as pd
from openpyxl.worksheet.worksheet import Worksheet


def formula_text(value: str, *, criteria: bool = False) -> str:
    """Escape formula strings and optionally COUNTIF wildcards."""
    if criteria:
        value = value.replace("~", "~~").replace("*", "~*").replace("?", "~?")
    return value.replace('"', '""')


def csv_literal(value: object) -> object:
    """Escape formula-like text without changing numeric values."""
    if not isinstance(value, str) or not value:
        return value
    stripped = value.lstrip()
    if value[0] in "\t\r\n" or (stripped and stripped[0] in "=+-@"):
        return "'" + value
    return value


def csv_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Escape a display copy, including untrusted headers."""
    result = frame.map(csv_literal)
    result.columns = [csv_literal(column) for column in result.columns]
    return result


def literal_worksheet(sheet: Worksheet) -> None:
    """Mark evidence as text before adding trusted formulas."""
    for row in sheet:
        for cell in row:
            if isinstance(cell.value, str):
                cell.data_type = "s"
