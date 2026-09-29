from django.db import migrations
from django.utils import timezone


def resync_linked_training_approvals(apps, schema_editor):
    TrainingApprovalRequest = apps.get_model(
        "matrix",
        "TrainingApprovalRequest",
    )
    TrainingRecord = apps.get_model(
        "matrix",
        "TrainingRecord",
    )

    requests = (
        TrainingApprovalRequest.objects
        .exclude(approved__isnull=True)
        .exclude(training_record__isnull=True)
        .iterator()
    )

    for approval_request in requests:
        decision_date = (
            approval_request.reviewed_at
            or timezone.now()
        )
        is_approved = approval_request.approved is True

        TrainingRecord.objects.filter(
            pk=approval_request.training_record_id,
        ).update(
            approval_status=(
                "approved" if is_approved else "rejected"
            ),
            approved_by_id=approval_request.reviewed_by_id,
            approved_at=decision_date,
            rejection_reason=(
                ""
                if is_approved
                else approval_request.rejection_reason
            ),
            updated_at=decision_date,
        )

        if approval_request.line_id:
            training_record = TrainingRecord.objects.get(
                pk=approval_request.training_record_id,
            )
            training_record.lines.add(approval_request.line_id)


class Migration(migrations.Migration):
    dependencies = [
        (
            "matrix",
            "0051_sync_training_approval_statuses",
        ),
    ]

    operations = [
        migrations.RunPython(
            resync_linked_training_approvals,
            migrations.RunPython.noop,
        ),
    ]
