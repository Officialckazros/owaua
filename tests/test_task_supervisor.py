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
        supervisor = TaskSupervisor(restart_base_seconds=10)

        async def worker() -> None:
            raise RuntimeError("boom")

        with mock.patch("builtins.print") as output:
            supervisor.start_background("worker", worker)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        output.assert_called_once_with("[background] worker stopped: RuntimeError: boom")
        await supervisor.close()

    async def test_background_failure_restarts_with_health_record(self) -> None:
        supervisor = TaskSupervisor(restart_base_seconds=0, restart_max_seconds=0)
        attempts = 0
        running = asyncio.Event()

        async def worker() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary")
            running.set()
            await asyncio.Event().wait()

        with mock.patch("builtins.print"):
            supervisor.start_background("worker", worker)
            await running.wait()

        state = supervisor.health()
        self.assertEqual(attempts, 2)
        self.assertEqual(state["background"]["worker"]["failures"], 1)
        self.assertTrue(state["background"]["worker"]["running"])
        await supervisor.close()

    async def test_transient_limit_closes_and_counts_rejected_work(self) -> None:
        supervisor = TaskSupervisor(max_transient=1)
        waiting = asyncio.Event()

        async def worker() -> None:
            await waiting.wait()

        self.assertTrue(supervisor.start_transient(worker(), name="first"))
        rejected = worker()
        with mock.patch("builtins.print"):
            self.assertFalse(supervisor.start_transient(rejected, name="second"))

        state = supervisor.health()
        self.assertEqual(state["transient_running"], 1)
        self.assertEqual(state["transient_dropped"], 1)
        await supervisor.close()
