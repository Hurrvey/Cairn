"""Bounded, in-memory safety checks for Office Open XML packages."""

from __future__ import annotations

import io
import posixpath
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from cairn.ingestion.errors import ParseCorruptFile
from cairn.ingestion.office_compat import safe_xml_events

_MAX_MEMBERS = 10_000
_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 100
_MIN_RATIO_CHECK_BYTES = 1024
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_TABLE_CELLS = 1_000_000
MAX_TABLE_ROWS = 100_000
MAX_TABLE_COLUMNS = 1024
_MAX_XML_BYTES = 16 * 1024 * 1024
_MAX_XML_NODES = 500_000
_MAX_XML_DEPTH = 128
_DRIVE_PATH = re.compile(r"^[A-Za-z]:")
_RELATIONSHIP_TAG = "Relationship"
_RELATIONSHIP_MODE = "TargetMode"
_EXTERNAL_MODE = "external"
_EXTERNAL_TARGET = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|[\\/]{2})")


@dataclass(frozen=True, slots=True)
class SafeOfficePackage:
    """Validated ZIP metadata and source bytes retained for library loading."""

    data: bytes
    names: tuple[str, ...]


def inspect_ooxml_package(data: bytes) -> SafeOfficePackage:
    """Validate an OOXML ZIP without extracting or following package links."""
    if len(data) > MAX_INPUT_BYTES:
        raise ParseCorruptFile("The Office package exceeds the input safety bound.")
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise ParseCorruptFile("Encrypted Office containers are not supported.")
    stream = io.BytesIO(data)
    try:
        with zipfile.ZipFile(stream) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > _MAX_MEMBERS:
                raise ParseCorruptFile("The Office package has an unsafe member count.")
            names: list[str] = []
            seen: set[str] = set()
            total_size = 0
            for info in infos:
                name = _validate_name(info.orig_filename)
                if name in seen:
                    raise ParseCorruptFile("The Office package contains duplicate members.")
                seen.add(name)
                names.append(name)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ParseCorruptFile("Office ZIP symbolic links are not supported.")
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ParseCorruptFile("The Office ZIP compression method is unsupported.")
                if info.flag_bits & 0x1:
                    raise ParseCorruptFile("Encrypted Office ZIP members are not supported.")
                if info.file_size < 0 or info.file_size > _MAX_MEMBER_BYTES:
                    raise ParseCorruptFile("An Office ZIP member exceeds the safety bound.")
                total_size += info.file_size
                if total_size > _MAX_TOTAL_BYTES:
                    raise ParseCorruptFile("The Office package exceeds the expansion bound.")
                if info.file_size >= _MIN_RATIO_CHECK_BYTES and (
                    info.compress_size == 0
                    or info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO
                ):
                    raise ParseCorruptFile("An Office ZIP member has an unsafe compression ratio.")
            if "[Content_Types].xml" not in seen:
                raise ParseCorruptFile("The Office package content-type manifest is missing.")
            manifest = archive.read("[Content_Types].xml")
            declarations = _inspect_xml_member(manifest, "[Content_Types].xml", seen)
            xml_names = {
                entry["PartName"].lstrip("/")
                for entry in declarations
                if "PartName" in entry
                and entry.get("ContentType", "").lower().endswith(("+xml", "/xml"))
            }
            xml_extensions = {
                entry["Extension"].lower()
                for entry in declarations
                if "Extension" in entry
                and entry.get("ContentType", "").lower().endswith(("+xml", "/xml"))
            }
            for info in infos:
                name = _validate_name(info.filename)
                if info.is_dir():
                    continue
                with archive.open(info) as member:
                    payload = member.read(_MAX_MEMBER_BYTES + 1)
                if len(payload) != info.file_size:
                    raise ParseCorruptFile("An Office ZIP member has an invalid expanded size.")
                if (
                    name.lower().endswith((".xml", ".rels"))
                    or name in xml_names
                    or name.rsplit(".", 1)[-1].lower() in xml_extensions
                ):
                    _inspect_xml_member(payload, name, seen)
            return SafeOfficePackage(data=data, names=tuple(names))
    except ParseCorruptFile:
        raise
    except Exception as exc:
        raise ParseCorruptFile() from exc


