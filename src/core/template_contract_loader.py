from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from src.core.exceptions import ConfigError


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_ROOT = REPO_ROOT / "reports"
_SUPPORTED_RULE_CONDITIONS = {"baseline_available"}


@dataclass(frozen=True, slots=True)
class TemplateSectionRule:
    render_only_if: str | None = None


@dataclass(frozen=True, slots=True)
class TemplateFamilyContract:
    name: str
    order: tuple[str, ...]
    mandatory: tuple[str, ...]
    optional: tuple[str, ...]
    rules: dict[str, TemplateSectionRule]

    def rule_for(self, section_id: str) -> TemplateSectionRule | None:
        return self.rules.get(section_id)


@dataclass(frozen=True, slots=True)
class TemplateContract:
    schema_version: str
    default_family: str
    allowed_families: tuple[str, ...]
    families: dict[str, TemplateFamilyContract]

    def family_for(self, edition_family: str) -> TemplateFamilyContract | None:
        return self.families.get(edition_family)


def get_template_contract_path(edition_name: str, reports_root: Path = REPORTS_ROOT) -> Path:
    return reports_root / edition_name / "template_contract.yaml"


def load_template_contract_for_edition(
    edition_name: str,
    reports_root: Path = REPORTS_ROOT,
) -> TemplateContract | None:
    contract_path = get_template_contract_path(edition_name, reports_root=reports_root)
    if not contract_path.exists():
        return None
    return load_template_contract(contract_path)


def load_template_contract(path: Path) -> TemplateContract:
    with path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    if document.get("schema_version") != "1.0":
        raise ConfigError(f"Unsupported template contract schema version in {path}")

    family_settings = document.get("edition_family", {})
    if not isinstance(family_settings, dict):
        raise ConfigError(f"edition_family must be a mapping in {path}")

    default_family = family_settings.get("default")
    if not isinstance(default_family, str) or not default_family.strip():
        raise ConfigError(f"edition_family.default is required in {path}")

    allowed_families = _parse_identifier_list(path, family_settings.get("allowed", []), "edition_family.allowed")
    if default_family not in allowed_families:
        raise ConfigError(f"edition_family.default must be listed in edition_family.allowed in {path}")

    raw_families = document.get("families", {})
    if not isinstance(raw_families, dict) or not raw_families:
        raise ConfigError(f"families must be a non-empty mapping in {path}")

    families: dict[str, TemplateFamilyContract] = {}
    for family_name, raw_family in raw_families.items():
        if not isinstance(family_name, str) or not family_name.strip():
            raise ConfigError(f"Family names must be non-empty strings in {path}")
        if family_name not in allowed_families:
            raise ConfigError(f"Family {family_name!r} must be listed in edition_family.allowed in {path}")
        if not isinstance(raw_family, dict):
            raise ConfigError(f"Family {family_name!r} must be a mapping in {path}")

        order = _parse_identifier_list(path, raw_family.get("order", []), f"families.{family_name}.order")
        mandatory = _parse_identifier_list(
            path,
            raw_family.get("mandatory", []),
            f"families.{family_name}.mandatory",
        )
        optional = _parse_identifier_list(
            path,
            raw_family.get("optional", []),
            f"families.{family_name}.optional",
        )
        _ensure_subset(path, family_name, mandatory, order, field_name="mandatory")
        _ensure_subset(path, family_name, optional, order, field_name="optional")

        raw_rules = raw_family.get("rules", {})
        if not isinstance(raw_rules, dict):
            raise ConfigError(f"families.{family_name}.rules must be a mapping in {path}")
        rules: dict[str, TemplateSectionRule] = {}
        for section_id, raw_rule in raw_rules.items():
            if section_id not in order:
                raise ConfigError(
                    f"families.{family_name}.rules.{section_id} must reference a section listed in order in {path}"
                )
            if not isinstance(raw_rule, dict):
                raise ConfigError(f"families.{family_name}.rules.{section_id} must be a mapping in {path}")
            render_only_if = raw_rule.get("render_only_if")
            if render_only_if is not None:
                if not isinstance(render_only_if, str) or render_only_if not in _SUPPORTED_RULE_CONDITIONS:
                    supported = ", ".join(sorted(_SUPPORTED_RULE_CONDITIONS))
                    raise ConfigError(
                        f"families.{family_name}.rules.{section_id}.render_only_if must be one of {supported} in {path}"
                    )
            rules[section_id] = TemplateSectionRule(render_only_if=render_only_if)

        families[family_name] = TemplateFamilyContract(
            name=family_name,
            order=order,
            mandatory=mandatory,
            optional=optional,
            rules=rules,
        )

    if default_family not in families:
        raise ConfigError(f"edition_family.default {default_family!r} must be defined under families in {path}")

    return TemplateContract(
        schema_version="1.0",
        default_family=default_family,
        allowed_families=allowed_families,
        families=families,
    )


def _parse_identifier_list(path: Path, value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{field_name} must be a list in {path}")
    identifiers: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(f"{field_name} entries must be non-empty strings in {path}")
        identifiers.append(entry.strip())
    if len(set(identifiers)) != len(identifiers):
        raise ConfigError(f"{field_name} contains duplicate entries in {path}")
    return tuple(identifiers)


def _ensure_subset(
    path: Path,
    family_name: str,
    subset: tuple[str, ...],
    order: tuple[str, ...],
    *,
    field_name: str,
) -> None:
    missing = [section_id for section_id in subset if section_id not in order]
    if missing:
        raise ConfigError(
            f"families.{family_name}.{field_name} entries must appear in order in {path}: {', '.join(missing)}"
        )