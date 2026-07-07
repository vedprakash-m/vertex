# Phase 1 — Chart config schema (JSON Schema for chart_config validation)
# Schema validation is done via jsonschema library in kusto_query_loader
# This file is the canonical schema definition referenced by the spec §5.1

CHART_CONFIG_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "ChartConfig",
    "description": "Declarative chart configuration for Vertex chart pipeline (R3)",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "type": {
            "type": "string",
            "enum": ["line", "bar", "stacked_bar", "scatter", "combined"],
            "description": "Chart type. 'combined' requires 'primary' and 'secondary' sub-configs."
        },
        "x_axis": {
            "type": "string",
            "description": "Column name for X axis values."
        },
        "y_axes": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 8,
            "description": "List of column names for Y axis series (1-8 series allowed)."
        },
        "y_axis_labels": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Optional human-readable labels for Y axis columns."
        },
        "goal_lines": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "label": {"type": "string"},
                    "value": {"type": "number"}
                },
                "required": ["label", "value"]
            },
            "description": "Optional horizontal goal/threshold lines."
        },
        "annotations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "x": {"type": "string", "description": "X axis value for annotation"},
                    "label": {"type": "string", "description": "Annotation text"}
                },
                "required": ["x", "label"]
            },
            "description": "Optional annotation markers on the chart."
        },
        "palette": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional color palette hints (hex colors)."
        },
        "summary_hint": {
            "type": "string",
            "enum": ["trend_up", "trend_down", "stable", "spike", "drop", "none"],
            "description": "Hint for trend description when renderer cannot auto-detect."
        },
        "primary": {
            "$ref": "#/definitions/sub_chart",
            "description": "Primary sub-chart config for combined chart type."
        },
        "secondary": {
            "$ref": "#/definitions/sub_chart",
            "description": "Secondary sub-chart config for combined chart type."
        }
    },
    "required": ["type", "x_axis", "y_axes"],
    "if": {
        "properties": {"type": {"const": "line"}},
        "required": ["type"]
    },
    "then": {
        "properties": {"x_axis": {"type": "string"}, "y_axes": {"type": "array", "minItems": 1}},
        "required": ["x_axis", "y_axes"]
    },
    # combined type requires primary and secondary
    "allOf": [
        {
            "if": {"properties": {"type": {"const": "combined"}}},
            "then": {
                "properties": {
                    "primary": {"$ref": "#/definitions/sub_chart"},
                    "secondary": {"$ref": "#/definitions/sub_chart"}
                },
                "required": ["primary", "secondary"]
            }
        }
    ],
    "definitions": {
        "sub_chart": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "type": {"type": "string", "enum": ["line", "bar"]},
                "x_axis": {"type": "string"},
                "y_axes": {"type": "array", "items": {"type": "string"}, "minItems": 1}
            },
            "required": ["type", "x_axis", "y_axes"]
        }
    }
}

# Fields that must NOT appear in chart_config (enforcing deferred features)
FORBIDDEN_CHART_CONFIG_KEYS = frozenset([
    "show_previous_issue",
    "comparison_rows",
    "interactive_spec",
    "vega_lite_config",
])