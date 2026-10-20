"""The demo's scenes: which captured marks make each frame, how long each frame is held in the GIF, the
caption beside the phone and the mark used for the PNG still."""

from __future__ import annotations

from dataclasses import dataclass

GIF_MIN_S = 12.0
GIF_MAX_S = 20.0


@dataclass(frozen=True)
class Shot:
    mark: str
    hold_s: float


@dataclass(frozen=True)
class Scene:
    id: str
    title: str
    subtitle: str
    shots: tuple[Shot, ...]
    still: str  # the mark of the PNG still
    alt: str  # the still's alt text (README)


SCENES: tuple[Scene, ...] = (
    Scene(
        "01-proposal-card",
        "Gate 1: a proposal",
        "Plan, likely files and a size estimate, before any implementation compute is spent.",
        (Shot("01-proposal", 1.5), Shot("01-proposal-press", 0.5)),
        "01-proposal",
        "Telegram proposal card for acme/widgets issue 1234 with the problem, approach, likely "
        "files, size estimate, signals and the Start work, Skip, Adjust plan, Never, Later and Details buttons",
    ),
    Scene(
        "02-start-work-confirm",
        "Start work, then Confirm",
        "Two taps; the confirm text names the exact proposal version and hash.",
        (Shot("02-confirm-panel", 1.1), Shot("02-started", 0.8)),
        "02-confirm-panel",
        "Proposal card after the first tap on Start work: a confirm panel naming the "
        "proposal version, size class and hash, with Confirm start and Cancel buttons",
    ),
    Scene(
        "03-pr-preview",
        "Gate 2: the PR preview",
        "Built by the gatekeeper from the exact commit; size estimate versus actual, code and test split.",
        (Shot("03-preview", 2.0),),
        "03-preview",
        "PR preview card labelled DRY RUN with the change summary, the size estimate versus the actual diff "
        "split into code and test lines, the commit and branch, and the Approve and Submit, Reject, Explore, "
        "Files, Request changes and Edit text buttons",
    ),
    Scene(
        "04-diff-explorer",
        "Explore the diff",
        "Nine sections, all derived from the gatekeeper's mirror.",
        (Shot("04-explorer", 0.8), Shot("04-diff", 1.3)),
        "04-diff",
        "Diff explorer replying to the PR preview card, open at section 6 of 9, Diff, listing the two changed "
        "files with their hunk ranges and navigation buttons",
    ),
    Scene(
        "05-approve-dry-run",
        "Approve, then Confirm",
        "The confirm button names the mode: DRY RUN by default, nothing is published.",
        (Shot("05-press-approve", 0.5), Shot("05-dry-run-panel", 1.3), Shot("05-dry-run-done", 1.0)),
        "05-dry-run-panel",
        "PR preview card after Approve: a DRY RUN confirm panel stating that nothing will be published, "
        "with Confirm dry run and Cancel buttons",
    ),
    Scene(
        "06-submitted-live-status",
        "LIVE: submitted, then watched",
        "Here LIVE runs against the fake GitHub of the test harness.",
        (Shot("06-live-panel", 0.8), Shot("06-submitted", 1.5)),
        "06-submitted",
        "PR card marked Submitted, PR 501, with a live status card below it showing the "
        "PR state, CI checks, reviews, comments and the watch round",
    ),
    Scene(
        "07-dashboard-status",
        "Dashboard and /status",
        "The pinned dashboard in Control; /status sends a copy where you ask.",
        (Shot("07-status", 1.5),),
        "07-status",
        "Control topic with the pinned dashboard bar and the /status reply: state UP in DRY RUN, egress, "
        "running work, approvals, queue, daily limits, models and the Refresh, Approvals, Pause, Quiet, "
        "Down and Kill buttons",
    ),
    Scene(
        "08-assistant-answer",
        "The assistant",
        "Hermes answers in its own topic, streamed as drafts, kept apart from approvals.",
        (Shot("08-draft-1", 0.4), Shot("08-draft-2", 0.4), Shot("08-answer", 1.3)),
        "08-answer",
        "Assistant topic with a question about the two approval gates and the streamed answer, ending with "
        "the model footer",
    ),
    Scene(
        "09-kill",
        "/kill",
        "Stops every stage, job and outbound action; survives a reboot.",
        (Shot("09-kill", 1.6),),
        "09-kill",
        "Control topic after /kill: the bot confirms it is killed and how to resume, and the pinned "
        "dashboard bar shows the killed state",
    ),
)


def total_s() -> float:
    return round(sum(shot.hold_s for scene in SCENES for shot in scene.shots), 3)


def check() -> None:
    ids = [s.id for s in SCENES]
    if ids != sorted(ids) or len(set(ids)) != len(ids) or len(ids) != 9:
        raise ValueError(f"scene ids must be 9, unique and ordered: {ids}")
    for scene in SCENES:
        if scene.still not in {shot.mark for shot in scene.shots}:
            raise ValueError(f"{scene.id}: the still {scene.still} is not one of its shots")
        if not all(shot.mark.startswith(scene.id[:2]) for shot in scene.shots):
            raise ValueError(f"{scene.id}: a shot of another scene")
    if not GIF_MIN_S <= total_s() <= GIF_MAX_S:
        raise ValueError(f"GIF duration {total_s()} s outside {GIF_MIN_S}-{GIF_MAX_S} s")
