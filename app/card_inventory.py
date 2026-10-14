"""Inventory units for line-based and multiline grouped cards."""

GROUPED_CARD_SEPARATOR = "======== CARD ========"
INVENTORY_CARD_TYPES = ("data", "grouped_data")


def split_card_inventory(card_type: str, content: str | None) -> list[str]:
    if card_type == "data":
        return [line.strip() for line in (content or "").split("\n") if line.strip()]
    if card_type != "grouped_data":
        return []

    # Only a standalone separator line splits inventory. Keep field spacing,
    # indentation and blank lines inside each account bundle.
    blocks = []
    lines = []
    for line in (content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.strip() == GROUPED_CARD_SEPARATOR:
            blocks.append(lines)
            lines = []
        else:
            lines.append(line)
    blocks.append(lines)

    units = []
    for block in blocks:
        start, end = 0, len(block)
        while start < end and not block[start].strip():
            start += 1
        while end > start and not block[end - 1].strip():
            end -= 1
        if start < end:
            units.append("\n".join(block[start:end]))
    return units


def join_card_inventory(card_type: str, units: list[str]) -> str:
    separator = f"\n\n{GROUPED_CARD_SEPARATOR}\n\n" if card_type == "grouped_data" else "\n"
    return separator.join(units)
