"""A tiny keyed registry, shared by guards and side effects."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T", bound=Callable[..., object])


class AlreadyRegistered(RuntimeError):
    """Two different callables were registered under the same key."""


class NotRegistered(KeyError):
    """No callable is registered under the requested key."""

    def __init__(self, key: str, kind: str, known: list[str]) -> None:
        hint = ", ".join(sorted(known)[:20]) or "<none registered>"
        super().__init__(f"No {kind} registered under {key!r}. Known keys: {hint}.")
        self.key = key


@dataclass
class Registry(Generic[T]):
    """Maps stable string keys to callables.

    Registration is idempotent for the *same* object, so a module imported twice under
    different paths does not blow up, while a genuine key clash still does.
    """

    kind: str
    _entries: dict[str, T] = field(default_factory=dict, repr=False)

    def register(self, key: str, func: T, *, replace: bool = False) -> T:
        existing = self._entries.get(key)
        if existing is not None and existing is not func and not replace:
            raise AlreadyRegistered(
                f"{self.kind} {key!r} is already registered to "
                f"{existing.__module__}.{existing.__qualname__}."
            )
        self._entries[key] = func
        return func

    def __call__(self, key: str, *, replace: bool = False) -> Callable[[T], T]:
        """Use the registry itself as a decorator factory."""

        def decorator(func: T) -> T:
            return self.register(key, func, replace=replace)

        return decorator

    def get(self, key: str) -> T:
        try:
            return self._entries[key]
        except KeyError:
            raise NotRegistered(key, self.kind, list(self._entries)) from None

    def unregister(self, key: str) -> None:
        self._entries.pop(key, None)

    def keys(self) -> list[str]:
        return sorted(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._entries))

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()