def _validate_name(raw_name: str) -> str:
    name = raw_name
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or name.startswith("/")
        or _DRIVE_PATH.match(name)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ParseCorruptFile("The Office package contains an unsafe member path.")
    return name


def _inspect_xml_member(data: bytes, name: str, names: set[str]) -> list[dict[str, str]]:
    if len(data) > _MAX_XML_BYTES:
        raise ParseCorruptFile("An Office XML member exceeds the safety bound.")
    declarations: list[dict[str, str]] = []
    depth = 0
    nodes = 0
    max_row = 0
    max_column = 0
    expanded_cells = 0
    try:
        for event, element in safe_xml_events(data):
            if event == "end":
                depth -= 1
                element.clear()
                continue
            depth += 1
            nodes += 1
            if depth > _MAX_XML_DEPTH or nodes > _MAX_XML_NODES:
                raise ParseCorruptFile("The Office XML complexity exceeds the safety bound.")
            tag = element.tag.rsplit("}", 1)[-1]
            if tag in {"gridSpan", "gridBefore", "gridAfter"}:
                raw_span = element.attrib.get(
                    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val", "1"
                )
                span = int(raw_span)
                expanded_cells += span
                if span < 0 or span > MAX_TABLE_COLUMNS or expanded_cells > MAX_TABLE_CELLS:
                    raise ParseCorruptFile("The Word table span exceeds the safety bound.")
            if name == "[Content_Types].xml" and tag in {"Override", "Default"}:
                declarations.append(dict(element.attrib))
            if name.lower().endswith(".rels") and tag == _RELATIONSHIP_TAG:
                _inspect_relationship(element.attrib, name, names)
            if name.startswith("xl/worksheets/") and tag in {"row", "c"}:
                reference = element.attrib.get("r", "")
                if tag == "row" and reference:
                    max_row = max(max_row, int(reference))
                elif tag == "c" and reference:
                    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]*)", reference)
                    if match is None:
                        raise ParseCorruptFile("The worksheet cell coordinate is invalid.")
                    column = 0
                    for letter in match[1]:
                        column = column * 26 + ord(letter) - ord("A") + 1
                    max_column = max(max_column, column)
                    max_row = max(max_row, int(match[2]))
                if (
                    max_row > MAX_TABLE_ROWS
                    or max_column > MAX_TABLE_COLUMNS
                    or max_row * max_column > MAX_TABLE_CELLS
                ):
                    raise ParseCorruptFile("The worksheet dimensions exceed the safety bound.")
    except ParseCorruptFile:
        raise
    except Exception as exc:
        raise ParseCorruptFile("An Office XML member is invalid or contains a DTD/entity.") from exc
    return declarations


def _inspect_relationship(attributes: dict[str, str], name: str, names: set[str]) -> None:
    target = unquote(attributes.get("Target", ""))
    mode = attributes.get(_RELATIONSHIP_MODE, "").lower()
    if (
        mode not in {"", "internal"}
        or _EXTERNAL_TARGET.match(target)
        or "\\" in target
        or "\x00" in target
    ):
        raise ParseCorruptFile("External Office relationships are not supported.")
    parsed = urlsplit(target)
    if not parsed.path or parsed.query:
        raise ParseCorruptFile("An Office relationship target is invalid.")
    base = posixpath.dirname(posixpath.dirname(name))
    resolved = posixpath.normpath(
        parsed.path.lstrip("/")
        if parsed.path.startswith("/")
        else posixpath.join(base, parsed.path)
    )
    if resolved == ".." or resolved.startswith("../") or resolved not in names:
        raise ParseCorruptFile("An Office relationship escapes the package or is missing.")
