"""Bundled locales are complete and consistent; operator overrides are applied on top."""
import json
import logging
import re
import string
from pathlib import Path

import pytest

from bot.middlewares.i18n import JsonI18n

ROOT = Path(__file__).resolve().parents[1]
LOCALES = {lang: json.loads((ROOT / "locales" / f"{lang}.json").read_text(encoding="utf-8"))
           for lang in ("ru", "en")}

# i18n calls with a literal key, and message keys returned by services
_CALL_RE = re.compile(
    r"""(?:\b_|get_text|_adm|_i|gettext|_t)\(\s*(?:[\w.]+\s*,\s*)?(?:key\s*=\s*)?["']([a-z][a-z0-9_]+)["']""")
_MESSAGE_KEY_RE = re.compile(r"""["']message_key["']\s*[:,]\s*["']([a-z][a-z0-9_]+)["']""")


def _keys_used_in_code():
    used = {}
    for path in (ROOT / "bot").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in list(_CALL_RE.finditer(text)) + list(_MESSAGE_KEY_RE.finditer(text)):
            used.setdefault(match.group(1), path.relative_to(ROOT).as_posix())
    return used


def test_ru_and_en_have_the_same_keys():
    ru, en = set(LOCALES["ru"]), set(LOCALES["en"])
    assert sorted(ru - en) == [] and sorted(en - ru) == []


def test_keys_used_in_code_exist():
    missing = {key: where for key, where in _keys_used_in_code().items()
               if any(key not in data for data in LOCALES.values())}
    assert missing == {}


def _placeholders(text):
    return {field for _, field, _, _ in string.Formatter().parse(text) if field}


def test_placeholders_match_between_languages():
    mismatched = {key: (_placeholders(LOCALES["ru"][key]), _placeholders(LOCALES["en"][key]))
                  for key in LOCALES["ru"] if key in LOCALES["en"]
                  and _placeholders(LOCALES["ru"][key]) != _placeholders(LOCALES["en"][key])}
    assert mismatched == {}


def _write(directory, name, content):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(content, encoding="utf-8")


def test_overrides_replace_only_listed_keys(tmp_path, caplog):
    base, overrides = tmp_path / "locales", tmp_path / "overrides"
    _write(base, "ru.json", json.dumps({"a": "база", "b": "оставить"}))
    _write(overrides, "ru.json", json.dumps({"a": "своё", "typo_key": "x"}))
    with caplog.at_level(logging.WARNING):
        i18n = JsonI18n(str(base), default="ru", overrides_path=str(overrides))
    assert i18n.gettext("ru", "a") == "своё"
    assert i18n.gettext("ru", "b") == "оставить"
    assert "typo_key" in caplog.text


def test_broken_override_file_keeps_bundled_texts(tmp_path, caplog):
    base, overrides = tmp_path / "locales", tmp_path / "overrides"
    _write(base, "en.json", json.dumps({"a": "base"}))
    _write(overrides, "en.json", "{not json")
    with caplog.at_level(logging.ERROR):
        i18n = JsonI18n(str(base), default="en", overrides_path=str(overrides))
    assert i18n.gettext("en", "a") == "base"
    assert "overrides" in caplog.text


@pytest.mark.parametrize("overrides_path", [None, "", "missing-dir"])
def test_no_overrides_is_fine(tmp_path, overrides_path):
    base = tmp_path / "locales"
    _write(base, "en.json", json.dumps({"a": "base"}))
    path = str(tmp_path / overrides_path) if overrides_path else overrides_path
    i18n = JsonI18n(str(base), default="en", overrides_path=path)
    assert i18n.gettext("en", "a") == "base"
