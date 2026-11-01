"""Guards on ``strings.json``.

Home Assistant resolves an entity's or an action's name from a translation key
at runtime, so a key that is used in code but missing from ``strings.json``
fails silently — the entity simply shows its key, or an error renders as
"unknown error". Nothing in the test suite would otherwise notice, and
hassfest only catches it once the tree is pushed.

These checks are stdlib-only: the files are read as text and as JSON, so they
run without Home Assistant installed.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "tesla_telemetry"
)
_STRINGS = json.loads((_DIR / "strings.json").read_text(encoding="utf-8"))

# Entity translation keys assigned to an attribute or passed as ``key=``.
_KEY_PATTERNS = (
    re.compile(r'_attr_translation_key\s*=\s*"([a-z0-9_]+)"'),
    re.compile(r'self\._attr_translation_key\s*=\s*"([a-z0-9_]+)"'),
    re.compile(r'^\s*key="([a-z0-9_]+)"', re.MULTILINE),
)

#: Constructors that take their translation key as the final positional
#: argument — ``DoorBinarySensor(coordinator, slug, bit, key)`` and
#: ``WindowBinarySensor(coordinator, signal, slug, key)``.
_POSITIONAL_KEY_CONSTRUCTORS = frozenset({"DoorBinarySensor", "WindowBinarySensor"})

_PLATFORM_FILES = {
    "sensor": "sensor.py",
    "binary_sensor": "binary_sensor.py",
    "device_tracker": "device_tracker.py",
}


def _positional_keys(path: Path) -> set[str]:
    """Translation keys passed positionally to a door/window constructor.

    Parsed rather than pattern-matched on purpose: a bare quoted
    ``"window_front_driver"`` elsewhere in the module — a signal constant, a log
    line — must not count as a used translation key, or the orphan check below
    would keep passing after the real key was renamed away.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name not in _POSITIONAL_KEY_CONSTRUCTORS:
            continue
        last = node.args[-1]
        if isinstance(last, ast.Constant) and isinstance(last.value, str):
            keys.add(last.value)
    return keys


def _keys_used(platform: str) -> set[str]:
    path = _DIR / _PLATFORM_FILES[platform]
    text = path.read_text(encoding="utf-8")
    found = _positional_keys(path)
    for pattern in _KEY_PATTERNS:
        found |= set(pattern.findall(text))
    return found


def test_every_entity_translation_key_is_declared() -> None:
    for platform in _PLATFORM_FILES:
        used = _keys_used(platform)
        declared = set(_STRINGS.get("entity", {}).get(platform, {}))
        assert used, f"{platform}: no translation keys found in the source"
        assert used <= declared, (
            f"{platform}: used in code but missing from strings.json: "
            f"{sorted(used - declared)}"
        )


def test_no_orphaned_entity_translations() -> None:
    """A declared key nobody uses is dead weight and hides a rename."""
    for platform in _PLATFORM_FILES:
        used = _keys_used(platform)
        declared = set(_STRINGS.get("entity", {}).get(platform, {}))
        assert declared <= used, (
            f"{platform}: declared in strings.json but unused in code: "
            f"{sorted(declared - used)}"
        )


def test_every_entity_translation_has_a_name() -> None:
    for platform, entries in _STRINGS.get("entity", {}).items():
        for key, body in entries.items():
            assert body.get("name"), f"entity.{platform}.{key} has no name"


def test_entity_names_are_sentence_case() -> None:
    """Home Assistant's entity naming convention."""
    for platform, entries in _STRINGS.get("entity", {}).items():
        for key, body in entries.items():
            name = body["name"]
            assert name[0].isupper(), f"entity.{platform}.{key} not capitalised"
            assert not name.endswith("."), f"entity.{platform}.{key} ends with a dot"


def test_every_exception_translation_key_is_declared() -> None:
    """Every ``translation_key=`` on a raised HA error needs an entry."""
    declared = set(_STRINGS.get("exceptions", {}))
    used: dict[str, set[str]] = {}
    raise_call = re.compile(
        r"(?:ServiceValidationError|HomeAssistantError|ConfigEntryNotReady|"
        r"ConfigEntryAuthFailed)\("
    )
    for path in _DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for call in raise_call.finditer(text):
            # translation_key= is always within the first argument block
            match = re.search(r'translation_key="([a-z0-9_]+)"', text[call.end() : call.end() + 600])
            if match:
                used.setdefault(match.group(1), set()).add(path.name)

    assert set(used) <= declared, (
        f"raised but missing from strings.json exceptions: "
        f"{sorted(set(used) - declared)}"
    )
    assert declared <= set(used), (
        f"declared but never raised: {sorted(declared - set(used))}"
    )


