from __future__ import annotations


def _stringify(value: object) -> str:
    return "" if value is None else str(value)


def box_title(title: str) -> str:
    title = _stringify(title).strip()
    width = max(len(title) + 2, 24)
    top = "+" + "-" * width + "+"
    middle = f"| {title.ljust(width - 1)}|"
    return "\n".join([top, middle, top])


def key_value_block(title: str, rows: list[tuple[str, object]]) -> str:
    label_width = max((len(_stringify(label)) for label, _ in rows), default=8)
    value_width = max((len(_stringify(value)) for _, value in rows), default=8)
    lines = [box_title(title)]
    border = f"+-{'-' * label_width}-+-{'-' * value_width}-+"
    lines.append(border)
    for label, value in rows:
        lines.append(
            f"| {_stringify(label).ljust(label_width)} | {_stringify(value).ljust(value_width)} |"
        )
    lines.append(border)
    return "\n".join(lines)


def table(title: str, headers: list[str], rows: list[list[object]]) -> str:
    widths = [len(_stringify(header)) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(_stringify(cell)))

    def render_row(values: list[object]) -> str:
        cells = [f" {_stringify(value).ljust(widths[index])} " for index, value in enumerate(values)]
        return "|" + "|".join(cells) + "|"

    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    lines = [box_title(title), border, render_row(headers), border]
    for row in rows:
        lines.append(render_row(row))
    lines.append(border)
    return "\n".join(lines)


def bullet_block(title: str, items: list[str]) -> str:
    lines = [box_title(title)]
    if not items:
        lines.append("- none")
    else:
        lines.extend(f"- {item}" for item in items)
    return "\n".join(lines)
