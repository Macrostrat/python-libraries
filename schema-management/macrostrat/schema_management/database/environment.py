"""Where an environment sits on the scale from a laptop to production."""

from enum import Enum


class EnvironmentClass(str, Enum):
    """One scale for which schema layers apply and which migrations are ready."""

    Local = "local"
    Development = "development"
    Staging = "staging"
    Production = "production"

    @property
    def rank(self) -> int:
        """Position on the scale: local 0 … production 3."""
        return _CLASS_ORDER.index(self)


_CLASS_ORDER = (
    EnvironmentClass.Local,
    EnvironmentClass.Development,
    EnvironmentClass.Staging,
    EnvironmentClass.Production,
)


def classes_up_to(top: EnvironmentClass) -> frozenset:
    """Every class from ``local`` up to and including *top*.

    ``classes_up_to(Development)`` is ``{local, development}`` — the set a
    development-only schema layer applies in.
    """
    top = EnvironmentClass(top)
    return frozenset(c for c in _CLASS_ORDER if c.rank <= top.rank)


def environment_class(value) -> EnvironmentClass:
    """A class from its name; anything unrecognized fails closed to production."""
    if isinstance(value, EnvironmentClass):
        return value
    # Another library's enum of the same classes is taken by its value.
    value = getattr(value, "value", value)
    try:
        return EnvironmentClass(str(value).strip().lower())
    except ValueError:
        return EnvironmentClass.Production
