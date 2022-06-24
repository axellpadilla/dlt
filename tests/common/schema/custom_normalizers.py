from dlt.common.normalizers.json import TUnpackedRowIterator
from dlt.common.schema.schema import Schema
from dlt.common.typing import TEvent


def normalize_table_name(name: str) -> str:
    return name.capitalize()


def normalize_column_name(name: str) -> str:
    return "column_" + name.lower()


def extend_schema(schema: Schema) -> None:
    json_config = schema._normalizers_config["json"]["config"]
    schema._hints["not_null"] = json_config["not_null"]


def normalize(schema: Schema, source_event: TEvent, load_id: str) -> TUnpackedRowIterator:
    yield ("table", None), source_event
