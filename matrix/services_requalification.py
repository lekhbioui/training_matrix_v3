"""Creation and decisions shared by the existing requalification entry points."""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone

from .models import (
    Employee, MatrixEntry, RequalificationApprovalRequest,
    RequalificationRecord, Task, TrainingRecord,
    delete_unused_requalification_plan,
)


LEADER_REQUALIFICATION_DAYS = 188


def leader_requalification_block_reason(employee, line, today):
    """Only the supervisor's global leader/line workflow uses this policy."""
    if RequalificationApprovalRequest.objects.filter(
        employee=employee, line=line, approved__isnull=True,
    ).exists():
        return "Une demande de requalification est déjà en attente pour ce chef et cette ligne."
    if RequalificationRecord.objects.filter(
        employee=employee, line=line, task__isnull=True,
        requalification_date__gte=today - timedelta(days=LEADER_REQUALIFICATION_DAYS),
    ).exists():
        return "Ce chef est déjà requalifié sur cette ligne. Le renouvellement est autorisé à partir de J+189."
    return None


def leader_tasks_to_requalify(employee, line, today):
    """Shared eligibility for the leader page and the transactional creation."""
    if leader_requalification_block_reason(employee, line, today):
        return []
    threshold_date = today - timedelta(days=LEADER_REQUALIFICATION_DAYS)
    overdue = []
    for task in Task.objects.filter(lines=line):
        last = TrainingRecord.objects.filter(
            employee=employee, task=task, lines=line,
        ).order_by("-date").first()
        if not last or not last.date:
            overdue.append({
                "task": task, "task_str": str(task), "last_date": None,
                "days_since": None, "days_overdue": None, "urgency": "critical",
                "current_level": 0, "current_level_display": "Jamais formé",
            })
            continue
        if last.date > threshold_date:
            continue
        if RequalificationRecord.objects.filter(
            employee=employee, line=line, task=task,
            requalification_date__gte=threshold_date,
        ).exists():
            continue
        days = (today - last.date).days
        overdue.append({
            "task": task, "task_str": str(task), "last_date": last.date,
            "days_since": days, "days_overdue": days - LEADER_REQUALIFICATION_DAYS,
            "urgency": "critical" if days > 250 else "warning" if days > 200 else "info",
            "current_level": last.level, "current_level_display": last.get_level_display(),
        })
    return overdue


def _validate_and_lock_leader_requalification(employee, line, user, date):
    # A no-op row write serializes competing creations until commit, including
    # on SQLite where select_for_update() does not lock rows. No data is changed.
    Employee.objects.filter(pk=employee.pk).update(first_name=F("first_name"))
    employee.refresh_from_db()
    if (not employee.user_id or employee.user_id == user.pk or not line
            or not line.team_leaders.filter(pk=employee.user_id).exists()):
        raise PermissionDenied("Ce chef n’est pas affecté à la ligne sélectionnée.")
    today = timezone.now().date()
    reason = leader_requalification_block_reason(employee, line, today)
    # A backdated submission cannot place two requalifications inside 188 days.
    if not reason and date != today:
        reason = leader_requalification_block_reason(employee, line, date)
    if reason:
        raise ValidationError(reason)
    if not leader_tasks_to_requalify(employee, line, today):
        raise ValidationError("Aucune tâche ne nécessite de requalification pour ce chef sur cette ligne.")


def is_requalification_rh(user):
    return user.is_authenticated and (
        user.is_superuser or user.groups.filter(name__iexact="RH").exists()
    )


def is_requalification_management(user):
    """Existing management roles take precedence over team assignments."""
    return user.is_authenticated and (
        is_requalification_rh(user)
        or user.groups.filter(name__iexact="Superviseur").exists()
    )


def can_add_requalification_plan(user):
    """A chief is identified by their lines or managed employees, not admin rights."""
    return user.is_authenticated and not is_requalification_management(user) and (
        user.managed_lines.exists() or user.managed_employees.exists()
    )


def plan_display(approval_request, user, *, is_rh=None):
    if not approval_request or not approval_request.has_training_plan:
        return {"training_plan_url": None, "training_plan_label": "Plan indisponible"}
    if is_rh is None:
        is_rh = is_requalification_rh(user)
    if not is_rh and approval_request.requested_by_id != user.pk:
        return {"training_plan_url": None, "training_plan_label": "Accès réservé"}
    return {"training_plan_url": reverse("requalification_plan_view", args=[approval_request.pk]),
            "training_plan_label": "Voir le plan"}


