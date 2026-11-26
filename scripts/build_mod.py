import argparse
from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile

from build_release import digest, write_zip
from validate_translation import ROOT, check_translation, read_json


BEPINEX_SHA256 = "82f9878551030f54657792c0740d9d51a09500eeae1fba21106b0c441e6732c4"
DOORSTOP_SOURCE_SHA256 = "a71789ffcb359f6a37c6d2c7bbdd0527231e9db00bf46637693cff88ba7ffb80"
DOORSTOP_SOURCE_ROOT = "UnityDoorstop-33dab9a6733862eb81869ff08431d9478b28784b"
LOADER_REFERENCES = ("BepInEx/core/BepInEx.dll", "BepInEx/core/0Harmony.dll")
LICENSE_DIR = "ATC-RU-Licenses"


def verified_bytes(path, expected):
    payload = path.read_bytes()
    if digest(payload) != expected:
        raise ValueError(f"SHA-256 архива не совпал: {path}")
    return payload


def windows_bundle(bepinex_zip, doorstop_source_zip):
    with zipfile.ZipFile(io.BytesIO(verified_bytes(bepinex_zip, BEPINEX_SHA256))) as archive:
        files = {entry.filename: archive.read(entry) for entry in archive.infolist() if not entry.is_dir()}

    source = verified_bytes(doorstop_source_zip, DOORSTOP_SOURCE_SHA256)
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        files[f"{LICENSE_DIR}/UnityDoorstop-LICENSE.txt"] = archive.read(f"{DOORSTOP_SOURCE_ROOT}/LICENSE")

    files[f"{LICENSE_DIR}/UnityDoorstop-4.5.0-source.zip"] = source
    files[f"{LICENSE_DIR}/BepInEx-changelog.txt"] = files.pop("changelog.txt")
    files[f"{LICENSE_DIR}/THIRD-PARTY.txt"] = (ROOT / "mod/THIRD-PARTY.txt").read_bytes()
    return files


def build(game_dir, bepinex_dir, revision, output_dir, *, bepinex_zip=None, doorstop_source_zip=None):
    if not re.fullmatch(r"v[1-9][0-9]*", revision):
        raise ValueError("Версия выпуска должна быть v2, v3 и т. д.")
    game_dir = game_dir.resolve(strict=True)
    if output_dir.resolve().is_relative_to(game_dir):
        raise ValueError("Папка выпуска должна быть вне установки Steam")

    bundle = windows_bundle(bepinex_zip, doorstop_source_zip) if bepinex_zip else {}

    contract = read_json(ROOT / "locales/format.json")
    rows = read_json(ROOT / "locales/ru.json")
    errors = check_translation(rows, contract)
    if errors:
        raise ValueError("\n".join(errors))
    records = [{"key": key, "source": contract["source_sha256"][key], "text": value}
               for key, value in rows.items() if value is not None]

    csc = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        if bepinex_zip:
            bepinex_dir = temp
            for name in LOADER_REFERENCES:
                reference = temp / name
                reference.parent.mkdir(parents=True, exist_ok=True)
                reference.write_bytes(bundle[name])

        managed = game_dir / "AThousandCloys_Data/Managed"
        references = [managed / f"{name}.dll" for name in (
            "mscorlib", "System", "System.Core", "System.Runtime.Serialization", "System.Xml", "netstandard",
            "UnityEngine", "UnityEngine.CoreModule", "UnityEngine.AssetBundleModule", "UnityEngine.TextRenderingModule",
            "UnityEngine.TextCoreFontEngineModule", "UnityEngine.TextCoreTextEngineModule", "UnityEngine.InputModule",
            "Unity.InputSystem", "Unity.TextMeshPro", "Unity.ResourceManager",
        )] + [bepinex_dir / name for name in LOADER_REFERENCES]
        for reference in references:
            if not reference.is_file():
                raise FileNotFoundError(reference)

        dll = temp / "ATC.Russian.dll"
        subprocess.run([
            str(csc), "/nologo", "/noconfig", "/nostdlib+", "/codepage:65001", "/target:library",
            "/optimize+", "/out:" + str(dll),
            *["/reference:" + str(reference) for reference in references],
            str(ROOT / "mod/RussianTranslation.cs"),
        ], check=True)
        files = {
            "BepInEx/plugins/ATC-Russian/ATC.Russian.dll": dll.read_bytes(),
            "BepInEx/plugins/ATC-Russian/strings.json": json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8"),
            "ATC-Russian-README.txt": (ROOT / "mod/README.txt").read_bytes(),
        }

    edition = "Windows" if bepinex_zip else "BepInEx"
    target = output_dir / f"A-Thousand-Cloys-RU-{edition}-{revision}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    # BepInEx invalidates its plugin metadata cache by modification time.
    return target, write_zip(target, {**bundle, **files}, timestamp=datetime.now().timetuple()[:6])


def main():
    parser = argparse.ArgumentParser(description="Собрать русский мод A Thousand Cloys для BepInEx")
    parser.add_argument("--game-dir", type=Path, required=True)
    loader = parser.add_mutually_exclusive_group(required=True)
    loader.add_argument("--bepinex-zip", type=Path, help="Официальный ZIP BepInEx 5.4.23.5 x64 для полного комплекта")
    loader.add_argument("--bepinex-dir", type=Path, help="Распакованный BepInEx для сборки только плагина")
    parser.add_argument("--doorstop-source-zip", type=Path, help="ZIP исходников UnityDoorstop 4.5.0 для полного комплекта")
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if bool(args.bepinex_zip) != bool(args.doorstop_source_zip):
        parser.error("Для полного комплекта укажите --bepinex-zip и --doorstop-source-zip вместе")
    target, checksum = build(
        args.game_dir, args.bepinex_dir, args.revision, args.output_dir,
        bepinex_zip=args.bepinex_zip, doorstop_source_zip=args.doorstop_source_zip,
    )
    print(f"{target}\nSHA-256 {checksum}")


if __name__ == "__main__":
    main()
