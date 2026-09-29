from django.db import migrations
from django.utils import timezone


def sync_training_approval_statuses(apps, schema_editor):
    TrainingApprovalRequest = apps.get_model(
        "matrix",
        "TrainingApprovalRequest",
    )
    TrainingRecord = apps.get_model(
        "matrix",
        "TrainingRecord",
    )

    decided_requests = (
        TrainingApprovalRequest.objects
        .exclude(approved__isnull=True)
        .exclude(training_record__isnull=True)
        .iterator()
    )

    for approval_request in decided_requests:
        expected_status = (
            "approved"
            if approval_request.approved
            else "rejected"
        )
        decision_date = (
            approval_request.reviewed_at
            or timezone.now()
        )

        TrainingRecord.objects.filter(
            pk=approval_request.training_record_id,
        ).update(
            approval_status=expected_status,
            approved_by_id=approval_request.reviewed_by_id,
            approved_at=decision_date,
            rejection_reason=(
                ""
                if approval_request.approved
                else approval_request.rejection_reason
            ),
            updated_at=decision_date,
        )

        if approval_request.line_id:
            training_record = TrainingRecord.objects.get(
                pk=approval_request.training_record_id,
            )
            training_record.lines.add(
                approval_request.line_id,
            )


class Migration(migrations.Migration):
    dependencies = [
        (
            "matrix",
            "0050_trainingapprovalrequest_notification_seen",
        ),
    ]

    operations = [
        migrations.RunPython(
            sync_training_approval_statuses,
            migrations.RunPython.noop,
        ),
    ]