def create_requalification(*, form, user, direct_leader=False):
    if not user.is_authenticated:
        raise PermissionDenied
    if not form.is_valid():
        raise ValidationError("Le formulaire de requalification est invalide.")
    data = form.cleaned_data
    employee, line, task = data["employee_id"], data.get("line_id"), data.get("task_id")
    is_rh = is_requalification_rh(user)
    direct = is_rh or direct_leader
    if direct_leader:
        if not (is_rh or user.groups.filter(name="Superviseur").exists()):
            raise PermissionDenied
        if employee.user_id == user.pk or task is not None:
            raise PermissionDenied
    elif not is_rh and employee.manager_id != user.pk:
        raise PermissionDenied

    date = data["requalification_date"]
    records = TrainingRecord.objects.filter(employee=employee, lines=line)
    last_record = (records.order_by("-date").first() if direct_leader else
                   records.filter(task=task).order_by("-date").first() if task and line else None)
    previous_level = last_record.level if last_record else (0 if direct_leader else None)
    new_level = data.get("new_level")
    if new_level is None:
        new_level = 0 if direct_leader else (previous_level if previous_level is not None else 0)
    original_date = last_record.date if last_record and last_record.date else date
    evaluator_name = data.get("requalifier_name", "")
    if direct_leader and not evaluator_name:
        rh_employee = Employee.objects.filter(
            Q(user=user) | Q(first_name__iexact=user.first_name, last_name__iexact=user.last_name)
            | Q(matricule__iexact=user.username)
        ).first()
        evaluator_name = (f"{rh_employee.first_name} {rh_employee.last_name}" if rh_employee
                          else user.get_full_name() or user.username)

    req = RequalificationApprovalRequest(
        employee=employee, task=task, line=line, previous_level=previous_level,
        requested_result=data["result"], requested_new_level=new_level,
        original_training_date=original_date, requalification_date=date,
        days_since_training=(date - original_date).days, notes=data.get("notes", ""),
        requalifier_name=evaluator_name, requested_by=user,
    )
    field = req._meta.get_field("training_plan_image")
    storage = field.storage
    stored_name = None
    try:
        with transaction.atomic():
            if direct_leader:
                _validate_and_lock_leader_requalification(employee, line, user, date)
            if not direct and RequalificationApprovalRequest.objects.filter(
                employee=employee, task=task, line=line, approved__isnull=True,
            ).exists():
                raise ValidationError("Une demande est déjà en attente pour cet employé, cette tâche et cette ligne.")
            # Recheck the actor here; never trust an injected form field.
            if can_add_requalification_plan(user):
                uploaded = data.get("training_plan_image")
                if not uploaded:
                    raise ValidationError("Le plan de formation est obligatoire pour créer une requalification.")
                uploaded.seek(0)
                stored_name = storage.save(field.generate_filename(req, uploaded.name), uploaded)
                req.training_plan_image = stored_name
            req.save(force_insert=True)
            if direct:
                _decide_requalification(req=req, user=user, approve=True, decision_notes="")
    except Exception:
        req.training_plan_image.close()
        delete_unused_requalification_plan(storage, stored_name)
        raise
    finally:
        req.training_plan_image.close()
    return req


@transaction.atomic
def decide_requalification(*, request_id, user, approve, decision_notes=""):
    # Keep the existing decision-page permission: superuser only.
    if not user.is_authenticated or not user.is_superuser:
        raise PermissionDenied
    req = RequalificationApprovalRequest.objects.select_for_update().select_related(
        "employee", "line", "task", "requested_by",
    ).get(pk=request_id)
    return _decide_requalification(req=req, user=user, approve=approve, decision_notes=decision_notes)


def _decide_requalification(*, req, user, approve, decision_notes):
    """Called only inside the creation/decision transaction after authorization."""
    if req.approved is not None:
        raise ValidationError("Cette demande a déjà été traitée.")
    if approve:
        # No status, record or line write may precede this check.
        req.validate_training_plan_for_approval()
    decision_date = timezone.now()
    updated = RequalificationApprovalRequest.objects.filter(
        pk=req.pk, approved__isnull=True,
    )._record_decision(
        approved=approve, decision_by=user, decision_date=decision_date,
        decision_notes=decision_notes, notification_seen=False,
    )
    if updated != 1:
        raise ValidationError("Cette demande a déjà été traitée.")
    req.approved = approve
    req.decision_by = user
    req.decision_date = decision_date
    req.decision_notes = decision_notes
    if not approve:
        return req

    line = req.line or req.employee.line
    training = None
    if req.task_id and line:
        training = TrainingRecord.objects.create(
            employee=req.employee, task=req.task, level=req.requested_new_level,
            date=req.requalification_date, trainer=None,
        )
        training.lines.set([line])
        MatrixEntry.objects.update_or_create(
            employee=req.employee, task=req.task, defaults={"level": req.requested_new_level},
        )
    RequalificationRecord.objects.create(
        approval_request=req, training_record=training,
        employee=req.employee, task=req.task, line=line,
        previous_level=req.previous_level if req.previous_level is not None else 0,
        result=req.requested_result, new_level=req.requested_new_level,
        original_training_date=req.original_training_date,
        requalification_date=req.requalification_date, days_since_training=req.days_since_training,
        evaluated_by=req.requested_by, notes=req.notes, requalifier_name=req.requalifier_name,
    )
    return req
