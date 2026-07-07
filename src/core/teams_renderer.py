from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

from src.core.exceptions import RenderError
from src.core.html_renderer import REPORTS_ROOT, TEMPLATES_ROOT, RenderContext, build_render_payload
from src.core.jinja_filters import JINJA_FILTERS, JINJA_GLOBALS


class TeamsRenderer:
    def __init__(
        self,
        edition_name: str,
        reports_root: Path = REPORTS_ROOT,
        templates_root: Path = TEMPLATES_ROOT,
    ) -> None:
        search_paths = [str(reports_root / edition_name / "templates"), str(templates_root)]
        self.environment = Environment(
            loader=FileSystemLoader(search_paths),
            autoescape=False,
            trim_blocks=False,
            lstrip_blocks=False,
            undefined=StrictUndefined,
        )
        self.environment.filters.update(JINJA_FILTERS)
        self.environment.globals.update(JINJA_GLOBALS)

    def render(self, context: RenderContext) -> str:
        return self.render_template("base.teams.j2", **build_render_payload(context))

    def render_template(self, template_name: str, /, **context: object) -> str:
        try:
            template = self.environment.get_template(template_name)
        except TemplateNotFound as exc:
            raise RenderError(f"Missing template: {template_name}") from exc
        return template.render(**context).strip() + "\n"