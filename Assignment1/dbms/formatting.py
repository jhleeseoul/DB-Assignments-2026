from typing import Any


def _to_display_value(value) -> str:
    if value is None:
        return "null"
    return str(value)


def _format_result_set(headers: list[str], rows: list[list[Any]]) -> list[str]:
    line = "-" * 60
    output = [line]
    if headers:
        output.append(" | ".join(headers))
    for row in rows:
        output.append(" | ".join(_to_display_value(v) for v in row))
    output.append(line)
    output.append(f"{len(rows)} row{'s' if len(rows) != 1 else ''} in set")
    return output
