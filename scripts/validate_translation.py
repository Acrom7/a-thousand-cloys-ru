from collections import Counter
from pathlib import Path
import argparse
import json
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = r"%[A-Za-z][A-Za-z0-9_.:-]*%"
TOKENS = re.compile(rf"\[{PLACEHOLDER}\]|{PLACEHOLDER}(?:(?!{PLACEHOLDER})%)?|<[^<>\n]+>|%")
TAGS = re.compile(r"<[^<>\n]+>")
INVALID_UNICODE = re.compile(r"[\ud800-\udfff]")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Повторяющийся ключ {key!r}")
        result[key] = value
    return result


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)


def check_translation(rows, contract):
    if not isinstance(rows, dict):
        return ["Ожидается JSON-объект с ключами перевода."]
    keys = set(contract["keys"])
    blank = set(contract["blank"])
    font_chars = set(contract["font_characters"])
    problems = [f"Пропущен ключ {key}" for key in sorted(keys - rows.keys())]
    problems += [f"Лишний ключ {key}" for key in sorted(rows.keys() - keys)]
    for key in contract["keys"]:
        if key not in rows:
            continue
        ru = rows[key]
        if key in blank:
            if ru is not None:
                problems.append(f"{key} — служебный ключ, требуется null")
            continue
        if not isinstance(ru, str) or not ru.strip():
            problems.append(f"{key} — требуется непустая строка на русском")
            continue
        if INVALID_UNICODE.search(ru):
            problems.append(f"{key} — непарный суррогат Unicode")
            continue
        unsupported = {char for char in ru if char not in font_chars and char not in "\n\r\t"}
        if unsupported:
            codes = ", ".join(f"{c!r} (U+{ord(c):04X})" for c in sorted(unsupported))
            problems.append(f"{key} — символы вне атласа шрифта: {codes}")
        wanted = contract["tokens"].get(key, [])
        if Counter(TOKENS.findall(ru)) != Counter(wanted):
            problems.append(f"{key} — ожидаются плейсхолдеры и теги {wanted}")
        elif TAGS.findall(ru) != [token for token in wanted if token.startswith("<")]:
            problems.append(f"{key} — нарушен порядок тегов")
    return problems


def main():
    parser = argparse.ArgumentParser(description="Проверка русского перевода A Thousand Cloys")
    parser.add_argument("path", type=Path, nargs="?", default=ROOT / "locales/ru.json")
    args = parser.parse_args()
    try:
        contract = read_json(ROOT / "locales/format.json")
        problems = check_translation(read_json(args.path), contract)
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print(f"Проверено {len(contract['keys']) - len(contract['blank'])} строк, ошибок нет.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
