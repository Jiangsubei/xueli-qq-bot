from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Dict

from tomlkit import aot, document, dumps, inline_table, item, parse, table
from tomlkit.items import AoT, InlineTable, Table
from tomlkit.toml_document import TOMLDocument


def load_toml_data(path: str | Path) -> Dict[str, Any]:
    with Path(path).open("rb") as handle:
        data = tomllib.load(handle)
    return dict(data) if isinstance(data, dict) else {}


def parse_toml_document(path: str | Path) -> TOMLDocument:
    file_path = Path(path)
    if not file_path.exists():
        return document()
    return parse(file_path.read_text(encoding="utf-8"))


def toml_to_plain_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): toml_to_plain_data(item_value) for key, item_value in value.items()}
    if isinstance(value, list):
        return [toml_to_plain_data(item_value) for item_value in value]
    if hasattr(value, "unwrap"):
        return toml_to_plain_data(value.unwrap())
    return value


def build_toml_document(data: Dict[str, Any]) -> TOMLDocument:
    doc = document()
    sync_toml_container(doc, data)
    return doc


def dumps_toml_document(data: Dict[str, Any]) -> str:
    return dumps(build_toml_document(data))


def sync_toml_container(container: TOMLDocument | Table | InlineTable, data: Dict[str, Any]) -> None:
    existing_keys = [str(key) for key in container.keys()]
    for key in existing_keys:
        if key not in data:
            del container[key]

    for key, raw_value in data.items():
        if raw_value is None:
            if key in container:
                del container[key]
            continue

        current_value = container.get(key)
        if isinstance(raw_value, dict):
            if isinstance(current_value, (Table, InlineTable)):
                sync_toml_container(current_value, raw_value)
                continue
            child = table() if isinstance(container, (TOMLDocument, Table)) else inline_table()
            sync_toml_container(child, raw_value)
            container[key] = child
            continue

        if isinstance(raw_value, list) and raw_value and all(isinstance(item_value, dict) for item_value in raw_value):
            array_table = aot()
            for entry in raw_value:
                child = table()
                sync_toml_container(child, entry)
                array_table.append(child)
            container[key] = array_table
            continue

        container[key] = item(raw_value)


def prune_none_values(value: Any) -> Any:
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for key, item_value in value.items():
            cleaned = prune_none_values(item_value)
            if cleaned is None:
                continue
            result[str(key)] = cleaned
        return result
    if isinstance(value, list):
        result = []
        for item_value in value:
            cleaned = prune_none_values(item_value)
            if cleaned is not None:
                result.append(cleaned)
        return result
    return value
