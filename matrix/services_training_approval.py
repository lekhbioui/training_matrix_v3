
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    TrainingApprovalRequest,
    TrainingRecord,
    TrainingLevel,
    MatrixEntry,
    Task,
)


@transaction.atomic
def approve_training_request(
    *,
    request_id,
    user,
    allow_missing_plan=False,
):
    """
    Approuver une demande et synchroniser la formation.

    allow_missing_plan=True :
        dérogation explicite utilisée par le superadmin.

    Répare également une ancienne approbation incomplète.
    """

    if not user.is_authenticated:
        raise ValidationError(
            "Utilisateur non authentifié."
        )

    if not (
        user.is_superuser
        or user.groups.filter(name__iexact="RH").exists()
    ):
        raise ValidationError(
            "Vous n'avez pas les droits d'approbation."
        )

    if allow_missing_plan and not user.is_superuser:
        raise ValidationError(
            "Seul un superadministrateur peut approuver sans plan."
        )

    approval = (
        TrainingApprovalRequest.objects
        .select_for_update()
        .select_related(
            "employee",
            "employee__line",
            "task",
            "line",
            "training_record",
        )
        .get(pk=request_id)
    )

    if approval.approved is False:
        raise ValidationError(
            "Cette demande a déjà été refusée."
        )

    line = approval.line or approval.employee.line

    if line is None:
        raise ValidationError(
            "Aucune ligne de production associée."
        )

    if not Task.objects.filter(
        pk=approval.task_id,
        lines=line,
    ).exists():
        raise ValidationError(
            "La tâche ne correspond pas à la ligne."
        )

    if not (
        approval.employee.visible_lines.filter(pk=line.pk).exists()
        or approval.employee.user_id is not None
        and line.team_leaders.filter(
            pk=approval.employee.user_id
        ).exists()
    ):
        raise ValidationError(
            "L'employé n'est pas visible sur cette ligne."
        )

    if not allow_missing_plan:
        approval.validate_training_plan_for_approval()

    level = approval.requested_level

    if level not in (
        TrainingLevel.IN_TRAINING,
        TrainingLevel.SUPERVISED,
        TrainingLevel.TRAINER,
    ):
        raise ValidationError(
            "Niveau de formation invalide."
        )

    training = approval.training_record

    # Retrouver uniquement un record en attente non lié
    # à une autre demande. Ne jamais réutiliser au hasard
    # une ancienne formation approuvée.
    if training is None:
        candidates = (
            TrainingRecord.objects
            .select_for_update()
            .filter(
                employee_id=approval.employee_id,
                task_id=approval.task_id,
                level=level,
                approval_status=(
                    TrainingRecord.ApprovalStatus.PENDING
                ),
                rh_approval_request__isnull=True,
            )
            .order_by("-created_at", "-pk")
        )

        training = candidates.filter(
            lines=line
        ).first()

        if training is None:
            training = candidates.filter(
                lines__isnull=True
            ).first()

    # Le niveau I exige son record et son questionnaire
    # préexistants.
    if level == TrainingLevel.IN_TRAINING:
        if training is None:
            raise ValidationError(
                "Formation niveau I introuvable."
            )

        if not hasattr(training, "questionnaire"):
            raise ValidationError(
                "Questionnaire niveau I introuvable."
            )

        if not training.questionnaire.is_compliant:
            raise ValidationError(
                "Le questionnaire niveau I n'est pas conforme."
            )

    if training is not None:
        if (
            training.employee_id != approval.employee_id
            or training.task_id != approval.task_id
            or training.level != level
        ):
            raise ValidationError(
                "La formation associée ne correspond pas à la demande."
            )

        # Ne jamais réapprouver un record refusé.
        if (
            training.approval_status
            == TrainingRecord.ApprovalStatus.REJECTED
        ):
            raise ValidationError(
                "La formation associée a déjà été refusée."
            )

    decision_date = timezone.now()

    # L/U : créer le record seulement s'il est absent.
    if training is None:
        training = TrainingRecord.objects.create(
            employee=approval.employee,
            task=approval.task,
            level=level,
            date=(
                approval.training_date
                or timezone.localdate()
            ),
            trainer=None,
            approval_status=(
                TrainingRecord.ApprovalStatus.APPROVED
            ),
            approved_by=user,
            approved_at=decision_date,
            rejection_reason="",
        )

    # Préserver l'auteur et la date d'approbation
    # historique lors d'une simple réparation.
    already_approved = approval.approved is True

    record_updates = {
        "approval_status": (
            TrainingRecord.ApprovalStatus.APPROVED
        ),
        "rejection_reason": "",
        "updated_at": decision_date,
    }

    if not already_approved or not training.approved_at:
        record_updates["approved_by_id"] = user.pk
        record_updates["approved_at"] = decision_date

    if training.date is None:
        record_updates["date"] = (
            approval.training_date
            or timezone.localdate()
        )

    TrainingRecord.objects.filter(
        pk=training.pk
    ).update(**record_updates)

    # La matrice filtre explicitement ce ManyToMany.
    training.lines.add(line)

    request_updates = {
        "approved": True,
        "training_record_id": training.pk,
        "line_id": line.pk,
        "rejection_reason": "",
    }

    if not already_approved:
        request_updates.update({
            "reviewed_by_id": user.pk,
            "reviewed_at": decision_date,
            "notification_seen": False,
        })

    TrainingApprovalRequest.objects.filter(
        pk=approval.pk
    ).update(**request_updates)

    training.refresh_from_db()

    # Vérifier exactement les critères de matrix_view().
    visible = TrainingRecord.objects.filter(
        pk=training.pk,
        employee_id=approval.employee_id,
        task_id=approval.task_id,
        level=level,
        lines=line,
        approval_status=(
            TrainingRecord.ApprovalStatus.APPROVED
        ),
    ).exists()

    if not visible:
        raise ValidationError(
            "La synchronisation de la formation a échoué."
        )

    # Mettre à jour le niveau maximal.
    entry, created = MatrixEntry.objects.select_for_update().get_or_create(
        employee_id=approval.employee_id,
        task_id=approval.task_id,
        defaults={
            "level": level,
            "last_trained": training.date,
        },
    )

    if not created:
        updates = {}

        if level > entry.level:
            updates["level"] = level

        if level >= entry.level:
            if (
                training.date
                and (
                    entry.last_trained is None
                    or training.date >= entry.last_trained
                )
            ):
                updates["last_trained"] = training.date

        if updates:
            MatrixEntry.objects.filter(
                pk=entry.pk
            ).update(**updates)

    return training