def test_exception_placeholders_are_present_in_the_message() -> None:
    declared = _STRINGS.get("exceptions", {})
    for path in _DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r'translation_key="([a-z0-9_]+)",\s*translation_placeholders=\{([^}]*)\}',
            text,
        ):
            key, blob = match.group(1), match.group(2)
            message = declared.get(key, {}).get("message", "")
            for placeholder in re.findall(r'"([a-z_]+)":', blob):
                assert "{" + placeholder + "}" in message, (
                    f"{path.name}: {key} passes {placeholder!r} but the "
                    f"message has no {{{placeholder}}}"
                )


def test_services_declared_in_strings_match_services_yaml() -> None:
    services_yaml = (_DIR / "services.yaml").read_text(encoding="utf-8")
    yaml_services = {
        match.group(1)
        for match in re.finditer(r"^([a-z0-9_]+):$", services_yaml, re.MULTILINE)
    }
    declared = set(_STRINGS.get("services", {}))
    assert yaml_services == declared, (
        f"services.yaml {sorted(yaml_services)} != strings.json {sorted(declared)}"
    )


def test_services_carry_a_name_and_description() -> None:
    for service, body in _STRINGS.get("services", {}).items():
        assert body.get("name"), f"services.{service} has no name"
        assert body.get("description"), f"services.{service} has no description"
        for field, field_body in body.get("fields", {}).items():
            assert field_body.get("name"), f"services.{service}.fields.{field} has no name"


def test_translations_en_json_matches_strings_json() -> None:
    """``translations/en.json`` is generated from ``strings.json``."""
    en = json.loads((_DIR / "translations" / "en.json").read_text(encoding="utf-8"))
    assert en == _STRINGS, "translations/en.json is out of sync with strings.json"


def _leaf_paths(node: object, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    if not isinstance(node, dict):
        return {prefix}
    found: set[tuple[str, ...]] = set()
    for key, value in node.items():
        found |= _leaf_paths(value, (*prefix, key))
    return found


def test_no_translation_file_has_keys_that_do_not_exist() -> None:
    """A translated key ``strings.json`` does not define is silently ignored,
    which is how a typo survives review — the string simply never shows up."""
    known = _leaf_paths(_STRINGS)
    for path in sorted((_DIR / "translations").glob("*.json")):
        translated = _leaf_paths(json.loads(path.read_text(encoding="utf-8")))
        unknown = translated - known
        assert not unknown, (
            f"{path.name}: keys not in strings.json: "
            f"{sorted('.'.join(u) for u in unknown)}"
        )


def test_entity_names_are_unique_within_a_language() -> None:
    """Two entities of one device sharing a name collide in entity_id.

    English distinguishes ``charging_cable`` (the sensor reporting the cable
    *type*) from ``charge_cable`` (the binary sensor reporting presence), and a
    translation has to keep that distinction.
    """
    for path in sorted((_DIR / "translations").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        names = [
            body["name"]
            for platform in data.get("entity", {}).values()
            for body in platform.values()
        ]
        duplicates = {n for n in names if names.count(n) > 1}
        assert not duplicates, f"{path.name}: duplicate entity names {duplicates}"


def test_shipped_translations_are_complete() -> None:
    """A shipped translation must not fall back to English.

    Home Assistant merges per key, so a half-finished file silently renders as
    a mix of two languages. Only complete translations are published here;
    anything else belongs in a draft until it is finished.
    """
    expected = _leaf_paths(_STRINGS)
    for path in sorted((_DIR / "translations").glob("*.json")):
        if path.name == "en.json":
            continue
        translated = _leaf_paths(json.loads(path.read_text(encoding="utf-8")))
        missing = expected - translated
        assert not missing, (
            f"{path.name} is missing {len(missing)} string(s): "
            f"{sorted('.'.join(m) for m in missing)[:5]}"
        )
