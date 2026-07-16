#!/usr/bin/env python3
"""Export the Game/Device EQ preset catalogue bundled with SteelSeries GG.

The GG installer contains SQLite migrations rather than a pre-populated
database.  This utility applies the Engine migrations to a temporary SQLite
database and exports the current contents of ``device_game_eq_presets``.
It never installs or starts SteelSeries GG, and it never touches USB devices.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


MIGRATIONS_PREFIX = "apps/engine/db/migrations/"
PRESET_TABLE = "device_game_eq_presets"
# This table was introduced by the 20231221130000 migration. Earlier Engine
# files cannot affect it, so avoiding them prevents needless extraction of
# old multi-megabyte game-integration seed data.
PRESET_TABLE_INTRODUCED_AT = (20231221130000,)
DEFAULT_INSTALLER = Path(__file__).with_name("SteelSeriesGG114.0.0Setup.exe")


class ExtractionError(RuntimeError):
    """An installer could not be read or its migrations could not be applied."""


@dataclass(frozen=True)
class Migration:
    """One Engine migration and its embedded chronological identifier."""

    version: tuple[int, ...]
    sequence: tuple[int, ...]
    archive_path: str


def parse_version(value: str) -> tuple[int, ...]:
    """Return a sortable version tuple for GG's version-directory names."""

    if value.startswith("gg-"):
        value = value[3:]
    match = re.fullmatch(r"(\d+(?:\.\d+)*)", value)
    if not match:
        raise ExtractionError(f"Unsupported GG migration version directory: {value!r}")
    return tuple(int(part) for part in value.split("."))


def parse_sequence(filename: str) -> tuple[int, ...]:
    """Order migration files within a GG version directory."""

    stem = filename.removesuffix(".sql").removesuffix("_squash")
    numbers = re.findall(r"\d+", stem)
    if not numbers:
        raise ExtractionError(f"Unsupported GG migration file name: {filename!r}")
    return tuple(int(number) for number in numbers)


def require_7z() -> str:
    executable = shutil.which("7z")
    if not executable:
        raise ExtractionError("The '7z' executable is required to read the GG installer.")
    return executable


