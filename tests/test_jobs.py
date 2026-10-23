"""Job state rules and the scheduling queue, tested without Qt."""

import pytest

from fileconverter.services.jobs import (
    ALLOWED_TRANSITIONS,
    CancelOutcome,
    InvalidTransitionError,
    Job,
    JobQueue,
    JobState,
)


def job_in(state):
    job = Job(1, source=None)
    job.state = state
    return job


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (JobState.READY, JobState.QUEUED),
        (JobState.QUEUED, JobState.RUNNING),
        (JobState.QUEUED, JobState.CANCELLED),
        (JobState.RUNNING, JobState.CANCELLING),
        (JobState.RUNNING, JobState.SUCCEEDED),
        (JobState.CANCELLING, JobState.CANCELLED),
        (JobState.CANCELLING, JobState.SUCCEEDED),  # output committed before the request
    ],
)
def test_allowed_transitions(start, end):
    job = job_in(start)
    job.transition_to(end)
    assert job.state is end


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (JobState.READY, JobState.RUNNING),  # must be queued first
        (JobState.READY, JobState.SUCCEEDED),
        (JobState.QUEUED, JobState.SUCCEEDED),  # cannot finish without running
        (JobState.SUCCEEDED, JobState.CANCELLED),  # terminal states are final
        (JobState.CANCELLED, JobState.QUEUED),
        (JobState.FAILED, JobState.RUNNING),
        (JobState.UNSUPPORTED, JobState.QUEUED),
    ],
)
def test_forbidden_transitions(start, end):
    job = job_in(start)
    with pytest.raises(InvalidTransitionError):
        job.transition_to(end)
    assert job.state is start


def test_every_state_has_transition_rules():
    assert set(ALLOWED_TRANSITIONS) == set(JobState)


def test_queue_respects_concurrency_limit():
    queue = JobQueue(max_running=2)
    for job_id in (1, 2, 3):
        queue.submit(job_id)

    started = [job_id for job_id, _ in queue.take_startable()]

    assert started == [1, 2]
    assert queue.take_startable() == []  # no free slot
    queue.finished(1)
    assert [job_id for job_id, _ in queue.take_startable()] == [3]


def test_same_job_cannot_be_scheduled_twice():
    queue = JobQueue(max_running=1)
    queue.submit(1)
    with pytest.raises(ValueError):
        queue.submit(1)
    queue.take_startable()
    with pytest.raises(ValueError):
        queue.submit(1)  # also not while running


def test_cancel_pending_job_removes_it():
    queue = JobQueue(max_running=1)
    queue.submit(1)
    queue.submit(2)
    queue.take_startable()  # 1 runs, 2 waits

    assert queue.cancel(2) is CancelOutcome.REMOVED
    queue.finished(1)
    assert queue.take_startable() == []  # 2 never starts
    assert queue.is_idle()


def test_cancel_running_job_sets_its_event_only():
    queue = JobQueue(max_running=2)
    queue.submit(1)
    queue.submit(2)
    events = dict(queue.take_startable())

    assert queue.cancel(1) is CancelOutcome.REQUESTED
    assert events[1].is_set()
    assert not events[2].is_set()


def test_cancel_unknown_or_finished_job():
    queue = JobQueue(max_running=1)
    assert queue.cancel(99) is CancelOutcome.NOT_ACTIVE
    queue.submit(1)
    queue.take_startable()
    queue.finished(1)
    assert queue.cancel(1) is CancelOutcome.NOT_ACTIVE


def test_finished_releases_resources():
    queue = JobQueue(max_running=1)
    queue.submit(1)
    queue.take_startable()
    queue.finished(1)
    assert queue.running_count == 0
    assert queue.is_idle()


def test_invalid_limit_is_rejected():
    with pytest.raises(ValueError):
        JobQueue(max_running=0)
