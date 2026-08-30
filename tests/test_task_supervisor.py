from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from owaua.services.task_supervisor import TaskSupervisor


class TaskSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_named_background_task_starts_only_once_while_running(self) -> None:
        supervisor = TaskSupervisor()
        started = 0
        waiting = asyncio.Event()

        async def worker() -> None:
            nonlocal started
            started += 1
            await waiting.wait()

        supervisor.start_background("worker", worker)
        supervisor.start_background("worker", worker)
        await asyncio.sleep(0)

        self.assertEqual(started, 1)
        await supervisor.close()

    async def test_completed_background_task_can_be_started_again(self) -> None:
        supervisor = TaskSupervisor()
        started = 0

        async def worker() -> None:
            nonlocal started
            started += 1

        supervisor.start_background("worker", worker)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        supervisor.start_background("worker", worker)
        await asyncio.sleep(0)

        self.assertEqual(started, 2)
        await supervisor.close()

    async def test_close_cancels_and_awaits_background_and_transient_tasks(self) -> None:
        supervisor = TaskSupervisor()
        started = asyncio.Event()
        finished = 0

        async def worker() -> None:
            nonlocal finished
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished += 1

        supervisor.start_background("background", worker)
        supervisor.start_transient(worker())
        await started.wait()
        await asyncio.sleep(0)
        await supervisor.close()
        await supervisor.close()

        self.assertEqual(finished, 2)

    async def test_background_failure_keeps_existing_diagnostic(self) -> None:
        supervisor = TaskSupervisor()

        async def worker() -> None:
            raise RuntimeError("boom")

        with mock.patch("builtins.print") as output:
            supervisor.start_background("worker", worker)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        output.assert_called_once_with("[background] worker stopped: RuntimeError: boom")
        await supervisor.close()
