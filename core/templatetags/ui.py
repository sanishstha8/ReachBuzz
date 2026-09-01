"""Small presentation helpers shared by the dashboard templates."""

from __future__ import annotations

from django import template

register = template.Library()

# Django message levels -> Bootstrap contextual colour names.
MESSAGE_LEVEL_COLOURS = {
    "debug": "secondary",
    "info": "info",
    "success": "success",
    "warning": "warning",
    "error": "danger",
}


@register.filter
def bootstrap_colour(level_tag: str) -> str:
    """Map a Django message level tag to a Bootstrap colour name."""
    return MESSAGE_LEVEL_COLOURS.get(str(level_tag), "secondary")


@register.filter
def field_errors(field) -> str:
    """Join a bound field's errors into one string for aria-describedby text."""
    return " ".join(str(error) for error in field.errors)
