from typing import Optional, cast, TypedDict, Any

from dlt.common.schema import Schema
from dlt.common.utils import uniq_id, digest128
from dlt.common.typing import StrStr, StrStrStr, TEvent, StrAny
from dlt.common.normalizers.json import TUnpackedRowIterator
from dlt.common.sources import DLT_METADATA_FIELD, TEventDLTMeta, get_table_name


class TEventRow(TypedDict, total=False):
    _timestamp: float  # used for partitioning
    _dist_key: str  # distribution key used for clustering
    _record_hash: str  # unique id of current row
    _root_hash: str  # unique id of top level parent


class TEventRowRoot(TEventRow, total=False):
    _load_id: str  # load id to identify records loaded together that ie. need to be processed
    __dlt_meta: TEventDLTMeta  # stores metadata


class TEventRowChild(TEventRow, total=False):
    _parent_hash: str  # unique id of parent row
    _pos: int  # position in the list of rows
    value: Any  # for lists of simple types


class JSONNormalizerConfigProgapagtion(TypedDict, total=True):
    default: StrStr
    tables: StrStrStr


class JSONNormalizerConfig(TypedDict, total=True):
    generate_record_hash: bool
    propagation: JSONNormalizerConfigProgapagtion


# subsequent nested fields will be separated with the string below, applies both to field and table names
PATH_SEPARATOR = "__"


# for those paths the complex nested objects should be left in place
# current use case: we want to preserve event_slot__value in db even if it's an object
def _is_complex_type(schema: Schema, table: str, field_name: str, v: Any) -> bool:
    column = schema._schema_tables.get(table, {}).get(field_name, None)
    if column is None:
        data_type = schema.get_preferred_type(field_name)
    else:
        data_type = column["data_type"]
    return data_type == "complex"


def _flatten(schema: Schema, table: str, dict_row: TEventRowChild) -> TEventRowChild:
    out_rec_row: TEventRowChild = {}

    def norm_row_dicts(dict_row: StrAny, parent_name: Optional[str]) -> None:
        for k, v in dict_row.items():
            corrected_k = schema.normalize_column_name(k)
            child_name = corrected_k if not parent_name else f'{parent_name}{PATH_SEPARATOR}{corrected_k}'
            if isinstance(v, dict):
                if _is_complex_type(schema, table, child_name, v):
                    out_rec_row[child_name] = v  # type: ignore
                else:
                    norm_row_dicts(v, parent_name=child_name)

            else:
                out_rec_row[child_name] = v  # type: ignore

    norm_row_dicts(dict_row, None)
    return out_rec_row


def _get_child_row_hash(parent_hash: str, child_table: str, list_pos: int) -> str:
    # create deterministic unique id of the child row taking into account that all lists are ordered
    # and all child tables must be lists
    return digest128(f"{parent_hash}_{child_table}_{list_pos}")


def _get_content_hash(schema: Schema, table: str, row: StrAny) -> str:
    # generate content hashes only for tables with merge content disposition
    # TODO: extend schema with write disposition
    # WARNING: row may contain complex types: exclude from hash
    return digest128(uniq_id())


def _normalize_row(
    schema: Schema,
    dict_row: TEventRowChild,
    extend: TEventRowChild,
    table: str,
    parent_table: Optional[str] = None,
    parent_hash: Optional[str] = None,
    pos: Optional[int] = None
    ) -> TUnpackedRowIterator:

    def _append_child_meta(_row: TEventRowChild, _hash: str, _p_hash: str, _p_pos: int) -> TEventRowChild:
        _row["_parent_hash"] = _p_hash
        _row["_pos"] = _p_pos
        _row.update(extend)

        return _row

    is_top_level = parent_table is None

    # flatten current row
    flattened_row = _flatten(schema, table, dict_row)
    # infer record hash or leave existing primary key if present
    record_hash = flattened_row.get("_record_hash", None)
    if not record_hash:
        # check if we have primary key: if so use it
        primary_key = schema.filter_row_with_hint(table, "primary_key", flattened_row)
        if primary_key:
            # create row id from primary key
            record_hash = digest128("_".join(map(lambda v: str(v), primary_key.values())))
        elif not is_top_level:
            # child table row deterministic hash
            record_hash = _get_child_row_hash(parent_hash, table, pos)
            # link to parent table
            _append_child_meta(flattened_row, record_hash, parent_hash, pos)
        else:
            # create random row id, note that incremental loads will not work with such tables
            # TODO: create deterministic hash from all new_dict_row elements
            record_hash = _get_content_hash(schema, table, flattened_row)
        flattened_row["_record_hash"] = record_hash

    # if _root_hash propagation requested and we are at the top level then update extend
    if "_root_hash" in extend and extend["_root_hash"] is None and is_top_level:
        extend["_root_hash"] = record_hash

    # yield parent table first
    yield (table, parent_table), flattened_row

    # generate child tables only for lists
    lists = [k for k in flattened_row if isinstance(flattened_row[k], list)]  # type: ignore
    for k in lists:
        child_table = f"{table}{PATH_SEPARATOR}{k}"
        # this will skip empty lists
        v: TEventRowChild
        for idx, v in enumerate(flattened_row[k]):  # type: ignore
            # yield child table row
            tv = type(v)
            if tv is dict:
                yield from _normalize_row(schema, v, extend, child_table, table, record_hash, idx)
            elif tv is list:
                # unpack lists of lists
                raise ValueError(v)
            else:
                # list of simple types
                child_row_hash = _get_child_row_hash(record_hash, child_table, idx)
                e = _append_child_meta({"value": v, "_record_hash": child_row_hash}, child_row_hash, record_hash, idx)
                yield (child_table, table), e
        if not _is_complex_type(schema, table, k, []):
            # remove child list
            del flattened_row[k]  # type: ignore


def extend_schema(schema: Schema) -> None:
    # extends schema instance
    if "not_null" in schema._hints and "^_record_hash$" in schema._hints["not_null"]:
        return
    schema.merge_hints(
        {
            "not_null": ["^_record_hash$", "^_root_hash$", "^_parent_hash$", "^_pos$", "_load_id"],
            "foreign_key": ["^_parent_hash$"],
            "unique": ["^_record_hash$"]
        }
    )


def normalize(schema: Schema, source_event: TEvent, load_id: str) -> TUnpackedRowIterator:
    # we will extend event with all the fields necessary to load it as root row
    event = cast(TEventRowRoot, source_event)
    # identify load id if loaded data must be processed after loading incrementally
    event["_load_id"] = load_id
    # find table name
    table_name = schema.normalize_table_name(get_table_name(event) or schema.schema_name)
    # drop dlt metadata before normalizing
    event.pop(DLT_METADATA_FIELD, None)  # type: ignore
    # TODO: if table_name exist get "_dist_key" and "_timestamp" from the table definition in schema and propagate, if not take them from global hints
    # use event type or schema name as table name, request _root_hash propagation
    yield from _normalize_row(schema, cast(TEventRowChild, event), {"_root_hash": None}, table_name)