def run_7z(executable: str, arguments: Sequence[str]) -> bytes:
    result = subprocess.run(
        [executable, *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ExtractionError(f"7z failed ({' '.join(arguments)}): {detail or 'no diagnostic'}")
    return result.stdout


def list_migrations(executable: str, installer: Path) -> list[Migration]:
    """List the Engine migration SQL files packaged in an installer."""

    listing = run_7z(executable, ["l", "-slt", str(installer)]).decode("utf-8", "replace")
    migrations: list[Migration] = []
    for line in listing.splitlines():
        if not line.startswith("Path = "):
            continue
        archive_path = line.removeprefix("Path = ")
        if not archive_path.startswith(MIGRATIONS_PREFIX) or not archive_path.endswith(".sql"):
            continue
        relative = archive_path.removeprefix(MIGRATIONS_PREFIX)
        parts = relative.split("/")
        if len(parts) != 2:
            continue
        version_dir, filename = parts
        migrations.append(
            Migration(
                version=parse_version(version_dir),
                sequence=parse_sequence(filename),
                archive_path=archive_path,
            )
        )

    if not migrations:
        raise ExtractionError(
            "The installer contains no Engine SQLite migrations under "
            f"{MIGRATIONS_PREFIX!r}."
        )
    # The archive contains both the pre-GG Engine ``3.x`` directories and the
    # newer ``gg-*`` directories. Their directory versions overlap and must
    # not be compared directly (for example, gg-0.1.0 is from 2020). The SQL
    # file prefix is the chronological migration ID used by the Engine.
    return sorted(migrations, key=lambda migration: (migration.sequence, migration.version, migration.archive_path))


# GG emits both ``-- +goose Up`` and ``-- +goose Up-- <file>.sql``.
UP_MARKER = re.compile(r"^-- \+goose Up(?:(?:\s|--).*)?$", re.MULTILINE)
DOWN_MARKER = re.compile(r"^-- \+goose Down(?:(?:\s|--).*)?$", re.MULTILINE)


def up_sql(migration: str, archive_path: str) -> str:
    """Keep exactly the up portion of a Goose migration file."""

    up = UP_MARKER.search(migration)
    if not up:
        raise ExtractionError(f"{archive_path}: missing '-- +goose Up' marker")
    down = DOWN_MARKER.search(migration, up.end())
    return migration[up.end() : down.start() if down else len(migration)]


def split_sql_statements(sql: str) -> Iterable[str]:
    """Split SQL without mistaking punctuation inside quoted preset JSON for a terminator."""

    buffer: list[str] = []
    for line in sql.splitlines(keepends=True):
        buffer.append(line)
        candidate = "".join(buffer)
        if sqlite3.complete_statement(candidate):
            yield candidate
            buffer.clear()
    if "".join(buffer).strip():
        raise ExtractionError("Migration ended with an incomplete SQL statement")


def apply_preset_migrations(
    executable: str, installer: Path, migrations: Iterable[Migration], *, quiet: bool
) -> sqlite3.Connection:
    """Replay only statements that create or modify the independent preset table.

    Engine migrations also contain millions of unrelated game-integration
    rows. The preset table has no foreign-key dependency on those tables, so
    replaying statements that name it is equivalent for this catalogue while
    avoiding the unrelated database bulk.
    """

    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = OFF")

    relevant_number = 0
    for migration in migrations:
        if migration.sequence < PRESET_TABLE_INTRODUCED_AT:
            continue
        source = run_7z(executable, ["x", "-so", str(installer), migration.archive_path])
        sql = up_sql(source.decode("utf-8-sig", "replace"), migration.archive_path)
        if PRESET_TABLE not in sql.casefold():
            continue
        relevant_number += 1
        if not quiet:
            print(f"Applying {relevant_number:2d}: {migration.archive_path}", file=sys.stderr)
        for statement in split_sql_statements(sql):
            if PRESET_TABLE not in statement.casefold():
                continue
            try:
                connection.executescript(statement)
            except sqlite3.Error as error:
                connection.close()
                raise ExtractionError(
                    f"{migration.archive_path}: preset-table migration failed: {error}"
                ) from error

    return connection


def decode_json(value: str | None, column: str, preset_id: str) -> object | None:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise ExtractionError(
            f"Preset {preset_id!r} has invalid JSON in {column}: {error.msg}"
        ) from error


def load_presets(connection: sqlite3.Connection) -> list[dict[str, object]]:
    """Read the current catalogue, retaining GG's exact EQ JSON payload."""

    try:
        rows = connection.execute(
            """
            SELECT id, display_name, alias_name, eq_preset_data, device_id,
                   preset_type, metadata, supported_mode
            FROM device_game_eq_presets
            ORDER BY display_name COLLATE NOCASE, id
            """
        ).fetchall()
    except sqlite3.Error as error:
        raise ExtractionError(f"GG's Engine database has no readable {PRESET_TABLE} table: {error}") from error

    presets: list[dict[str, object]] = []
    for row in rows:
        preset_id, name, alias, eq_json, device_id, preset_type, metadata_json, supported_mode = row
        presets.append(
            {
                "id": preset_id,
                "display_name": name,
                "alias_name": alias,
                "eq_preset_data": decode_json(eq_json, "eq_preset_data", preset_id),
                "device_id": device_id,
                "preset_type": preset_type,
                "metadata": decode_json(metadata_json, "metadata", preset_id),
                "supported_mode": supported_mode,
            }
        )
    return presets


def filter_presets(presets: Iterable[dict[str, object]], query: str | None) -> list[dict[str, object]]:
    if not query:
        return list(presets)
    needle = query.casefold()
    return [
        preset
        for preset in presets
        if needle in str(preset["display_name"]).casefold()
        or needle in str(preset["alias_name"]).casefold()
    ]


def render_json(presets: list[dict[str, object]], pretty: bool) -> str:
    return json.dumps(presets, indent=2 if pretty else None, ensure_ascii=False, sort_keys=False) + "\n"


def render_csv(presets: list[dict[str, object]]) -> str:
    output = io.StringIO(newline="")
    fields = [
        "id",
        "display_name",
        "alias_name",
        "device_id",
        "preset_type",
        "supported_mode",
        "eq_preset_data",
        "metadata",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for preset in presets:
        writer.writerow(
            {
                **{field: preset[field] for field in fields[:6]},
                "eq_preset_data": json.dumps(preset["eq_preset_data"], ensure_ascii=False, separators=(",", ":")),
                "metadata": json.dumps(preset["metadata"], ensure_ascii=False, separators=(",", ":")),
            }
        )
    return output.getvalue()


def write_output(content: str, destination: str | None) -> None:
    if destination is None or destination == "-":
        sys.stdout.write(content)
        return
    path = Path(destination)
    path.write_text(content, encoding="utf-8")
    print(f"Wrote {path}", file=sys.stderr)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export SteelSeries GG Engine Game/Device EQ presets from a GG installer."
    )
    parser.add_argument(
        "--installer",
        type=Path,
        default=DEFAULT_INSTALLER,
        help=f"path to the SteelSeries GG installer (default: {DEFAULT_INSTALLER})",
    )
    parser.add_argument("--format", choices=("json", "csv"), default="json", help="output format (default: json)")
    parser.add_argument("--output", metavar="PATH", help="write to PATH instead of standard output; use - for stdout")
    parser.add_argument("--filter", metavar="TEXT", help="only export names or aliases containing TEXT")
    parser.add_argument("--compact", action="store_true", help="write compact JSON instead of indented JSON")
    parser.add_argument("--quiet", action="store_true", help="do not print migration progress")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = make_parser().parse_args(argv)
    try:
        installer = arguments.installer.expanduser()
        if not installer.is_file():
            raise ExtractionError(f"Installer not found: {installer}")
        executable = require_7z()
        migrations = list_migrations(executable, installer)
        connection = apply_preset_migrations(executable, installer, migrations, quiet=arguments.quiet)
        try:
            presets = filter_presets(load_presets(connection), arguments.filter)
        finally:
            connection.close()
        if arguments.format == "json":
            output = render_json(presets, pretty=not arguments.compact)
        else:
            output = render_csv(presets)
        write_output(output, arguments.output)
        print(f"Exported {len(presets)} GG Game/Device EQ preset(s).", file=sys.stderr)
        return 0
    except ExtractionError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
