import argparse
from collections import defaultdict
import hashlib
import io
import json
import lzma
from pathlib import Path
import re
import struct
import sys
import zipfile
import zlib

from validate_translation import ROOT, check_translation, read_json


BASE = ROOT / "game-build.json"
UNITY_ASSET = "Assets/Data/Localization/Localization.json"
FONT_OPTIONS = 12522
LOCALE_OPTIONS = 14426


def digest(data):
    return hashlib.sha256(data).hexdigest()


def apply_delta(original, delta, expected_size):
    source = io.BytesIO(delta)
    if source.read(6) != b"ATCRU1":
        raise ValueError("Неверный заголовок бинарного патча")
    result = bytearray()
    while op := source.read(1):
        if op == b"\0":
            offset, size = struct.unpack("<II", source.read(8))
            if not size or offset + size > len(original) or len(result) + size > expected_size:
                raise ValueError("Выход за границы бинарного патча")
            result.extend(original[offset:offset + size])
        elif op == b"\1":
            size, = struct.unpack("<I", source.read(4))
            if not size or len(result) + size > expected_size:
                raise ValueError("Лишние данные бинарного патча")
            chunk = source.read(size)
            if len(chunk) != size:
                raise ValueError("Бинарный патч обрезан")
            result.extend(chunk)
        else:
            raise ValueError("Неизвестная операция бинарного патча")
    if len(result) != expected_size:
        raise ValueError("Неверный размер распакованного бинарного патча")
    return bytes(result)


def make_delta(original, modified):
    seed = 16
    anchors = defaultdict(list)
    for pos in range(0, len(original) - seed + 1, 4):
        points = anchors[original[pos:pos + seed]]
        if len(points) < 8:
            points.append(pos)
    result = bytearray(b"ATCRU1")
    pos = pending = 0

    def literal(end):
        if end > pending:
            result.extend(struct.pack("<BI", 1, end - pending))
            result.extend(modified[pending:end])

    while pos + seed <= len(modified):
        best_offset = best_size = 0
        for offset in anchors.get(modified[pos:pos + seed], ()):
            size = seed
            limit = min(len(modified) - pos, len(original) - offset)
            while size + 256 <= limit and original[offset + size:offset + size + 256] == modified[pos + size:pos + size + 256]:
                size += 256
            while size < limit and original[offset + size] == modified[pos + size]:
                size += 1
            if size > best_size:
                best_offset, best_size = offset, size
        if best_size < seed:
            pos += 1
            continue
        literal(pos)
        result.extend(struct.pack("<BII", 0, best_offset, best_size))
        pos += best_size
        pending = pos
    literal(len(modified))
    return bytes(result)


def unity_crc(data):
    from lz4.block import decompress

    def uncompress(raw, size, flags):
        mode = flags & 63
        if mode == 0:
            result = raw
        elif mode in (2, 3):
            result = decompress(raw, uncompressed_size=size)
        elif mode == 1:
            result = lzma.decompress(raw[:5] + struct.pack("<Q", size) + raw[5:], format=lzma.FORMAT_ALONE)
        else:
            raise ValueError(f"Неизвестный метод сжатия UnityFS {mode}")
        if len(result) != size:
            raise ValueError("Размер блока UnityFS не совпал")
        return result

    def align(offset):
        return (offset + 15) & ~15

    if not data.startswith(b"UnityFS\0"):
        raise ValueError("Ожидается UnityFS")
    version, = struct.unpack_from(">I", data, 8)
    offset = data.index(0, 12) + 1
    offset = data.index(0, offset) + 1
    total, compressed, uncompressed, flags = struct.unpack_from(">QIII", data, offset)
    if total != len(data):
        raise ValueError("Неверная длина UnityFS")
    offset += struct.calcsize(">QIII")
    if version >= 7:
        offset = align(offset)
    directory_offset = len(data) - compressed if flags & 0x80 else offset
    directory = uncompress(data[directory_offset:directory_offset + compressed], uncompressed, flags)
    if not flags & 0x80:
        offset += compressed
    if flags & 0x200:
        offset = align(offset)
    count, = struct.unpack_from(">I", directory, 16)
    crc = 0
    for index in range(count):
        size, packed, mode = struct.unpack_from(">IIH", directory, 20 + 10 * index)
        crc = zlib.crc32(uncompress(data[offset:offset + packed], size, mode), crc)
        offset += packed
    return crc


def read_original(game_dir, entry):
    installed = game_dir / entry["path"]
    backup = game_dir / ".atc-ru-backup" / (entry["id"] + ".original")
    for path in (installed, backup):
        if path.is_file():
            data = path.read_bytes()
            if digest(data) == entry["original"]:
                return data
    raise ValueError(f"Исходный файл нужной сборки не найден: {entry['path']}")


def repack_locale(original, rows):
    import UnityPy

    environment = UnityPy.load(original)
    path = environment.container[UNITY_ASSET].deref()
    asset = path.read()
    table = json.loads(asset.m_Script)
    if path.path_id != 7616933462121382825 or table.keys() != rows.keys():
        raise ValueError("Структура таблицы в Steam-сборке отличается")
    for key, value in rows.items():
        if value is not None:
            table[key]["0"] = value
    asset.m_Script = json.dumps(table, ensure_ascii=False)
    asset.save()
    payload = environment.file.save(packer="original")
    reopened = UnityPy.load(payload).container[UNITY_ASSET].deref()
    if reopened.path_id != path.path_id or json.loads(reopened.read().m_Script) != table:
        raise ValueError("Данные пересобранного бандла не совпали")
    return payload


