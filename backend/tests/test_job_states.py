from datetime import timedelta

import pytest

from app.db.models import DocumentStatus, JobStatus
from app.ingestion.jobs import (
    DOCUMENT_TRANSITIONS,
    InvalidTransitionError,
    check_document_transition,
    check_job_transition,
    retry_delay,
    sanitize_error_message,
)

D = DocumentStatus
J = JobStatus


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (D.PENDING, D.PROCESSING),
        (D.PROCESSING, D.COMPLETED),
        (D.PROCESSING, D.FAILED),
        (D.PROCESSING, D.PENDING),
        (D.COMPLETED, D.PENDING),
        (D.FAILED, D.PENDING),
    ],
)
def test_allowed_document_transitions(current: DocumentStatus, target: DocumentStatus) -> None:
    check_document_transition(current.value, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (D.PENDING, D.COMPLETED),  # never completed without processing
        (D.PENDING, D.FAILED),
        (D.COMPLETED, D.FAILED),  # a completed index is never thrown away by a failure
        (D.COMPLETED, D.PROCESSING),
        (D.FAILED, D.COMPLETED),
        (D.FAILED, D.PROCESSING),
        (D.PENDING, D.PENDING),
    ],
)
def test_forbidden_document_transitions(current: DocumentStatus, target: DocumentStatus) -> None:
    with pytest.raises(InvalidTransitionError):
        check_document_transition(current.value, target)


def test_every_document_state_has_a_way_forward() -> None:
    assert all(DOCUMENT_TRANSITIONS[state] for state in DocumentStatus)


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (J.PENDING, J.PROCESSING, True),
        (J.PROCESSING, J.COMPLETED, True),
        (J.PROCESSING, J.FAILED, True),
        (J.PROCESSING, J.PENDING, True),
        (J.PENDING, J.COMPLETED, False),
        (J.COMPLETED, J.PENDING, False),
        (J.FAILED, J.PENDING, False),  # retries create a new job instead
        (J.COMPLETED, J.FAILED, False),
    ],
)
def test_job_transitions(current: JobStatus, target: JobStatus, allowed: bool) -> None:
    if allowed:
        check_job_transition(current.value, target)
    else:
        with pytest.raises(InvalidTransitionError):
            check_job_transition(current.value, target)


def test_retry_delay_is_exponential_and_capped() -> None:
    assert [retry_delay(30, attempt).total_seconds() for attempt in (1, 2, 3, 4)] == [
        30,
        60,
        120,
        240,
    ]
    assert retry_delay(30, 50) == timedelta(hours=1)
    assert retry_delay(0, 3) == timedelta(0)


def test_error_messages_are_single_line_and_bounded() -> None:
    message = sanitize_error_message("line one\n  line two\t" + "x" * 2000)
    assert "\n" not in message
    assert message.startswith("line one line two ")
    assert len(message) == 1000
