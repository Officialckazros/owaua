"""Ownership and shutdown for process-lifetime asyncio tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any


class TaskSupervisor:
    """Own named background work and short-lived event tasks."""

    def __init__(self) -> None:
        self._background: dict[str, asyncio.Task[Any]] = {}
        self._transient: set[asyncio.Task[Any]] = set()

    def start_background(
        self,
        name: str,
        coroutine_factory: Callable[[], Coroutine[Any, Any, Any]],
    ) -> None:
        """Start one named process-lifetime task, including after reconnects."""
        existing = self._background.get(name)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(coroutine_factory(), name=f"owaua:{name}")
        self._background[name] = task

        def _finished(done: asyncio.Task[Any]) -> None:
            if self._background.get(name) is done:
                self._background.pop(name, None)
            if done.cancelled():
                return
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                print(f"[background] {name} stopped: {type(error).__name__}: {error}")

        task.add_done_callback(_finished)

    def start_transient(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        """Keep a short-lived event task alive until completion."""
        task = asyncio.create_task(coroutine)
        self._transient.add(task)
        task.add_done_callback(self._transient.discard)

    async def close(self) -> None:
        """Cancel and await every task still owned by this supervisor."""
        tasks = [*self._background.values(), *self._transient]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background.clear()
        self._transient.clear()