def catalog_for(original, originals, outputs):
    if len(original) != 115210:
        raise ValueError("Размер каталога Addressables отличается")
    catalog = bytearray(original)
    for ident, offset in (("04", FONT_OPTIONS), ("05", LOCALE_OPTIONS)):
        if struct.unpack_from("<II", original, offset) != (unity_crc(originals[ident]), len(originals[ident])):
            raise ValueError(f"Каталог и исходный бандл {ident} не совпали")
        struct.pack_into("<II", catalog, offset, unity_crc(outputs[ident]), len(outputs[ident]))
    return bytes(catalog)


def release_entries(base, outputs, previous_zips):
    supported = {item["id"]: {item["modified"], *item["previous"]} for item in base["files"]}
    for path in previous_zips:
        with zipfile.ZipFile(path) as archive:
            previous = json.loads(archive.read("manifest.json"))
        if previous["steamBuild"] != base["steamBuild"] or previous["appId"] != base["appId"]:
            raise ValueError(f"Предыдущий архив для другой сборки: {path}")
        previous_by_id = {entry["id"]: entry for entry in previous["files"]}
        for entry in base["files"]:
            old = previous_by_id[entry["id"]]
            if old["original"] != entry["original"] or old["path"] != entry["path"]:
                raise ValueError(f"Предыдущий архив использует другие исходные данные: {path}")
            supported[entry["id"]].update({old["modified"], *old.get("previous", ())})
    entries = []
    for entry in base["files"]:
        ident = entry["id"]
        after = digest(outputs[ident])
        entries.append({**entry, "modified": after, "size": len(outputs[ident]),
                        "previous": sorted(supported[ident] - {after, entry["original"]})})
    return entries


def write_zip(target, files, *, timestamp=(1980, 1, 1, 0, 0, 0)):
    sidecar = target.with_suffix(".zip.sha256")
    if sidecar.exists() or sidecar.is_symlink():
        raise FileExistsError(f"Файл контрольной суммы уже существует: {sidecar}")
    sidecar_created = False
    with target.open("x+b") as handle:
        try:
            with zipfile.ZipFile(handle, "w") as archive:
                for name, payload in sorted(files.items()):
                    info = zipfile.ZipInfo(name, date_time=timestamp)
                    info.create_system = 0
                    archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            with zipfile.ZipFile(handle) as archive:
                if archive.testzip():
                    raise ValueError("Контрольное чтение ZIP не прошло")
            handle.seek(0)
            sha = digest(handle.read())
            with sidecar.open("x", encoding="ascii", newline="\n") as checksum:
                sidecar_created = True
                checksum.write(f"{sha}  {target.name}\n")
        except BaseException:
            handle.close()
            target.unlink()
            if sidecar_created:
                sidecar.unlink()
            raise
    return sha


def build(game, base_zip, revision, output_dir, previous_zips):
    base = read_json(BASE)
    game = game.resolve(strict=True)
    if output_dir.resolve().is_relative_to(game):
        raise ValueError("Папка выпуска должна быть вне установки Steam")
    if digest(base_zip.read_bytes()) != base["base_zip_sha256"]:
        raise ValueError("Нужен базовый ZIP русификатора с SHA-256 из game-build.json")
    contract = read_json(ROOT / "locales/format.json")
    rows = read_json(ROOT / "locales/ru.json")
    errors = check_translation(rows, contract)
    if errors:
        raise ValueError("\n".join(errors))
    originals = {entry["id"]: read_original(game, entry) for entry in base["files"]}
    outputs, patches = {}, {}
    with zipfile.ZipFile(base_zip) as support:
        for entry in base["files"][:4]:
            ident = entry["id"]
            name = f"patches/{ident}.atcd"
            patches[name] = support.read(name)
            outputs[ident] = apply_delta(originals[ident], patches[name], entry["size"])
            if digest(outputs[ident]) != entry["modified"]:
                raise ValueError(f"Базовый патч {ident} не совпал с манифестом")
    outputs["05"] = repack_locale(originals["05"], rows)
    outputs["06"] = catalog_for(originals["06"], originals, outputs)
    for ident in ("05", "06"):
        delta = make_delta(originals[ident], outputs[ident])
        if apply_delta(originals[ident], delta, len(outputs[ident])) != outputs[ident]:
            raise ValueError(f"Не удалось повторно применить патч {ident}")
        patches[f"patches/{ident}.atcd"] = delta
    entries = release_entries(base, outputs, previous_zips)
    metadata = {key: base[key] for key in ("gameVersion", "steamBuild", "appId")}
    manifest = {**metadata, "release": revision, "files": entries}
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"A-Thousand-Cloys-RU-Steam-{base['gameVersion']}-{revision}.zip"
    files = {**patches, "manifest.json": (json.dumps(manifest, indent=2) + "\n").encode("utf-8")}
    for name in ("Install-RU.ps1", "Install-RU.cmd", "Uninstall-RU.cmd", "README.txt"):
        files[name] = (ROOT / "installer" / name).read_bytes()
    return target, write_zip(target, files)


def main():
    parser = argparse.ArgumentParser(description="Сборка ZIP русификатора из локальной Steam-игры")
    parser.add_argument("--game-dir", type=Path, required=True)
    parser.add_argument("--base-zip", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--previous", type=Path, action="append", default=[])
    args = parser.parse_args()
    if not re.fullmatch(r"v[1-9][0-9]*", args.revision):
        parser.error("Ожидается версия v1, v2 и далее")
    try:
        output, sha = build(args.game_dir, args.base_zip, args.revision, args.out, args.previous)
    except (OSError, ValueError, KeyError, struct.error, zipfile.BadZipFile) as error:
        print(f"Сборка остановлена. {error}", file=sys.stderr)
        return 1

    print(f"{output}\nSHA-256 {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
