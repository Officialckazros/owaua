"""Ownership and shutdown for process-lifetime asyncio tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any


class TaskSupervisor:
    """Own named background work and short-lived event tasks."""

    def __init__(
        self,
        *,
        restart_base_seconds: float = 1.0,
        restart_max_seconds: float = 60.0,
        max_transient: int = 1_000,
    ) -> None:
        self._background: dict[str, asyncio.Task[Any]] = {}
        self._transient: set[asyncio.Task[Any]] = set()
        self._restart_base_seconds = max(0.0, float(restart_base_seconds))
        self._restart_max_seconds = max(
            self._restart_base_seconds, float(restart_max_seconds)
        )
        self._max_transient = max(1, int(max_transient))
        self._background_failures: dict[str, int] = {}
        self._background_last_error: dict[str, str] = {}
        self._transient_failures = 0
        self._transient_dropped = 0
        self._closing = False

    async def _run_background(
        self,
        name: str,
        coroutine_factory: Callable[[], Coroutine[Any, Any, Any]],
    ) -> None:
        consecutive_failures = 0
        while not self._closing:
            try:
                await coroutine_factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                consecutive_failures += 1
                self._background_failures[name] = self._background_failures.get(name, 0) + 1
                rendered = f"{type(error).__name__}: {error}"[:500]
                self._background_last_error[name] = rendered
                print(f"[background] {name} stopped: {rendered}")
                delay = min(
                    self._restart_max_seconds,
                    self._restart_base_seconds * (2 ** min(consecutive_failures - 1, 8)),
                )
                if delay:
                    await asyncio.sleep(delay)

    def start_background(
        self,
        name: str,
        coroutine_factory: Callable[[], Coroutine[Any, Any, Any]],
    ) -> None:
        """Start one named process-lifetime task, including after reconnects."""
        existing = self._background.get(name)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._run_background(name, coroutine_factory), name=f"owaua:{name}"
        )
        self._background[name] = task

        def _finished(done: asyncio.Task[Any]) -> None:
            if self._background.get(name) is done:
                self._background.pop(name, None)
            if not done.cancelled():
                try:
                    done.exception()
                except asyncio.CancelledError:
                    pass

        task.add_done_callback(_finished)

    def start_transient(
        self, coroutine: Coroutine[Any, Any, Any], *, name: str = "event"
    ) -> bool:
        """Keep bounded short-lived event work alive until completion."""
        if self._closing or len(self._transient) >= self._max_transient:
            coroutine.close()
            self._transient_dropped += 1
            if self._transient_dropped == 1 or self._transient_dropped % 100 == 0:
                print(
                    f"[transient] dropped {name}: limit {self._max_transient} reached "
                    f"({self._transient_dropped} total)"
                )
            return False
        task = asyncio.create_task(coroutine, name=f"owaua:{name}")
        self._transient.add(task)

        def _finished(done: asyncio.Task[Any]) -> None:
            self._transient.discard(done)
            if done.cancelled():
                return
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                self._transient_failures += 1
                print(f"[transient] {name} failed: {type(error).__name__}: {error}")

        task.add_done_callback(_finished)
        return True

    def health(self) -> dict[str, Any]:
        """Return content-free task diagnostics for health and admin surfaces."""
        background = {
            name: {
                "running": not task.done(),
                "failures": self._background_failures.get(name, 0),
                "last_error": self._background_last_error.get(name, ""),
            }
            for name, task in sorted(self._background.items())
        }
        return {
            "healthy": all(item["running"] for item in background.values())
            and self._transient_dropped == 0,
            "background": background,
            "transient_running": len(self._transient),
            "transient_limit": self._max_transient,
            "transient_failures": self._transient_failures,
            "transient_dropped": self._transient_dropped,
        }

    async def close(self) -> None:
        """Cancel and await every task still owned by this supervisor."""
        self._closing = True
        tasks = [*self._background.values(), *self._transient]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background.clear()
        self._transient.clear()
