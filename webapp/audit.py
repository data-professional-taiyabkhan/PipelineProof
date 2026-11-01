"""Append-only audit log: who did what, to which record, when."""
from __future__ import annotations

from . import db

ACTION_LABELS = {
    "org.created": "Created the organisation",
    "member.added": "Added a team member",
    "member.pending": "Invited a team member",
    "member.role_changed": "Changed a team member's role",
    "member.removed": "Removed a team member",
    "user.signed_in": "Signed in",
    "demo.entered": "Opened the assessor demo",
    "assessment.created": "Created an assessment",
    "assessment.closed": "Closed an assessment",
    "assessment.reopened": "Reopened an assessment",
    "invite.created": "Invited a candidate",
    "invite.withdrawn": "Withdrew an invite",
    "invite.opened": "Candidate opened their invite",
    "candidate.consented": "Candidate accepted the privacy notice",
    "submission.uploaded": "Candidate uploaded a submission",
    "submission.preflight_passed": "Pre-flight check passed",
    "submission.preflight_failed": "Pre-flight check failed",
    "submission.submitted": "Candidate submitted for evaluation",
    "submission.demo_run": "Started a live demo evaluation",
    "submission.rerun": "Re-ran an evaluation",
    "evaluation.started": "Evaluation started",
    "evaluation.finished": "Evaluation finished",
    "evaluation.error": "Evaluation failed with an error",
    "review.recorded": "Recorded a review decision",
    "feedback.released": "Released feedback to the candidate",
    "contest.opened": "Candidate contested the result",
    "contest.answered": "Answered a contest",
    "export.csv": "Exported results to CSV",
    "retention.deleted": "Deleted data past its retention period",
}


def log(org_id, actor_type: str, actor_label: str, action: str, entity_type: str | None = None,
        entity_id=None, details: dict | None = None, actor_id=None, c=None):
    db.run("""insert into pp.audit_log (org_id, actor_type, actor_id, actor_label, action, entity_type, entity_id, details)
              values (%s, %s, %s, %s, %s, %s, %s, %s)""",
           (org_id, actor_type, actor_id, actor_label[:200], action, entity_type,
            str(entity_id) if entity_id is not None else None, db.json(details or {})), c=c)


def by_ctx(ctx, action: str, entity_type=None, entity_id=None, details=None, c=None):
    log(ctx.org["id"] if ctx.org else None, ctx.actor_type, ctx.label, action, entity_type, entity_id, details,
        actor_id=ctx.actor_id, c=c)
