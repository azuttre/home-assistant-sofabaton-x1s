"""JobRunner lifecycle: shutdown drains non-cancellable work (review of 635ecfe, finding 3)."""

from __future__ import annotations

import asyncio
import threading

from sofabaton_server.jobs import JobRunner
from sofabaton_server.problems import problem_body


def test_shutdown_drains_a_non_cancellable_job_and_cancels_a_cancellable_one() -> None:
    async def main():
        started, release, completed = threading.Event(), threading.Event(), threading.Event()
        runner = JobRunner(problem_for=problem_body)

        def engine_write() -> None:
            started.set()
            assert release.wait(10)
            completed.set()

        async def restore(progress):
            await asyncio.to_thread(engine_write)
            return {"restored": 1}

        gate = asyncio.Event()

        async def refresh(progress):
            await gate.wait()

        restore_job = runner.start("hub-a", "restore", restore, cancellable=False)
        refresh_job = runner.start("hub-b", "refresh", refresh, cancellable=True)
        assert await asyncio.to_thread(started.wait, 2)

        # Release the engine thread a moment after shutdown starts waiting.
        async def release_later():
            await asyncio.sleep(0.2)
            release.set()

        asyncio.ensure_future(release_later())
        await runner.shutdown()
        assert completed.is_set()                      # shutdown waited for the write
        assert restore_job.status == "done" and restore_job.result == {"restored": 1}
        assert refresh_job.status == "cancelled"

    asyncio.run(main())


def test_shutdown_gives_up_on_a_stuck_job_after_the_drain_timeout() -> None:
    async def main():
        runner = JobRunner(problem_for=problem_body)
        forever = asyncio.Event()

        async def stuck(progress):
            await forever.wait()

        job = runner.start("hub", "restore", stuck, cancellable=False)
        await asyncio.sleep(0.01)
        await runner.shutdown(drain_timeout=0.1)
        assert job.status == "running"                 # honest: it is still running
        forever.set()
        await asyncio.sleep(0.01)
        assert job.status == "done"

    asyncio.run(main())
