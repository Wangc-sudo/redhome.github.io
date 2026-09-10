from dataclasses import dataclass


@dataclass(frozen=True)
class FieldMapping:
    source_name: str
    column: str
    source_type: str


@dataclass(frozen=True)
class DingTalkSheet:
    base_id: str
    sheet_id: str
    sheet_name: str
    dataset: str
    target_table: str
    max_pages: int
    fields: tuple[FieldMapping, ...]
