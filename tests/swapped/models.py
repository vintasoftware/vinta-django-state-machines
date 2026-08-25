"""The tenant model a project swaps in, standing in for a real ``Organization``."""

from __future__ import annotations

from django.db import models


class Organization(models.Model):
    """What ``STATE_MACHINES_SCOPE_MODEL`` points at in this settings module.

    It carries the two members the library asks of a scope model, which is what keeps an
    exported machine portable: the primary key does not travel between databases, the
    slug does.
    """

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=200, blank=True)

    class Meta:
        app_label = "swapped"

    def __str__(self) -> str:
        return self.slug

    @property
    def scope_key(self) -> str:
        return f"org:{self.slug}"

    @classmethod
    def from_scope_key(cls, key: str) -> Organization | None:
        return cls.objects.filter(slug=key.removeprefix("org:")).first()
