import logging

from django import forms
from django.contrib import messages
from django.http import JsonResponse
from django.http import FileResponse, Http404, HttpResponseForbidden
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.views.decorators.http import require_http_methods, require_safe
from django.views.decorators.cache import never_cache
from django.urls import reverse
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from matrix.decorators import superuser_required
from matrix.resources import User
from .models import Employee, LineTransfer, PendingEmployee, RequalificationApprovalRequest, Task, MatrixEntry, TrainingLevel
from .serializers import EmployeeSerializer, TaskSerializer, MatrixEntrySerializer
from django.shortcuts import redirect, get_object_or_404
from .forms import EmployeeExcelImportForm, EmployeeForm, EmployeeFormRH, EmployeeRHRegisterForm, EmployeeTransferForm, ProductionLineForm, RHTeamLeaderTrainingForm, TaskForm
from .models import TrainingRecord
from .forms import TrainingRecordForm
from .forms import TrainingPlanImageForm, RequalificationForm
from .services_requalification import create_requalification, decide_requalification, plan_display
from .services_requalification import LEADER_REQUALIFICATION_DAYS, leader_tasks_to_requalify
from django.db import DatabaseError, OperationalError
from django.db.models import Q
from .models import ProductionLine
from django.contrib.auth.decorators import login_required, user_passes_test
from .models import TrainingApprovalRequest
from django.utils import timezone

logger = logging.getLogger(__name__)

from django.contrib.auth.views import LoginView
from django.urls import reverse

class CustomLoginView(LoginView):
    """
    Vue de connexion personnalisée qui redirige :
    - Les super admins    → /rh (employee_list_rh)
    - Les comptes Qualité → /quality/
    - Les chefs d'équipe  → /matrix
    """
    template_name = 'matrix/login.html'

    def get_success_url(self):
        user = self.request.user

        # ✅ Super Admin → espace RH
        if user.is_superuser:
            return reverse('employee_list_rh')

        # ✅ Compte Qualité → espace Qualité
        if (
            hasattr(user, 'quality_profile')
            and user.quality_profile.is_active
        ):
            return reverse('quality_dashboard')

        # ✅ Chef d'équipe → matrice
        return reverse('matrix_view')
    
class EmployeeViewSet(viewsets.ModelViewSet):
    serializer_class = EmployeeSerializer
    permission_classes = [IsAuthenticated]
    queryset = Employee.objects.none()

    def get_queryset(self):
        user = self.request.user
        return Employee.objects.filter(manager=user)


class TaskViewSet(viewsets.ModelViewSet):
    serializer_class = TaskSerializer
    permission_classes = [IsAuthenticated]
    queryset = Task.objects.none()

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser or user.groups.filter(name="RH").exists():
            return Task.objects.all().order_by("order")
        lines = ProductionLine.objects.filter(team_leaders=user)
        return Task.objects.filter(line__in=lines).order_by("order")


class MatrixEntryViewSet(viewsets.ModelViewSet):
    serializer_class = MatrixEntrySerializer
    permission_classes = [IsAuthenticated]
    queryset = MatrixEntry.objects.none()

    def get_queryset(self):
        user = self.request.user
        return MatrixEntry.objects.filter(employee__manager=user)


def matrix_by_line(request, line_id):
    line = get_object_or_404(ProductionLine, id=line_id)
    employees = Employee.objects.filter(line=line).order_by("last_name", "first_name")
    tasks = Task.objects.all().order_by("order")

    matrix = []
    for task in tasks:
        row = {"task": task, "cells": []}
        for emp in employees:
            cell = MatrixEntry.objects.filter(employee=emp, task=task).first()
            row["cells"].append(cell)
        matrix.append(row)

    context = {
        "line": line,
        "employees": employees,
        "matrix": matrix,
    }
    return render(request, "matrix/matrix_by_line.html", context)


def line_list(request):
    lines = ProductionLine.objects.all().order_by("code")
    return render(request, "matrix/line_list.html", {"lines": lines})


@login_required
def matrix_view(request):
    user = request.user

    # ================================================================
    # NOTIFICATIONS DES DÉCISIONS RH
    # ================================================================

    my_decided_requests = (
        TrainingApprovalRequest.objects.filter(
            requested_by=user,
            approved__isnull=False,
            notification_seen=False,
        )
        .select_related(
            "employee",
            "task",
            "line",
        )
        .order_by("date_requested")
    )

    for approval_request in my_decided_requests:
        line_info = ""

        if approval_request.line:
            line_info = (
                f" [{approval_request.line.name}]"
            )

        if approval_request.approved is True:
            messages.success(
                request,
                (
                    "✅ Votre demande a été approuvée par RH : "
                    f"{approval_request.employee} - "
                    f"{approval_request.task}"
                    f"{line_info} "
                    f"(niveau "
                    f"{approval_request.get_requested_level_display()})"
                ),
            )

        elif approval_request.approved is False:
            refusal_message = (
                "❌ Votre demande a été refusée par RH : "
                f"{approval_request.employee} - "
                f"{approval_request.task}"
                f"{line_info} "
                f"(niveau "
                f"{approval_request.get_requested_level_display()})"
            )

            if approval_request.rejection_reason:
                refusal_message += (
                    f" — Motif : "
                    f"{approval_request.rejection_reason}"
                )

            messages.warning(
                request,
                refusal_message,
            )

        approval_request.notification_seen = True
        approval_request.save(
            update_fields=["notification_seen"]
        )

    # ================================================================
    # RÉCUPÉRATION DES LIGNES ACCESSIBLES
    # ================================================================

    selected_line_id = request.GET.get("line_id")

    all_user_lines = (
        ProductionLine.objects.filter(
            team_leaders=user
        )
        .prefetch_related("team_leaders")
        .order_by("code", "name")
    )

    has_multiple_lines = (
        all_user_lines.count() > 1
    )

    # ================================================================
    # TROUVER LE PROFIL EMPLOYEE DU CHEF CONNECTÉ
    # ================================================================

    team_leader_employee = None

    try:
        if hasattr(user, "employee_profile"):
            team_leader_employee = (
                user.employee_profile
            )
    except Exception:
        team_leader_employee = None

    if not team_leader_employee:
        team_leader_employee = (
            Employee.objects.filter(
                Q(user=user)
                | Q(
                    first_name__iexact=user.first_name,
                    last_name__iexact=user.last_name,
                )
                | Q(
                    matricule__iexact=user.username
                )
            )
            .select_related(
                "user",
                "manager",
                "line",
            )
            .first()
        )

    # ================================================================
    # SÉLECTION DE LA LIGNE
    # ================================================================

    if selected_line_id:
        try:
            selected_line_id = int(
                selected_line_id
            )

            lines = all_user_lines.filter(
                id=selected_line_id
            )

        except (ValueError, TypeError):
            selected_line_id = None
            lines = all_user_lines

    else:
        # Si le chef possède plusieurs lignes,
        # demander d'abord de sélectionner une ligne.
        if has_multiple_lines:
            lines_with_leaders = [
                {
                    "line": line,
                    "leaders": (
                        line.team_leaders.all()
                    ),
                }
                for line in all_user_lines
            ]

            context = {
                "lines": all_user_lines,
                "lines_with_leaders": (
                    lines_with_leaders
                ),
                "employees": [],
                "matrix": [],
                "selected_line_id": None,
                "has_multiple_lines": (
                    has_multiple_lines
                ),
                "notifications": [],
                "development_alerts": [],
                "completion_rate": 0,
                "total_missing": 0,
                "total_alerts": 0,
                "total_critical": 0,
                "total_warning": 0,
                "total_ready": 0,
                "show_line_selector": True,
                "team_leader_employee": (
                    team_leader_employee
                ),
            }

            return render(
                request,
                "matrix/matrix.html",
                context,
            )

        lines = all_user_lines

    current_line = lines.first()

    # ================================================================
    # AUCUNE LIGNE DISPONIBLE
    # ================================================================

    if not current_line:
        context = {
            "lines": all_user_lines,
            "lines_with_leaders": [],
            "current_line": None,
            "employees": [],
            "matrix": [],
            "selected_line_id": None,
            "has_multiple_lines": (
                has_multiple_lines
            ),
            "notifications": [],
            "development_alerts": [],
            "completion_rate": 0,
            "total_missing": 0,
            "total_alerts": 0,
            "total_critical": 0,
            "total_warning": 0,
            "total_ready": 0,
            "team_leader_employee": (
                team_leader_employee
            ),
            "show_line_selector": True,
        }

        return render(
            request,
            "matrix/matrix.html",
            context,
        )

    selected_line_id = current_line.id

    # ================================================================
    # RÉCUPÉRER LES EMPLOYÉS QUI SONT CHEFS D'ÉQUIPE
    # ================================================================

    team_leader_user_ids = (
        User.objects.filter(
            managed_lines__isnull=False
        )
        .distinct()
        .values_list("id", flat=True)
    )

    team_leader_emp_ids = set(
        Employee.objects.filter(
            user_id__in=team_leader_user_ids
        ).values_list("id", flat=True)
    )

    # Ajouter aussi les profils trouvés par matricule ou nom.
    team_leader_users = User.objects.filter(
        id__in=team_leader_user_ids
    )

    for leader_user in team_leader_users:
        leader_employee = (
            Employee.objects.filter(
                Q(user=leader_user)
                | Q(
                    first_name__iexact=(
                        leader_user.first_name
                    ),
                    last_name__iexact=(
                        leader_user.last_name
                    ),
                )
                | Q(
                    matricule__iexact=(
                        leader_user.username
                    )
                )
            )
            .only("id")
            .first()
        )

        if leader_employee:
            team_leader_emp_ids.add(
                leader_employee.id
            )

    # ================================================================
    # EMPLOYÉS DE LA LIGNE
    # ================================================================

    line_employees = (
        Employee.objects.filter(
            visible_lines=current_line,
            manager=user,
        )
        .exclude(
            id__in=team_leader_emp_ids
        )
        .select_related(
            "manager",
            "line",
            "user",
        )
        .prefetch_related(
            "visible_lines",
        )
        .distinct()
        .order_by(
            "last_name",
            "first_name",
        )
    )

    employees_list = list(line_employees)

    # Ajouter le chef connecté au début de la matrice.
    if team_leader_employee:
        employee_ids_in_list = {
            employee.id
            for employee in employees_list
        }

        if (
            team_leader_employee.id
            not in employee_ids_in_list
        ):
            employees_list.insert(
                0,
                team_leader_employee,
            )

    # ================================================================
    # TÂCHES DE LA LIGNE
    # ================================================================

    tasks = list(
        Task.objects.filter(
            lines=current_line
        )
        .distinct()
        .order_by(
            "order",
            "task_number",
        )
    )

    employee_ids = [
        employee.id
        for employee in employees_list
    ]

    task_ids = [
        task.id
        for task in tasks
    ]

    # ================================================================
    # FORMATIONS APPROUVÉES
    # ================================================================

    # Important :
    # les formations PENDING ou REJECTED ne sont pas placées
    # dans training_dict et ne sont donc pas affichées comme
    # niveaux acquis dans la matrice.
    all_training_records = (
        TrainingRecord.objects.filter(
            employee_id__in=employee_ids,
            task_id__in=task_ids,
            lines=current_line,
            approval_status=(
                TrainingRecord
                .ApprovalStatus
                .APPROVED
            ),
        )
        .select_related(
            "employee",
            "task",
            "trainer",
            "approved_by",
        )
        .prefetch_related(
            "lines",
        )
        .distinct()
    )

    # Dictionnaire :
    # (employee_id, task_id, level) -> dernier TrainingRecord approuvé
    training_dict = {}

    for training_record in all_training_records:
        key = (
            training_record.employee_id,
            training_record.task_id,
            training_record.level,
        )

        existing_record = training_dict.get(key)

        if not existing_record:
            training_dict[key] = (
                training_record
            )
            continue

        existing_date = existing_record.date
        new_date = training_record.date

        if new_date and existing_date:
            if new_date > existing_date:
                training_dict[key] = (
                    training_record
                )

        elif new_date and not existing_date:
            training_dict[key] = (
                training_record
            )

    # ================================================================
    # DEMANDES EN ATTENTE RH
    # ================================================================

    requests_on_current_line = (
        TrainingApprovalRequest.objects.filter(
            employee_id__in=employee_ids,
            task_id__in=task_ids,
            approved__isnull=True,
            line=current_line,
        )
        .select_related(
            "employee",
            "task",
            "line",
            "training_record",
        )
    )

    # Anciennes demandes qui ne possèdent pas de ligne.
    requests_no_line_fallback = (
        TrainingApprovalRequest.objects.filter(
            employee_id__in=employee_ids,
            task_id__in=task_ids,
            approved__isnull=True,
            line__isnull=True,
            employee__line=current_line,
        )
        .select_related(
            "employee",
            "task",
            "training_record",
        )
    )

    # Dictionnaire :
    # (employee_id, task_id, requested_level) -> dernière demande pending
    approval_dict = {}

    pending_requests = list(
        requests_on_current_line
    ) + list(
        requests_no_line_fallback
    )

    for approval_request in pending_requests:
        key = (
            approval_request.employee_id,
            approval_request.task_id,
            approval_request.requested_level,
        )

        existing_request = approval_dict.get(
            key
        )

        if not existing_request:
            approval_dict[key] = (
                approval_request
            )

        elif (
            approval_request.date_requested
            > existing_request.date_requested
        ):
            approval_dict[key] = (
                approval_request
            )

    # ================================================================
    # CONSTRUCTION DE LA MATRICE
    # ================================================================

    matrix = []

    for task in tasks:
        row = {
            "task": task,
            "cells": [],
        }

        for employee in employees_list:
            is_team_leader = bool(
                team_leader_employee
                and employee.id
                == team_leader_employee.id
            )

            team_leader_name = None

            if employee.manager:
                team_leader_name = (
                    (
                        f"{employee.manager.first_name} "
                        f"{employee.manager.last_name}"
                    ).strip()
                    or employee.manager.username
                )

            emp_visible_lines = (
                employee.visible_lines.filter(
                    team_leaders=user
                )
                .exclude(
                    id=current_line.id
                )
                .distinct()
            )

            # --------------------------------------------------------
            # Demandes en attente pour les trois niveaux
            # --------------------------------------------------------

            pending_i = approval_dict.get(
                (
                    employee.id,
                    task.id,
                    TrainingLevel.IN_TRAINING,
                )
            )

            pending_l = approval_dict.get(
                (
                    employee.id,
                    task.id,
                    TrainingLevel.SUPERVISED,
                )
            )

            pending_u = approval_dict.get(
                (
                    employee.id,
                    task.id,
                    TrainingLevel.TRAINER,
                )
            )

            cell = {
                "employee": employee,
                "is_team_leader": (
                    is_team_leader
                ),
                "team_leader": (
                    employee.manager
                ),
                "team_leader_name": (
                    team_leader_name
                ),
                "other_lines": (
                    emp_visible_lines
                ),
                "is_multiline": (
                    emp_visible_lines.exists()
                ),

                # Niveaux approuvés uniquement.
                "I": training_dict.get(
                    (
                        employee.id,
                        task.id,
                        TrainingLevel.IN_TRAINING,
                    )
                ),
                "L": training_dict.get(
                    (
                        employee.id,
                        task.id,
                        TrainingLevel.SUPERVISED,
                    )
                ),
                "U": training_dict.get(
                    (
                        employee.id,
                        task.id,
                        TrainingLevel.TRAINER,
                    )
                ),

                # Demandes qui attendent RH.
                "pending": {
                    "I": pending_i,
                    "L": pending_l,
                    "U": pending_u,
                },
            }

            row["cells"].append(cell)

        matrix.append(row)

    # ================================================================
    # ALERTES DE DÉVELOPPEMENT
    # ================================================================

    today = timezone.now().date()

    development_alerts = []

    threshold_i_to_l = 7
    threshold_l_to_u = 21

    for employee in employees_list:
        is_leader = bool(
            team_leader_employee
            and employee.id
            == team_leader_employee.id
        )

        # Ne pas générer d'alertes pour le chef d'équipe.
        if is_leader:
            continue

        employee_alerts = []

        for task in tasks:
            record_i = training_dict.get(
                (
                    employee.id,
                    task.id,
                    TrainingLevel.IN_TRAINING,
                )
            )

            record_l = training_dict.get(
                (
                    employee.id,
                    task.id,
                    TrainingLevel.SUPERVISED,
                )
            )

            record_u = training_dict.get(
                (
                    employee.id,
                    task.id,
                    TrainingLevel.TRAINER,
                )
            )

            pending_i = approval_dict.get(
                (
                    employee.id,
                    task.id,
                    TrainingLevel.IN_TRAINING,
                )
            )

            pending_l = approval_dict.get(
                (
                    employee.id,
                    task.id,
                    TrainingLevel.SUPERVISED,
                )
            )

            pending_u = approval_dict.get(
                (
                    employee.id,
                    task.id,
                    TrainingLevel.TRAINER,
                )
            )

            # Aucun niveau approuvé ni demande en attente.
            if (
                not record_i
                and not record_l
                and not record_u
                and not pending_i
                and not pending_l
                and not pending_u
            ):
                continue

            # --------------------------------------------------------
            # Niveau I approuvé -> prêt pour L
            # --------------------------------------------------------

            if (
                record_i
                and not record_l
                and not pending_l
            ):
                if record_i.date is None:
                    continue

                days_at_i = (
                    today - record_i.date
                ).days

                if days_at_i >= threshold_i_to_l:
                    if days_at_i > 30:
                        urgency = "critical"
                    elif days_at_i > 14:
                        urgency = "warning"
                    else:
                        urgency = "info"

                    employee_alerts.append(
                        {
                            "task": task,
                            "current_level": "I",
                            "next_level": "L",
                            "days_since": days_at_i,
                            "threshold": (
                                threshold_i_to_l
                            ),
                            "days_overdue": (
                                days_at_i
                                - threshold_i_to_l
                            ),
                            "urgency": urgency,
                            "training_date": (
                                record_i.date
                            ),
                            "message": (
                                f"Niveau I depuis "
                                f"{days_at_i} jours "
                                "→ Prêt pour L"
                            ),
                        }
                    )

            # --------------------------------------------------------
            # Niveau L approuvé -> prêt pour U
            # --------------------------------------------------------

            if (
                record_l
                and not record_u
                and not pending_u
            ):
                if record_l.date is None:
                    continue

                days_at_l = (
                    today - record_l.date
                ).days

                if days_at_l >= threshold_l_to_u:
                    if days_at_l > 60:
                        urgency = "critical"
                    elif days_at_l > 30:
                        urgency = "warning"
                    else:
                        urgency = "info"

                    employee_alerts.append(
                        {
                            "task": task,
                            "current_level": "L",
                            "next_level": "U",
                            "days_since": days_at_l,
                            "threshold": (
                                threshold_l_to_u
                            ),
                            "days_overdue": (
                                days_at_l
                                - threshold_l_to_u
                            ),
                            "urgency": urgency,
                            "training_date": (
                                record_l.date
                            ),
                            "message": (
                                f"Niveau L depuis "
                                f"{days_at_l} jours "
                                "→ Prêt pour U"
                            ),
                        }
                    )

        if employee_alerts:
            urgency_order = {
                "critical": 0,
                "warning": 1,
                "info": 2,
            }

            employee_alerts.sort(
                key=lambda alert: (
                    urgency_order.get(
                        alert["urgency"],
                        3,
                    ),
                    -alert["days_since"],
                )
            )

            development_alerts.append(
                {
                    "employee": employee,
                    "alerts": employee_alerts,
                    "total_alerts": len(
                        employee_alerts
                    ),
                    "critical_count": len(
                        [
                            alert
                            for alert
                            in employee_alerts
                            if alert["urgency"]
                            == "critical"
                        ]
                    ),
                    "warning_count": len(
                        [
                            alert
                            for alert
                            in employee_alerts
                            if alert["urgency"]
                            == "warning"
                        ]
                    ),
                    "ready_count": len(
                        [
                            alert
                            for alert
                            in employee_alerts
                            if alert["urgency"]
                            == "info"
                        ]
                    ),
                    "max_urgency": (
                        employee_alerts[0][
                            "urgency"
                        ]
                    ),
                }
            )

    development_alerts.sort(
        key=lambda item: (
            {
                "critical": 0,
                "warning": 1,
                "info": 2,
            }.get(
                item["max_urgency"],
                3,
            ),
            -item["total_alerts"],
        )
    )

    # ================================================================
    # STATISTIQUES
    # ================================================================

    total_cells = (
        len(employees_list)
        * len(tasks)
        * 3
    )

    filled_cells = sum(
        1
        for task in tasks
        for employee in employees_list
        for level in (
            TrainingLevel.IN_TRAINING,
            TrainingLevel.SUPERVISED,
            TrainingLevel.TRAINER,
        )
        if training_dict.get(
            (
                employee.id,
                task.id,
                level,
            )
        )
    )

    if total_cells > 0:
        completion_rate = round(
            filled_cells
            / total_cells
            * 100,
            1,
        )
    else:
        completion_rate = 0

    total_alerts = sum(
        item["total_alerts"]
        for item in development_alerts
    )

    total_critical = sum(
        item["critical_count"]
        for item in development_alerts
    )

    total_warning = sum(
        item["warning_count"]
        for item in development_alerts
    )

    total_ready = sum(
        item["ready_count"]
        for item in development_alerts
    )

    # ================================================================
    # INFORMATIONS DES LIGNES
    # ================================================================

    lines_with_leaders = [
        {
            "line": line,
            "leaders": line.team_leaders.all(),
        }
        for line in all_user_lines
    ]

    # ================================================================
    # CONTEXTE
    # ================================================================

    context = {
        "lines": all_user_lines,
        "current_line": current_line,
        "employees": employees_list,
        "matrix": matrix,
        "lines_with_leaders": (
            lines_with_leaders
        ),
        "selected_line_id": (
            selected_line_id
        ),
        "has_multiple_lines": (
            has_multiple_lines
        ),
        "development_alerts": (
            development_alerts
        ),
        "total_alerts": total_alerts,
        "total_critical": (
            total_critical
        ),
        "total_warning": total_warning,
        "total_ready": total_ready,
        "completion_rate": (
            completion_rate
        ),
        "total_missing": total_alerts,
        "team_leader_employee": (
            team_leader_employee
        ),
        "show_line_selector": False,
        "notifications": [],
    }

    return render(
        request,
        "matrix/matrix.html",
        context,
    )


from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.http import JsonResponse

@login_required
def change_password_team_leader(request):
    """
    Vue pour permettre aux chefs d'équipe de changer leur mot de passe.
    Supporte les requêtes AJAX et les requêtes normales.
    """
    if request.method == 'POST':
        form = PasswordChangeForm(request.user, request.POST)
        
        if form.is_valid():
            user = form.save()
            # Maintenir la session active après le changement
            update_session_auth_hash(request, user)
            
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': True,
                    'message': 'Votre mot de passe a été modifié avec succès.'
                })
            else:
                messages.success(request, 'Votre mot de passe a été modifié avec succès.')
                return redirect('matrix_view')
        else:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                errors = {}
                for field, error_list in form.errors.items():
                    errors[field] = [str(e) for e in error_list]
                return JsonResponse({
                    'success': False,
                    'errors': errors
                }, status=400)
    else:
        form = PasswordChangeForm(request.user)
    
    # Pour les requêtes non-AJAX, rendre une page complète
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        context = {
            'form': form,
            'title': 'Changer mon mot de passe'
        }
        return render(request, 'matrix/change_password.html', context)
    
    return JsonResponse({'error': 'Méthode non autorisée'}, status=405)

@login_required
def employee_list(request):
    """
    Liste des employés du chef d'équipe.
    Affiche sur quelles lignes chaque opérateur est visible.
    """
    user = request.user
    
    # ✅ Récupérer les employés gérés par ce chef
    employees = Employee.objects.filter(
        manager=user
    ).prefetch_related(
        'visible_lines'   # ✅ Pré-charger les lignes de visibilité
    ).select_related(
        'line'
    ).order_by('last_name', 'first_name')
    
    # Lignes gérées par ce chef (pour le contexte)
    managed_lines = ProductionLine.objects.filter(team_leaders=user)
    
    return render(request, "matrix/employee_list.html", {
        "employees":     employees,
        "managed_lines": managed_lines,
    })


@login_required
def employee_create(request):
    """
    ✅ Chef d'équipe ajoute un opérateur via son matricule.
    Convertit le PendingEmployee en Employee (opérateur actif).
    Ne crée PAS un nouvel employé directement.
    """
    if request.method == "POST":
        form = EmployeeForm(request.POST, user=request.user)

        if form.is_valid():
            # ✅ form.save() gère tout :
            # 1. Trouve le PendingEmployee par matricule
            # 2. Crée l'Employee (opérateur actif)
            # 3. Assigne manager + line + visible_lines
            # 4. Supprime le PendingEmployee
            try:
                employee = form.save(commit=True)

                visible_names = ", ".join(
                    employee.visible_lines.values_list(
                        'name', flat=True
                    )
                )
                nb_lines  = employee.visible_lines.count()
                emp_name  = (
                    f"{employee.first_name} "
                    f"{employee.last_name}".strip()
                )
                line_code = (
                    employee.line.code
                    if employee.line else "N/A"
                )

                messages.success(
                    request,
                    f"✅ Opérateur ajouté : {emp_name} "
                    f"({employee.matricule}) | "
                    f"Ligne principale : {line_code} | "
                    f"Visible dans {nb_lines} matrice(s) : "
                    f"{visible_names}"
                )
                return redirect('matrix_view')

            except Exception as e:
                messages.error(
                    request,
                    f"❌ Erreur lors de l'ajout : {e}"
                )

        else:
            # ── Afficher les erreurs du formulaire ─────────────────
            for field, errors in form.errors.items():
                for error in errors:
                    if field == '__all__':
                        messages.error(request, f"❌ {error}")
                    else:
                        field_label = (
                            form.fields[field].label
                            if field in form.fields
                            else field
                        )
                        messages.error(
                            request,
                            f"❌ {field_label} : {error}"
                        )

    else:
        form = EmployeeForm(user=request.user)

    return render(
        request,
        'matrix/employee_form.html',
        {'form': form}
    )


@login_required
def employee_update(request, pk):
    """
    Modification d'un opérateur — met à jour les lignes de visibilité.
    """
    employee = get_object_or_404(Employee, pk=pk, manager=request.user)
    
    if request.method == "POST":
        form = EmployeeForm(request.POST, instance=employee, user=request.user)
        
        if form.is_valid():
            # ✅ form.save() gère le M2M visible_lines via la méthode
            #    save() surchargée dans EmployeeForm
            form.save()
            
            nb_lines = employee.visible_lines.count()
            visible_lines_names = ", ".join(
                employee.visible_lines.values_list('name', flat=True)
            )
            
            messages.success(
                request,
                f"✅ {employee.first_name} {employee.last_name} mis à jour. "
                f"Visible dans {nb_lines} matrice(s) : {visible_lines_names}."
            )
            return redirect("employee_list")
        
        messages.error(
            request,
            "Modification impossible. Merci de corriger les erreurs."
        )
    else:
        form = EmployeeForm(instance=employee, user=request.user)
    
    return render(request, "matrix/employee_form.html", {
        "form":     form,
        "employee": employee,
    })


@login_required
def employee_delete(request, pk):
    """
    Suppression d'un opérateur.
    Le M2M visible_lines est supprimé automatiquement par Django.
    """
    employee = get_object_or_404(Employee, pk=pk, manager=request.user)
    
    if request.method == "POST":
        emp_name = f"{employee.first_name} {employee.last_name}".strip()
        employee.delete()  # ✅ Django supprime automatiquement les M2M
        
        messages.success(
            request,
            f"✅ {emp_name} a été supprimé(e) avec succès."
        )
        return redirect("employee_list")
    
    return render(request, "matrix/employee_confirm_delete.html", {
        "employee": employee
    })



@login_required
@require_safe
@never_cache
def training_requests_list(request):
    # Toutes les demandes appartenant au chef connecté.
    # Cette base permet de garantir qu'un chef ne peut jamais
    # consulter les demandes d'un autre chef via les filtres.
    base_requests = TrainingApprovalRequest.objects.filter(
        requested_by=request.user,
    )

    requests = base_requests.select_related(
        "employee",
        "task",
        "line",
    ).order_by(
        "-date_requested",
        "-pk",
    )

    # -------------------------
    # Filtre par statut
    # -------------------------
    status = request.GET.get("status", "all")

    filters = {
        "pending": None,
        "approved": True,
        "rejected": False,
    }

    if status in filters:
        requests = requests.filter(
            approved=filters[status]
        )
    else:
        status = "all"

    # -------------------------
    # Filtre par opérateur
    # -------------------------
    selected_operator_id = None
    operator_param = request.GET.get("operator", "").strip()

    if operator_param:
        try:
            selected_operator_id = int(operator_param)
        except (TypeError, ValueError):
            selected_operator_id = None

    if selected_operator_id is not None:
        requests = requests.filter(
            employee_id=selected_operator_id
        )

    # Afficher uniquement les opérateurs pour lesquels
    # ce chef a déjà créé au moins une demande.
    operators = Employee.objects.filter(
        pk__in=base_requests.values("employee_id")
    ).order_by("pk")

    return render(
        request,
        "matrix/training_requests_list.html",
        {
            "page_obj": Paginator(
                requests,
                25,
            ).get_page(request.GET.get("page")),

            "selected_status": status,

            "selected_operator_id": selected_operator_id,

            "operators": operators,

            "status_filters": [
                ("all", "Toutes"),
                ("pending", "En attente"),
                ("approved", "Approuvées"),
                ("rejected", "Refusées"),
            ],
        },
    )


def _delete_replaced_training_plan(storage, name):
    if name:
        try:
            storage.delete(name)
        except OSError:
            logger.exception("Could not remove replaced training plan %s", name)


@login_required
@require_http_methods(["GET", "POST"])
@never_cache
def training_plan_upload(request, request_id):
    approval_request = get_object_or_404(
        TrainingApprovalRequest.objects.select_related("employee", "task", "line"),
        pk=request_id,
        requested_by=request.user,
        requested_level__in=(TrainingLevel.SUPERVISED, TrainingLevel.TRAINER),
    )
    if not approval_request.is_pending:
        return HttpResponseForbidden("Cette demande a été traitée. Le plan est en lecture seule.")

    form = TrainingPlanImageForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
    )
    if request.method == "POST" and form.is_valid():
        field = approval_request._meta.get_field("training_plan_image")
        storage = field.storage
        uploaded = form.cleaned_data["training_plan_image"]
        old_name = approval_request.training_plan_image.name
        new_name = storage.save(field.generate_filename(approval_request, uploaded.name), uploaded)
        try:
            with transaction.atomic():
                # A conditional write also protects SQLite, where row locks are unavailable.
                updated = TrainingApprovalRequest.objects.filter(
                    pk=approval_request.pk,
                    requested_by=request.user,
                    approved__isnull=True,
                    training_plan_image=old_name,
                ).update(training_plan_image=new_name)
                if updated:
                    transaction.on_commit(
                        lambda: _delete_replaced_training_plan(storage, old_name)
                    )
        except Exception:
            _delete_replaced_training_plan(storage, new_name)
            raise
        if not updated:
            _delete_replaced_training_plan(storage, new_name)
            messages.error(request, "La demande a changé entre-temps. Rechargez-la avant de réessayer.")
        else:
            messages.success(request, "Plan de formation enregistré. La demande reste en attente de décision RH.")
        return redirect("training_requests_list")

    return render(request, "matrix/training_plan_form.html", {
        "form": form, "approval_request": approval_request,
    })


from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_safe
from django.views.decorators.cache import never_cache
from django.shortcuts import get_object_or_404, render
from django.http import FileResponse, Http404
from django.urls import reverse


@login_required
@require_safe
@never_cache
def training_plan_view(request, request_id):
    requests = TrainingApprovalRequest.objects.all()

    if not is_rh_user(request.user):
        requests = requests.filter(requested_by=request.user)

    approval_request = get_object_or_404(
        requests,
        pk=request_id
    )

    if not approval_request.training_plan_image:
        raise Http404("Aucun plan de formation disponible.")

    return render(
        request,
        "matrix/training_plan_view.html",
        {
            "approval_request": approval_request,
        }
    )


@login_required
@require_safe
@never_cache
def training_plan_image(request, request_id):
    requests = TrainingApprovalRequest.objects.all()

    if not is_rh_user(request.user):
        requests = requests.filter(requested_by=request.user)

    approval_request = get_object_or_404(
        requests,
        pk=request_id
    )

    plan = approval_request.training_plan_image

    if not plan:
        raise Http404("Aucun plan de formation disponible.")

    try:
        stream = plan.open("rb")
    except OSError:
        raise Http404(
            "Le fichier du plan de formation est indisponible."
        )

    suffix = plan.name.rsplit(".", 1)[-1].lower()

    content_types = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }

    response = FileResponse(
        stream,
        content_type=content_types.get(
            suffix,
            "application/octet-stream"
        ),
    )

    response["X-Content-Type-Options"] = "nosniff"

    return response


def _get_requalification_plan(request, request_id):
    requests = RequalificationApprovalRequest.objects.all()
    if not is_rh_user(request.user):
        requests = requests.filter(requested_by=request.user)
    return get_object_or_404(requests, pk=request_id)


@login_required
@require_safe
@never_cache
def requalification_plan_view(request, request_id):
    req = _get_requalification_plan(request, request_id)
    if not req.has_training_plan:
        raise Http404("Le plan de formation est indisponible.")
    return render(request, "matrix/training_plan_view.html", {
        "approval_request": req,
        "plan_image_url": reverse("requalification_plan_image", args=[req.pk]),
    })


@login_required
@require_safe
@never_cache
def requalification_plan_image(request, request_id):
    req = _get_requalification_plan(request, request_id)
    plan = req.training_plan_image
    if not plan:
        raise Http404("Le plan de formation est indisponible.")
    try:
        stream = plan.storage.open(plan.name, "rb")
    except (OSError, ValueError):
        raise Http404("Le plan de formation est indisponible.")
    suffix = plan.name.rsplit(".", 1)[-1].lower()
    response = FileResponse(stream, content_type={
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
    }.get(suffix, "application/octet-stream"))
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def training_record_list(request):
    records = TrainingRecord.objects.filter(employee__manager=request.user)
    return render(request, "matrix/trainingrecord_list.html", {"records": records})


from django.db import transaction
from django.utils import timezone

from .models import (
    TrainingQuestion,
    TrainingQuestionAnswer,
    TrainingQuestionnaire,
)


def save_level_i_questionnaire(
    *,
    training_record,
    form,
    answered_by,
    line,
):
    """
    Enregistre le questionnaire correspondant au niveau I.

    En cas de modification d'un niveau I existant :
    - le questionnaire est mis à jour ;
    - les anciennes réponses sont remplacées.
    """

    general_comment = (
        form.cleaned_data.get(
            "questionnaire_general_comment",
            "",
        )
        or ""
    ).strip()

    questionnaire, created = (
        TrainingQuestionnaire.objects.update_or_create(
            training_record=training_record,
            defaults={
                "line": line,
                "answered_by": answered_by,
                "general_comment": general_comment,
                "completed_at": timezone.now(),
            },
        )
    )

    # Remplacer les réponses précédentes.
    questionnaire.answers.all().delete()

    answers_to_create = []
    questionnaire_is_compliant = True

    for item in form.training_questions:
        question = item["question"]
        answer_field_name = item["answer_field"]
        comment_field_name = item["comment_field"]

        answer_value = form.cleaned_data.get(
            answer_field_name
        )

        comment_value = (
            form.cleaned_data.get(
                comment_field_name,
                "",
            )
            or ""
        ).strip()

        boolean_answer = None
        text_answer = ""
        answer_is_compliant = True

        if (
            question.answer_type
            == TrainingQuestion.AnswerType.YES_NO
        ):
            boolean_answer = answer_value

            if (
                answer_value is not None
                and question.expected_boolean
                is not None
            ):
                answer_is_compliant = (
                    answer_value
                    == question.expected_boolean
                )

        elif (
            question.answer_type
            == TrainingQuestion.AnswerType.TEXT
        ):
            text_answer = str(
                answer_value or ""
            ).strip()

        if (
            question.is_blocking
            and not answer_is_compliant
        ):
            questionnaire_is_compliant = False

        answers_to_create.append(
            TrainingQuestionAnswer(
                questionnaire=questionnaire,
                question=question,
                question_text_snapshot=question.text,
                answer_type_snapshot=question.answer_type,
                boolean_answer=boolean_answer,
                text_answer=text_answer,
                comment=comment_value,
                is_compliant=answer_is_compliant,
            )
        )

    if answers_to_create:
        TrainingQuestionAnswer.objects.bulk_create(
            answers_to_create
        )

    questionnaire.is_compliant = (
        questionnaire_is_compliant
    )
    questionnaire.save(
        update_fields=[
            "is_compliant",
            "line",
            "answered_by",
            "general_comment",
            "completed_at",
        ]
    )

    return questionnaire

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse

from .forms import TrainingRecordForm
from .models import (
    TrainingApprovalRequest,
    TrainingLevel,
    TrainingRecord,
)


@login_required
def training_record_create(request):
    """
    Niveau I :
    - création du TrainingRecord en attente ;
    - enregistrement du questionnaire ;
    - création d'une demande RH ;
    - aucune modification de la matrice avant validation RH.

    Niveaux L et U :
    - fonctionnement actuel avec demande RH.
    """

    employee_id = request.GET.get("employee")
    task_id = request.GET.get("task")
    line_id = request.GET.get("line_id")
    level_id = request.GET.get("level")

    if request.method == "POST":
        post_line_id = request.POST.get("line")

        effective_line_id = (
            post_line_id or line_id
        )

        form = TrainingRecordForm(
            request.POST,
            user=request.user,
            selected_line_id=effective_line_id,
        )

        is_valid = form.is_valid()

        # ========================================================
        # NIVEAUX L ET U : DEMANDE RH CRÉÉE PAR LE FORMULAIRE
        # ========================================================
        if (
            not is_valid
            and form.rh_request_created
        ):
            info = form.rh_request_info

            if info:
                line_name = (
                    info["line"].name
                    if info.get("line")
                    else "N/A"
                )

                if info.get("created"):
                    messages.info(
                        request,
                        (
                            "📋 Demande RH créée : "
                            f"{info['employee']} — "
                            f"{info['task']} — "
                            f"niveau {info['level']} — "
                            f"ligne « {line_name} »."
                        ),
                    )
                else:
                    messages.warning(
                        request,
                        (
                            "⚠️ Une demande RH est déjà "
                            "en attente pour cette formation."
                        ),
                    )

            if effective_line_id:
                return redirect(
                    f"{reverse('matrix_view')}"
                    f"?line_id={effective_line_id}"
                )

            return redirect("matrix_view")

        # ========================================================
        # NIVEAU I VALIDE
        # ========================================================
        if is_valid:
            employee = form.cleaned_data["employee"]
            task = form.cleaned_data["task"]
            level = int(form.cleaned_data["level"])
            line = form.cleaned_data["line"]
            training_date = form.cleaned_data["date"]
            trainer = form.cleaned_data.get("trainer")

            if level != TrainingLevel.IN_TRAINING:
                messages.error(
                    request,
                    (
                        "❌ Les niveaux L et U doivent être "
                        "envoyés via une demande RH."
                    ),
                )

                return render(
                    request,
                    "matrix/trainingrecord_form.html",
                    {
                        "form": form,
                        "selected_line_id": (
                            effective_line_id
                        ),
                    },
                )

            try:
                with transaction.atomic():
                    # --------------------------------------------
                    # Créer le niveau I en attente
                    # --------------------------------------------
                    record = TrainingRecord.objects.create(
                        employee=employee,
                        task=task,
                        level=TrainingLevel.IN_TRAINING,
                        date=training_date,
                        trainer=trainer,
                        approval_status=(
                            TrainingRecord
                            .ApprovalStatus
                            .PENDING
                        ),
                    )

                    record.lines.set([line])

                    # --------------------------------------------
                    # Enregistrer le questionnaire
                    # --------------------------------------------
                    save_level_i_questionnaire(
                        training_record=record,
                        form=form,
                        answered_by=request.user,
                        line=line,
                    )

                    # --------------------------------------------
                    # Créer la demande RH
                    # --------------------------------------------
                    approval_request = (
                        TrainingApprovalRequest.objects.create(
                            employee=employee,
                            task=task,
                            requested_level=(
                                TrainingLevel.IN_TRAINING
                            ),
                            requested_by=request.user,
                            training_date=training_date,
                            line=line,
                            training_record=record,
                            approved=None,
                        )
                    )

                    # --------------------------------------------
                    # Envoyer l'email RH
                    # --------------------------------------------
                    form._send_rh_email(
                        employee=employee,
                        task=task,
                        new_level=(
                            TrainingLevel.IN_TRAINING
                        ),
                        days_since_last=0,
                        training_date=training_date,
                        request_object=approval_request,
                        line=line,
                    )

                    # IMPORTANT :
                    # ne pas créer ou modifier MatrixEntry ici.

            except Exception as error:
                messages.error(
                    request,
                    (
                        "❌ Une erreur est survenue pendant "
                        "la création de la demande RH : "
                        f"{error}"
                    ),
                )

                return render(
                    request,
                    "matrix/trainingrecord_form.html",
                    {
                        "form": form,
                        "selected_line_id": (
                            effective_line_id
                        ),
                    },
                )

            messages.info(
                request,
                (
                    "⏳ Le niveau I et son questionnaire ont "
                    "été enregistrés. La formation est maintenant "
                    "en attente de validation RH. Elle apparaîtra "
                    "dans la matrice après approbation."
                ),
            )

            if effective_line_id:
                return redirect(
                    f"{reverse('matrix_view')}"
                    f"?line_id={effective_line_id}"
                )

            return redirect("matrix_view")

        # ========================================================
        # ERREURS DU FORMULAIRE
        # ========================================================
        has_real_errors = any(
            error.code != "rh_request_pending"
            for field_errors
            in form.errors.as_data().values()
            for error in field_errors
        )

        if has_real_errors:
            messages.error(
                request,
                (
                    "❌ Enregistrement impossible. "
                    "Merci de corriger les erreurs "
                    "du formulaire."
                ),
            )

        return render(
            request,
            "matrix/trainingrecord_form.html",
            {
                "form": form,
                "selected_line_id": effective_line_id,
            },
        )

    # ============================================================
    # GET
    # ============================================================
    initial = {}

    if employee_id:
        initial["employee"] = employee_id

    if task_id:
        initial["task"] = task_id

    # Attention : le niveau I vaut 0.
    if level_id is not None and level_id != "":
        initial["level"] = level_id

    form = TrainingRecordForm(
        user=request.user,
        initial=initial,
        selected_line_id=line_id,
    )

    return render(
        request,
        "matrix/trainingrecord_form.html",
        {
            "form": form,
            "selected_line_id": line_id,
        },
    )

from django.contrib import messages
from django.contrib.auth.decorators import (
    login_required,
    user_passes_test,
)
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import (
    MatrixEntry,
    TrainingApprovalRequest,
    TrainingRecord,
)


def is_rh_user(user):
    return (
        user.is_authenticated
        and (
            user.is_superuser
            or user.groups.filter(
                name__iexact="RH"
            ).exists()
        )
    )


from django.contrib import messages
from django.contrib.auth.decorators import (
    login_required,
    user_passes_test,
)
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import (
    MatrixEntry,
    TrainingApprovalRequest,
    TrainingLevel,
    TrainingRecord,
)





@login_required
@user_passes_test(is_rh_user)
@require_POST
@transaction.atomic
def review_training_request(request, request_id):
    """
    Approuve ou refuse une demande de formation.

    Synchronise :
    - TrainingApprovalRequest.approved ;
    - TrainingRecord.approval_status ;
    - la ligne du TrainingRecord ;
    - MatrixEntry.

    Cette vue peut également réparer une demande déjà approuvée
    dont le TrainingRecord est encore en statut pending.
    """

    approval_request = get_object_or_404(
        TrainingApprovalRequest.objects
        .select_for_update()
        .select_related(
            "employee",
            "employee__line",
            "task",
            "line",
            "training_record",
            "reviewed_by",
        ),
        pk=request_id,
    )

    decision = request.POST.get("decision")

    rejection_reason = (
        request.POST.get(
            "rejection_reason",
            "",
        )
        or ""
    ).strip()

    if decision not in {"approve", "reject"}:
        messages.error(
            request,
            "❌ Décision invalide.",
        )
        return redirect("rh_training_approvals")

    # ============================================================
    # DÉTERMINER LA LIGNE
    # ============================================================

    training_line = (
        approval_request.line
        or approval_request.employee.line
    )

    if not training_line:
        messages.error(
            request,
            (
                "❌ Aucune ligne de production n'est associée "
                "à cette demande."
            ),
        )
        return redirect("rh_training_approvals")

    # ============================================================
    # RECHERCHER LE TRAINING RECORD
    # ============================================================

    training_record = approval_request.training_record

    if not training_record:
        records_queryset = (
            TrainingRecord.objects
            .select_for_update()
            .filter(
                employee=approval_request.employee,
                task=approval_request.task,
                level=approval_request.requested_level,
            )
        )

        # 1. Chercher un TrainingRecord pending sur la bonne ligne.
        training_record = (
            records_queryset.filter(
                approval_status=(
                    TrainingRecord
                    .ApprovalStatus
                    .PENDING
                ),
                lines=training_line,
            )
            .distinct()
            .order_by("-created_at", "-id")
            .first()
        )

        # 2. Chercher un pending sans filtrer la ligne.
        if not training_record:
            training_record = (
                records_queryset.filter(
                    approval_status=(
                        TrainingRecord
                        .ApprovalStatus
                        .PENDING
                    ),
                )
                .distinct()
                .order_by("-created_at", "-id")
                .first()
            )

        # 3. Chercher une formation déjà approuvée sur la ligne.
        if not training_record:
            training_record = (
                records_queryset.filter(
                    approval_status=(
                        TrainingRecord
                        .ApprovalStatus
                        .APPROVED
                    ),
                    lines=training_line,
                )
                .distinct()
                .order_by("-created_at", "-id")
                .first()
            )

        if training_record:
            approval_request.training_record = (
                training_record
            )

    logger.info(
        "RH training review: request_id=%s training_record_id=%s "
        "decision=%s status_before=%s",
        approval_request.pk,
        training_record.pk if training_record else None,
        decision,
        (
            training_record.approval_status
            if training_record
            else None
        ),
    )

    # ============================================================
    # APPROBATION
    # ============================================================

    if decision == "approve":
        decision_date = timezone.now()

        # Une demande déjà refusée ne doit pas être réapprouvée.
        if approval_request.approved is False:
            messages.error(
                request,
                (
                    "❌ Cette demande a déjà été refusée. "
                    "Créez une nouvelle demande."
                ),
            )
            return redirect("rh_training_approvals")

        # Si la demande et la formation sont déjà approuvées,
        # aucune autre modification n'est nécessaire.
        if (
            approval_request.approved is True
            and training_record
            and training_record.approval_status
            == TrainingRecord.ApprovalStatus.APPROVED
            and training_record.lines.filter(
                pk=training_line.pk
            ).exists()
        ):
            messages.info(
                request,
                (
                    "ℹ️ Cette demande et sa formation "
                    "sont déjà approuvées."
                ),
            )
            return redirect("rh_training_approvals")

        # --------------------------------------------------------
        # NIVEAU I
        # --------------------------------------------------------
        # Le niveau I doit déjà avoir un TrainingRecord,
        # créé avec le questionnaire.
        try:
            approval_request.validate_training_plan_for_approval()
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
            return redirect("rh_training_approvals")

        if (
            approval_request.requested_level
            == TrainingLevel.IN_TRAINING
            and not training_record
        ):
            messages.error(
                request,
                (
                    "❌ Aucun TrainingRecord de niveau I "
                    "n'est associé à cette demande."
                ),
            )
            return redirect("rh_training_approvals")

        # --------------------------------------------------------
        # NIVEAUX L ET U
        # --------------------------------------------------------
        # Pour L ou U, créer le TrainingRecord s'il n'existe pas.
        if not training_record:
            training_record = (
                TrainingRecord.objects.create(
                    employee=approval_request.employee,
                    task=approval_request.task,
                    level=(
                        approval_request.requested_level
                    ),
                    date=(
                        approval_request.training_date
                        or timezone.localdate()
                    ),
                    trainer=None,
                    approval_status=(
                        TrainingRecord
                        .ApprovalStatus
                        .APPROVED
                    ),
                    approved_by=request.user,
                    approved_at=decision_date,
                    rejection_reason="",
                )
            )

        # ========================================================
        # MISE À JOUR FORCÉE DU TRAINING RECORD
        # ========================================================
        # QuerySet.update() évite qu'un éventuel save() personnalisé
        # remette approval_status à pending.

        training_record_updates = {
            "approval_status": (
                TrainingRecord.ApprovalStatus.APPROVED
            ),
            "approved_by_id": request.user.id,
            "approved_at": decision_date,
            "rejection_reason": "",
            "updated_at": decision_date,
        }

        if (
            not training_record.date
            and approval_request.training_date
        ):
            training_record_updates["date"] = (
                approval_request.training_date
            )

        updated_record_count = (
            TrainingRecord.objects.filter(
                pk=training_record.pk
            ).update(
                **training_record_updates
            )
        )

        if updated_record_count != 1:
            raise RuntimeError(
                "Le TrainingRecord n'a pas pu être mis à jour."
            )

        logger.info(
            "RH training review: request_id=%s training_record_id=%s "
            "status_after_save=%s",
            approval_request.pk,
            training_record.pk,
            TrainingRecord.objects.values_list(
                "approval_status",
                flat=True,
            ).get(pk=training_record.pk),
        )

        training_record.refresh_from_db()

        logger.info(
            "RH training review: request_id=%s training_record_id=%s "
            "status_after_refresh=%s",
            approval_request.pk,
            training_record.pk,
            training_record.approval_status,
        )

        # Associer obligatoirement la ligne au TrainingRecord.
        training_record.lines.add(training_line)

        logger.info(
            "RH training review: request_id=%s training_record_id=%s "
            "line_id=%s line_associated=%s",
            approval_request.pk,
            training_record.pk,
            training_line.pk,
            training_record.lines.filter(
                pk=training_line.pk,
            ).exists(),
        )

        # ========================================================
        # MISE À JOUR DE LA DEMANDE RH
        # ========================================================

        decision_queryset = TrainingApprovalRequest.objects.filter(pk=approval_request.pk)
        if approval_request.requires_training_plan:
            # Fail the whole transaction if the plan or decision changed since review.
            decision_queryset = decision_queryset.filter(
                approved=approval_request.approved,
                training_plan_image=approval_request.training_plan_image.name,
            )
        updated_request_count = (
            decision_queryset.update(
                approved=True,
                reviewed_by_id=request.user.id,
                reviewed_at=decision_date,
                rejection_reason="",
                notification_seen=False,
                training_record_id=training_record.id,
                line_id=training_line.id,
            )
        )

        if updated_request_count != 1:
            raise RuntimeError(
                "La demande RH n'a pas pu être mise à jour."
            )

        approval_request.refresh_from_db()
        training_record.refresh_from_db()

        # ========================================================
        # VÉRIFIER LA SYNCHRONISATION
        # ========================================================

        line_is_associated = (
            training_record.lines.filter(
                pk=training_line.pk
            ).exists()
        )

        visible_in_matrix = (
            TrainingRecord.objects.filter(
                pk=training_record.pk,
                employee_id=approval_request.employee_id,
                task_id=approval_request.task_id,
                level=approval_request.requested_level,
                lines=training_line,
                approval_status=(
                    TrainingRecord
                    .ApprovalStatus
                    .APPROVED
                ),
            )
            .distinct()
            .exists()
        )

        if (
            approval_request.approved is not True
            or training_record.approval_status
            != TrainingRecord.ApprovalStatus.APPROVED
            or not line_is_associated
            or not visible_in_matrix
        ):
            raise RuntimeError(
                (
                    "Échec de synchronisation : "
                    f"demande={approval_request.approved}, "
                    f"formation="
                    f"{training_record.approval_status}, "
                    f"ligne={line_is_associated}, "
                    f"visible={visible_in_matrix}."
                )
            )

        # ========================================================
        # METTRE À JOUR MATRIX ENTRY
        # ========================================================

        matrix_entry, matrix_entry_created = (
            MatrixEntry.objects.get_or_create(
                employee=training_record.employee,
                task=training_record.task,
                defaults={
                    "level": training_record.level,
                    "last_trained": training_record.date,
                },
            )
        )

        if not matrix_entry_created:
            matrix_entry_updates = {}

            current_matrix_level = matrix_entry.level

            # Ne jamais diminuer le niveau dans MatrixEntry.
            if (
                training_record.level
                > current_matrix_level
            ):
                matrix_entry_updates["level"] = (
                    training_record.level
                )

            # Mettre à jour la date si le niveau approuvé
            # est égal ou supérieur au niveau actuel.
            if (
                training_record.level
                >= current_matrix_level
            ):
                matrix_entry_updates["last_trained"] = (
                    training_record.date
                )

            if matrix_entry_updates:
                MatrixEntry.objects.filter(
                    pk=matrix_entry.pk
                ).update(
                    **matrix_entry_updates
                )

        messages.success(
            request,
            (
                "✅ La formation a été approuvée et "
                "synchronisée avec la matrice. "
                f"Niveau : "
                f"{training_record.get_level_display()}. "
                f"Ligne : {training_line.name}."
            ),
        )

        return redirect("rh_training_approvals")

    # ============================================================
    # REFUS
    # ============================================================

    # Une demande approuvée ne doit pas être refusée.
    if approval_request.approved is True:
        messages.error(
            request,
            (
                "❌ Cette demande est déjà approuvée "
                "et ne peut plus être refusée."
            ),
        )
        return redirect("rh_training_approvals")

    # Une demande déjà refusée ne doit pas être retraitée.
    if approval_request.approved is False:
        messages.info(
            request,
            "ℹ️ Cette demande est déjà refusée.",
        )
        return redirect("rh_training_approvals")

    if not rejection_reason:
        messages.error(
            request,
            "❌ Le motif du refus est obligatoire.",
        )
        return redirect("rh_training_approvals")

    decision_date = timezone.now()

    # ============================================================
    # REFUSER LA DEMANDE RH
    # ============================================================

    request_updates = {
        "approved": False,
        "reviewed_by_id": request.user.id,
        "reviewed_at": decision_date,
        "rejection_reason": rejection_reason,
        "notification_seen": False,
        "line_id": training_line.id,
    }

    if training_record:
        request_updates["training_record_id"] = (
            training_record.id
        )

    updated_request_count = (
        TrainingApprovalRequest.objects.filter(
            pk=approval_request.pk
        ).update(
            **request_updates
        )
    )

    if updated_request_count != 1:
        raise RuntimeError(
            "La demande RH n'a pas pu être refusée."
        )

    # ============================================================
    # REFUSER LE TRAINING RECORD
    # ============================================================

    if training_record:
        updated_record_count = (
            TrainingRecord.objects.filter(
                pk=training_record.pk
            ).update(
                approval_status=(
                    TrainingRecord
                    .ApprovalStatus
                    .REJECTED
                ),
                approved_by_id=request.user.id,
                approved_at=decision_date,
                rejection_reason=rejection_reason,
                updated_at=decision_date,
            )
        )

        if updated_record_count != 1:
            raise RuntimeError(
                (
                    "La formation associée n'a pas pu "
                    "être refusée."
                )
            )

    messages.warning(
        request,
        (
            "❌ La demande a été refusée. "
            "Le niveau ne sera pas affiché dans la matrice."
        ),
    )

    return redirect("rh_training_approvals")


@login_required
def training_record_update(request, pk):
    """
    ✅ Modification d'un enregistrement.
    Vérifie que l'employé est toujours visible sur la ligne.
    """
    record = get_object_or_404(
        TrainingRecord, pk=pk, employee__manager=request.user
    )

    # ✅ Récupérer la ligne de cet enregistrement
    record_line = record.lines.first()

    if request.method == "POST":
        form = TrainingRecordForm(
            request.POST,
            instance=record,
            user=request.user,
            selected_line_id=record_line.id if record_line else None
        )

        if form.is_valid():
            record = form.save()

            matrix_entry, created = MatrixEntry.objects.get_or_create(
                employee=record.employee,
                task=record.task,
                defaults={'level': record.level}
            )
            if not created:
                matrix_entry.level = record.level
                matrix_entry.save()

            messages.success(
                request,
                f"✅ Formation mise à jour : {record.employee} — "
                f"{record.task} (niveau {record.get_level_display()})."
            )
            return redirect("matrix_view")

        else:
            if form.rh_request_created and form.rh_request_info:
                info = form.rh_request_info
                messages.info(
                    request,
                    f"📋 Demande de validation RH créée !\n"
                    f"Niveau {info['level']} pour "
                    f"{info['employee']} - {info['task']}\n"
                    f"En attente de validation par le service RH."
                )
                return redirect("matrix_view")

    else:
        form = TrainingRecordForm(
            instance=record,
            user=request.user,
            selected_line_id=record_line.id if record_line else None
        )

    return render(
        request,
        "matrix/trainingrecord_form.html",
        {
            "form":             form,
            "selected_line_id": record_line.id if record_line else None,
        }
    )


@login_required
def training_record_delete(request, pk):
    """Suppression d'un enregistrement de formation."""
    record = get_object_or_404(
        TrainingRecord, pk=pk, employee__manager=request.user
    )

    if request.method == "POST":
        employee = record.employee
        task     = record.task
        line     = record.lines.first()

        # Supprimer le MatrixEntry associé
        MatrixEntry.objects.filter(
            employee=employee,
            task=task
        ).delete()

        record.delete()

        messages.success(
            request,
            f"✅ Formation supprimée : {employee} — {task}"
            + (f" (ligne {line.code})" if line else "") + "."
        )
        return redirect("matrix_view")

    return render(
        request,
        "matrix/trainingrecord_confirm_delete.html",
        {"record": record}
    )

@superuser_required
@login_required
def employee_list_rh(request):
    query = request.GET.get('q', '').strip()

    employees = Employee.objects.all().select_related('line')

    if query:
        employees = employees.filter(
            Q(first_name__icontains=query) |
            Q(last_name__icontains=query) |
            Q(matricule__icontains=query)
        )

    employees = employees.order_by('last_name', 'first_name')

    employees_with_stats = []
    
    for employee in employees:
        if employee.line:
            total_tasks_on_line = Task.objects.filter(lines=employee.line).count()
            
            records = TrainingRecord.objects.filter(
                employee=employee,
                lines=employee.line
            ).order_by('task', '-date')
            
            task_levels = {}
            for record in records:
                if record.task_id not in task_levels:
                    task_levels[record.task_id] = record.level
            
            level_counts = {0: 0, 1: 0, 3: 0}
            for level in task_levels.values():
                if level in level_counts:
                    level_counts[level] += 1
            
            total_trained = len(task_levels)
            not_trained = max(0, total_tasks_on_line - total_trained)
            
            if total_tasks_on_line > 0:
                level_percentages = {
                    0: round((level_counts[0] / total_tasks_on_line) * 100),
                    1: round((level_counts[1] / total_tasks_on_line) * 100),
                    3: round((level_counts[3] / total_tasks_on_line) * 100),
                    'not_trained': round((not_trained / total_tasks_on_line) * 100),
                }
                
                if total_trained > 0:
                    score_global = round(
                        (level_counts[0] * 33 + 
                         level_counts[1] * 66 + 
                         level_counts[3] * 100) / total_trained
                    )
                else:
                    score_global = 0
                    
                coverage = round((total_trained / total_tasks_on_line) * 100)
            else:
                level_percentages = {0: 0, 1: 0, 3: 0, 'not_trained': 100}
                score_global = 0
                coverage = 0
            
            employee.level_stats = level_percentages
            employee.level_counts = level_counts
            employee.total_trained = total_trained
            employee.total_tasks = total_tasks_on_line
            employee.not_trained = not_trained
            employee.score_global = score_global
            employee.coverage = coverage
        else:
            employee.level_stats = {0: 0, 1: 0, 3: 0, 'not_trained': 100}
            employee.level_counts = {0: 0, 1: 0, 3: 0}
            employee.total_trained = 0
            employee.total_tasks = 0
            employee.not_trained = 0
            employee.score_global = 0
            employee.coverage = 0
        
        employees_with_stats.append(employee)

    return render(request, 'matrix/employee_list_rh.html', {
        'employees': employees_with_stats,
        'query': query,
    })


from django.db.models import Prefetch


@login_required
def employee_history(request, employee_id):
    employee = get_object_or_404(Employee, id=employee_id)

    selected_line  = request.GET.get("line")
    selected_level = request.GET.get("level")

    records = TrainingRecord.objects.filter(employee=employee)

    if selected_line:
        records = records.filter(lines__id=selected_line).distinct()

    if selected_level:
        records = records.filter(level=selected_level)

    records = (
        records
        .select_related('task', 'trainer', 'trainer__manager', 'rh_approval_request', 'requalification_record__approval_request')
        .prefetch_related(
            Prefetch(
                'lines',
                queryset=ProductionLine.objects.prefetch_related(
                    Prefetch('team_leaders', queryset=User.objects.order_by('pk'))
                ).order_by('name')
            ),
            Prefetch(
                'task__lines',
                queryset=ProductionLine.objects.prefetch_related(
                    Prefetch('team_leaders', queryset=User.objects.order_by('pk'))
                ),
            ),
        )
        .order_by('-date')
    )

    # ════════════════════════════════════════════════════════════════
    # Transferts
    # ════════════════════════════════════════════════════════════════
    all_transfers = (
        LineTransfer.objects
        .filter(employee=employee)
        .select_related('from_line', 'to_line', 'transferred_by')
        .order_by('-transfer_date', '-id')
    )

    all_transfers_list = list(all_transfers)

    transfers = []
    for i, transfer in enumerate(all_transfers_list):
        exit_date = None if i == 0 else all_transfers_list[i - 1].transfer_date

        transfers.append({
            "type":           "transfert",
            "date":           transfer.transfer_date,
            "entry_date":     transfer.transfer_date,
            "exit_date":      exit_date,
            "from_line":      transfer.from_line.name if transfer.from_line else "Nouvelle embauche",
            "to_line":        transfer.to_line.name if transfer.to_line else "Non assignée",
            "reason":         transfer.reason,
            "transferred_by": transfer.transferred_by,
        })

    if transfers and employee.line:
        first_transfer = transfers[0]
        if first_transfer['to_line'] != employee.line.name:
            for idx, t in enumerate(transfers):
                if t['to_line'] == employee.line.name:
                    transfers[0], transfers[idx] = transfers[idx], transfers[0]
                    transfers[0]['exit_date'] = None
                    if len(transfers) > 1:
                        transfers[1]['exit_date'] = transfers[0]['entry_date']
                    break

    # ════════════════════════════════════════════════════════════════
    # ✅ Formations avec chef d'équipe
    # ════════════════════════════════════════════════════════════════
    can_review_plans = is_rh_user(request.user)
    formations = []
    for r in records:
        lines = list(r.lines.all())

        if not lines and r.task:
            lines = list(r.task.lines.all())

        line_names = [l.name for l in lines]

        # ── Trouver le chef d'équipe qui a supervisé la formation ──
        # Méthode 1 : via le manager de l'employé à la date de formation
        # Méthode 2 : via les team_leaders des lignes de la formation
        chef_equipe = None
        chef_equipe_name = None

        # Priorité 1 : manager actuel de l'employé (chef d'équipe direct)
        if employee.manager:
            chef_equipe = employee.manager
            chef_equipe_name = (
                employee.manager.get_full_name() or
                employee.manager.username
            )

        # Priorité 2 : team_leader de la ligne de formation
        # (plus précis si l'employé a changé de chef depuis)
        if lines:
            for line in lines:
                team_leaders = line.team_leaders.all()
                if team_leaders.exists():
                    tl = team_leaders.first()
                    chef_equipe = tl
                    chef_equipe_name = tl.get_full_name() or tl.username
                    break

        # The document stays on the original request; no archive or file copy.
        training_plan_url = None
        training_plan_label = "—"
        if r.is_approved and r.level in (TrainingLevel.SUPERVISED, TrainingLevel.TRAINER):
            approval_request = getattr(r, "rh_approval_request", None)
            training_plan_label = "Plan indisponible"
            if approval_request and approval_request.is_approved and approval_request.has_training_plan:
                if can_review_plans or approval_request.requested_by_id == request.user.pk:
                    training_plan_url = reverse("training_plan_view", args=[approval_request.pk])
                else:
                    training_plan_label = "Accès réservé"

        requalification = getattr(r, "requalification_record", None)
        if requalification:
            document = plan_display(requalification.approval_request, request.user, is_rh=can_review_plans)
            training_plan_url = document["training_plan_url"]
            training_plan_label = document["training_plan_label"]
        formations.append({
            "requalification": requalification,
            "type":              "formation",
            "date":              r.date,
            "task":              r.task.title if r.task else "Tâche supprimée",
            "lines":             line_names,
            "level":             r.get_level_display(),
            "trainer":           r.trainer,          # ← formateur (Employee)
            "chef_equipe":       chef_equipe,         # ← chef d'équipe (User)
            "chef_equipe_name":  chef_equipe_name,    # ← nom du chef d'équipe
            "training_plan_url": training_plan_url,
            "training_plan_label": training_plan_label,
        })

    formations.sort(key=lambda x: x["date"], reverse=True)

    # Keep formation counts unchanged. Only standalone requalifications are
    # additional events; a linked TrainingRecord is labelled in place above.
    requalifications = RequalificationRecord.objects.filter(
        employee=employee, training_record__isnull=True,
    ).filter(Q(approval_request__isnull=True) | Q(approval_request__approved=True)).select_related(
        'approval_request', 'line', 'task', 'evaluated_by',
    )
    if selected_line:
        requalifications = requalifications.filter(line_id=selected_line)
    if selected_level:
        requalifications = requalifications.filter(new_level=selected_level)
    standalone = []
    for rec in requalifications:
        standalone.append({
            "type": "requalification", "date": rec.requalification_date,
            "task": rec.task.title if rec.task else "Requalification globale",
            "lines": [rec.line.name] if rec.line else [],
            "level": rec.get_new_level_display(), "requalification": rec,
            "chef_equipe_name": rec.get_requalifier_display(),
            **plan_display(rec.approval_request, request.user, is_rh=can_review_plans),
        })
    formation_events = sorted(formations + standalone, key=lambda item: item["date"] or timezone.now().date(), reverse=True)
    history = formation_events + transfers
    history.sort(key=lambda x: x["date"], reverse=True)

    all_lines  = ProductionLine.objects.all().order_by("name")
    all_levels = TrainingLevel.choices

    stats = {
        'total_formations': len(formations),
        'total_transferts': len(transfers),
        'lignes_formees': ProductionLine.objects.filter(
            training_records__employee=employee
        ).distinct().count(),
    }

    return render(request, "matrix/employee_history.html", {
        "employee":      employee,
        "history":       history,
        "formations":    formations,
        "formation_events": formation_events,
        "transfers":     transfers,
        "all_lines":     all_lines,
        "all_levels":    all_levels,
        "selected_line": selected_line,
        "selected_level": selected_level,
        "stats":         stats,
    })


@login_required
def rh_manage_tasks_lines(request):
    if request.method == 'POST':
        if 'task_submit' in request.POST:
            task_form = TaskForm(request.POST)
            if task_form.is_valid():
                task = task_form.save()
                messages.success(request, f'✅ Tâche "{task.title}" ajoutée avec succès!')
                return redirect('rh_manage_tasks_lines')
            else:
                messages.error(request, '❌ Erreur lors de l\'ajout de la tâche. Vérifiez les champs.')
        else:
            task_form = TaskForm()

        if 'line_submit' in request.POST:
            line_form = ProductionLineForm(request.POST)
            if line_form.is_valid():
                line = line_form.save()
                messages.success(request, f'✅ Ligne "{line.name}" ajoutée avec succès!')
                return redirect('rh_manage_tasks_lines')
            else:
                messages.error(request, '❌ Erreur lors de l\'ajout de la ligne. Vérifiez les champs.')
        else:
            line_form = ProductionLineForm()
    else:
        task_form = TaskForm()
        line_form = ProductionLineForm()

    tasks = Task.objects.all().order_by('order')
    lines = ProductionLine.objects.all().order_by('code')
    team_leaders = User.objects.all()

    context = {
        'task_form': task_form,
        'line_form': line_form,
        'tasks': tasks,
        'lines': lines,
        'team_leaders': team_leaders,
    }
    return render(request, 'matrix/rh_tasks_lines.html', context)


@login_required
def task_update(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if request.method == 'POST':
        form = TaskForm(request.POST, instance=task)
        if form.is_valid():
            form.save()
            messages.success(request, f'✏️ Tâche "{task.title}" modifiée avec succès!')
            return redirect('rh_manage_tasks_lines')
        else:
            messages.error(request, '❌ Erreur lors de la modification de la tâche.')
    else:
        form = TaskForm(instance=task)
    return render(request, 'matrix/task_form.html', {'form': form, 'task': task})


@login_required
def task_delete(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if request.method == 'POST':
        task_title = task.title
        task.delete()
        messages.success(request, f'🗑️ Tâche "{task_title}" supprimée avec succès!')
        return redirect('rh_manage_tasks_lines')
    return render(request, 'matrix/task_confirm_delete.html', {'task': task})


@login_required
def line_update(request, pk):
    line = get_object_or_404(ProductionLine, pk=pk)
    if request.method == 'POST':
        form = ProductionLineForm(request.POST, instance=line)
        if form.is_valid():
            form.save()
            messages.success(request, f'✏️ Ligne "{line.name}" modifiée avec succès!')
            return redirect('rh_manage_tasks_lines')
        else:
            messages.error(request, '❌ Erreur lors de la modification de la ligne.')
    else:
        form = ProductionLineForm(instance=line)
    return render(request, 'matrix/line_form.html', {'form': form, 'line': line})


@login_required
def line_delete(request, pk):
    line = get_object_or_404(ProductionLine, pk=pk)
    if request.method == 'POST':
        line_name = line.name
        line.delete()
        messages.success(request, f'🗑️ Ligne "{line_name}" supprimée avec succès!')
        return redirect('rh_manage_tasks_lines')
    return render(request, 'matrix/line_confirm_delete.html', {'line': line})


# views.py

@login_required
def get_tasks_by_employee(request):
    """
    API pour récupérer les tâches selon l'employé OU la ligne
    Accepte : employee_id OU line_id
    """
    employee_id = request.GET.get('employee_id')
    line_id = request.GET.get('line_id')
    
    tasks = []
    
    # ═══════════════════════════════════════════════════════════════════
    # Priorité 1 : Si on a un line_id direct, l'utiliser
    # ═══════════════════════════════════════════════════════════════════
    if line_id:
        try:
            tasks = Task.objects.filter(lines__id=line_id).order_by('order', 'task_number').values(
                'id', 'title', 'code', 'task_number'
            )
            return JsonResponse({'tasks': list(tasks)})
        except Exception as e:
            return JsonResponse({'tasks': [], 'error': str(e)})
    
    # ═══════════════════════════════════════════════════════════════════
    # Priorité 2 : Si on a un employee_id, récupérer sa ligne
    # ═══════════════════════════════════════════════════════════════════
    if employee_id:
        try:
            employee = Employee.objects.select_related('line').get(pk=employee_id)
            
            if employee.line:
                tasks = Task.objects.filter(lines=employee.line).order_by('order', 'task_number').values(
                    'id', 'title', 'code', 'task_number'
                )
                return JsonResponse({'tasks': list(tasks)})
            else:
                return JsonResponse({'tasks': [], 'message': 'Employé sans ligne assignée'})
                
        except Employee.DoesNotExist:
            return JsonResponse({'tasks': [], 'error': 'Employé non trouvé'})
    
    # Aucun paramètre fourni
    return JsonResponse({'tasks': []})


@login_required
def line_detail(request, line_id):
    """
    Vue RH détaillée d'une ligne.
    ✅ Demandes d'approbation filtrées par ligne.
    """
    from django.db.models import Q, Count

    line         = get_object_or_404(ProductionLine, id=line_id)
    team_leaders = line.team_leaders.all()
    tasks        = Task.objects.filter(lines=line).distinct().order_by("order")

    levels = [
        {'level': 0, 'label': 'I'},
        {'level': 1, 'label': 'L'},
        {'level': 3, 'label': 'U'},
    ]

    # ═══════════════════════════════════════════════════════════════════
    # PROFILS EMPLOYEE DES CHEFS D'ÉQUIPE
    # ═══════════════════════════════════════════════════════════════════
    team_leader_employees = {}

    for leader in team_leaders:
        leader_employee = None
        if hasattr(leader, 'employee_profile'):
            try:
                leader_employee = leader.employee_profile
            except Exception:
                pass
        if not leader_employee:
            leader_employee = Employee.objects.filter(
                Q(first_name__iexact=leader.first_name,
                  last_name__iexact=leader.last_name) |
                Q(matricule__iexact=leader.username)
            ).first()
        if leader_employee:
            team_leader_employees[leader.id] = leader_employee

    leader_employee_ids = {
        le.id for le in team_leader_employees.values() if le
    }

    # ═══════════════════════════════════════════════════════════════════
    # ✅ DEMANDES D'APPROBATION GLOBALES POUR CETTE LIGNE
    # ═══════════════════════════════════════════════════════════════════
    all_employee_for_line_ids = list(
        Employee.objects.filter(
            visible_lines=line,
            manager__in=team_leaders
        ).values_list('id', flat=True)
    )

    # Demandes avec ligne explicite
    global_requests_on_line = TrainingApprovalRequest.objects.filter(
        employee_id__in=all_employee_for_line_ids,
        task__in=tasks,
        approved__isnull=True,
        line=line                          # ✅ FILTRE PAR LIGNE
    ).select_related('employee', 'task', 'line')

    # Demandes sans ligne (fallback : ligne principale = cette ligne)
    global_requests_no_line = TrainingApprovalRequest.objects.filter(
        employee_id__in=all_employee_for_line_ids,
        task__in=tasks,
        approved__isnull=True,
        line__isnull=True,
        employee__line=line
    ).select_related('employee', 'task')

    # Dictionnaire global des approbations pour cette ligne
    global_approval_dict = {}
    for req in list(global_requests_on_line) + list(global_requests_no_line):
        key = (req.employee_id, req.task_id, req.requested_level)
        if key not in global_approval_dict:
            global_approval_dict[key] = req
        elif req.date_requested > global_approval_dict[key].date_requested:
            global_approval_dict[key] = req

    # ═══════════════════════════════════════════════════════════════════
    # MATRICE PAR CHEF D'ÉQUIPE
    # ═══════════════════════════════════════════════════════════════════
    matrices_by_leader = []

    for leader in team_leaders:
        leader_employee = team_leader_employees.get(leader.id)

        leader_operators_qs = Employee.objects.filter(
            visible_lines=line,
            manager=leader
        ).exclude(
            id__in=leader_employee_ids
        ).select_related('line', 'manager').order_by("last_name", "first_name")

        leader_employees = list(leader_operators_qs)

        if leader_employee:
            if leader_employee.id not in [e.id for e in leader_employees]:
                leader_employees.insert(0, leader_employee)

        employee_ids = [e.id for e in leader_employees]

        records = TrainingRecord.objects.filter(
            employee_id__in=employee_ids,
            task__in=tasks,
            lines=line
        ).select_related('employee', 'task', 'trainer').order_by(
            'employee', 'task', '-date'
        )

        entry_map = {}
        for r in records:
            key = (r.task_id, r.employee_id)
            entry_map.setdefault(key, []).append(r)

        # ✅ Approbations filtrées pour ce chef
        leader_emp_ids = [e.id for e in leader_employees]

        leader_requests_on_line = TrainingApprovalRequest.objects.filter(
            employee_id__in=leader_emp_ids,
            task__in=tasks,
            approved__isnull=True,
            line=line                      # ✅ FILTRE PAR LIGNE
        ).select_related('employee', 'task')

        leader_requests_no_line = TrainingApprovalRequest.objects.filter(
            employee_id__in=leader_emp_ids,
            task__in=tasks,
            approved__isnull=True,
            line__isnull=True,
            employee__line=line
        ).select_related('employee', 'task')

        leader_approval_dict = {}
        for req in list(leader_requests_on_line) + list(leader_requests_no_line):
            key = (req.employee_id, req.task_id, req.requested_level)
            if key not in leader_approval_dict:
                leader_approval_dict[key] = req
            elif req.date_requested > leader_approval_dict[key].date_requested:
                leader_approval_dict[key] = req

        # Construction matrice
        matrix = []
        for task in tasks:
            row = {'task': task, 'cells': []}
            for emp in leader_employees:
                is_leader_emp = (
                    leader_employee and emp.id == leader_employee.id
                )
                other_visible = emp.visible_lines.exclude(
                    pk=line.pk
                ).values_list('code', flat=True)

                row['cells'].append({
                    'records':        entry_map.get((task.id, emp.id), []),
                    'employee':       emp,
                    'is_team_leader': is_leader_emp,
                    'other_lines':    list(other_visible),
                    'is_multiline':   emp.visible_lines.exclude(pk=line.pk).exists(),
                    # ✅ Pending filtrés par ligne
                    'pending_L': leader_approval_dict.get(
                        (emp.id, task.id, TrainingLevel.SUPERVISED)
                    ),
                    'pending_U': leader_approval_dict.get(
                        (emp.id, task.id, TrainingLevel.TRAINER)
                    ),
                })
            matrix.append(row)

        operator_count  = len([
            e for e in leader_employees
            if not (leader_employee and e.id == leader_employee.id)
        ])
        total_cells     = len(leader_employees) * tasks.count()
        filled_cells    = len(entry_map)
        completion_rate = (
            round(filled_cells / total_cells * 100, 1)
            if total_cells > 0 else 0
        )

        multiline_count = Employee.objects.filter(
            visible_lines=line, manager=leader
        ).exclude(
            id__in=leader_employee_ids
        ).annotate(
            vl_count=Count('visible_lines')
        ).filter(vl_count__gte=2).count()

        matrices_by_leader.append({
            'leader':             leader,
            'leader_name':        leader.get_full_name() or leader.username,
            'leader_employee':    leader_employee,
            'employees':          leader_employees,
            'matrix':             matrix,
            'employee_count':     len(leader_employees),
            'operator_count':     operator_count,
            'multiline_count':    multiline_count,
            'completion_rate':    completion_rate,
            'has_leader_profile': leader_employee is not None,
        })

    # ═══════════════════════════════════════════════════════════════════
    # MATRICE GLOBALE
    # ═══════════════════════════════════════════════════════════════════
    all_operators_qs = Employee.objects.filter(
        visible_lines=line,
        manager__in=team_leaders
    ).exclude(
        id__in=leader_employee_ids
    ).select_related('line', 'manager').order_by("last_name", "first_name")

    all_employees = list(all_operators_qs)

    for leader_emp in team_leader_employees.values():
        if leader_emp and leader_emp.id not in [e.id for e in all_employees]:
            all_employees.insert(0, leader_emp)

    all_employee_ids = [e.id for e in all_employees]

    all_records = TrainingRecord.objects.filter(
        employee_id__in=all_employee_ids,
        task__in=tasks,
        lines=line
    ).select_related('employee', 'task', 'trainer').order_by(
        'employee', 'task', '-date'
    )

    global_entry_map = {}
    for r in all_records:
        key = (r.task_id, r.employee_id)
        global_entry_map.setdefault(key, []).append(r)

    global_matrix = []
    for task in tasks:
        row = {'task': task, 'cells': []}
        for emp in all_employees:
            is_leader_emp = emp.id in leader_employee_ids
            other_visible = emp.visible_lines.exclude(
                pk=line.pk
            ).values_list('code', flat=True)

            row['cells'].append({
                'records':        global_entry_map.get((task.id, emp.id), []),
                'employee':       emp,
                'is_team_leader': is_leader_emp,
                'other_lines':    list(other_visible),
                'is_multiline':   emp.visible_lines.exclude(pk=line.pk).exists(),
                # ✅ Pending filtrés par ligne
                'pending_L': global_approval_dict.get(
                    (emp.id, task.id, TrainingLevel.SUPERVISED)
                ),
                'pending_U': global_approval_dict.get(
                    (emp.id, task.id, TrainingLevel.TRAINER)
                ),
            })
        global_matrix.append(row)

    # ═══════════════════════════════════════════════════════════════════
    # STATISTIQUES GLOBALES
    # ═══════════════════════════════════════════════════════════════════
    total_operators = len([
        e for e in all_employees if e.id not in leader_employee_ids
    ])
    total_leaders_with_profile = len([
        le for le in team_leader_employees.values() if le
    ])

    global_total_cells  = len(all_employees) * tasks.count()
    global_filled_cells = len(global_entry_map)
    global_completion_rate = (
        round(global_filled_cells / global_total_cells * 100, 1)
        if global_total_cells > 0 else 0
    )

    multiline_operators = Employee.objects.filter(
        visible_lines=line,
        manager__in=team_leaders
    ).exclude(
        id__in=leader_employee_ids
    ).annotate(
        vl_count=Count('visible_lines')
    ).filter(vl_count__gte=2)

    secondary_line_operators = Employee.objects.filter(
        visible_lines=line,
        manager__in=team_leaders
    ).exclude(
        id__in=leader_employee_ids
    ).exclude(line=line).count()

    context = {
        "line":                  line,
        "team_leaders":          team_leaders,
        "team_leader_employees": team_leader_employees,
        "employees":             all_employees,
        "matrix":                global_matrix,
        "levels":                levels,
        "tasks":                 tasks,
        "matrices_by_leader":    matrices_by_leader,
        "has_multiple_leaders":  team_leaders.count() > 1,
        "stats": {
            "total_employees":      len(all_employees),
            "total_operators":      total_operators,
            "total_leaders":        team_leaders.count(),
            "leaders_with_profile": total_leaders_with_profile,
            "total_tasks":          tasks.count(),
            "completion_rate":      global_completion_rate,
            "multiline_operators":  multiline_operators.count(),
            "secondary_operators":  secondary_line_operators,
        },
        "multiline_operators_list": list(
            multiline_operators.values(
                'id', 'first_name', 'last_name', 'matricule'
            )
        ),
    }

    return render(request, "matrix/line_detail.html", context)


# ═══════════════════════════════════════════════════════════════════════════════
# ✅ VUE MODIFIÉE : rh_training_approvals (Niveaux L & U uniquement)
# ═══════════════════════════════════════════════════════════════════════════════
@login_required
def rh_training_approvals(request):
    """
    ✅ Affiche et traite UNIQUEMENT les demandes de Niveau L et U.
    Les niveaux I sont gérés séparément dans rh_level_i_approvals.
    """
    if not request.user.is_superuser:
        messages.error(request, "❌ Accès réservé au service RH.")
        return redirect('matrix_view')

    # Compatibilité avec les anciens formulaires
    if request.method == "POST":
        request_id = request.POST.get("request_id")
        action = request.POST.get("action")

        if not request_id or action not in {"approve", "reject"}:
            messages.error(request, "Requête de validation RH invalide.")
            return redirect("rh_training_approvals")

        post_data = request.POST.copy()
        post_data["decision"] = action
        request.POST = post_data

        return review_training_request(
            request,
            request_id=int(request_id),
        )

    # ✅ MODIFICATION : Exclure les niveaux I
    pending = TrainingApprovalRequest.objects.filter(
        approved__isnull=True
    ).exclude(
        requested_level=TrainingLevel.IN_TRAINING  # ← Nouvelle exclusion
    ).select_related(
        'employee', 'employee__line',
        'task', 'requested_by', 'line'
    ).order_by("-date_requested")

    if request.method == "POST":
        req_id = request.POST.get("request_id")
        action = request.POST.get("action")

        if not req_id or not action:
            messages.error(request, "❌ Requête invalide.")
            return redirect("rh_training_approvals")

        req = get_object_or_404(TrainingApprovalRequest, pk=req_id)

        if req.approved is not None:
            messages.warning(
                request,
                f"⚠️ Cette demande a déjà été traitée "
                f"({'approuvée' if req.approved else 'refusée'})."
            )
            return redirect("rh_training_approvals")

        if action == "approve":
            formation_line = req.line

            if not formation_line:
                formation_line = req.employee.line

            training_date = (
                req.training_date
                if req.training_date
                else timezone.now().date()
            )

            existing = None
            if formation_line:
                existing = TrainingRecord.objects.filter(
                    employee=req.employee,
                    task=req.task,
                    level=req.requested_level,
                    lines=formation_line
                ).first()

            if existing:
                messages.warning(
                    request,
                    f"⚠️ Un niveau "
                    f"{req.get_requested_level_display()} "
                    f"existe déjà pour "
                    f"{req.employee.first_name} "
                    f"{req.employee.last_name} "
                    f"sur '{formation_line.name if formation_line else 'N/A'}'."
                )
            else:
                record = TrainingRecord.objects.create(
                    employee=req.employee,
                    task=req.task,
                    level=req.requested_level,
                    date=training_date,
                    trainer=None,
                )

                record.lines.clear()

                if formation_line:
                    record.lines.add(formation_line)

                existing_entry = MatrixEntry.objects.filter(
                    employee=req.employee,
                    task=req.task
                ).first()

                if not existing_entry:
                    MatrixEntry.objects.create(
                        employee=req.employee,
                        task=req.task,
                        level=req.requested_level
                    )
                elif req.requested_level > existing_entry.level:
                    existing_entry.level = req.requested_level
                    existing_entry.save()

                line_info = (
                    f" sur '{formation_line.name}'"
                    if formation_line else ""
                )
                messages.success(
                    request,
                    f"✅ Formation approuvée : "
                    f"{req.employee.first_name} "
                    f"{req.employee.last_name} — "
                    f"{req.task}{line_info} "
                    f"(niveau {req.get_requested_level_display()})."
                )

            req.approved = True
            req.decision_date = timezone.now()
            req.notification_seen = False
            req.save()

        elif action == "reject":
            req.approved = False
            req.decision_date = timezone.now()
            req.notification_seen = False
            req.save()

            messages.warning(
                request,
                f"❌ Formation refusée : "
                f"{req.employee.first_name} "
                f"{req.employee.last_name} — "
                f"{req.task} "
                f"(niveau {req.get_requested_level_display()})."
            )

        else:
            messages.error(request, f"❌ Action inconnue : '{action}'.")

        return redirect("rh_training_approvals")

    # ── GET : Enrichir les demandes L/U ─────────────────────────────
    pending_with_context = []
    for req in pending:
        formation_line = req.line or req.employee.line

        record_I = None
        if formation_line:
            record_I = TrainingRecord.objects.filter(
                employee=req.employee,
                task=req.task,
                level=TrainingLevel.IN_TRAINING,
                lines=formation_line,
            ).first()

        pending_with_context.append({
            'request': req,
            'formation_line': formation_line,
            'record_I': record_I,
            'days_since_I': (
                (req.training_date - record_I.date).days
                if record_I and req.training_date and record_I.date
                else None
            ),
        })

    # ✅ Compteur pour la sidebar (badges de notification)
    count_pending_i = TrainingApprovalRequest.objects.filter(
        approved__isnull=True,
        requested_level=TrainingLevel.IN_TRAINING
    ).count()

    context = {
        "pending": pending,
        "pending_with_context": pending_with_context,
        "total_pending": pending.count(),
        "count_pending_i": count_pending_i,  # ← Pour la sidebar
    }
    return render(
        request,
        "matrix/rh_training_approvals.html",
        context
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ✅ NOUVELLE VUE : rh_level_i_approvals (Niveaux I + Questionnaires)
# ═══════════════════════════════════════════════════════════════════════════════
@login_required
def rh_level_i_approvals(request):
    """
    ✅ Affiche et traite UNIQUEMENT les demandes de Niveau I.
    Affiche les questionnaires complets remplis sur le terrain.
    """
    if not request.user.is_superuser:
        messages.error(request, "❌ Accès réservé au service RH.")
        return redirect('matrix_view')

    # ── TRAITEMENT POST ─────────────────────────────────────────────
    if request.method == "POST":
        req_id = request.POST.get("request_id")
        action = request.POST.get("action")
        rejection_reason = request.POST.get("rejection_reason", "").strip()

        if not req_id or action not in {"approve", "reject"}:
            messages.error(request, "❌ Requête invalide.")
            return redirect("rh_level_i_approvals")

        req = get_object_or_404(
            TrainingApprovalRequest,
            pk=req_id,
            requested_level=TrainingLevel.IN_TRAINING  # ← Sécurité
        )

        if req.approved is not None:
            messages.warning(
                request,
                f"⚠️ Cette demande a déjà été traitée "
                f"({'approuvée' if req.approved else 'refusée'})."
            )
            return redirect("rh_level_i_approvals")

        # Déterminer la ligne de formation
        formation_line = req.line or req.employee.line
        training_date = req.training_date or timezone.now().date()

        # Trouver le TrainingRecord associé (si créé lors de la saisie)
        record = TrainingRecord.objects.filter(
            employee=req.employee,
            task=req.task,
            level=TrainingLevel.IN_TRAINING,
            lines=formation_line,
        ).first()

        if action == "approve":
            # Si le record n'existe pas, on le crée
            if not record:
                record = TrainingRecord.objects.create(
                    employee=req.employee,
                    task=req.task,
                    level=TrainingLevel.IN_TRAINING,
                    date=training_date,
                    trainer=None,
                )
                record.lines.clear()
                if formation_line:
                    record.lines.add(formation_line)

            # Marquer le record comme approuvé
            if hasattr(record, 'approval_status'):
                record.approval_status = TrainingRecord.ApprovalStatus.APPROVED
                record.save()

            # Mettre à jour MatrixEntry
            existing_entry = MatrixEntry.objects.filter(
                employee=req.employee,
                task=req.task
            ).first()

            if not existing_entry:
                MatrixEntry.objects.create(
                    employee=req.employee,
                    task=req.task,
                    level=TrainingLevel.IN_TRAINING
                )
            elif TrainingLevel.IN_TRAINING > existing_entry.level:
                existing_entry.level = TrainingLevel.IN_TRAINING
                existing_entry.save()

            req.approved = True
            req.decision_date = timezone.now()
            req.notification_seen = False
            req.save()

            line_info = f" sur '{formation_line.name}'" if formation_line else ""
            messages.success(
                request,
                f"✅ Formation Niveau I approuvée : "
                f"{req.employee.first_name} {req.employee.last_name} — "
                f"{req.task}{line_info}."
            )

        elif action == "reject":
            # Marquer le record comme rejeté (si existant)
            if record and hasattr(record, 'approval_status'):
                record.approval_status = TrainingRecord.ApprovalStatus.REJECTED
                record.save()

            req.approved = False
            req.decision_date = timezone.now()
            req.notification_seen = False
            if hasattr(req, 'rejection_reason'):
                req.rejection_reason = rejection_reason
            req.save()

            messages.warning(
                request,
                f"❌ Formation Niveau I refusée : "
                f"{req.employee.first_name} {req.employee.last_name} — "
                f"{req.task}."
            )

        return redirect("rh_level_i_approvals")

    # ── GET : Récupération des demandes Niveau I ────────────────────
    pending = TrainingApprovalRequest.objects.filter(
        approved__isnull=True,
        requested_level=TrainingLevel.IN_TRAINING  # ← Uniquement Niveau I
    ).select_related(
        'employee', 'employee__line',
        'task', 'requested_by', 'line'
    ).order_by("-date_requested")

    # Enrichir chaque demande avec son questionnaire
    pending_with_questionnaire = []
    for req in pending:
        formation_line = req.line or req.employee.line

        # Chercher le TrainingRecord associé
        record = TrainingRecord.objects.filter(
            employee=req.employee,
            task=req.task,
            level=TrainingLevel.IN_TRAINING,
            lines=formation_line,
        ).first()

        # Récupérer les réponses au questionnaire
        questionnaire_data = None
        if record:
            try:
                questionnaire = record.questionnaire
                answers_list = []
                
                for ans in questionnaire.answers.select_related('question').all():
                    # Déterminer si la réponse est conforme
                    is_conforme = None
                    display_value = "—"
                    
                    if ans.question.answer_type == 'YES_NO':
                        if ans.boolean_answer is True:
                            display_value = "Oui"
                        elif ans.boolean_answer is False:
                            display_value = "Non"
                        
                        # Vérifier la conformité
                        if ans.question.expected_boolean is not None:
                            is_conforme = (ans.boolean_answer == ans.question.expected_boolean)
                    else:
                        display_value = ans.text_answer or "—"
                    
                    answers_list.append({
                        'question': ans.question,
                        'value': display_value,
                        'comment': ans.comment or "",
                        'is_conforme': is_conforme,
                        'is_blocking': ans.question.is_blocking,
                    })
                
                questionnaire_data = {
                    'answers': answers_list,
                    'general_comment': questionnaire.general_comment or "",
                }
            except Exception:
                questionnaire_data = None

        pending_with_questionnaire.append({
            'request': req,
            'formation_line': formation_line,
            'record': record,
            'questionnaire': questionnaire_data,
        })

    # Compteur pour la sidebar
    count_pending_lu = TrainingApprovalRequest.objects.filter(
        approved__isnull=True
    ).exclude(
        requested_level=TrainingLevel.IN_TRAINING
    ).count()

    context = {
        "pending_with_questionnaire": pending_with_questionnaire,
        "total_pending_i": pending.count(),
        "count_pending_lu": count_pending_lu,
    }
    return render(
        request,
        "matrix/rh_level_i_approvals.html",
        context
    )


# ═══════════════════════════════════════════════════════════════════════════════
# RESTE DU CODE (inchangé)
# ═══════════════════════════════════════════════════════════════════════════════

import json
from django.db.models import Count, Q, Avg, Max, Min, Sum, F, Value, CharField
from django.db.models.functions import TruncMonth, TruncWeek, Concat
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone
from datetime import datetime, timedelta
from collections import defaultdict

# Importez vos modèles ici
from .models import (
    ProductionLine, Employee, Task, TrainingRecord, 
    TrainingLevel, TrainingApprovalRequest
)


from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.views.decorators.cache import cache_page
from django.utils import timezone
from django.db.models import Q, Count, Avg
from django.db.models.functions import TruncMonth
from datetime import timedelta
import json

# Importe tes modèles
from .models import (
    ProductionLine, Employee, Task, TrainingRecord, 
    TrainingLevel, TrainingApprovalRequest,
    RequalificationApprovalRequest, RequalificationRecord
)


import json
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.views.decorators.cache import cache_page
from django.shortcuts import render

from .models import (
    Employee, ProductionLine, Task, TrainingRecord,
    TrainingLevel, RequalificationRecord,
    RequalificationApprovalRequest, TrainingApprovalRequest,
    MatrixEntry,
)


@login_required
@cache_page(60 * 5)
def rh_dashboard(request):
    """
    Dashboard RH — VERSION COMPLÈTE CORRIGÉE
    ✅ Utilise visible_lines pour tous les calculs
    """

    today  = timezone.now().date()
    alerts = []

    # ══════════════════════════════════════════════════════════════════
    # 1. CHARGEMENT UNIQUE DE TOUTES LES DONNÉES
    # ══════════════════════════════════════════════════════════════════

    # Lignes
    lines      = list(
        ProductionLine.objects.prefetch_related(
            'team_leaders'
        ).order_by('code')
    )
    lines_by_id  = {l.id: l for l in lines}
    total_lines  = len(lines)

    # Employés avec visible_lines
    employees_with_lines = list(
        Employee.objects.prefetch_related(
            'visible_lines'
        ).select_related('line', 'manager').all()
    )
    employees        = employees_with_lines
    employees_by_id  = {e.id: e for e in employees}
    total_employees  = len(employees)

    # ✅ Index employé → lignes visibles
    employee_visible_lines = {}
    for emp in employees_with_lines:
        employee_visible_lines[emp.id] = list(emp.visible_lines.all())

    # ✅ Index ligne → employés visibles
    visible_employees_by_line = {}
    for emp in employees_with_lines:
        for line in employee_visible_lines.get(emp.id, []):
            visible_employees_by_line.setdefault(
                line.id, []
            ).append(emp)

    # Index employés par manager
    employees_by_manager = {}
    for e in employees:
        if e.manager_id:
            employees_by_manager.setdefault(
                e.manager_id, []
            ).append(e)

    # Tâches
    all_tasks    = list(
        Task.objects.prefetch_related('lines').order_by('code')
    )
    tasks_by_id  = {t.id: t for t in all_tasks}
    total_tasks  = len(all_tasks)

    # Index tâches par ligne
    tasks_by_line = {}
    for task in all_tasks:
        for line in task.lines.all():
            tasks_by_line.setdefault(line.id, set()).add(task.id)

    # ✅ Records avec leurs lignes (ManyToMany)
    all_records_with_lines = list(
        TrainingRecord.objects.select_related(
            'employee', 'employee__line',
            'employee__manager', 'task', 'trainer'
        ).prefetch_related('lines').all()
    )
    records       = all_records_with_lines
    total_records = len(records)

    # ══════════════════════════════════════════════════════════════════
    # 2. INDEXATION DES RECORDS
    # ══════════════════════════════════════════════════════════════════

    records_by_employee = {}
    records_by_task     = {}
    records_by_emp_task = {}

    for r in records:
        records_by_employee.setdefault(r.employee_id, []).append(r)
        records_by_task.setdefault(r.task_id, []).append(r)
        key = (r.employee_id, r.task_id)
        records_by_emp_task.setdefault(key, []).append(r)

    # ✅ Index (employee_id, line_id) → dernier record
    last_record_by_emp_line = {}
    for r in records:
        if not r.date:
            continue
        for line in r.lines.all():
            key      = (r.employee_id, line.id)
            existing = last_record_by_emp_line.get(key)
            if not existing or r.date > existing.date:
                last_record_by_emp_line[key] = r

    # ✅ Index ligne_id → records (via ManyToMany)
    records_by_line_visible = {}
    for r in records:
        for line in r.lines.all():
            records_by_line_visible.setdefault(line.id, []).append(r)

    # ══════════════════════════════════════════════════════════════════
    # 3. STATISTIQUES DES NIVEAUX
    # ══════════════════════════════════════════════════════════════════

    i_count = 0
    l_count = 0
    u_count = 0

    for r in records:
        if r.level == TrainingLevel.IN_TRAINING:
            i_count += 1
        elif r.level == TrainingLevel.SUPERVISED:
            l_count += 1
        elif r.level == TrainingLevel.TRAINER:
            u_count += 1

    level_counts = {
        'i_count': i_count,
        'l_count': l_count,
        'u_count': u_count,
    }

    total_levels      = sum(level_counts.values()) or 1
    level_distribution = {
        'i_pct': round((i_count / total_levels) * 100, 1),
        'l_pct': round((l_count / total_levels) * 100, 1),
        'u_pct': round((u_count / total_levels) * 100, 1),
    }

    # ══════════════════════════════════════════════════════════════════
    # 4. INDICATEURS GLOBAUX
    # ✅ Basé sur visible_lines
    # ══════════════════════════════════════════════════════════════════

    trainers_set               = set(
        r.employee_id for r in records
        if r.level == TrainingLevel.TRAINER
    )
    unique_trainers            = len(trainers_set)

    employees_with_training_set = set(r.employee_id for r in records)
    employees_with_training     = len(employees_with_training_set)
    employees_without_training  = total_employees - employees_with_training

    # ✅ Taux de couverture basé sur visible_lines
    total_possible_combinations = 0
    for emp in employees_with_lines:
        for line in employee_visible_lines.get(emp.id, []):
            line_task_count = len(tasks_by_line.get(line.id, set()))
            total_possible_combinations += line_task_count * 3

    max_possible         = total_possible_combinations or 1
    global_coverage_rate = round(
        (total_records / max_possible) * 100, 1
    )

    mastered_records = l_count + u_count
    max_mastery      = (total_possible_combinations * 2 // 3) or 1
    mastery_rate     = round(
        (mastered_records / max_mastery) * 100, 1
    )

    polyvalence_by_emp = {}
    for r in records:
        if r.level in [
            TrainingLevel.SUPERVISED,
            TrainingLevel.TRAINER
        ]:
            polyvalence_by_emp.setdefault(
                r.employee_id, set()
            ).add(r.task_id)

    if polyvalence_by_emp:
        avg_tasks_per_employee = sum(
            len(t) for t in polyvalence_by_emp.values()
        ) / len(polyvalence_by_emp)
    else:
        avg_tasks_per_employee = 0

    polyvalence_rate = round(
        (avg_tasks_per_employee / total_tasks) * 100, 1
    ) if total_tasks else 0

    # ══════════════════════════════════════════════════════════════════
    # 5. PÉRIODES DE TEMPS
    # ══════════════════════════════════════════════════════════════════

    current_month_start  = today.replace(day=1)
    last_month_start     = (
        current_month_start - timedelta(days=1)
    ).replace(day=1)
    last_week            = today - timedelta(days=7)
    last_30_days         = today - timedelta(days=30)
    last_90_days         = today - timedelta(days=90)

    current_month_records    = 0
    last_month_records_count = 0
    this_week_records        = 0
    last_30_days_records     = 0

    for r in records:
        if not r.date:
            continue
        if r.date >= current_month_start:
            current_month_records += 1
        if last_month_start <= r.date < current_month_start:
            last_month_records_count += 1
        if r.date >= last_week:
            this_week_records += 1
        if r.date >= last_30_days:
            last_30_days_records += 1

    month_evolution = current_month_records - last_month_records_count
    month_evolution_pct = (
        round(
            (month_evolution / last_month_records_count) * 100, 1
        )
        if last_month_records_count > 0
        else (100 if current_month_records > 0 else 0)
    )

    # ══════════════════════════════════════════════════════════════════
    # 6. DONNÉES MENSUELLES POUR GRAPHIQUES
    # ══════════════════════════════════════════════════════════════════

    start_month  = (
        today.replace(day=1) - timedelta(days=365)
    ).replace(day=1)

    monthly_data = {}
    for r in records:
        if not r.date or r.date < start_month:
            continue
        month_key = r.date.replace(day=1)
        if month_key not in monthly_data:
            monthly_data[month_key] = {
                'total':   0, 'i_count': 0,
                'l_count': 0, 'u_count': 0,
                'employees': set(), 'tasks': set(),
            }
        monthly_data[month_key]['total'] += 1
        if r.level == TrainingLevel.IN_TRAINING:
            monthly_data[month_key]['i_count'] += 1
        elif r.level == TrainingLevel.SUPERVISED:
            monthly_data[month_key]['l_count'] += 1
        elif r.level == TrainingLevel.TRAINER:
            monthly_data[month_key]['u_count'] += 1
        monthly_data[month_key]['employees'].add(r.employee_id)
        monthly_data[month_key]['tasks'].add(r.task_id)

    labels             = []
    series_total       = []
    series_i           = []
    series_l           = []
    series_u           = []
    series_employees   = []
    series_tasks       = []
    series_cumulative  = []
    cumulative_total   = 0

    m = start_month
    while m <= today:
        labels.append(m.strftime('%b %Y'))
        data = monthly_data.get(m)
        if data:
            series_total.append(data['total'])
            series_i.append(data['i_count'])
            series_l.append(data['l_count'])
            series_u.append(data['u_count'])
            series_employees.append(len(data['employees']))
            series_tasks.append(len(data['tasks']))
            cumulative_total += data['total']
        else:
            series_total.append(0)
            series_i.append(0)
            series_l.append(0)
            series_u.append(0)
            series_employees.append(0)
            series_tasks.append(0)
        series_cumulative.append(cumulative_total)

        if m.month == 12:
            m = m.replace(year=m.year + 1, month=1)
        else:
            m = m.replace(month=m.month + 1)

    # ══════════════════════════════════════════════════════════════════
    # 7. STATISTIQUES PAR LIGNE
    # ✅ Utilise visible_employees_by_line et records_by_line_visible
    # ══════════════════════════════════════════════════════════════════

    line_stats = []

    for line in lines:
        # ✅ Employés visibles sur cette ligne
        line_emps        = visible_employees_by_line.get(line.id, [])
        emp_count        = len(line_emps)

        task_ids         = tasks_by_line.get(line.id, set())
        total_line_tasks = len(task_ids)

        # ✅ Records liés à cette ligne via ManyToMany
        line_records      = [
            r for r in records_by_line_visible.get(line.id, [])
            if r.task_id in task_ids
        ]
        line_total_records = len(line_records)

        line_i = sum(
            1 for r in line_records
            if r.level == TrainingLevel.IN_TRAINING
        )
        line_l = sum(
            1 for r in line_records
            if r.level == TrainingLevel.SUPERVISED
        )
        line_u = sum(
            1 for r in line_records
            if r.level == TrainingLevel.TRAINER
        )

        line_trainers = len(set(
            r.employee_id for r in line_records
            if r.level == TrainingLevel.TRAINER
        ))

        mastered_tasks      = 0
        critical_tasks_list = []
        task_coverages      = []

        for task_id in task_ids:
            trained_emps = set(
                r.employee_id for r in line_records
                if r.task_id == task_id
                and r.level in [
                    TrainingLevel.SUPERVISED,
                    TrainingLevel.TRAINER,
                ]
            )
            trained_count = len(trained_emps)
            coverage      = round(
                (trained_count / emp_count) * 100, 1
            ) if emp_count else 0
            task_coverages.append(coverage)

            if emp_count > 0 and (trained_count / emp_count) >= 0.80:
                mastered_tasks += 1
            elif coverage < 30:
                task = tasks_by_id.get(task_id)
                if task:
                    critical_tasks_list.append({
                        'task':     task.title,
                        'coverage': coverage,
                    })

        avg_coverage = round(
            sum(task_coverages) / len(task_coverages), 1
        ) if task_coverages else 0

        mastery_pct = round(
            (mastered_tasks / total_line_tasks) * 100, 1
        ) if total_line_tasks else 0

        emp_polyvalence = []
        for emp in line_emps:
            emp_tasks = len(set(
                r.task_id for r in line_records
                if r.employee_id == emp.id
                and r.level in [
                    TrainingLevel.SUPERVISED,
                    TrainingLevel.TRAINER,
                ]
            ))
            emp_polyvalence.append(emp_tasks)

        avg_polyvalence   = (
            sum(emp_polyvalence) / len(emp_polyvalence)
            if emp_polyvalence else 0
        )
        polyvalence_index = round(
            (avg_polyvalence / total_line_tasks) * 100, 1
        ) if total_line_tasks else 0

        risk_score = 100 - min(
            100,
            (line_trainers / max(1, total_line_tasks)) * 100
        )

        team_leaders     = list(line.team_leaders.all())
        team_leaders_str = ", ".join([
            f"{l.first_name} {l.last_name}".strip() or l.username
            for l in team_leaders
        ]) if team_leaders else "N/A"

        if avg_coverage >= 70:
            status, status_color = 'excellent', 'success'
        elif avg_coverage >= 50:
            status, status_color = 'bon',       'info'
        elif avg_coverage >= 40:
            status, status_color = 'attention', 'warning'
        else:
            status, status_color = 'critique',  'danger'

        line_stats.append({
            'line_id':          line.id,
            'code':             line.code,
            'name':             line.name,
            'employees':        emp_count,
            'tasks':            total_line_tasks,
            'total_records':    line_total_records,
            'mastered_tasks':   mastered_tasks,
            'mastery_pct':      mastery_pct,
            'avg_coverage':     avg_coverage,
            'i_count':          line_i,
            'l_count':          line_l,
            'u_count':          line_u,
            'trainers':         line_trainers,
            'polyvalence_index': polyvalence_index,
            'risk_score':       round(risk_score, 1),
            'critical_tasks':   critical_tasks_list[:3],
            'team_leaders':     team_leaders_str,
            'status':           status,
            'status_color':     status_color,
        })

    line_stats_sorted = sorted(
        line_stats,
        key=lambda x: x['avg_coverage'],
        reverse=True,
    )

    avg_line_mastery = round(
        sum(l['avg_coverage'] for l in line_stats) /
        len(line_stats), 1
    ) if line_stats else 0

    avg_line_polyvalence = round(
        sum(l['polyvalence_index'] for l in line_stats) /
        len(line_stats), 1
    ) if line_stats else 0

    # ══════════════════════════════════════════════════════════════════
    # 8. REQUALIFICATION
    # ✅ Par ligne visible
    # ══════════════════════════════════════════════════════════════════

    threshold_days = 188
    threshold_date = today - timedelta(days=threshold_days)

    # Demandes en attente
    pending_reqs = list(
        RequalificationApprovalRequest.objects.filter(
            approved__isnull=True
        ).select_related('line')
    )
    pending_requalification_pairs = set()
    for req in pending_reqs:
        pending_requalification_pairs.add(
            (req.employee_id, req.task_id, req.line_id)
        )

    # Requalifications récentes
    recent_requalif_qs = list(
        RequalificationRecord.objects.filter(
            requalification_date__gte=threshold_date
        ).values_list('employee_id', 'task_id', 'line_id')
    )
    recent_requalification_pairs = set(recent_requalif_qs)

    employees_to_requalify_global = []
    requalification_by_line_dict  = {}

    for emp in employees_with_lines:
        for line in employee_visible_lines.get(emp.id, []):

            # Dernier record SUR CETTE LIGNE
            last_record = last_record_by_emp_line.get(
                (emp.id, line.id)
            )

            if not last_record or not last_record.date:
                continue
            if last_record.date > threshold_date:
                continue

            days_since = (today - last_record.date).days

            # Requalification récente sur cette ligne
            already = (
                (emp.id, last_record.task_id, line.id)
                in recent_requalification_pairs
                or
                (emp.id, None, line.id)
                in recent_requalification_pairs
            )
            if already:
                continue

            # Demande en attente sur cette ligne
            has_pending = (
                (emp.id, last_record.task_id, line.id)
                in pending_requalification_pairs
                or
                (emp.id, None, line.id)
                in pending_requalification_pairs
            )

            urgency = (
                'critical' if days_since > 250 else
                'warning'  if days_since > 200 else
                'info'
            )

            employees_to_requalify_global.append({
                'employee':              emp,
                'task':                  last_record.task,
                'line':                  line,
                'manager':               emp.manager,
                'last_training_date':    last_record.date,
                'current_level':         last_record.level,
                'current_level_display': last_record.get_level_display(),
                'days_since':            days_since,
                'days_overdue':          days_since - threshold_days,
                'urgency':               urgency,
                'has_pending_request':   has_pending,
            })

            line_key = line.code
            if line_key not in requalification_by_line_dict:
                requalification_by_line_dict[line_key] = {
                    'code':     line.code,
                    'name':     line.name,
                    'critical': 0,
                    'warning':  0,
                    'info':     0,
                    'total':    0,
                }
            requalification_by_line_dict[line_key][urgency] += 1
            requalification_by_line_dict[line_key]['total']  += 1

    employees_to_requalify_global.sort(
        key=lambda x: (
            {'critical': 0, 'warning': 1, 'info': 2}.get(
                x['urgency'], 3
            ),
            -x['days_since'],
        )
    )

    total_to_requalify = len(employees_to_requalify_global)

    requalification_stats = {
        'total':    total_to_requalify,
        'critical': len([
            e for e in employees_to_requalify_global
            if e['urgency'] == 'critical'
        ]),
        'warning':  len([
            e for e in employees_to_requalify_global
            if e['urgency'] == 'warning'
        ]),
        'info':     len([
            e for e in employees_to_requalify_global
            if e['urgency'] == 'info'
        ]),
        'total_employees': total_employees,
        'compliance_rate': round(
            (total_employees - total_to_requalify) /
            total_employees * 100, 1
        ) if total_employees > 0 else 100,
    }

    requalification_by_line = sorted(
        requalification_by_line_dict.values(),
        key=lambda x: x['total'],
        reverse=True,
    )

    existing_codes = {l['code'] for l in requalification_by_line}
    for line in lines:
        if line.code not in existing_codes:
            requalification_by_line.append({
                'code':     line.code,
                'name':     line.name,
                'critical': 0,
                'warning':  0,
                'info':     0,
                'total':    0,
            })

    pending_requalification_requests = list(
        RequalificationApprovalRequest.objects.filter(
            approved__isnull=True
        ).select_related(
            'employee', 'task', 'line', 'requested_by'
        ).order_by('-date_requested')[:10]
    )
    pending_requalification_count = len(pending_reqs)

    recent_requalifications = list(
        RequalificationRecord.objects.select_related(
            'employee', 'task', 'line', 'evaluated_by'
        ).order_by('-requalification_date')[:10]
    )

    requalified_employees = list(
        RequalificationRecord.objects.select_related(
            'employee', 'task', 'line', 'evaluated_by'
        ).order_by('-requalification_date')
    )

    requalification_results = {
        'total':    len(requalified_employees),
        'pass':     sum(
            1 for r in requalified_employees
            if r.result == 'PASS'
        ),
        'upgrade':  sum(
            1 for r in requalified_employees
            if r.result == 'UPGRADE'
        ),
        'downgrade': sum(
            1 for r in requalified_employees
            if r.result == 'DOWNGRADE'
        ),
        'retrain':  sum(
            1 for r in requalified_employees
            if r.result == 'RETRAIN'
        ),
    }

    if total_to_requalify > 0:
        alerts.append({
            'type':    'danger' if requalification_stats['critical'] > 0
                       else 'warning',
            'icon':    'fas fa-user-clock',
            'title':   f"{total_to_requalify} employé(s) à requalifier",
            'message': (
                f"{requalification_stats['critical']} critique(s), "
                f"{requalification_stats['warning']} attention"
            ),
            'link':    'rh_employees_to_requalify',
            'count':   total_to_requalify,
        })

    if pending_requalification_count > 0:
        alerts.append({
            'type':    'info',
            'icon':    'fas fa-clipboard-check',
            'title':   f"{pending_requalification_count} demande(s)",
            'message': "Demandes de requalification en attente RH",
            'link':    'rh_requalification_approvals',
            'count':   pending_requalification_count,
        })

    # ══════════════════════════════════════════════════════════════════
    # 9. ANALYSE DES TÂCHES
    # ══════════════════════════════════════════════════════════════════

    task_analysis = []

    for task in all_tasks:
        task_line_ids   = set(l.id for l in task.lines.all())
        task_lines_codes = [l.code for l in task.lines.all()]

        # ✅ Employés assignés via visible_lines
        emp_ids_for_task = set()
        for line_id in task_line_ids:
            for emp in visible_employees_by_line.get(line_id, []):
                emp_ids_for_task.add(emp.id)
        total_employees_for_task = len(emp_ids_for_task)

        task_records = [
            r for r in records_by_task.get(task.id, [])
            if r.employee_id in emp_ids_for_task
        ]

        employees_with_i = set(
            r.employee_id for r in task_records
            if r.level == TrainingLevel.IN_TRAINING
        )
        employees_with_l = set(
            r.employee_id for r in task_records
            if r.level == TrainingLevel.SUPERVISED
        )
        employees_with_u = set(
            r.employee_id for r in task_records
            if r.level == TrainingLevel.TRAINER
        )

        i_count_task = len(employees_with_i)
        l_count_task = len(employees_with_l)
        u_count_task = len(employees_with_u)

        employees_trained_ids  = set(
            r.employee_id for r in task_records
        )
        employees_mastered_ids = employees_with_l | employees_with_u
        employees_only_i       = employees_with_i - employees_mastered_ids

        total_employees_trained    = len(employees_trained_ids)
        total_employees_mastered   = len(employees_mastered_ids)
        total_employees_not_trained = (
            total_employees_for_task - total_employees_trained
        )
        total_employees_in_progress = len(employees_only_i)

        if total_employees_for_task > 0:
            coverage_pct     = round(
                total_employees_trained /
                total_employees_for_task * 100, 1
            )
            mastery_pct_task = round(
                total_employees_mastered /
                total_employees_for_task * 100, 1
            )
            not_trained_pct  = round(
                total_employees_not_trained /
                total_employees_for_task * 100, 1
            )
            trainer_pct      = round(
                u_count_task /
                total_employees_for_task * 100, 1
            )
        else:
            coverage_pct = mastery_pct_task = trainer_pct = 0
            not_trained_pct = 100

        task_records_sorted  = sorted(
            task_records, key=lambda r: r.date, reverse=True
        )
        last_training_record = (
            task_records_sorted[0] if task_records_sorted else None
        )
        last_training_date   = (
            last_training_record.date
            if last_training_record else None
        )
        trainings_this_month = sum(
            1 for r in task_records
            if r.date >= current_month_start
        )

        if u_count_task == 0:
            criticality       = "CRITIQUE"
            criticality_color = "danger"
            criticality_icon  = "exclamation-triangle"
            criticality_order = 1
        elif u_count_task < 2 and total_employees_for_task > 5:
            criticality       = "RISQUE"
            criticality_color = "warning"
            criticality_icon  = "exclamation-circle"
            criticality_order = 2
        elif mastery_pct_task < 30:
            criticality       = "ATTENTION"
            criticality_color = "info"
            criticality_icon  = "info-circle"
            criticality_order = 3
        else:
            criticality       = "OK"
            criticality_color = "success"
            criticality_icon  = "check-circle"
            criticality_order = 4

        employees_per_trainer = (
            round(total_employees_for_task / u_count_task, 1)
            if u_count_task > 0 else None
        )

        task_analysis.append({
            'task_id':                   task.id,
            'code':                      task.code,
            'title':                     task.title,
            'description':               getattr(task, 'description', ''),
            'lines':                     task_lines_codes,
            'lines_str':                 ", ".join(task_lines_codes)
                                         if task_lines_codes else "Aucune",
            'lines_count':               len(task_line_ids),
            'total_employees_assigned':  total_employees_for_task,
            'total_employees_trained':   total_employees_trained,
            'total_employees_mastered':  total_employees_mastered,
            'total_employees_not_trained': total_employees_not_trained,
            'total_employees_in_progress': total_employees_in_progress,
            'i_count':                   i_count_task,
            'l_count':                   l_count_task,
            'u_count':                   u_count_task,
            'coverage_pct':              coverage_pct,
            'mastery_pct':               mastery_pct_task,
            'not_trained_pct':           not_trained_pct,
            'trainer_pct':               trainer_pct,
            'employees_per_trainer':     employees_per_trainer,
            'last_training_date':        last_training_date,
            'trainings_this_month':      trainings_this_month,
            'criticality':               criticality,
            'criticality_color':         criticality_color,
            'criticality_icon':          criticality_icon,
            'criticality_order':         criticality_order,
        })

    tasks_with_no_trainer     = len([
        t for t in task_analysis if t['u_count'] == 0
    ])
    tasks_fully_covered       = len([
        t for t in task_analysis if t['mastery_pct'] >= 80
    ])
    tasks_partially_covered   = len([
        t for t in task_analysis
        if 30 <= t['mastery_pct'] < 80
    ])
    tasks_poorly_covered      = len([
        t for t in task_analysis if t['mastery_pct'] < 30
    ])

    if task_analysis:
        avg_employees_per_task  = round(
            sum(t['total_employees_assigned'] for t in task_analysis) /
            len(task_analysis), 1
        )
        avg_trained_per_task    = round(
            sum(t['total_employees_trained'] for t in task_analysis) /
            len(task_analysis), 1
        )
        avg_not_trained_per_task = round(
            sum(
                t['total_employees_not_trained']
                for t in task_analysis
            ) / len(task_analysis), 1
        )
        avg_coverage_pct        = round(
            sum(t['coverage_pct'] for t in task_analysis) /
            len(task_analysis), 1
        )
        avg_mastery_pct         = round(
            sum(t['mastery_pct'] for t in task_analysis) /
            len(task_analysis), 1
        )
        avg_trainers_per_task   = round(
            sum(t['u_count'] for t in task_analysis) /
            len(task_analysis), 1
        )
    else:
        avg_employees_per_task  = 0
        avg_trained_per_task    = 0
        avg_not_trained_per_task = 0
        avg_coverage_pct        = 0
        avg_mastery_pct         = 0
        avg_trainers_per_task   = 0

    total_employees_not_trained_global = sum(
        t['total_employees_not_trained'] for t in task_analysis
    )

    task_analysis_by_coverage    = sorted(
        task_analysis,
        key=lambda x: x['coverage_pct'],
        reverse=True,
    )
    task_analysis_by_criticality = sorted(
        task_analysis,
        key=lambda x: x['criticality_order'],
    )
    task_analysis_by_trainers    = sorted(
        task_analysis,
        key=lambda x: x['u_count'],
        reverse=True,
    )
    task_analysis_by_not_trained = sorted(
        task_analysis,
        key=lambda x: x['total_employees_not_trained'],
        reverse=True,
    )

    task_coverage_worst = sorted(
        task_analysis, key=lambda x: x['coverage_pct']
    )[:5]
    task_coverage_best  = sorted(
        task_analysis,
        key=lambda x: x['coverage_pct'],
        reverse=True,
    )[:5]
    critical_tasks = [
        t for t in task_analysis if t['u_count'] == 0
    ]

    # ══════════════════════════════════════════════════════════════════
    # 10. ANALYSE DES EMPLOYÉS
    # ✅ Basé sur visible_lines
    # ══════════════════════════════════════════════════════════════════

    employee_analysis = []

    for emp in employees_with_lines:
        emp_records   = records_by_employee.get(emp.id, [])
        emp_vis_lines = employee_visible_lines.get(emp.id, [])

        # ✅ Tâches de toutes les lignes visibles
        all_line_task_ids = set()
        for line in emp_vis_lines:
            all_line_task_ids.update(
                tasks_by_line.get(line.id, set())
            )
        total_line_tasks = len(all_line_task_ids) or 1

        emp_task_levels = {}
        for r in emp_records:
            if r.task_id not in all_line_task_ids:
                continue
            current_max = emp_task_levels.get(r.task_id, 0)
            level_value = 0
            if r.level == TrainingLevel.IN_TRAINING:
                level_value = 1
            elif r.level == TrainingLevel.SUPERVISED:
                level_value = 2
            elif r.level == TrainingLevel.TRAINER:
                level_value = 3
            emp_task_levels[r.task_id] = max(current_max, level_value)

        tasks_with_i_only = sum(
            1 for v in emp_task_levels.values() if v == 1
        )
        tasks_with_l_max  = sum(
            1 for v in emp_task_levels.values() if v == 2
        )
        tasks_with_u      = sum(
            1 for v in emp_task_levels.values() if v == 3
        )

        total_points = (
            tasks_with_i_only * 0.25 +
            tasks_with_l_max  * 0.75 +
            tasks_with_u      * 1.0
        )
        progress_pct = min(
            100.0,
            round((total_points / total_line_tasks) * 100, 1)
        )

        total_tasks_touched = (
            tasks_with_i_only + tasks_with_l_max + tasks_with_u
        )
        coverage_pct_emp = round(
            (total_tasks_touched / total_line_tasks) * 100, 1
        ) if total_line_tasks else 0

        u_ratio = (
            tasks_with_u / total_tasks_touched * 100
            if total_tasks_touched else 0
        )

        if (tasks_with_u > 0 and u_ratio >= 50
                and coverage_pct_emp >= 80):
            status, status_color = 'Expert',   'success'
        elif ((tasks_with_u > 0 or tasks_with_l_max > 0)
              and coverage_pct_emp >= 50):
            status, status_color = 'Confirmé', 'info'
        elif (total_tasks_touched > 0
              and coverage_pct_emp >= 25):
            status, status_color = 'En cours', 'warning'
        else:
            status, status_color = 'Débutant', 'secondary'

        emp_records_sorted  = sorted(
            emp_records, key=lambda r: r.date, reverse=True
        )
        last_training       = (
            emp_records_sorted[0] if emp_records_sorted else None
        )
        last_training_date  = (
            last_training.date if last_training else None
        )
        days_since_training = (
            (today - last_training_date).days
            if last_training_date else None
        )

        emp_line = emp.line

        employee_analysis.append({
            'id':               emp.id,
            'name':             f"{emp.first_name} {emp.last_name}",
            'matricule':        emp.matricule,
            'line':             emp_line.code if emp_line else "N/A",
            'line_name':        emp_line.name if emp_line else "N/A",
            # ✅ Toutes les lignes visibles
            'visible_lines':    ", ".join(
                l.code for l in emp_vis_lines
            ) if emp_vis_lines else "N/A",
            'visible_lines_count': len(emp_vis_lines),
            'manager':          (
                f"{emp.manager.first_name} {emp.manager.last_name}"
                if emp.manager else "N/A"
            ),
            'i_count':          tasks_with_i_only,
            'l_count':          tasks_with_l_max,
            'u_count':          tasks_with_u,
            'total_tasks':      total_tasks_touched,
            'total_line_tasks': total_line_tasks,
            'progress_pct':     progress_pct,
            'coverage_pct':     coverage_pct_emp,
            'last_training_date': last_training_date,
            'days_since_training': days_since_training,
            'status':           status,
            'status_color':     status_color,
        })

    sorted_polyvalent = sorted(
        [e for e in employee_analysis if e['u_count'] > 0],
        key=lambda x: (x['u_count'], x['progress_pct']),
        reverse=True,
    )

    top_polyvalent_grouped = []
    current_rank = 0
    prev_key     = None

    for emp in sorted_polyvalent:
        current_key = (emp['u_count'], emp['progress_pct'])
        if current_key != prev_key:
            current_rank += 1
            if current_rank > 10:
                break
            top_polyvalent_grouped.append({
                'rank':        current_rank,
                'u_count':     emp['u_count'],
                'progress_pct': emp['progress_pct'],
                'employees':   [emp],
            })
            prev_key = current_key
        else:
            top_polyvalent_grouped[-1]['employees'].append(emp)

    top_polyvalent = sorted_polyvalent[:10]

    top_trainers = sorted(
        [e for e in employee_analysis if e['u_count'] > 0],
        key=lambda x: (x['u_count'], x['progress_pct']),
        reverse=True,
    )[:10]

    employees_needing_training = sorted(
        [e for e in employee_analysis if e['progress_pct'] < 50],
        key=lambda x: x['progress_pct'],
    )[:10]

    inactive_employees = sorted(
        [
            e for e in employee_analysis
            if e['days_since_training'] is not None
            and e['days_since_training'] > 180
        ],
        key=lambda x: x['days_since_training'],
        reverse=True,
    )

    # ══════════════════════════════════════════════════════════════════
    # 11. STATS CHEFS D'ÉQUIPE PAR CHEF
    # ✅ Records filtrés par ligne du chef
    # ══════════════════════════════════════════════════════════════════

    team_leader_stats = []
    all_team_leaders  = set()

    for line in lines:
        for leader in line.team_leaders.all():
            all_team_leaders.add(leader)

    for leader in all_team_leaders:
        leader_lines       = [
            l for l in lines
            if leader in l.team_leaders.all()
        ]
        leader_lines_codes = [l.code for l in leader_lines]
        leader_line_ids    = {l.id for l in leader_lines}

        leader_employees       = employees_by_manager.get(leader.id, [])
        leader_employees_count = len(leader_employees)
        leader_emp_ids         = {e.id for e in leader_employees}

        leader_task_ids = set()
        for l in leader_lines:
            leader_task_ids.update(tasks_by_line.get(l.id, set()))
        leader_tasks_count = len(leader_task_ids)

        # ✅ Records sur les lignes du chef
        leader_records = []
        for r in records:
            if r.employee_id not in leader_emp_ids:
                continue
            if r.task_id not in leader_task_ids:
                continue
            for line in r.lines.all():
                if line.id in leader_line_ids:
                    leader_records.append(r)
                    break

        formations_count   = len(leader_records)
        unique_trainees    = len(set(
            r.employee_id for r in leader_records
        ))
        unique_tasks_count = len(set(
            r.task_id for r in leader_records
        ))

        leader_i = sum(
            1 for r in leader_records
            if r.level == TrainingLevel.IN_TRAINING
        )
        leader_l = sum(
            1 for r in leader_records
            if r.level == TrainingLevel.SUPERVISED
        )
        leader_u = sum(
            1 for r in leader_records
            if r.level == TrainingLevel.TRAINER
        )

        last_formation = (
            sorted(
                leader_records, key=lambda r: r.date, reverse=True
            )[0] if leader_records else None
        )
        last_formation_date = (
            last_formation.date if last_formation else None
        )

        this_month_count = sum(
            1 for r in leader_records
            if r.date >= current_month_start
        )
        last_month_count = sum(
            1 for r in leader_records
            if last_month_start <= r.date < current_month_start
        )

        evolution_pct = (
            round(
                (this_month_count - last_month_count) /
                last_month_count * 100, 1
            ) if last_month_count > 0
            else (100 if this_month_count > 0 else 0)
        )

        coverage_rate = round(
            (unique_trainees / leader_employees_count) * 100, 1
        ) if leader_employees_count > 0 else 0

        mastered_combinations = len(set(
            (r.employee_id, r.task_id) for r in leader_records
            if r.level in [
                TrainingLevel.SUPERVISED,
                TrainingLevel.TRAINER,
            ]
        ))
        total_possible      = (
            leader_employees_count * leader_tasks_count
        )
        mastery_rate_leader = min(
            100.0,
            round(
                mastered_combinations / total_possible * 100, 1
            ) if total_possible > 0 else 0
        )

        if leader_employees_count > 0 and leader_tasks_count > 0:
            emp_poly = []
            for emp in leader_employees:
                emp_mastered = len(set(
                    r.task_id for r in leader_records
                    if r.employee_id == emp.id
                    and r.level in [
                        TrainingLevel.SUPERVISED,
                        TrainingLevel.TRAINER,
                    ]
                ))
                emp_poly.append(emp_mastered)
            avg_mastered = (
                sum(emp_poly) / len(emp_poly) if emp_poly else 0
            )
            polyvalence_rate_leader = min(
                100.0,
                round(avg_mastered / leader_tasks_count * 100, 1)
            )
        else:
            polyvalence_rate_leader = 0

        avg_rate = (mastery_rate_leader + polyvalence_rate_leader) / 2
        if avg_rate >= 70:
            status, status_color = 'excellent', 'success'
        elif avg_rate >= 50:
            status, status_color = 'bon',       'info'
        elif avg_rate >= 30:
            status, status_color = 'attention', 'warning'
        else:
            status, status_color = 'critique',  'danger'

        leader_name = (
            f"{leader.first_name} {leader.last_name}".strip()
            or leader.username
        )

        team_leader_stats.append({
            'id':               leader.id,
            'name':             leader_name,
            'username':         leader.username,
            'email':            getattr(leader, 'email', ''),
            'lines':            ", ".join(leader_lines_codes),
            'lines_list':       leader_lines_codes,
            'lines_count':      len(leader_lines),
            'employees_count':  leader_employees_count,
            'tasks_count':      leader_tasks_count,
            'formations_total': formations_count,
            'unique_trainees':  unique_trainees,
            'unique_tasks':     unique_tasks_count,
            'i_count':          leader_i,
            'l_count':          leader_l,
            'u_count':          leader_u,
            'last_formation_date': last_formation_date,
            'this_month':       this_month_count,
            'last_month':       last_month_count,
            'evolution_pct':    evolution_pct,
            'coverage_rate':    min(100.0, coverage_rate),
            'mastery_rate':     mastery_rate_leader,
            'polyvalence_rate': polyvalence_rate_leader,
            'status':           status,
            'status_color':     status_color,
        })

    team_leader_stats = sorted(
        team_leader_stats,
        key=lambda x: x['mastery_rate'],
        reverse=True,
    )

    # ══════════════════════════════════════════════════════════════════
    # 12. STATS CHEFS D'ÉQUIPE PAR LIGNE
    # ══════════════════════════════════════════════════════════════════

    team_leader_stats_by_line = []

    for line in lines:
        line_leaders = list(line.team_leaders.all())
        if not line_leaders:
            continue

        leaders_names = [
            f"{l.first_name} {l.last_name}".strip() or l.username
            for l in line_leaders
        ]

        # ✅ Employés visibles sur cette ligne
        line_emps            = visible_employees_by_line.get(line.id, [])
        line_employees_count = len(line_emps)
        line_emp_ids         = {e.id for e in line_emps}

        line_task_ids   = tasks_by_line.get(line.id, set())
        line_tasks_count = len(line_task_ids)

        # ✅ Records via ManyToMany
        line_records = [
            r for r in records_by_line_visible.get(line.id, [])
            if r.task_id in line_task_ids
        ]

        formations_count   = len(line_records)
        unique_trainees    = len(set(
            r.employee_id for r in line_records
        ))
        unique_tasks_count = len(set(
            r.task_id for r in line_records
        ))

        line_i = sum(
            1 for r in line_records
            if r.level == TrainingLevel.IN_TRAINING
        )
        line_l = sum(
            1 for r in line_records
            if r.level == TrainingLevel.SUPERVISED
        )
        line_u = sum(
            1 for r in line_records
            if r.level == TrainingLevel.TRAINER
        )

        last_formation = (
            sorted(
                line_records, key=lambda r: r.date, reverse=True
            )[0] if line_records else None
        )
        last_formation_date = (
            last_formation.date if last_formation else None
        )

        this_month_count = sum(
            1 for r in line_records
            if r.date >= current_month_start
        )
        last_month_count = sum(
            1 for r in line_records
            if last_month_start <= r.date < current_month_start
        )

        evolution_pct = (
            round(
                (this_month_count - last_month_count) /
                last_month_count * 100, 1
            ) if last_month_count > 0
            else (100 if this_month_count > 0 else 0)
        )

        coverage_rate = min(
            100.0,
            round(
                unique_trainees / line_employees_count * 100, 1
            ) if line_employees_count > 0 else 0
        )

        mastered_combinations = len(set(
            (r.employee_id, r.task_id) for r in line_records
            if r.level in [
                TrainingLevel.SUPERVISED,
                TrainingLevel.TRAINER,
            ]
        ))
        total_possible      = (
            line_employees_count * line_tasks_count
        )
        mastery_rate_line   = min(
            100.0,
            round(
                mastered_combinations / total_possible * 100, 1
            ) if total_possible > 0 else 0
        )

        if line_employees_count > 0 and line_tasks_count > 0:
            emp_poly = []
            for emp in line_emps:
                emp_mastered = len(set(
                    r.task_id for r in line_records
                    if r.employee_id == emp.id
                    and r.level in [
                        TrainingLevel.SUPERVISED,
                        TrainingLevel.TRAINER,
                    ]
                ))
                emp_poly.append(emp_mastered)
            avg_mastered = (
                sum(emp_poly) / len(emp_poly) if emp_poly else 0
            )
            polyvalence_rate_line = min(
                100.0,
                round(avg_mastered / line_tasks_count * 100, 1)
            )
        else:
            polyvalence_rate_line = 0

        avg_rate = (mastery_rate_line + polyvalence_rate_line) / 2
        if avg_rate >= 70:
            status, status_color = 'excellent', 'success'
        elif avg_rate >= 50:
            status, status_color = 'bon',       'info'
        elif avg_rate >= 30:
            status, status_color = 'attention', 'warning'
        else:
            status, status_color = 'critique',  'danger'

        team_leader_stats_by_line.append({
            'line_id':          line.id,
            'line_code':        line.code,
            'line_name':        line.name,
            'leaders':          line_leaders,
            'leaders_names':    leaders_names,
            'leaders_str':      " & ".join(leaders_names),
            'leaders_count':    len(line_leaders),
            'employees_count':  line_employees_count,
            'tasks_count':      line_tasks_count,
            'formations_total': formations_count,
            'unique_trainees':  unique_trainees,
            'unique_tasks':     unique_tasks_count,
            'i_count':          line_i,
            'l_count':          line_l,
            'u_count':          line_u,
            'last_formation_date': last_formation_date,
            'this_month':       this_month_count,
            'last_month':       last_month_count,
            'evolution_pct':    evolution_pct,
            'coverage_rate':    coverage_rate,
            'mastery_rate':     mastery_rate_line,
            'polyvalence_rate': polyvalence_rate_line,
            'status':           status,
            'status_color':     status_color,
        })

    team_leader_stats_by_line = sorted(
        team_leader_stats_by_line,
        key=lambda x: x['mastery_rate'],
        reverse=True,
    )

    team_leader_summary = {
        'total_leaders':    len(team_leader_stats),
        'total_formations': sum(
            t['formations_total'] for t in team_leader_stats
        ),
        'avg_formations_per_leader': round(
            sum(t['formations_total'] for t in team_leader_stats) /
            max(1, len(team_leader_stats)), 1
        ),
        'avg_employees_per_leader':  round(
            sum(t['employees_count'] for t in team_leader_stats) /
            max(1, len(team_leader_stats)), 1
        ),
        'total_this_month': sum(
            t['this_month'] for t in team_leader_stats
        ),
        'best_performer':   (
            team_leader_stats[0] if team_leader_stats else None
        ),
    }

    # ══════════════════════════════════════════════════════════════════
    # 13. DEMANDES D'APPROBATION
    # ══════════════════════════════════════════════════════════════════

    pending_requests = list(
        TrainingApprovalRequest.objects.filter(
            approved__isnull=True
        ).select_related('employee', 'task')
        .order_by('-date_requested')[:10]
    )

    pending_count = TrainingApprovalRequest.objects.filter(
        approved__isnull=True
    ).count()

    approved_this_month = TrainingApprovalRequest.objects.filter(
        approved=True,
        reviewed_at__gte=current_month_start,
    ).count()

    rejected_this_month = TrainingApprovalRequest.objects.filter(
        approved=False,
        reviewed_at__gte=current_month_start,
    ).count()

    total_requests_this_month = (
        approved_this_month + rejected_this_month + pending_count
    )

    # ══════════════════════════════════════════════════════════════════
    # 14. FORMATIONS RÉCENTES & ACTIVITÉ
    # ══════════════════════════════════════════════════════════════════

    recent_trainings = sorted(
        records, key=lambda r: r.date, reverse=True
    )[:20]

    daily_activity = []
    for i in range(7):
        day   = today - timedelta(days=i)
        count = sum(1 for r in records if r.date == day)
        daily_activity.append({
            'date':     day,
            'day_name': day.strftime('%A'),
            'count':    count,
        })

    # ══════════════════════════════════════════════════════════════════
    # 15. DONNÉES POUR GRAPHIQUES
    # ══════════════════════════════════════════════════════════════════

    radar_data = {
        'labels':      [l['code'] for l in line_stats[:8]],
        'polyvalence': [l['polyvalence_index'] for l in line_stats[:8]],
        'mastery':     [l['mastery_pct'] for l in line_stats[:8]],
        'coverage':    [
            min(
                100,
                l['total_records'] /
                max(1, l['employees'] * l['tasks']) * 100
            )
            for l in line_stats[:8]
        ],
    }

    pie_levels = {
        'labels': [
            'En formation (I)',
            'Supervisé (L)',
            'Formateur (U)',
        ],
        'data':   [
            level_counts['i_count'],
            level_counts['l_count'],
            level_counts['u_count'],
        ],
        'colors': ['#ffc107', '#17a2b8', '#28a745'],
    }

    bar_lines = {
        'labels':  [l['code'] for l in line_stats_sorted[:10]],
        'mastery': [l['mastery_pct'] for l in line_stats_sorted[:10]],
        'employees': [l['employees'] for l in line_stats_sorted[:10]],
    }

    # ══════════════════════════════════════════════════════════════════
    # 16. ALERTES FINALES
    # ══════════════════════════════════════════════════════════════════

    if len(critical_tasks) > 0:
        alerts.append({
            'type':    'danger',
            'icon':    'fas fa-exclamation-triangle',
            'title':   f"{len(critical_tasks)} tâche(s) critique(s)",
            'message': "Tâches sans formateur ou à risque élevé",
            'link':    '#tasks-section',
            'count':   len(critical_tasks),
        })

    if pending_count > 0:
        alerts.append({
            'type':    'warning',
            'icon':    'fas fa-clock',
            'title':   f"{pending_count} demande(s) en attente",
            'message': "Demandes d'approbation à traiter",
            'link':    'rh_training_approvals',
            'count':   pending_count,
        })

    if employees_without_training > 0:
        alerts.append({
            'type':    'info',
            'icon':    'fas fa-user-graduate',
            'title':   f"{employees_without_training} employé(s) "
                       f"sans formation",
            'message': "Aucune formation enregistrée",
            'link':    '#employees-section',
            'count':   employees_without_training,
        })

    if len(inactive_employees) > 0:
        alerts.append({
            'type':    'secondary',
            'icon':    'fas fa-user-clock',
            'title':   f"{len(inactive_employees)} employé(s) inactif(s)",
            'message': "Aucune formation depuis > 180 jours",
            'link':    '#inactive-section',
            'count':   len(inactive_employees),
        })

    if month_evolution > 0:
        alerts.append({
            'type':    'success',
            'icon':    'fas fa-chart-line',
            'title':   f"+{month_evolution} ce mois",
            'message': f"Progression de {month_evolution_pct}% "
                       f"vs mois dernier",
            'link':    '#evolution-section',
            'count':   month_evolution,
        })

    # ══════════════════════════════════════════════════════════════════
    # 17. SCORE DE SANTÉ
    # ══════════════════════════════════════════════════════════════════

    risk_score_value = 100 - (
        len(critical_tasks) / max(1, total_tasks) * 100
    )

    health_score = min(100, max(0, round(
        (mastery_rate        * 0.35) +
        (polyvalence_rate    * 0.25) +
        (global_coverage_rate * 0.25) +
        (risk_score_value    * 0.15),
        1,
    )))

    if health_score >= 80:
        health_status, health_color = 'Excellent', 'success'
    elif health_score >= 60:
        health_status, health_color = 'Bon',       'info'
    elif health_score >= 40:
        health_status, health_color = 'Moyen',     'warning'
    else:
        health_status, health_color = 'Critique',  'danger'

    # ══════════════════════════════════════════════════════════════════
    # 18. CONTEXTE FINAL
    # ══════════════════════════════════════════════════════════════════

    context = {
        # Métadonnées
        'last_update':    timezone.now(),
        'report_period':  (
            f"{start_month.strftime('%d %b %Y')} - "
            f"{today.strftime('%d %b %Y')}"
        ),

        # Requalification
        'requalified_employees':           requalified_employees,
        'requalification_stats':           requalification_stats,
        'requalification_results':         requalification_results,
        'threshold_days':                  threshold_days,
        'employees_to_requalify_global':   employees_to_requalify_global,
        'requalification_by_line':         requalification_by_line,
        'pending_requalification_requests': pending_requalification_requests,
        'pending_requalification_count':   pending_requalification_count,
        'recent_requalifications':         recent_requalifications,

        # Statistiques globales
        'total_employees':           total_employees,
        'total_tasks':               total_tasks,
        'total_records':             total_records,
        'total_lines':               total_lines,
        'unique_trainers':           unique_trainers,
        'employees_with_training':   employees_with_training,
        'employees_without_training': employees_without_training,
        'global_coverage_rate':      global_coverage_rate,
        'mastery_rate':              mastery_rate,
        'polyvalence_rate':          polyvalence_rate,

        # Score de santé
        'health_score':  health_score,
        'health_status': health_status,
        'health_color':  health_color,

        # Niveaux
        'level_counts':       level_counts,
        'level_distribution': level_distribution,

        # Évolution mensuelle
        'current_month_records': current_month_records,
        'last_month_records':    last_month_records_count,
        'month_evolution':       month_evolution,
        'month_evolution_pct':   month_evolution_pct,
        'this_week_records':     this_week_records,
        'last_30_days_records':  last_30_days_records,

        # JSON graphiques
        'months_labels_json':    json.dumps(labels),
        'series_total_json':     json.dumps(series_total),
        'series_i_json':         json.dumps(series_i),
        'series_l_json':         json.dumps(series_l),
        'series_u_json':         json.dumps(series_u),
        'series_cumulative_json': json.dumps(series_cumulative),
        'series_employees_json': json.dumps(series_employees),
        'radar_data_json':       json.dumps(radar_data),
        'pie_levels_json':       json.dumps(pie_levels),
        'bar_lines_json':        json.dumps(bar_lines),

        # Stats par ligne
        'line_stats':          line_stats_sorted,
        'avg_line_mastery':    avg_line_mastery,
        'avg_line_polyvalence': avg_line_polyvalence,

        # Analyse tâches
        'task_analysis':               task_analysis,
        'task_analysis_by_coverage':   task_analysis_by_coverage,
        'task_analysis_by_criticality': task_analysis_by_criticality,
        'task_analysis_by_trainers':   task_analysis_by_trainers,
        'task_analysis_by_not_trained': task_analysis_by_not_trained,
        'task_coverage_worst':         task_coverage_worst,
        'task_coverage_best':          task_coverage_best,
        'critical_tasks':              critical_tasks,
        'total_critical_tasks':        len(critical_tasks),
        'tasks_with_no_trainer':       tasks_with_no_trainer,
        'tasks_fully_covered':         tasks_fully_covered,
        'tasks_partially_covered':     tasks_partially_covered,
        'tasks_poorly_covered':        tasks_poorly_covered,
        'avg_employees_per_task':      avg_employees_per_task,
        'avg_trained_per_task':        avg_trained_per_task,
        'avg_not_trained_per_task':    avg_not_trained_per_task,
        'avg_coverage_pct':            avg_coverage_pct,
        'avg_mastery_pct':             avg_mastery_pct,
        'avg_trainers_per_task':       avg_trainers_per_task,
        'total_employees_not_trained_global':
            total_employees_not_trained_global,

        # Analyse employés
        'employee_analysis':           employee_analysis,
        'top_polyvalent':              top_polyvalent,
        'top_polyvalent_grouped':      top_polyvalent_grouped,
        'top_trainers':                top_trainers,
        'employees_needing_training':  employees_needing_training,
        'inactive_employees':          inactive_employees,

        # Stats chefs d'équipe
        'team_leader_stats':          team_leader_stats,
        'team_leader_stats_by_line':  team_leader_stats_by_line,
        'team_leader_summary':        team_leader_summary,

        # Demandes
        'pending_requests':            pending_requests,
        'pending_count':               pending_count,
        'approved_this_month':         approved_this_month,
        'rejected_this_month':         rejected_this_month,
        'total_requests_this_month':   total_requests_this_month,

        # Activité
        'recent_trainings': recent_trainings,
        'daily_activity':   daily_activity,

        # Alertes
        'alerts':       alerts,
        'total_alerts': len(alerts),
    }

    return render(request, 'matrix/rh_dashboard.html', context)


@login_required
def transfer_employee_view(request, employee_id=None):
    employee = get_object_or_404(Employee, id=employee_id) if employee_id else None

    if request.method == "POST":
        form = EmployeeTransferForm(request.POST)
        if form.is_valid():
            employee = form.cleaned_data['employee']
            new_line = form.cleaned_data['new_line']
            reason = form.cleaned_data.get('reason', '')
            
            # ✅ Récupérer le chef d'équipe sélectionné
            selected_team_leader_id = request.POST.get('selected_team_leader')

            old_line = employee.line
            old_manager = employee.manager

            if old_line != new_line:
                employee._line_transfer_info = {
                    'from_line': old_line,
                    'to_line': new_line,
                    'transfer_date': timezone.now().date(),
                    'reason': reason,
                    'transferred_by': request.user
                }

            employee.line = new_line
            
            # ✅ Gestion du chef d'équipe selon le choix
            if hasattr(new_line, 'team_leaders'):
                team_leaders = new_line.team_leaders.all()
                
                if selected_team_leader_id:
                    # Chef d'équipe spécifiquement sélectionné par l'utilisateur
                    try:
                        from django.contrib.auth import get_user_model
                        User = get_user_model()
                        selected_manager = User.objects.get(id=selected_team_leader_id)
                        # Vérifier que ce manager appartient bien à cette ligne
                        if selected_manager in team_leaders:
                            employee.manager = selected_manager
                        else:
                            employee.manager = team_leaders.first()
                    except User.DoesNotExist:
                        employee.manager = team_leaders.first()
                else:
                    # Un seul chef ou aucun - assignation automatique
                    employee.manager = team_leaders.first()
            else:
                employee.manager = None
            
            employee.save()

            new_tasks = Task.objects.filter(lines=new_line)
            MatrixEntry.objects.filter(employee=employee).exclude(task__in=new_tasks).delete()

            for task in new_tasks:
                MatrixEntry.objects.get_or_create(
                    employee=employee,
                    task=task,
                    defaults={
                        'level': TrainingLevel.IN_TRAINING,
                        'transfer_date': timezone.now().date()
                    }
                )

            messages.success(
                request,
                f"L'employé {employee} a été transféré de '{old_line or 'Aucune ligne'}' vers '{new_line}'. "
                f"Il est maintenant sous la responsabilité de '{employee.manager or 'Aucun manager'}'."
            )
            return redirect("transfer_employee")
    else:
        form = EmployeeTransferForm(initial={'employee': employee} if employee else {})

    employees_data = []
    employees_queryset = Employee.objects.select_related('line', 'manager').all()
    
    for emp in employees_queryset:
        employees_data.append({
            'id': emp.id,
            'matricule': emp.matricule,
            'name': f"{emp.first_name} {emp.last_name}",
            'line': emp.line.name if emp.line else 'Non assigné',
            'lineId': emp.line.id if emp.line else None,
            'manager': emp.manager.get_full_name() if emp.manager else 'Aucun',
        })

    # ✅ Récupérer toutes les lignes avec leurs chefs d'équipe
    lines_data = []
    for line in ProductionLine.objects.prefetch_related('team_leaders').all():
        team_leaders_list = []
        for tl in line.team_leaders.all():
            team_leaders_list.append({
                'id': tl.id,
                'name': tl.get_full_name() if hasattr(tl, 'get_full_name') else f"{tl.first_name} {tl.last_name}",
                'matricule': getattr(tl, 'matricule', getattr(tl, 'username', '')),
            })
        
        lines_data.append({
            'id': line.id,
            'name': line.name,
            'team_leaders': team_leaders_list,
            'team_leaders_count': len(team_leaders_list)
        })

    return render(request, "matrix/transfer_employee.html", {
        "form": form,
        "employee": employee,
        "employees_json": json.dumps(employees_data),
        "lines_json": json.dumps(lines_data),  # ✅ Nouveau
    })


# ── API : Recherche par matricule ─────────────────────────────────────
@login_required
def get_employee_by_matricule(request):
    """
    ✅ Cherche dans PendingEmployee (pas Employee).
    Retourne les infos pour remplir le formulaire chef d'équipe.
    """
    matricule = request.GET.get('matricule', '').strip().upper()

    if not matricule:
        return JsonResponse({
            'found':   False,
            'message': 'Matricule vide.'
        })

    # ✅ Chercher dans PendingEmployee
    try:
        pending = PendingEmployee.objects.get(matricule=matricule)

        return JsonResponse({
            'found':      True,
            'matricule':  pending.matricule,
            'first_name': pending.first_name,
            'last_name':  pending.last_name,
            'hire_date':  (
                pending.hire_date.strftime('%Y-%m-%d')
                if pending.hire_date else ''
            ),
            'message': (
                f"✅ {pending.first_name} {pending.last_name} "
                f"trouvé — pré-enregistré par RH."
            ),
            'source': 'pending',
        })

    except PendingEmployee.DoesNotExist:
        pass

    # ✅ Vérifier si déjà un opérateur actif
    try:
        emp = Employee.objects.get(matricule=matricule)

        # Déjà assigné à ce chef
        if emp.manager == request.user:
            return JsonResponse({
                'found':   False,
                'message': (
                    f"❌ '{matricule}' est déjà dans votre équipe."
                ),
            })

        # Assigné à un autre chef
        if emp.manager:
            return JsonResponse({
                'found':   False,
                'message': (
                    f"❌ '{matricule}' est déjà assigné à "
                    f"{emp.manager.get_full_name() or emp.manager.username}."
                ),
            })

        # Existe sans manager (cas rare)
        return JsonResponse({
            'found':   False,
            'message': (
                f"❌ '{matricule}' existe mais n'est pas "
                f"pré-enregistré par RH. "
                f"Contactez le service RH."
            ),
        })

    except Employee.DoesNotExist:
        pass

    # ✅ Introuvable nulle part
    return JsonResponse({
        'found':   False,
        'message': (
            f"❌ Aucun employé trouvé avec le matricule '{matricule}'. "
            f"Contactez RH pour l'enregistrer d'abord."
        ),
    })


# ── RH : Pré-enregistrement ───────────────────────────────────────────
@login_required
def rh_employee_register(request):
    if not request.user.is_superuser:
        messages.error(request, "❌ Accès réservé au service RH.")
        return redirect('matrix_view')

    form       = EmployeeRHRegisterForm()
    excel_form = EmployeeExcelImportForm()

    if request.method == "POST":
        form_type = request.POST.get('form_type', '')

        # ── Import Excel ───────────────────────────────────────────
        if form_type == 'excel':
            excel_form = EmployeeExcelImportForm(
                request.POST, request.FILES
            )

            if excel_form.is_valid():
                try:
                    employees_data, parse_errors = excel_form.parse_excel()
                except Exception as e:
                    messages.error(request, f"❌ Erreur : {e}")
                    return redirect('rh_employee_register')

                created, skipped = [], []

                for emp_data in employees_data:

                    # ✅ Vérifier dans PendingEmployee ET Employee
                    already_pending  = PendingEmployee.objects.filter(
                        matricule=emp_data['matricule']
                    ).exists()
                    already_employee = Employee.objects.filter(
                        matricule=emp_data['matricule']
                    ).exists()

                    if already_pending or already_employee:
                        skipped.append(
                            f"{emp_data['matricule']} "
                            f"({emp_data['first_name']} "
                            f"{emp_data['last_name']})"
                        )
                        continue

                    # ✅ Stocker dans PendingEmployee (pas Employee)
                    PendingEmployee.objects.create(
                        matricule=emp_data['matricule'],
                        first_name=emp_data['first_name'],
                        last_name=emp_data['last_name'],
                        hire_date=emp_data['hire_date'],
                        created_by=request.user,
                    )
                    created.append(
                        f"{emp_data['matricule']} — "
                        f"{emp_data['first_name']} "
                        f"{emp_data['last_name']}"
                    )

                if created:
                    messages.success(
                        request,
                        f"✅ {len(created)} pré-enregistré(s) "
                        f"(en attente d'assignation) :\n" +
                        "\n".join(f"  • {c}" for c in created)
                    )
                if skipped:
                    messages.warning(
                        request,
                        f"⚠️ {len(skipped)} ignoré(s) "
                        f"(déjà existant) :\n" +
                        "\n".join(f"  • {s}" for s in skipped)
                    )
                if parse_errors:
                    messages.error(
                        request,
                        f"❌ {len(parse_errors)} erreur(s) :\n" +
                        "\n".join(f"  • {e}" for e in parse_errors)
                    )
                if not created and not skipped and not parse_errors:
                    messages.warning(request, "⚠️ Fichier vide.")

                return redirect('rh_employee_register')
            else:
                messages.error(request, "❌ Erreur fichier.")

        # ── Formulaire manuel ──────────────────────────────────────
        else:
            form = EmployeeRHRegisterForm(request.POST)
            if form.is_valid():
                # ✅ Stocker dans PendingEmployee (pas Employee)
                pending = PendingEmployee.objects.create(
                    matricule=form.cleaned_data['matricule'],
                    first_name=form.cleaned_data['first_name'],
                    last_name=form.cleaned_data['last_name'],
                    hire_date=form.cleaned_data.get('hire_date'),
                    created_by=request.user,
                )
                messages.success(
                    request,
                    f"✅ {pending.first_name} {pending.last_name} "
                    f"({pending.matricule}) pré-enregistré. "
                    f"En attente qu'un chef d'équipe l'ajoute."
                )
                return redirect('rh_employee_register')
            else:
                messages.error(request, "❌ Enregistrement impossible.")

    # ✅ Afficher les PendingEmployee (pas les Employee)
    recent_employees = PendingEmployee.objects.order_by('-created_at')[:10]

    return render(request, 'matrix/rh_employee_register.html', {
        'form':             form,
        'excel_form':       excel_form,
        'recent_employees': recent_employees,
        'title':            'Pré-enregistrer un employé',
        'submit_text':      'Enregistrer',
    })

# ── API : Télécharger le modèle Excel ────────────────────────────────
@login_required
def download_excel_template(request):
    """
    Télécharge un fichier Excel modèle pour l'import des employés.
    """
    if not request.user.is_superuser:
        return redirect('matrix_view')

    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from django.http import HttpResponse

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Employés"

    # ── En-têtes ──────────────────────────────────────────────────
    headers = ['matricule', 'prenom', 'nom', 'date_embauche']
    header_fill = PatternFill(
        start_color="1A237E",
        end_color="1A237E",
        fill_type="solid"
    )
    header_font = Font(
        color="FFFFFF", bold=True, size=11
    )

    for col, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill      = header_fill
        cell.font      = header_font
        cell.alignment = Alignment(horizontal='center')

    # ── Exemples ──────────────────────────────────────────────────
    exemples = [
        ['EMP001', 'Ahmed',   'Benali',  '01/03/2024'],
        ['EMP002', 'Karim',   'Meziani', '15/06/2023'],
        ['EMP003', 'Fatima',  'Ouali',   ''],
    ]
    for row_num, row_data in enumerate(exemples, start=2):
        for col, val in enumerate(row_data, start=1):
            ws.cell(row=row_num, column=col, value=val)

    # ── Largeur des colonnes ──────────────────────────────────────
    ws.column_dimensions['A'].width = 15
    ws.column_dimensions['B'].width = 20
    ws.column_dimensions['C'].width = 20
    ws.column_dimensions['D'].width = 18

    # ── Note ──────────────────────────────────────────────────────
    ws.cell(row=6, column=1, value="⚠️ Instructions :").font = Font(bold=True)
    ws.cell(row=7, column=1, value="• La colonne 'date_embauche' est optionnelle (format JJ/MM/AAAA)")
    ws.cell(row=8, column=1, value="• Le matricule doit être unique")
    ws.cell(row=9, column=1, value="• Supprimer les lignes d'exemple avant d'importer")

    # ── Réponse HTTP ──────────────────────────────────────────────
    response = HttpResponse(
        content_type=(
            'application/vnd.openxmlformats-officedocument'
            '.spreadsheetml.sheet'
        )
    )
    response['Content-Disposition'] = (
        'attachment; filename="modele_import_employes.xlsx"'
    )
    wb.save(response)
    return response


# ── RH : Création complète ────────────────────────────────────────────
@login_required
def rh_employee_create(request):
    """
    ✅ RH crée un employé complet avec choix du chef d'équipe.
    """
    if not request.user.is_superuser:
        messages.error(request, "❌ Accès réservé au service RH.")
        return redirect('matrix_view')

    lines_data = []
    for line in ProductionLine.objects.prefetch_related(
        'team_leaders'
    ).all().order_by('code'):
        leaders = [
            {
                'id':       leader.id,
                'name':     leader.get_full_name() or leader.username,
                'username': leader.username,
            }
            for leader in line.team_leaders.all()
        ]
        lines_data.append({
            'id':            line.id,
            'code':          line.code,
            'name':          line.name,
            'team_leaders':  leaders,
            'leaders_count': len(leaders),
        })

    if request.method == "POST":
        form = EmployeeFormRH(request.POST)

        if form.is_valid():
            employee         = form.save(commit=False)
            employee.manager = form.cleaned_data.get('manager')
            employee.save()

            manager_name = (
                employee.manager.get_full_name()
                if employee.manager else "Non assigné"
            )
            line_code = (
                employee.line.code if employee.line else "N/A"
            )

            messages.success(
                request,
                f"✅ Employé créé : "
                f"{employee.first_name} {employee.last_name} "
                f"({employee.matricule}) | "
                f"Ligne : {line_code} | "
                f"Responsable : {manager_name}."
            )
            return redirect('rh_employee_list')
        else:
            messages.error(
                request,
                "❌ Création impossible. "
                "Merci de corriger les erreurs."
            )
    else:
        form = EmployeeFormRH()

    return render(
        request,
        'matrix/rh_employee_form.html',
        {
            'form':        form,
            'title':       'Créer un employé',
            'submit_text': 'Créer l\'employé',
            'lines_json':  json.dumps(lines_data),
        }
    )



@login_required
def rh_employee_update(request, pk):
    """Modifier un employé existant"""
    
    employee = get_object_or_404(Employee, pk=pk)
    
    # ✅ Préparer les données des lignes pour le JavaScript
    lines_data = []
    for line in ProductionLine.objects.prefetch_related('team_leaders').all().order_by('code'):
        leaders = []
        for leader in line.team_leaders.all():
            leaders.append({
                'id': leader.id,
                'name': leader.get_full_name() or leader.username,
                'username': leader.username,
            })
        lines_data.append({
            'id': line.id,
            'code': line.code,
            'name': line.name,
            'team_leaders': leaders,
            'leaders_count': len(leaders),
        })
    
    if request.method == "POST":
        form = EmployeeFormRH(request.POST, instance=employee)
        
        if form.is_valid():
            employee = form.save(commit=False)
            employee.manager = form.cleaned_data.get('manager')
            employee.save()
            
            messages.success(
                request, 
                f"✅ Les informations de {employee.first_name} {employee.last_name} ont été mises à jour."
            )
            return redirect("employee_list_rh")
    else:
        form = EmployeeFormRH(instance=employee)
    
    # ✅ ID du manager actuel pour pré-sélection
    current_manager_id = employee.manager.id if employee.manager else None
    
    return render(request, "matrix/rh_employee_form.html", {
        "form": form,
        "employee": employee,
        "title": "Modifier un employé",
        "submit_text": "Enregistrer les modifications",
        "lines_json": json.dumps(lines_data),
        "current_manager_id": current_manager_id,
    })


@login_required
def rh_employee_delete(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    
    employee_name = f"{employee.first_name} {employee.last_name}"
    employee_matricule = employee.matricule
    employee_line = employee.line.name if employee.line else "N/A"
    
    employee.delete()
    
    messages.success(
        request, 
        f"L'employé {employee_name} (Matricule: {employee_matricule}) a été supprimé de la ligne {employee_line}."
    )
    return redirect("employee_list_rh")


import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from django.http import HttpResponse
from django.utils import timezone  # ✅ Utiliser Django timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from io import BytesIO
# ❌ SUPPRIMER : import datetime


@login_required
def export_dashboard_excel(request):
    """Export complet du dashboard en Excel"""
    
    wb = openpyxl.Workbook()
    
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1E40AF", end_color="1E40AF", fill_type="solid")
    alt_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    border = Border(
        left=Side(style='thin', color='E2E8F0'),
        right=Side(style='thin', color='E2E8F0'),
        top=Side(style='thin', color='E2E8F0'),
        bottom=Side(style='thin', color='E2E8F0')
    )
    
    ws1 = wb.active
    ws1.title = "Résumé Global"
    
    ws1['A1'] = "DASHBOARD RH - RAPPORT GLOBAL"
    ws1['A1'].font = Font(bold=True, size=16, color="1E40AF")
    ws1.merge_cells('A1:F1')
    
    # ✅ CORRIGÉ : timezone.now() au lieu de datetime.datetime.now()
    ws1['A2'] = f"Généré le: {timezone.now().strftime('%d/%m/%Y %H:%M')}"
    ws1['A2'].font = Font(italic=True, color="64748B")
    
    kpis = [
        ("Indicateur", "Valeur"),
        ("Total Employés", Employee.objects.count()),
        ("Total Tâches", Task.objects.count()),
        ("Total Formations", TrainingRecord.objects.count()),
        ("Formations Niveau I", TrainingRecord.objects.filter(level=TrainingLevel.IN_TRAINING).count()),
        ("Formations Niveau L", TrainingRecord.objects.filter(level=TrainingLevel.SUPERVISED).count()),
        ("Formations Niveau U", TrainingRecord.objects.filter(level=TrainingLevel.TRAINER).count()),
        ("Formateurs Uniques", TrainingRecord.objects.filter(level=TrainingLevel.TRAINER).values('employee').distinct().count()),
        ("Lignes de Production", ProductionLine.objects.count()),
    ]
    
    for row_idx, (label, value) in enumerate(kpis, start=4):
        ws1.cell(row=row_idx, column=1, value=label)
        ws1.cell(row=row_idx, column=2, value=value)
        if row_idx == 4:
            ws1.cell(row=row_idx, column=1).font = header_font
            ws1.cell(row=row_idx, column=1).fill = header_fill
            ws1.cell(row=row_idx, column=2).font = header_font
            ws1.cell(row=row_idx, column=2).fill = header_fill
    
    ws2 = wb.create_sheet("Analyse par Ligne")
    
    headers = ["Code", "Nom", "Employés", "Tâches", "Niveau I", "Niveau L", "Niveau U", "Formateurs", "Maîtrise %"]
    for col, header in enumerate(headers, 1):
        cell = ws2.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center')
    
    row = 2
    for line in ProductionLine.objects.all():
        emp_count = Employee.objects.filter(line=line).count()
        tasks = Task.objects.filter(lines=line).distinct()
        task_count = tasks.count()
        
        line_emps = Employee.objects.filter(line=line)
        line_records = TrainingRecord.objects.filter(employee__in=line_emps, task__in=tasks)
        
        i_count = line_records.filter(level=TrainingLevel.IN_TRAINING).count()
        l_count = line_records.filter(level=TrainingLevel.SUPERVISED).count()
        u_count = line_records.filter(level=TrainingLevel.TRAINER).count()
        trainers = line_records.filter(level=TrainingLevel.TRAINER).values('employee').distinct().count()
        
        mastery = round((l_count + u_count) / max(1, emp_count * task_count) * 100, 1)
        
        ws2.cell(row=row, column=1, value=line.code)
        ws2.cell(row=row, column=2, value=line.name)
        ws2.cell(row=row, column=3, value=emp_count)
        ws2.cell(row=row, column=4, value=task_count)
        ws2.cell(row=row, column=5, value=i_count)
        ws2.cell(row=row, column=6, value=l_count)
        ws2.cell(row=row, column=7, value=u_count)
        ws2.cell(row=row, column=8, value=trainers)
        ws2.cell(row=row, column=9, value=f"{mastery}%")
        
        if row % 2 == 0:
            for col in range(1, 10):
                ws2.cell(row=row, column=col).fill = alt_fill
        row += 1
    
    ws3 = wb.create_sheet("Analyse des Tâches")
    
    headers = ["Code", "Titre", "Lignes", "Niveau I", "Niveau L", "Niveau U", "Total Formés", "Couverture %", "Statut"]
    for col, header in enumerate(headers, 1):
        cell = ws3.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
    
    total_employees = Employee.objects.count()
    row = 2
    for task in Task.objects.all().order_by('code'):
        records = TrainingRecord.objects.filter(task=task)
        i_count = records.filter(level=TrainingLevel.IN_TRAINING).values('employee').distinct().count()
        l_count = records.filter(level=TrainingLevel.SUPERVISED).values('employee').distinct().count()
        u_count = records.filter(level=TrainingLevel.TRAINER).values('employee').distinct().count()
        total_trained = records.values('employee').distinct().count()
        coverage = round((total_trained / total_employees) * 100, 1) if total_employees else 0
        
        status = "OK" if u_count >= 2 else ("RISQUE" if u_count == 1 else "CRITIQUE")
        
        ws3.cell(row=row, column=1, value=task.code)
        ws3.cell(row=row, column=2, value=task.title)
        ws3.cell(row=row, column=3, value=", ".join(task.lines.values_list('code', flat=True)))
        ws3.cell(row=row, column=4, value=i_count)
        ws3.cell(row=row, column=5, value=l_count)
        ws3.cell(row=row, column=6, value=u_count)
        ws3.cell(row=row, column=7, value=total_trained)
        ws3.cell(row=row, column=8, value=f"{coverage}%")
        ws3.cell(row=row, column=9, value=status)
        
        status_cell = ws3.cell(row=row, column=9)
        if status == "CRITIQUE":
            status_cell.fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
            status_cell.font = Font(color="DC2626", bold=True)
        elif status == "RISQUE":
            status_cell.fill = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
            status_cell.font = Font(color="D97706", bold=True)
        else:
            status_cell.fill = PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid")
            status_cell.font = Font(color="059669", bold=True)
        
        row += 1
    
    ws4 = wb.create_sheet("Employés")
    
    headers = ["Matricule", "Nom", "Prénom", "Ligne", "Manager", "Tâches I", "Tâches L", "Tâches U", "Total", "Progression %"]
    for col, header in enumerate(headers, 1):
        cell = ws4.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
    
    row = 2
    for emp in Employee.objects.select_related('line', 'manager').all():
        records = TrainingRecord.objects.filter(employee=emp)
        i_count = records.filter(level=TrainingLevel.IN_TRAINING).values('task').distinct().count()
        l_count = records.filter(level=TrainingLevel.SUPERVISED).values('task').distinct().count()
        u_count = records.filter(level=TrainingLevel.TRAINER).values('task').distinct().count()
        
        line_tasks = Task.objects.filter(lines=emp.line).count() if emp.line else 1
        progress = round(((i_count * 0.25 + l_count * 0.75 + u_count) / line_tasks) * 100, 1)
        progress = min(100, progress)
        
        ws4.cell(row=row, column=1, value=emp.matricule)
        ws4.cell(row=row, column=2, value=emp.last_name)
        ws4.cell(row=row, column=3, value=emp.first_name)
        ws4.cell(row=row, column=4, value=emp.line.code if emp.line else "N/A")
        ws4.cell(row=row, column=5, value=f"{emp.manager.first_name} {emp.manager.last_name}" if emp.manager else "N/A")
        ws4.cell(row=row, column=6, value=i_count)
        ws4.cell(row=row, column=7, value=l_count)
        ws4.cell(row=row, column=8, value=u_count)
        ws4.cell(row=row, column=9, value=i_count + l_count + u_count)
        ws4.cell(row=row, column=10, value=f"{progress}%")
        
        row += 1
    
    for ws in [ws1, ws2, ws3, ws4]:
        for column in ws.columns:
            max_length = 0
            column_letter = get_column_letter(column[0].column)
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = min(50, max(12, max_length + 2))
            ws.column_dimensions[column_letter].width = adjusted_width
    
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    # ✅ CORRIGÉ
    response['Content-Disposition'] = f'attachment; filename="dashboard_rh_{timezone.now().strftime("%Y%m%d_%H%M")}.xlsx"'
    
    wb.save(response)
    return response


@login_required
def export_dashboard_pdf(request):
    """Export du dashboard en PDF"""
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), topMargin=1*cm, bottomMargin=1*cm)
    
    elements = []
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=20,
        textColor=colors.HexColor("#1E40AF"),
        spaceAfter=20
    )
    
    subtitle_style = ParagraphStyle(
        'CustomSubtitle',
        parent=styles['Heading2'],
        fontSize=14,
        textColor=colors.HexColor("#334155"),
        spaceBefore=15,
        spaceAfter=10
    )
    
    # ✅ CORRIGÉ
    elements.append(Paragraph("DASHBOARD RH - RAPPORT DE SYNTHÈSE", title_style))
    elements.append(Paragraph(f"Généré le {timezone.now().strftime('%d/%m/%Y à %H:%M')}", styles['Normal']))
    elements.append(Spacer(1, 20))
    
    elements.append(Paragraph("📊 Indicateurs Clés", subtitle_style))
    
    kpi_data = [
        ["Indicateur", "Valeur", "Indicateur", "Valeur"],
        ["Total Employés", str(Employee.objects.count()), "Total Tâches", str(Task.objects.count())],
        ["Total Formations", str(TrainingRecord.objects.count()), "Lignes", str(ProductionLine.objects.count())],
        ["Niveau I", str(TrainingRecord.objects.filter(level=TrainingLevel.IN_TRAINING).count()),
         "Niveau L", str(TrainingRecord.objects.filter(level=TrainingLevel.SUPERVISED).count())],
        ["Niveau U", str(TrainingRecord.objects.filter(level=TrainingLevel.TRAINER).count()),
         "Formateurs", str(TrainingRecord.objects.filter(level=TrainingLevel.TRAINER).values('employee').distinct().count())],
    ]
    
    kpi_table = Table(kpi_data, colWidths=[4*cm, 3*cm, 4*cm, 3*cm])
    kpi_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1E40AF")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor("#F8FAFC")),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
    ]))
    elements.append(kpi_table)
    elements.append(Spacer(1, 20))
    
    elements.append(Paragraph("🏭 Performance par Ligne", subtitle_style))
    
    line_data = [["Code", "Nom", "Employés", "Tâches", "I", "L", "U"]]
    for line in ProductionLine.objects.all()[:10]:
        emp_count = Employee.objects.filter(line=line).count()
        tasks = Task.objects.filter(lines=line).distinct()
        line_emps = Employee.objects.filter(line=line)
        line_records = TrainingRecord.objects.filter(employee__in=line_emps, task__in=tasks)
        
        line_data.append([
            line.code,
            line.name[:20],
            str(emp_count),
            str(tasks.count()),
            str(line_records.filter(level=TrainingLevel.IN_TRAINING).count()),
            str(line_records.filter(level=TrainingLevel.SUPERVISED).count()),
            str(line_records.filter(level=TrainingLevel.TRAINER).count()),
        ])
    
    line_table = Table(line_data, colWidths=[2*cm, 5*cm, 2*cm, 2*cm, 1.5*cm, 1.5*cm, 1.5*cm])
    line_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1E40AF")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
    ]))
    elements.append(line_table)
    
    doc.build(elements)
    
    buffer.seek(0)
    response = HttpResponse(buffer, content_type='application/pdf')
    # ✅ CORRIGÉ
    response['Content-Disposition'] = f'attachment; filename="dashboard_rh_{timezone.now().strftime("%Y%m%d_%H%M")}.pdf"'
    
    return response




# Ajouter à views.py

from datetime import timedelta
from django.db.models import Max, F
from .models import RequalificationRecord

# ═══════════════════════════════════════════════════════════════════════════════
# VUES REQUALIFICATION (188 JOURS)
# ═══════════════════════════════════════════════════════════════════════════════

@login_required
def employees_to_requalify(request):
    return _requalification_form_page(request)


def _requalification_form_page(request, form=None):
    """
    ✅ Affiche les employés à requalifier après 188 jours.
    Prend en compte les visible_lines (pas seulement la ligne principale).
    """
    user      = request.user
    today     = timezone.now().date()
    threshold_days = 188
    threshold_date = today - timedelta(days=threshold_days)

    is_rh = user.is_superuser or user.groups.filter(name="RH").exists()

    # ══════════════════════════════════════════════════════════════════
    # NOTIFICATIONS : Décisions RH pour ce chef d'équipe
    # ══════════════════════════════════════════════════════════════════
    my_decided = RequalificationApprovalRequest.objects.filter(
        requested_by=user,
        approved__isnull=False,
        notification_seen=False
    ).select_related('employee', 'task')

    for req in (my_decided if form is None else []):
        if req.approved:
            task_info  = f" - {req.task.title}" if req.task else ""
            level_info = (
                f" (Niveau: {req.get_requested_new_level_display()})"
                if req.task else ""
            )
            messages.success(
                request,
                f"✅ Requalification APPROUVÉE : "
                f"{req.employee.first_name} {req.employee.last_name}"
                f"{task_info} ({req.get_result_display()}){level_info}"
            )
        else:
            task_info = f" - {req.task.title}" if req.task else ""
            reason    = (
                f" — Motif: {req.decision_notes}"
                if req.decision_notes else ""
            )
            messages.warning(
                request,
                f"❌ Requalification REFUSÉE : "
                f"{req.employee.first_name} {req.employee.last_name}"
                f"{task_info}.{reason}"
            )
        req.notification_seen = True
        req.save()

    # ══════════════════════════════════════════════════════════════════
    # DEMANDES EN ATTENTE pour ce chef
    # ══════════════════════════════════════════════════════════════════
    pending_requests = RequalificationApprovalRequest.objects.filter(
        requested_by=user,
        approved__isnull=True
    ).select_related('employee', 'task', 'line').order_by('-date_requested')

    # ══════════════════════════════════════════════════════════════════
    # LIGNES selon le rôle
    # ══════════════════════════════════════════════════════════════════
    if is_rh:
        all_lines = ProductionLine.objects.all().order_by('code')
    else:
        all_lines = ProductionLine.objects.filter(
            team_leaders=user
        ).order_by('code')

    # Filtre par ligne
    selected_line_id = request.GET.get('line_id')
    if selected_line_id:
        try:
            selected_line_id = int(selected_line_id)
            filtered_lines = all_lines.filter(id=selected_line_id)
        except (ValueError, TypeError):
            filtered_lines = all_lines
            selected_line_id = None
    else:
        filtered_lines = all_lines

    # ══════════════════════════════════════════════════════════════════
    # ✅ EMPLOYÉS — via visible_lines (pas seulement line principale)
    # Un opérateur visible sur X, Y, Z doit apparaître dans les 3
    # ══════════════════════════════════════════════════════════════════
    if is_rh:
        employees = Employee.objects.filter(
            visible_lines__in=filtered_lines
        ).distinct().select_related(
            'line', 'manager'
        ).order_by('last_name', 'first_name')
    else:
        employees = Employee.objects.filter(
            manager=user,
            visible_lines__in=filtered_lines
        ).distinct().select_related(
            'line', 'manager'
        ).order_by('last_name', 'first_name')

    # ══════════════════════════════════════════════════════════════════
    # Pré-charger les demandes en attente
    # ══════════════════════════════════════════════════════════════════
    pending_pairs = set()
    for req in RequalificationApprovalRequest.objects.filter(
        approved__isnull=True
    ).select_related('line'):
        pending_pairs.add((req.employee_id, req.task_id, req.line_id))

    # ══════════════════════════════════════════════════════════════════
    # CONSTRUIRE LA LISTE PAR LIGNE
    # Pour chaque employé × chaque ligne visible → vérifier requalif
    # ══════════════════════════════════════════════════════════════════
    employees_to_requalify_list = []

    for emp in employees:

        # Lignes visibles de cet employé (filtrées par le contexte)
        emp_visible_lines = emp.visible_lines.filter(
            id__in=filtered_lines.values_list('id', flat=True)
        )

        for line in emp_visible_lines:

            # ── Dernière formation de l'employé SUR CETTE LIGNE ──────
            last_record = TrainingRecord.objects.filter(
                employee=emp,
                lines=line,              # ← filtre par ligne
            ).select_related('task').order_by('-date').first()

            if not last_record:
                continue

            if not last_record.date:
                continue

            # Vérifier seuil 188 jours
            if last_record.date > threshold_date:
                continue

            days_since = (today - last_record.date).days

            # ── Requalification récente SUR CETTE LIGNE ──────────────
            recent_requalif = RequalificationRecord.objects.filter(
                employee=emp,
                line=line,               # ← filtre par ligne
                requalification_date__gte=threshold_date
            ).filter(
                Q(task=last_record.task) | Q(task__isnull=True)
            ).exists()

            if recent_requalif:
                continue

            # ── Demande en attente SUR CETTE LIGNE ───────────────────
            has_pending = (
                emp.id,
                last_record.task_id,
                line.id
            ) in pending_pairs

            # ── Urgence ───────────────────────────────────────────────
            urgency = (
                'critical' if days_since > 250 else
                'warning'  if days_since > 200 else
                'info'
            )

            employees_to_requalify_list.append({
                'employee':           emp,
                'task':               last_record.task,
                'line':               line,          # ← ligne concernée
                'last_training_date': last_record.date,
                'current_level':      last_record.level,
                'current_level_display': last_record.get_level_display(),
                'days_since':         days_since,
                'days_overdue':       days_since - threshold_days,
                'urgency':            urgency,
                'has_pending_request': has_pending,
            })

    # Trier par urgence puis par nombre de jours
    employees_to_requalify_list.sort(
        key=lambda x: (
            {'critical': 0, 'warning': 1, 'info': 2}.get(x['urgency'], 3),
            -x['days_since']
        )
    )

    # ══════════════════════════════════════════════════════════════════
    # FORMULAIRE : employés avec leurs tâches PAR LIGNE
    # ══════════════════════════════════════════════════════════════════
    employees_for_form = []

    for emp in employees:
        emp_visible_lines = emp.visible_lines.filter(
            id__in=filtered_lines.values_list('id', flat=True)
        )

        for line in emp_visible_lines:
            # Tâches de cette ligne
            emp_tasks = Task.objects.filter(
                lines=line
            ).order_by('order', 'task_number')

            tasks_with_levels = []
            for task in emp_tasks:
                # Dernier record SUR CETTE LIGNE
                last_rec = TrainingRecord.objects.filter(
                    employee=emp,
                    task=task,
                    lines=line,          # ← filtre par ligne
                ).order_by('-date').first()

                task_pending = (
                    emp.id, task.id, line.id
                ) in pending_pairs

                tasks_with_levels.append({
                    'task':                  task,
                    'current_level':         last_rec.level if last_rec else None,
                    'current_level_display': (
                        last_rec.get_level_display()
                        if last_rec else 'Aucun'
                    ),
                    'last_date':             last_rec.date if last_rec else None,
                    'has_pending_request':   task_pending,
                })

            if tasks_with_levels:
                employees_for_form.append({
                    'employee': emp,
                    'line':     line,
                    'tasks':    tasks_with_levels,
                })

    # ══════════════════════════════════════════════════════════════════
    # STATISTIQUES
    # ══════════════════════════════════════════════════════════════════
    stats = {
        'total':    len(employees_to_requalify_list),
        'critical': len([
            e for e in employees_to_requalify_list
            if e['urgency'] == 'critical'
        ]),
        'warning':  len([
            e for e in employees_to_requalify_list
            if e['urgency'] == 'warning'
        ]),
        'info':     len([
            e for e in employees_to_requalify_list
            if e['urgency'] == 'info'
        ]),
        'total_employees':  employees.count(),
        'pending_requests': pending_requests.count(),
    }

    # ══════════════════════════════════════════════════════════════════
    # HISTORIQUE des requalifications
    # ══════════════════════════════════════════════════════════════════
    if is_rh:
        recent_requalifications = RequalificationRecord.objects.all(
        ).select_related(
            'employee', 'task', 'line', 'evaluated_by'
        ).order_by('-requalification_date')[:20]
    else:
        recent_requalifications = RequalificationRecord.objects.filter(
            evaluated_by=user
        ).select_related(
            'employee', 'task', 'line', 'evaluated_by'
        ).order_by('-requalification_date')[:10]

    if not is_rh:
        my_processed_requests = RequalificationApprovalRequest.objects.filter(
            requested_by=user,
            approved__isnull=False
        ).select_related(
            'employee', 'task', 'line', 'decision_by'
        ).order_by('-decision_date')[:10]
    else:
        my_processed_requests = None

    bound_form = form if form is not None else RequalificationForm(user=user)
    employee_lines = {}
    for item in employees_to_requalify_list:
        employee_lines.setdefault(str(item['employee'].pk), []).append({'line_id': item['line'].pk})
    context = {
        'form': bound_form,
        'employee_lines_json': employee_lines,
        'selected_employee': bound_form.cleaned_data.get('employee_id') if bound_form.is_bound else None,
        'employees_to_requalify': employees_to_requalify_list,
        'employees_for_form':     employees_for_form,
        'employees':              employees,
        'lines':                  all_lines,
        'selected_line_id':       selected_line_id,
        'threshold_days':         threshold_days,
        'stats':                  stats,
        'today':                  today,
        'recent_requalifications': recent_requalifications,
        'pending_requests':       pending_requests,
        'my_processed_requests':  my_processed_requests,
        'is_rh':                  is_rh,
        'result_choices': [
            ('PASS',     'Réussi - Maintien du niveau'),
            ('UPGRADE',  'Réussi - Montée en niveau'),
            ('DOWNGRADE','Échec - Descente en niveau'),
            ('RETRAIN',  'À reformer complètement'),
        ],
        'level_choices': [
            (0, 'I - En formation'),
            (1, 'L - Autonome supervisé'),
            (3, 'U - Formateur'),
        ],
    }

    return render(
        request,
        'matrix/employees_to_requalify.html',
        context
    )


@login_required
def process_requalification(request):
    if request.method != 'POST':
        return redirect('employees_to_requalify')
    # Resolve object authorization before parsing/validating the uploaded image.
    employees = Employee.objects.all()
    if not (request.user.is_superuser or request.user.groups.filter(name="RH").exists()):
        employees = employees.filter(manager=request.user)
    employee_id = request.POST.get('employee_id', '')
    if employee_id.isdecimal() and not employees.filter(pk=employee_id).exists():
        return HttpResponseForbidden("Vous n’êtes pas autorisé à effectuer cette requalification.")
    form = RequalificationForm(request.POST, request.FILES, user=request.user)
    if form.is_valid():
        try:
            req = create_requalification(form=form, user=request.user)
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        except (OSError, DatabaseError):
            logger.exception("Requalification creation failed")
            form.add_error(None, "Enregistrement impossible. Veuillez réessayer.")
        else:
            if req.approved is True:
                messages.success(request, "Requalification enregistrée.")
            else:
                messages.info(request, "Demande soumise à RH. En attente de validation RH.")
            return redirect('matrix_view')
    return _requalification_form_page(request, form=form)


@login_required
def rh_requalification_approvals(request):
    """
    ✅ Vue RH : Approuver/refuser les demandes de requalification.
    Utilise req.line pour créer le TrainingRecord sur la bonne ligne.
    """
    if not request.user.is_superuser:
        messages.error(request, "❌ Accès réservé au service RH.")
        return redirect('matrix_view')

    pending = RequalificationApprovalRequest.objects.filter(
        approved__isnull=True
    ).select_related(
        'employee', 'task', 'line', 'requested_by'
    ).order_by("-date_requested")

    recent_decisions = RequalificationApprovalRequest.objects.filter(
        approved__isnull=False
    ).select_related(
        'employee', 'task', 'line', 'requested_by', 'decision_by'
    ).order_by("-decision_date")[:20]

    if request.method == "POST":
        req_id        = request.POST.get("request_id")
        action        = request.POST.get("action")
        decision_notes = request.POST.get("decision_notes", "")

        req = get_object_or_404(RequalificationApprovalRequest, pk=req_id)
        if action not in ("approve", "reject"):
            messages.error(request, "Décision invalide.")
        else:
            try:
                decide_requalification(
                    request_id=req.pk, user=request.user, approve=action == "approve",
                    decision_notes=decision_notes,
                )
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            except (OSError, DatabaseError):
                logger.exception("Requalification decision failed")
                messages.error(request, "La décision n’a pas été enregistrée. Veuillez réessayer.")
            else:
                messages.success(request, "Requalification approuvée." if action == "approve" else "Requalification refusée.")
        return redirect("rh_requalification_approvals")

    pending = list(pending)
    for req in pending:
        req.plan_available = req.has_training_plan

    stats = {
        'pending': len(pending),
        'approved_today': RequalificationApprovalRequest.objects.filter(
            approved=True,
            decision_date__date=timezone.now().date()
        ).count(),
        'rejected_today': RequalificationApprovalRequest.objects.filter(
            approved=False,
            decision_date__date=timezone.now().date()
        ).count(),
    }

    context = {
        "pending":          pending,
        "recent_decisions": recent_decisions,
        "stats":            stats,
    }
    return render(
        request,
        "matrix/rh_requalification_approvals.html",
        context
    )

@login_required
def get_employee_tasks(request):
    """
    ✅ Retourne les tâches d'un employé pour une ligne donnée.
    Filtre les lignes selon celles à requalifier uniquement.
    """
    employee_id = request.GET.get('employee_id')
    line_id     = request.GET.get('line_id')

    if not employee_id:
        return JsonResponse({'tasks': [], 'lines': [], 'line': None})

    try:
        employee = Employee.objects.select_related('line').prefetch_related(
            'visible_lines'
        ).get(id=employee_id)
    except Employee.DoesNotExist:
        return JsonResponse({'tasks': [], 'lines': [], 'line': None})

    user      = request.user
    today     = timezone.now().date()
    threshold_date = today - timedelta(days=188)

    # ══════════════════════════════════════════════════════════════════
    # ✅ Trouver UNIQUEMENT les lignes où l'employé doit être requalifié
    # (dernière formation > 188 jours SUR CETTE LIGNE)
    # ══════════════════════════════════════════════════════════════════
    if user.is_superuser:
        candidate_lines = employee.visible_lines.all()
    else:
        candidate_lines = employee.visible_lines.filter(team_leaders=user)

    lines_to_requalify = []

    for line in candidate_lines:
        # Dernière formation SUR CETTE LIGNE
        last_record = TrainingRecord.objects.filter(
            employee=employee,
            lines=line,
        ).order_by('-date').first()

        if not last_record or not last_record.date:
            continue

        # Vérifier seuil 188 jours
        if last_record.date > threshold_date:
            continue

        # Vérifier si pas déjà requalifié récemment SUR CETTE LIGNE
        recent_requalif = RequalificationRecord.objects.filter(
            employee=employee,
            line=line,
            requalification_date__gte=threshold_date,
        ).exists()

        if recent_requalif:
            continue

        days_since = (today - last_record.date).days

        lines_to_requalify.append({
            'id':         line.id,
            'code':       line.code,
            'name':       line.name,
            'days_since': days_since,
            'last_date':  last_record.date.strftime('%d/%m/%Y'),
            'task_id':    last_record.task_id if last_record.task else None,
        })

    if not lines_to_requalify:
        return JsonResponse({
            'tasks':               [],
            'lines':               [],
            'selected_line':       None,
            'has_multiple_lines':  False,
            'employee':            {
                'id':        employee.id,
                'name':      f"{employee.first_name} {employee.last_name}",
                'matricule': employee.matricule,
            },
        })

    # ── Ligne sélectionnée ────────────────────────────────────────────
    selected_line     = None
    selected_line_data = None

    if line_id:
        # Chercher dans les lignes à requalifier
        for l in lines_to_requalify:
            if str(l['id']) == str(line_id):
                selected_line_data = l
                break

    if not selected_line_data:
        selected_line_data = lines_to_requalify[0]

    try:
        selected_line = ProductionLine.objects.get(pk=selected_line_data['id'])
    except ProductionLine.DoesNotExist:
        return JsonResponse({'tasks': [], 'lines': lines_to_requalify})

    # ── Tâches de la ligne sélectionnée ──────────────────────────────
    tasks = Task.objects.filter(
        lines=selected_line
    ).order_by('order', 'task_number')

    tasks_data = []
    for task in tasks:
        last_record = TrainingRecord.objects.filter(
            employee=employee,
            task=task,
            lines=selected_line,
        ).order_by('-date').first()

        tasks_data.append({
            'id':                    task.id,
            'code':                  task.code,
            'title':                 task.title,
            'current_level':         last_record.level if last_record else None,
            'current_level_display': (
                last_record.get_level_display()
                if last_record else 'Aucun'
            ),
            'last_date': (
                last_record.date.strftime('%d/%m/%Y')
                if last_record and last_record.date else None
            ),
        })

    return JsonResponse({
        'tasks':              tasks_data,
        'lines':              lines_to_requalify,    # ← lignes à requalifier uniquement
        'selected_line':      selected_line_data,
        'has_multiple_lines': len(lines_to_requalify) > 1,
        'employee': {
            'id':        employee.id,
            'name':      f"{employee.first_name} {employee.last_name}",
            'matricule': employee.matricule,
        },
    })

@login_required
def requalification_history(request):
    """
    Historique complet des requalifications
    """
    user = request.user
    is_rh = user.is_superuser or user.groups.filter(name="RH").exists()
    
    # Filtres
    selected_line_id = request.GET.get('line_id')
    selected_result = request.GET.get('result')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    
    # Requête de base
    if is_rh:
        requalifications = RequalificationRecord.objects.all()
    else:
        requalifications = RequalificationRecord.objects.filter(evaluated_by=user)
    
    # Appliquer les filtres
    if selected_line_id:
        requalifications = requalifications.filter(line_id=selected_line_id)
    if selected_result:
        requalifications = requalifications.filter(result=selected_result)
    if date_from:
        requalifications = requalifications.filter(requalification_date__gte=date_from)
    if date_to:
        requalifications = requalifications.filter(requalification_date__lte=date_to)
    
    requalifications = requalifications.select_related(
        'employee', 'task', 'line', 'evaluated_by', 'approval_request'
    ).order_by('-requalification_date')
    
    # Statistiques
    stats = {
        'total': requalifications.count(),
        'pass': requalifications.filter(result='PASS').count(),
        'upgrade': requalifications.filter(result='UPGRADE').count(),
        'downgrade': requalifications.filter(result='DOWNGRADE').count(),
        'retrain': requalifications.filter(result='RETRAIN').count(),
    }
    
    # Lignes pour le filtre
    if is_rh:
        all_lines = ProductionLine.objects.all()
    else:
        all_lines = ProductionLine.objects.filter(team_leaders=user)
    
    for rec in requalifications:
        document = plan_display(rec.approval_request, user, is_rh=is_rh)
        rec.training_plan_url = document["training_plan_url"]
        rec.training_plan_label = document["training_plan_label"]
    context = {
        'requalifications': requalifications,
        'stats': stats,
        'lines': all_lines,
        'selected_line_id': int(selected_line_id) if selected_line_id else None,
        'selected_result': selected_result,
        'date_from': date_from,
        'date_to': date_to,
        'result_choices': RequalificationRecord.RESULT_CHOICES,
        'is_rh': is_rh,
    }
    
    return render(request, 'matrix/requalification_history.html', context)

# ═══════════════════════════════════════════════════════════════════════════════
# VUE RH : Liste globale des employés à requalifier
# ═══════════════════════════════════════════════════════════════════════════════

@login_required
def rh_employees_to_requalify(request):
    """
    ✅ Affiche les employés à requalifier après 188 jours.
    - Prend en compte les visible_lines (pas seulement ligne principale)
    - Une entrée par employé × ligne à requalifier
    - Passe les données JSON au template pour le sélecteur de lignes
    """
    user           = request.user
    today          = timezone.now().date()
    threshold_days = 188
    threshold_date = today - timedelta(days=threshold_days)

    is_rh = user.is_superuser or user.groups.filter(name="RH").exists()

    # ══════════════════════════════════════════════════════════════════
    # NOTIFICATIONS : Décisions RH pour ce chef d'équipe
    # ══════════════════════════════════════════════════════════════════
    my_decided = RequalificationApprovalRequest.objects.filter(
        requested_by=user,
        approved__isnull=False,
        notification_seen=False
    ).select_related('employee', 'task')

    for req in my_decided:
        if req.approved:
            task_info  = f" - {req.task.title}" if req.task else ""
            level_info = (
                f" (Niveau: {req.get_requested_new_level_display()})"
                if req.task else ""
            )
            messages.success(
                request,
                f"✅ Requalification APPROUVÉE : "
                f"{req.employee.first_name} {req.employee.last_name}"
                f"{task_info} ({req.get_result_display()}){level_info}"
            )
        else:
            task_info = f" - {req.task.title}" if req.task else ""
            reason    = (
                f" — Motif: {req.decision_notes}"
                if req.decision_notes else ""
            )
            messages.warning(
                request,
                f"❌ Requalification REFUSÉE : "
                f"{req.employee.first_name} {req.employee.last_name}"
                f"{task_info}.{reason}"
            )
        req.notification_seen = True
        req.save()

    # ══════════════════════════════════════════════════════════════════
    # DEMANDES EN ATTENTE pour ce chef
    # ══════════════════════════════════════════════════════════════════
    pending_requests = RequalificationApprovalRequest.objects.filter(
        requested_by=user,
        approved__isnull=True
    ).select_related(
        'employee', 'task', 'line'
    ).order_by('-date_requested')

    # ══════════════════════════════════════════════════════════════════
    # LIGNES selon le rôle
    # ══════════════════════════════════════════════════════════════════
    if is_rh:
        all_lines = ProductionLine.objects.all().order_by('code')
    else:
        all_lines = ProductionLine.objects.filter(
            team_leaders=user
        ).order_by('code')

    # Filtre par ligne
    selected_line_id = request.GET.get('line_id')
    if selected_line_id:
        try:
            selected_line_id = int(selected_line_id)
            filtered_lines   = all_lines.filter(id=selected_line_id)
        except (ValueError, TypeError):
            filtered_lines   = all_lines
            selected_line_id = None
    else:
        filtered_lines = all_lines

    # ══════════════════════════════════════════════════════════════════
    # EMPLOYÉS via visible_lines
    # ══════════════════════════════════════════════════════════════════
    if is_rh:
        employees = Employee.objects.filter(
            visible_lines__in=filtered_lines
        ).distinct().select_related(
            'line', 'manager'
        ).order_by('last_name', 'first_name')
    else:
        employees = Employee.objects.filter(
            manager=user,
            visible_lines__in=filtered_lines
        ).distinct().select_related(
            'line', 'manager'
        ).order_by('last_name', 'first_name')

    # ══════════════════════════════════════════════════════════════════
    # PRÉ-CHARGER les demandes en attente
    # ══════════════════════════════════════════════════════════════════
    pending_pairs = set()
    for req in RequalificationApprovalRequest.objects.filter(
        approved__isnull=True
    ).select_related('line'):
        pending_pairs.add((req.employee_id, req.task_id, req.line_id))

    # ══════════════════════════════════════════════════════════════════
    # CONSTRUIRE LA LISTE : employé × ligne à requalifier
    # ══════════════════════════════════════════════════════════════════
    employees_to_requalify_list = []

    # Dictionnaire pour le JS : {employee_id: [lignes à requalifier]}
    employee_lines_to_requalify = {}

    for emp in employees:

        # Lignes visibles de cet employé dans le contexte filtré
        emp_visible_lines = emp.visible_lines.filter(
            id__in=filtered_lines.values_list('id', flat=True)
        ).order_by('code')

        for line in emp_visible_lines:

            # ── Dernière formation SUR CETTE LIGNE ───────────────────
            last_record = TrainingRecord.objects.filter(
                employee=emp,
                lines=line,
            ).select_related('task').order_by('-date').first()

            if not last_record or not last_record.date:
                continue

            # ── Vérifier seuil 188 jours ─────────────────────────────
            if last_record.date > threshold_date:
                continue

            days_since = (today - last_record.date).days

            # ── Requalification récente SUR CETTE LIGNE ───────────────
            recent_requalif = RequalificationRecord.objects.filter(
                employee=emp,
                line=line,
                requalification_date__gte=threshold_date,
            ).filter(
                Q(task=last_record.task) | Q(task__isnull=True)
            ).exists()

            if recent_requalif:
                continue

            # ── Demande en attente SUR CETTE LIGNE ───────────────────
            has_pending = (
                emp.id,
                last_record.task_id,
                line.id
            ) in pending_pairs

            # ── Urgence ───────────────────────────────────────────────
            urgency = (
                'critical' if days_since > 250 else
                'warning'  if days_since > 200 else
                'info'
            )

            # ── Ajouter à la liste principale ────────────────────────
            employees_to_requalify_list.append({
                'employee':              emp,
                'task':                  last_record.task,
                'line':                  line,
                'last_training_date':    last_record.date,
                'current_level':         last_record.level,
                'current_level_display': last_record.get_level_display(),
                'days_since':            days_since,
                'days_overdue':          days_since - threshold_days,
                'urgency':               urgency,
                'has_pending_request':   has_pending,
            })

            # ── Ajouter au dictionnaire JS ────────────────────────────
            emp_id = str(emp.id)
            if emp_id not in employee_lines_to_requalify:
                employee_lines_to_requalify[emp_id] = []

            # Éviter les doublons de ligne pour un même employé
            line_already_added = any(
                l['line_id'] == line.id
                for l in employee_lines_to_requalify[emp_id]
            )
            if not line_already_added:
                employee_lines_to_requalify[emp_id].append({
                    'line_id':   line.id,
                    'line_code': line.code,
                    'line_name': line.name,
                    'task_id':   last_record.task_id if last_record.task else None,
                    'days_since': days_since,
                    'urgency':   urgency,
                })

    # ── Trier par urgence puis par jours ─────────────────────────────
    employees_to_requalify_list.sort(
        key=lambda x: (
            {'critical': 0, 'warning': 1, 'info': 2}.get(x['urgency'], 3),
            -x['days_since']
        )
    )

    # ── Convertir en JSON pour le template ───────────────────────────
    import json
    employee_lines_json = json.dumps(employee_lines_to_requalify)

    # ══════════════════════════════════════════════════════════════════
    # FORMULAIRE : employés avec leurs tâches PAR LIGNE
    # ══════════════════════════════════════════════════════════════════
    employees_for_form = []

    for emp in employees:
        emp_visible_lines = emp.visible_lines.filter(
            id__in=filtered_lines.values_list('id', flat=True)
        ).order_by('code')

        for line in emp_visible_lines:
            emp_tasks = Task.objects.filter(
                lines=line
            ).order_by('order', 'task_number')

            tasks_with_levels = []
            for task in emp_tasks:
                last_rec = TrainingRecord.objects.filter(
                    employee=emp,
                    task=task,
                    lines=line,
                ).order_by('-date').first()

                task_pending = (
                    emp.id, task.id, line.id
                ) in pending_pairs

                tasks_with_levels.append({
                    'task':                  task,
                    'current_level':         last_rec.level if last_rec else None,
                    'current_level_display': (
                        last_rec.get_level_display()
                        if last_rec else 'Aucun'
                    ),
                    'last_date':           last_rec.date if last_rec else None,
                    'has_pending_request': task_pending,
                })

            if tasks_with_levels:
                employees_for_form.append({
                    'employee': emp,
                    'line':     line,
                    'tasks':    tasks_with_levels,
                })

    # ══════════════════════════════════════════════════════════════════
    # STATISTIQUES
    # ══════════════════════════════════════════════════════════════════
    stats = {
        'total': len(employees_to_requalify_list),
        'critical': len([
            e for e in employees_to_requalify_list
            if e['urgency'] == 'critical'
        ]),
        'warning': len([
            e for e in employees_to_requalify_list
            if e['urgency'] == 'warning'
        ]),
        'info': len([
            e for e in employees_to_requalify_list
            if e['urgency'] == 'info'
        ]),
        'total_employees':  employees.count(),
        'pending_requests': pending_requests.count(),
    }

    # ══════════════════════════════════════════════════════════════════
    # HISTORIQUE des requalifications
    # ══════════════════════════════════════════════════════════════════
    if is_rh:
        recent_requalifications = RequalificationRecord.objects.all(
        ).select_related(
            'employee', 'task', 'line', 'evaluated_by', 'approval_request'
        ).order_by('-requalification_date')[:20]
    else:
        recent_requalifications = RequalificationRecord.objects.filter(
            evaluated_by=user
        ).select_related(
            'employee', 'task', 'line', 'evaluated_by', 'approval_request'
        ).order_by('-requalification_date')[:10]

    if not is_rh:
        my_processed_requests = RequalificationApprovalRequest.objects.filter(
            requested_by=user,
            approved__isnull=False
        ).select_related(
            'employee', 'task', 'line', 'decision_by'
        ).order_by('-decision_date')[:10]
    else:
        my_processed_requests = None

    # ══════════════════════════════════════════════════════════════════
    # CONTEXT
    # ══════════════════════════════════════════════════════════════════
    for rec in recent_requalifications:
        document = plan_display(rec.approval_request, user, is_rh=is_rh)
        rec.training_plan_url = document['training_plan_url']
        rec.training_plan_label = document['training_plan_label']
    context = {
        'employees_to_requalify':  employees_to_requalify_list,
        'employees_for_form':      employees_for_form,
        'employees':               employees,
        'lines':                   all_lines,
        'selected_line_id':        selected_line_id,
        'threshold_days':          threshold_days,
        'stats':                   stats,
        'today':                   today,
        'recent_requalifications': recent_requalifications,
        'pending_requests':        pending_requests,
        'my_processed_requests':   my_processed_requests,
        'is_rh':                   is_rh,

        # ✅ JSON pour le sélecteur de lignes dans le JS
        'employee_lines_json': employee_lines_json,

        'result_choices': [
            ('PASS',      'Réussi - Maintien du niveau'),
            ('UPGRADE',   'Réussi - Montée en niveau'),
            ('DOWNGRADE', 'Échec - Descente en niveau'),
            ('RETRAIN',   'À reformer complètement'),
        ],
        'level_choices': [
            (0, 'I - En formation'),
            (1, 'L - Autonome supervisé'),
            (3, 'U - Formateur'),
        ],
    }

    return render(
        request,
        'matrix/rh_employees_to_requalify.html',
        context
    )


# ************************************************************************

@login_required
def rh_requalify_team_leader(request):
    """
    Vue RH/Superviseur pour requalifier les chefs d'équipe.
    - Affiche toutes les tâches en retard (> 188j) par ligne (informatif)
    - Requalification GLOBALE par ligne (pas par tâche)
    """
    user  = request.user
    today = timezone.now().date()

    is_rh          = user.is_superuser or user.groups.filter(name="RH").exists()
    is_superviseur = user.groups.filter(name="Superviseur").exists()

    if not (is_rh or is_superviseur):
        messages.error(request, "❌ Accès réservé au service RH et aux Superviseurs.")
        return redirect('matrix_view')

    threshold_days = LEADER_REQUALIFICATION_DAYS

    # ══════════════════════════════════════════════════════════════
    # POST : Traitement du formulaire (requalification GLOBALE)
    # ══════════════════════════════════════════════════════════════
    form = RequalificationForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        user=user, direct_leader=True,
    )
    if request.method == 'POST' and form.is_valid():
        try:
            create_requalification(form=form, user=user, direct_leader=True)
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        except (OSError, DatabaseError) as exc:
            if isinstance(exc, OperationalError) and "locked" in str(exc).lower():
                # Do not query the still-locked rows to rebuild the form.
                return HttpResponse(
                    "Une requalification est en cours d’enregistrement. Actualisez la page avant de réessayer.",
                    status=409,
                )
            logger.exception("Direct leader requalification failed")
            form.add_error(None, "Enregistrement impossible. Veuillez réessayer.")
        else:
            messages.success(request, "Requalification enregistrée.")
            return redirect('rh_requalify_team_leader')

    # ══════════════════════════════════════════════════════════════
    # GET : Construire les données
    # ══════════════════════════════════════════════════════════════

    team_leader_users = User.objects.exclude(pk=user.pk).filter(
        managed_lines__isnull=False
    ).distinct()

    leaders_data = []

    for tl_user in team_leader_users:

        # ── Trouver l'employé lié à ce chef ───────────────────────
        leader_emp = Employee.objects.filter(user=tl_user).first()

        if not leader_emp:
            continue

        # ── Lignes gérées par ce chef ──────────────────────────────
        managed_lines = ProductionLine.objects.filter(
            team_leaders=tl_user
        ).order_by('code')

        lines_to_requalify = []

        for line in managed_lines:

            # ── Toutes les tâches de cette ligne ───────────────────
            tasks_on_line = Task.objects.filter(lines=line)

            tasks_overdue = leader_tasks_to_requalify(leader_emp, line, today)

            # ── Pas de tâches en retard → passer ───────────────────
            if not tasks_overdue:
                continue

            # ── Urgence globale de la ligne ────────────────────────
            urgency_order = {'critical': 0, 'warning': 1, 'info': 2}
            line_urgency  = min(
                tasks_overdue,
                key=lambda x: urgency_order.get(x['urgency'], 3)
            )['urgency']

            # ── Dernière formation sur la ligne (toutes tâches) ────
            last_line_record = TrainingRecord.objects.filter(
                employee=leader_emp,
                lines=line,
            ).order_by('-date').first()

            lines_to_requalify.append({
                'line':          line,
                'tasks_overdue': tasks_overdue,
                'task_count':    tasks_on_line.count(),
                'overdue_count': len(tasks_overdue),
                'last_date':     (
                    last_line_record.date if last_line_record else None
                ),
                'urgency':       line_urgency,
            })

        # ── N'ajouter ce chef QUE s'il a des lignes à requalifier ──
        if not lines_to_requalify:
            continue

        # ── Urgence globale du chef ────────────────────────────────
        urgency_order  = {'critical': 0, 'warning': 1, 'info': 2}
        global_urgency = min(
            lines_to_requalify,
            key=lambda x: urgency_order.get(x['urgency'], 3)
        )['urgency']

        leaders_data.append({
            'employee':           leader_emp,
            'user':               tl_user,
            'lines_to_requalify': lines_to_requalify,
            'global_urgency':     global_urgency,
            'total_lines':        len(lines_to_requalify),
        })

    # ── Trier par urgence ─────────────────────────────────────────
    urgency_order = {'critical': 0, 'warning': 1, 'info': 2}
    leaders_data.sort(
        key=lambda x: urgency_order.get(x['global_urgency'], 3)
    )

    # ── Historique récent ─────────────────────────────────────────
    # ✅ Récupère tous les employés liés à un user chef d'équipe
    # sans dépendre de leaders_data
    all_leader_employees = Employee.objects.filter(
        user__managed_lines__isnull=False
    ).distinct()

    all_team_leader_emp_ids = list(
        all_leader_employees.values_list('id', flat=True)
    )

    recent_requalifications = RequalificationRecord.objects.filter(
        employee_id__in=all_team_leader_emp_ids
    ).select_related(
        'employee', 'task', 'line', 'evaluated_by', 'approval_request'
    ).order_by('-requalification_date')[:15]

    # ── Statistiques ──────────────────────────────────────────────
    total_lines_count = sum(
        len(l['lines_to_requalify']) for l in leaders_data
    )
    total_tasks_count = sum(
        len(line['tasks_overdue'])
        for l in leaders_data
        for line in l['lines_to_requalify']
    )

    stats = {
        'total_leaders': len(leaders_data),
        'total_lines':   total_lines_count,
        'total_tasks':   total_tasks_count,
        'critical': len([
            l for l in leaders_data
            if l['global_urgency'] == 'critical'
        ]),
        'warning': len([
            l for l in leaders_data
            if l['global_urgency'] == 'warning'
        ]),
        'info': len([
            l for l in leaders_data
            if l['global_urgency'] == 'info'
        ]),
    }

    for rec in recent_requalifications:
        document = plan_display(rec.approval_request, user, is_rh=is_rh)
        rec.training_plan_url = document['training_plan_url']
        rec.training_plan_label = document['training_plan_label']
    leaders_lines_data = [{
        'employee_id': item['employee'].pk,
        'lines_detail': [{
            'id': line_data['line'].pk, 'code': line_data['line'].code,
            'name': line_data['line'].name, 'task_count': line_data['task_count'],
            'overdue_count': line_data['overdue_count'], 'urgency': line_data['urgency'],
            'last_date': line_data['last_date'].isoformat() if line_data['last_date'] else '',
        } for line_data in item['lines_to_requalify']],
    } for item in leaders_data]
    context = {
        'leaders_lines_data': leaders_lines_data,
        'form': form,
        'selected_employee': form.cleaned_data.get('employee_id') if form.is_bound else None,
        'leaders_data':            leaders_data,
        'recent_requalifications': recent_requalifications,
        'stats':                   stats,
        'today':                   today,
        'threshold_days':          threshold_days,
        'is_rh':                   is_rh,
        'is_superviseur':          is_superviseur,
        
        'result_choices': [
            ('PASS',    'Réussi - Maintien du niveau'),
            ('UPGRADE', 'Réussi - Montée en niveau'),
            ('RETRAIN', 'À reformer complètement'),
        ],
        'level_choices': [
            (0, 'I - En formation'),
            (1, 'L - Autonome supervisé'),
            (3, 'U - Formateur'),
        ],
    }

    return render(
        request,
        'matrix/rh_requalify_team_leader.html',
        context
    )

# **************************************************************************

from django.http import HttpResponse
from django.conf import settings
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.drawing.image import Image
from datetime import datetime
from django.contrib.auth.decorators import login_required
import os

from .models import Employee, ProductionLine, Task, TrainingRecord


@login_required
def export_matrice_excel(request):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.drawing.image import Image
    from openpyxl.worksheet.page import PageMargins
    from django.http import HttpResponse
    from django.conf import settings
    from django.db.models import Q
    from django.contrib.auth.models import User
    from datetime import datetime
    import os

    selected_line_id = request.GET.get('line_id')
    selected_emp_ids = request.GET.getlist('emp_ids')
    current_user     = request.user

    if not selected_line_id or selected_line_id == 'None':
        first_line = ProductionLine.objects.filter(
            team_leaders=current_user
        ).first()
        if first_line:
            selected_line_id = first_line.id
        else:
            wb = Workbook()
            ws = wb.active
            ws.cell(row=1, column=1).value = "Aucune ligne de production disponible"
            response = HttpResponse(
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
            response['Content-Disposition'] = 'attachment; filename="Matrice_Vide.xlsx"'
            wb.save(response)
            return response

    try:
        production_line    = ProductionLine.objects.get(id=selected_line_id)
        selected_line_name = production_line.name
    except ProductionLine.DoesNotExist:
        production_line    = None
        selected_line_name = "Ligne inconnue"

    # ═══════════════════════════════════════════════════════
    # DROITS
    # ═══════════════════════════════════════════════════════
    is_team_leader       = False
    team_leader_name     = "Non assigne"
    team_leader_employee = None

    if production_line:
        if current_user in production_line.team_leaders.all():
            is_team_leader   = True
            team_leader_name = (
                current_user.get_full_name() or
                current_user.username
            )

            if hasattr(current_user, 'employee_profile'):
                try:
                    team_leader_employee = current_user.employee_profile
                except Exception:
                    pass

            if not team_leader_employee:
                team_leader_employee = Employee.objects.filter(
                    Q(first_name__iexact=current_user.first_name,
                      last_name__iexact=current_user.last_name) |
                    Q(matricule__iexact=current_user.username)
                ).first()

        elif (current_user.is_superuser or
              current_user.groups.filter(name='RH').exists()):
            team_leader_name = (
                f"Tous les chefs "
                f"({production_line.team_leaders.count()} chef(s))"
            )
        else:
            wb = Workbook()
            ws = wb.active
            ws.cell(row=1, column=1).value = "ACCES REFUSE"
            response = HttpResponse(
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
            response['Content-Disposition'] = 'attachment; filename="Acces_Refuse.xlsx"'
            wb.save(response)
            return response

    # ═══════════════════════════════════════════════════════
    # IDs des chefs
    # ═══════════════════════════════════════════════════════
    team_leader_user_ids = User.objects.filter(
        managed_lines__isnull=False
    ).distinct().values_list('id', flat=True)

    leader_employee_ids = set(
        Employee.objects.filter(
            user_id__in=team_leader_user_ids
        ).values_list('id', flat=True)
    )

    # ═══════════════════════════════════════════════════════
    # EMPLOYÉS — même logique que matrix_view
    # ═══════════════════════════════════════════════════════
    if production_line:
        if is_team_leader:
            operators_qs = Employee.objects.filter(
                visible_lines=production_line,
                manager=current_user          # ✅ filtre par chef
            ).exclude(
                id__in=leader_employee_ids
            ).select_related(
                'manager', 'line'
            ).order_by('last_name', 'first_name')

            employees_list = list(operators_qs)

            if team_leader_employee and team_leader_employee.id not in [
                e.id for e in employees_list
            ]:
                employees_list.insert(0, team_leader_employee)

        else:
            # RH : grouper par chef, comme export_matrice_select
            employees_list = []
            seen_ids       = set()

            for leader in production_line.team_leaders.all():
                leader_emp = None
                if hasattr(leader, 'employee_profile'):
                    try:
                        leader_emp = leader.employee_profile
                    except Exception:
                        pass
                if not leader_emp:
                    leader_emp = Employee.objects.filter(
                        Q(first_name__iexact=leader.first_name,
                          last_name__iexact=leader.last_name) |
                        Q(matricule__iexact=leader.username)
                    ).first()

                if leader_emp and leader_emp.id not in seen_ids:
                    employees_list.append(leader_emp)
                    seen_ids.add(leader_emp.id)

                leader_operators = Employee.objects.filter(
                    visible_lines=production_line,
                    manager=leader            # ✅ filtre par chef
                ).exclude(
                    id__in=leader_employee_ids
                ).select_related(
                    'manager', 'line'
                ).order_by('last_name', 'first_name')

                for emp in leader_operators:
                    if emp.id not in seen_ids:
                        employees_list.append(emp)
                        seen_ids.add(emp.id)
    else:
        if current_user.is_superuser:
            employees_list = list(
                Employee.objects.all()
                .select_related('manager', 'line')
                .order_by('last_name', 'first_name')
            )
        else:
            employees_list = list(
                Employee.objects.filter(manager=current_user)
                .select_related('manager', 'line')
                .order_by('last_name', 'first_name')
            )

    # ═══════════════════════════════════════════════════════
    # FILTRER PAR SÉLECTION (si emp_ids fournis)
    # ═══════════════════════════════════════════════════════
    if selected_emp_ids:
        selected_ids_int = set()
        for x in selected_emp_ids:
            try:
                selected_ids_int.add(int(x))
            except (ValueError, TypeError):
                pass
        if selected_ids_int:
            employees_list = [
                e for e in employees_list
                if e.id in selected_ids_int
            ]

    if not employees_list:
        wb = Workbook()
        ws = wb.active
        ws.cell(row=1, column=1).value = "AUCUN EMPLOYE TROUVE"
        response = HttpResponse(
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = 'attachment; filename="Matrice_SansEmployes.xlsx"'
        wb.save(response)
        return response

    # ═══════════════════════════════════════════════════════
    # TÂCHES
    # ═══════════════════════════════════════════════════════
    if production_line:
        tasks = Task.objects.filter(
            lines=production_line
        ).distinct().order_by('order', 'task_number')
    else:
        tasks = Task.objects.all().order_by('order', 'task_number')

    employee_ids = [e.id for e in employees_list]

    all_training_records = TrainingRecord.objects.filter(
        employee_id__in=employee_ids,
        task__in=tasks,
        lines=production_line
    ).select_related('employee', 'task', 'trainer').distinct()

    training_dict = {}
    for rec in all_training_records:
        key = (rec.employee_id, rec.task_id, rec.level)
        if key not in training_dict:
            training_dict[key] = rec
        else:
            existing_date = training_dict[key].date
            new_date      = rec.date
            if new_date and existing_date:
                if new_date > existing_date:
                    training_dict[key] = rec
            elif new_date and not existing_date:
                training_dict[key] = rec

    # ═══════════════════════════════════════════════════════
    # MATRICE
    # ═══════════════════════════════════════════════════════
    matrix = []
    for task in tasks:
        row = {'task': task, 'cells': []}
        for employee in employees_list:
            is_chef = bool(
                team_leader_employee and
                employee.id == team_leader_employee.id
            )
            emp_manager      = getattr(employee, 'manager', None)
            emp_manager_name = None
            if emp_manager:
                emp_manager_name = (
                    emp_manager.last_name or
                    emp_manager.username
                )

            row['cells'].append({
                'I':               training_dict.get((employee.id, task.id, 0)),
                'L':               training_dict.get((employee.id, task.id, 1)),
                'U':               training_dict.get((employee.id, task.id, 3)),
                'employee':        employee,
                'is_team_leader':  is_chef,
                'team_leader_name': emp_manager_name,
            })
        matrix.append(row)

    # ═══════════════════════════════════════════════════════
    # WORKBOOK
    # ═══════════════════════════════════════════════════════
    wb = Workbook()
    ws = wb.active

    invalid_chars = ['\\', '/', '?', '*', '[', ']', ':']
    safe_title    = selected_line_name
    for ch in invalid_chars:
        safe_title = safe_title.replace(ch, '-')
    ws.title = f"Matrice {safe_title[:22]}"

    ws.sheet_view.showGridLines = False
    ws.page_setup.orientation   = 'landscape'
    ws.page_setup.paperSize     = ws.PAPERSIZE_A3
    ws.page_setup.fitToWidth    = 1
    ws.page_setup.fitToHeight   = 0
    ws.page_margins = PageMargins(
        left=0.4, right=0.4,
        top=0.5,  bottom=0.5
    )

    thin_border = Border(
        left   = Side(style='thin', color='D1D5DB'),
        right  = Side(style='thin', color='D1D5DB'),
        top    = Side(style='thin', color='D1D5DB'),
        bottom = Side(style='thin', color='D1D5DB'),
    )

    header_fill = PatternFill(start_color="F3F4F6", end_color="F3F4F6", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="374151")
    chef_fill   = PatternFill(start_color="EDE9FE", end_color="EDE9FE", fill_type="solid")
    chef_font   = Font(name="Calibri", size=9, bold=True, color="5B21B6")

    level_styles = {
        'I':       PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid"),
        'L':       PatternFill(start_color="FFFBEB", end_color="FFFBEB", fill_type="solid"),
        'U':       PatternFill(start_color="ECFCCB", end_color="ECFCCB", fill_type="solid"),
        'default': PatternFill(start_color="F9FAFB", end_color="F9FAFB", fill_type="solid"),
    }

    level_fonts = {
        'I':       Font(name="Calibri", color="B91C1C", bold=True, size=10),
        'L':       Font(name="Calibri", color="D97706", bold=True, size=10),
        'U':       Font(name="Calibri", color="65A30D", bold=True, size=10),
        'default': Font(name="Calibri", color="6B7280", size=9),
    }

    total_cols = len(employees_list) + 1

    ws.column_dimensions['A'].width = 35
    for i in range(2, total_cols + 1):
        ws.column_dimensions[get_column_letter(i)].width = 18

    for logo_path in [
        os.path.join(settings.BASE_DIR, 'static', 'images', 'logo.png'),
        os.path.join(settings.BASE_DIR, 'matrix', 'static', 'images', 'logo.png'),
        os.path.join(settings.BASE_DIR, 'static', 'logo.png'),
    ]:
        if os.path.exists(logo_path):
            try:
                logo        = Image(logo_path)
                logo.width  = 120
                logo.height = 60
                ws.add_image(logo, 'A1')
                break
            except Exception:
                pass

    ws.row_dimensions[1].height = 25
    ws.row_dimensions[2].height = 25
    ws.row_dimensions[3].height = 25
    ws.row_dimensions[4].height = 10

    # Titre
    ws.merge_cells(start_row=1, start_column=3, end_row=1, end_column=total_cols)
    c           = ws.cell(row=1, column=3)
    c.value     = "MATRICE DE FORMATION"
    c.font      = Font(name="Calibri", size=18, bold=True, color="4F46E5")
    c.alignment = Alignment(horizontal="center", vertical="center")

    # Ligne de production
    ws.merge_cells(start_row=2, start_column=3, end_row=2, end_column=total_cols)
    c           = ws.cell(row=2, column=3)
    c.value     = f"Ligne de production : {selected_line_name}"
    c.font      = Font(name="Calibri", size=12, bold=True, color="1F2937")
    c.alignment = Alignment(horizontal="center", vertical="center")

    # Infos
    ws.merge_cells(start_row=3, start_column=3, end_row=3, end_column=total_cols)
    c = ws.cell(row=3, column=3)

    operator_count = len([
        e for e in employees_list
        if not (team_leader_employee and e.id == team_leader_employee.id)
    ])

    c.value = (
        f"Chef d'equipe : {team_leader_name}"
        f"  |  Operateurs : {operator_count}"
        f"  |  Taches : {tasks.count()}"
        f"  |  Date : {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )
    c.font      = Font(name="Calibri", size=11, color="374151")
    c.alignment = Alignment(horizontal="center", vertical="center")

    # En-têtes employés
    header_row = 5
    ws.row_dimensions[header_row].height = 50

    task_header           = ws.cell(row=header_row, column=1)
    task_header.value     = "Tache / Niveau"
    task_header.fill      = header_fill
    task_header.font      = header_font
    task_header.alignment = Alignment(horizontal="center", vertical="center")
    task_header.border    = thin_border

    for idx, emp in enumerate(employees_list, start=2):
        cell    = ws.cell(row=header_row, column=idx)
        is_chef = bool(
            team_leader_employee and
            emp.id == team_leader_employee.id
        )

        emp_name      = f"{emp.first_name or ''} {emp.last_name or ''}".strip()
        emp_matricule = emp.matricule or ''

        if is_chef:
            cell.value = f"{emp_name}\n({emp_matricule})\n[CHEF]"
            cell.fill  = chef_fill
            cell.font  = chef_font
        else:
            cell.value = f"{emp_name}\n({emp_matricule})"
            cell.fill  = header_fill
            cell.font  = Font(name="Calibri", size=9, bold=True)

        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border    = thin_border

    # Matrice
    current_row = header_row + 1

    for row_data in matrix:
        task       = row_data['task']
        cells_data = row_data['cells']

        ws.merge_cells(
            start_row=current_row, start_column=1,
            end_row=current_row,   end_column=total_cols
        )
        task_cell           = ws.cell(row=current_row, column=1)
        task_cell.value     = f"  {task.task_number:02d}.  {task.title}"
        task_cell.fill      = PatternFill(start_color="E5E7EB", end_color="E5E7EB", fill_type="solid")
        task_cell.font      = Font(name="Calibri", bold=True, size=10, color="1F2937")
        task_cell.alignment = Alignment(horizontal="left", vertical="center")
        task_cell.border    = thin_border
        ws.row_dimensions[current_row].height = 25
        current_row += 1

        for level in ['I', 'L', 'U']:
            level_cell           = ws.cell(row=current_row, column=1)
            level_cell.value     = level
            level_cell.fill      = level_styles[level]
            level_cell.font      = level_fonts[level]
            level_cell.alignment = Alignment(horizontal="center", vertical="center")
            level_cell.border    = thin_border

            for idx, cell_data in enumerate(cells_data, start=2):
                cell    = ws.cell(row=current_row, column=idx)
                record  = cell_data.get(level)
                is_chef = cell_data.get('is_team_leader', False)
                tl_name = cell_data.get('team_leader_name')

                if record:
                    content_parts = [level]

                    if record.date:
                        content_parts.append(record.date.strftime('%d/%m/%Y'))

                    display_name = None
                    if is_chef:
                        if record.trainer:
                            display_name = (
                                record.trainer.last_name or
                                record.trainer.matricule or
                                "RH"
                            )
                        else:
                            display_name = "RH"
                    else:
                        if tl_name:
                            display_name = tl_name

                    if display_name:
                        content_parts.append(display_name)

                    cell.value = "\n".join(content_parts)
                    cell.fill  = level_styles[level]
                    cell.font  = Font(
                        name="Calibri", size=8,
                        bold=True, color=level_fonts[level].color
                    )
                else:
                    cell.value = ""
                    cell.fill  = level_styles['default']
                    cell.font  = level_fonts['default']

                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                cell.border    = thin_border

            ws.row_dimensions[current_row].height = 55
            current_row += 1

    # Légende
    current_row += 1
    ws.row_dimensions[current_row].height = 10
    current_row += 1

    ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=5)
    leg_h           = ws.cell(row=current_row, column=1)
    leg_h.value     = "LEGENDE"
    leg_h.font      = Font(name="Calibri", bold=True, size=11, color="FFFFFF")
    leg_h.fill      = PatternFill(start_color="374151", end_color="374151", fill_type="solid")
    leg_h.alignment = Alignment(horizontal="center", vertical="center")
    leg_h.border    = thin_border
    ws.row_dimensions[current_row].height = 22

    legend_items = [
        ("I",    "En periode de formation (sous supervision)", "FEE2E2", "B91C1C"),
        ("L",    "Approuve (autonome)",                        "FFFBEB", "D97706"),
        ("U",    "Prepare pour former d'autres (formateur)",   "ECFCCB", "65A30D"),
        
    ]

    for idx, (symbol, description, bg_color, text_color) in enumerate(legend_items):
        row = current_row + idx + 1
        ws.row_dimensions[row].height = 16

        s       = ws.cell(row=row, column=1)
        s.value = symbol
        s.fill  = PatternFill(start_color=bg_color, end_color=bg_color, fill_type="solid")
        s.font  = Font(name="Calibri", bold=True, size=9, color=text_color)
        s.alignment = Alignment(horizontal="center", vertical="center")
        s.border    = thin_border

        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        d           = ws.cell(row=row, column=2)
        d.value     = description
        d.font      = Font(name="Calibri", size=9, color="374151")
        d.alignment = Alignment(horizontal="left", vertical="center")
        d.border    = thin_border

    ws.freeze_panes = f"B{header_row + 1}"

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

    safe_line = selected_line_name
    for ch in invalid_chars:
        safe_line = safe_line.replace(ch, '-')
    safe_line = safe_line.replace(' ', '_')

    safe_chef = "".join(
        c for c in team_leader_name.split()[0] if c.isalnum()
    )[:20]

    filename = (
        f"Matrice_{safe_line}_{safe_chef}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    wb.save(response)
    return response





@login_required
def export_matrice_select(request):
    current_user     = request.user
    selected_line_id = request.GET.get('line_id')

    # ═══════════════════════════════════════════════════════
    # TOUTES les lignes du chef (pour le sélecteur)
    # ═══════════════════════════════════════════════════════
    is_rh = (
        current_user.is_superuser or
        current_user.groups.filter(name='RH').exists()
    )

    if is_rh:
        all_user_lines = ProductionLine.objects.all().order_by('name')
    else:
        all_user_lines = ProductionLine.objects.filter(
            team_leaders=current_user
        ).order_by('name')

    # ═══════════════════════════════════════════════════════
    # Ligne sélectionnée
    # ═══════════════════════════════════════════════════════
    if not selected_line_id or selected_line_id == 'None':
        first_line = all_user_lines.first()
        if first_line:
            selected_line_id = first_line.id
        else:
            messages.warning(request, "Aucune ligne de production disponible.")
            return redirect('matrix_view')

    try:
        selected_line_id = int(selected_line_id)
        production_line  = ProductionLine.objects.get(id=selected_line_id)
    except (ProductionLine.DoesNotExist, ValueError, TypeError):
        messages.error(request, "Ligne de production introuvable.")
        return redirect('matrix_view')

    # Vérifier les droits
    is_team_leader = current_user in production_line.team_leaders.all()

    if not is_team_leader and not is_rh:
        messages.error(request, "Vous n'avez pas acces a cette ligne.")
        return redirect('matrix_view')

    # ═══════════════════════════════════════════════════════
    # IDs des chefs
    # ═══════════════════════════════════════════════════════
    team_leader_user_ids = User.objects.filter(
        managed_lines__isnull=False
    ).distinct().values_list('id', flat=True)

    leader_employee_ids = set(
        Employee.objects.filter(
            user_id__in=team_leader_user_ids
        ).values_list('id', flat=True)
    )

    # ═══════════════════════════════════════════════════════
    # Profil Employee du chef connecté
    # ═══════════════════════════════════════════════════════
    team_leader_employee = None

    if is_team_leader:
        if hasattr(current_user, 'employee_profile'):
            try:
                team_leader_employee = current_user.employee_profile
            except Exception:
                pass
        if not team_leader_employee:
            team_leader_employee = Employee.objects.filter(
                Q(first_name__iexact=current_user.first_name,
                  last_name__iexact=current_user.last_name) |
                Q(matricule__iexact=current_user.username)
            ).first()

    # ═══════════════════════════════════════════════════════
    # EMPLOYÉS — même logique que matrix_view
    # ═══════════════════════════════════════════════════════
    if is_team_leader:
        operators_qs = Employee.objects.filter(
            visible_lines=production_line,
            manager=current_user
        ).exclude(
            id__in=leader_employee_ids
        ).select_related(
            'manager', 'line'
        ).order_by('last_name', 'first_name')

        employees_list = list(operators_qs)

        if team_leader_employee:
            if team_leader_employee.id not in [e.id for e in employees_list]:
                employees_list.insert(0, team_leader_employee)
    else:
        employees_list = []
        seen_ids       = set()

        for leader in production_line.team_leaders.all():
            leader_emp = None
            if hasattr(leader, 'employee_profile'):
                try:
                    leader_emp = leader.employee_profile
                except Exception:
                    pass
            if not leader_emp:
                leader_emp = Employee.objects.filter(
                    Q(first_name__iexact=leader.first_name,
                      last_name__iexact=leader.last_name) |
                    Q(matricule__iexact=leader.username)
                ).first()

            if leader_emp and leader_emp.id not in seen_ids:
                employees_list.append(leader_emp)
                seen_ids.add(leader_emp.id)

            leader_operators = Employee.objects.filter(
                visible_lines=production_line,
                manager=leader
            ).exclude(
                id__in=leader_employee_ids
            ).select_related(
                'manager', 'line'
            ).order_by('last_name', 'first_name')

            for emp in leader_operators:
                if emp.id not in seen_ids:
                    employees_list.append(emp)
                    seen_ids.add(emp.id)

    # ═══════════════════════════════════════════════════════
    # Préparer les données
    # ═══════════════════════════════════════════════════════
    employees_with_meta = []
    for emp in employees_list:
        is_chef = bool(
            team_leader_employee and
            emp.id == team_leader_employee.id
        )

        emp_manager_name = None
        if emp.manager:
            emp_manager_name = (
                emp.manager.get_full_name() or
                emp.manager.username
            )

        employees_with_meta.append({
            'emp':          emp,
            'is_chef':      is_chef,
            'manager_name': emp_manager_name,
        })

    context = {
        'production_line':      production_line,
        'all_user_lines':       all_user_lines,
        'employees_with_meta':  employees_with_meta,
        'team_leader_employee': team_leader_employee,
        'total_count':          len(employees_list),
        'is_rh':                is_rh,
        'selected_line_id':     selected_line_id,
    }

    return render(request, 'matrix/export_select.html', context)

# ═══════════════════════════════════════════════════════════════════════════════
# TRANSFERT CHEF D'ÉQUIPE (avec approbation RH)
# ═══════════════════════════════════════════════════════════════════════════════

from .forms import TeamLeaderTransferForm
from .models import LineTransferApprovalRequest

# ═══════════════════════════════════════════════════════════════════════════════
# TRANSFERT PAR CHEF D'ÉQUIPE (avec approbation RH)
# ═══════════════════════════════════════════════════════════════════════════════

@login_required
def team_leader_transfer_view(request):
    """
    Vue de transfert pour les chefs d'équipe.
    Le transfert se fait VERS UN AUTRE CHEF D'ÉQUIPE.
    Si le chef destinataire gère plusieurs lignes,
    on propose le choix de la ligne.
    """
    user = request.user

    # Vérifier que l'utilisateur est bien un chef d'équipe
    is_team_leader = ProductionLine.objects.filter(
        team_leaders=user
    ).exists()
    if not is_team_leader:
        messages.error(request, "❌ Accès refusé. Vous devez être chef d'équipe.")
        return redirect('matrix_view')

    # ═══════════════════════════════════════════════════════════════
    # NOTIFICATIONS : Décisions RH
    # ═══════════════════════════════════════════════════════════════
    my_decided_transfers = LineTransferApprovalRequest.objects.filter(
        requested_by=user,
        approved__isnull=False,
        notification_seen=False
    )

    for req in my_decided_transfers:
        if req.approved:
            messages.success(
                request,
                f"✅ Transfert APPROUVÉ par RH : "
                f"{req.employee.first_name} {req.employee.last_name} "
                f"→ {req.to_line.name} "
                f"(Chef: {req.new_manager.get_full_name() or req.new_manager.username})"
            )
        else:
            reason = (
                f" — Motif : {req.decision_notes}"
                if req.decision_notes else ""
            )
            messages.warning(
                request,
                f"❌ Transfert REFUSÉ par RH : "
                f"{req.employee.first_name} {req.employee.last_name} "
                f"→ {req.to_line.name}.{reason}"
            )
        req.notification_seen = True
        req.save()

    # ═══════════════════════════════════════════════════════════════
    # POST : Traitement du formulaire
    # ═══════════════════════════════════════════════════════════════
    if request.method == "POST":
        employee_id    = request.POST.get('employee_id', '').strip()
        target_chef_id = request.POST.get('target_chef_id', '').strip()
        target_line_id = request.POST.get('target_line_id', '').strip()
        reason         = request.POST.get('reason', '').strip()

        # ── Validations ───────────────────────────────────────────
        errors = []
        if not employee_id:
            errors.append("Veuillez sélectionner un opérateur.")
        if not target_chef_id:
            errors.append("Veuillez sélectionner un chef d'équipe destinataire.")
        if not target_line_id:
            errors.append("Veuillez sélectionner la ligne de destination.")

        if errors:
            for error in errors:
                messages.error(request, f"❌ {error}")
            return redirect('team_leader_transfer')

        # ── Employé ───────────────────────────────────────────────
        try:
            employee = Employee.objects.get(id=int(employee_id), manager=user)
        except (Employee.DoesNotExist, ValueError):
            messages.error(request, "❌ Opérateur introuvable ou non autorisé.")
            return redirect('team_leader_transfer')

        # ── Chef destinataire ─────────────────────────────────────
        try:
            target_chef = User.objects.get(id=int(target_chef_id))
        except (User.DoesNotExist, ValueError):
            messages.error(request, "❌ Chef d'équipe destinataire introuvable.")
            return redirect('team_leader_transfer')

        # Vérifier que c'est un vrai chef d'équipe
        if not ProductionLine.objects.filter(team_leaders=target_chef).exists():
            messages.error(request, "❌ L'utilisateur sélectionné n'est pas chef d'équipe.")
            return redirect('team_leader_transfer')

        # Vérifier que ce n'est pas le même chef
        if target_chef == user:
            messages.warning(request, "⚠️ Vous ne pouvez pas transférer vers vous-même.")
            return redirect('team_leader_transfer')

        # ── Ligne de destination ──────────────────────────────────
        try:
            new_line = ProductionLine.objects.get(
                id=int(target_line_id),
                team_leaders=target_chef     # ← vérifier que la ligne appartient au chef
            )
        except (ProductionLine.DoesNotExist, ValueError):
            messages.error(request, "❌ Ligne de destination invalide pour ce chef.")
            return redirect('team_leader_transfer')

        old_line = employee.line

        # ── Vérifier que ce n'est pas la même ligne ───────────────
        if old_line == new_line:
            messages.warning(request, "⚠️ L'employé est déjà sur cette ligne.")
            return redirect('team_leader_transfer')

        # ── Vérifier demande en attente ───────────────────────────
        existing = LineTransferApprovalRequest.objects.filter(
            employee=employee,
            approved__isnull=True
        ).first()

        if existing:
            messages.warning(
                request,
                f"⚠️ Une demande de transfert est déjà en attente pour "
                f"{employee.first_name} {employee.last_name}. "
                f"Veuillez attendre la décision RH."
            )
            return redirect('team_leader_transfer')

        # ── Créer la demande ──────────────────────────────────────
        LineTransferApprovalRequest.objects.create(
            employee=employee,
            from_line=old_line,
            to_line=new_line,
            new_manager=target_chef,
            reason=reason,
            requested_by=user,
        )

        old_line_name = old_line.name if old_line else 'Aucune ligne'
        chef_name = target_chef.get_full_name() or target_chef.username

        messages.info(
            request,
            f"📋 Demande de transfert soumise au service RH :\n"
            f"{employee.first_name} {employee.last_name}\n"
            f"De : {old_line_name}\n"
            f"Vers : {new_line.name} (Chef: {chef_name})\n"
            f"En attente de validation."
        )
        return redirect('team_leader_transfer')

    # ═══════════════════════════════════════════════════════════════
    # GET : Données pour le template
    # ═══════════════════════════════════════════════════════════════

    # ── Mes employés ──────────────────────────────────────────────
    my_employees = Employee.objects.filter(
        manager=user
    ).select_related(
        'line'
    ).prefetch_related(
        'visible_lines'
    ).order_by('last_name', 'first_name')

    employees_data = []
    for emp in my_employees:
        visible_line_ids   = list(emp.visible_lines.values_list('id', flat=True))
        visible_line_names = list(emp.visible_lines.values_list('name', flat=True))
        visible_line_codes = list(emp.visible_lines.values_list('code', flat=True))

        employees_data.append({
            'id':               emp.id,
            'matricule':        emp.matricule,
            'name':             f"{emp.first_name} {emp.last_name}",
            'line':             emp.line.name if emp.line else 'Non assigné',
            'lineId':           emp.line.id if emp.line else None,
            'lineCode':         emp.line.code if emp.line else None,
            'visibleLineIds':   visible_line_ids,
            'visibleLineNames': visible_line_names,
            'visibleLineCodes': visible_line_codes,
            'isMultiline':      len(visible_line_ids) > 1,
            'visibleLineCount': len(visible_line_ids),
        })

    # ── Autres chefs d'équipe (sauf moi) ──────────────────────────
    other_team_leaders = User.objects.filter(
        managed_lines__isnull=False
    ).exclude(
        id=user.id
    ).distinct()

    chefs_data = []
    for chef in other_team_leaders:
        # Lignes gérées par ce chef
        chef_lines = ProductionLine.objects.filter(
            team_leaders=chef
        ).order_by('code')

        lines_list = []
        for line in chef_lines:
            operators_count = Employee.objects.filter(
                visible_lines=line
            ).count()

            lines_list.append({
                'id':              line.id,
                'code':            line.code,
                'name':            line.name,
                'operators_count': operators_count,
            })

        chefs_data.append({
            'id':          chef.id,
            'name':        chef.get_full_name() or chef.username,
            'username':    chef.username,
            'lines':       lines_list,
            'lines_count': len(lines_list),
        })

    # ── Demandes en attente ───────────────────────────────────────
    pending_requests = LineTransferApprovalRequest.objects.filter(
        requested_by=user,
        approved__isnull=True
    ).select_related(
        'employee', 'from_line', 'to_line', 'new_manager'
    ).order_by('-date_requested')

    # ── Historique des demandes traitées ──────────────────────────
    processed_requests = LineTransferApprovalRequest.objects.filter(
        requested_by=user,
        approved__isnull=False
    ).select_related(
        'employee', 'from_line', 'to_line', 'decision_by', 'new_manager'
    ).order_by('-decision_date')[:10]

    context = {
        "my_employees":       my_employees,
        "employees_json":     json.dumps(employees_data),
        "chefs_json":         json.dumps(chefs_data),
        "pending_requests":   pending_requests,
        "processed_requests": processed_requests,
    }

    return render(request, "matrix/team_leader_transfer.html", context)


@login_required
def rh_transfer_approvals(request):
    """
    Vue RH pour approuver ou refuser les demandes de transfert.
    ✅ Met à jour visible_lines lors de l'approbation.
    """
    user = request.user

    # ═══════════════════════════════════════════════════════════════
    # POST : Traitement
    # ═══════════════════════════════════════════════════════════════
    if request.method == "POST":
        req_id = request.POST.get("request_id")
        action = request.POST.get("action")

        req = get_object_or_404(LineTransferApprovalRequest, pk=req_id)

        if req.approved is not None:
            messages.warning(request, "⚠️ Cette demande a déjà été traitée.")
            return redirect("rh_transfer_approvals")

        # ══════════════════════════════════════════════════════════
        # APPROBATION
        # ══════════════════════════════════════════════════════════
        if action == "approve":
            employee = req.employee
            old_line = req.from_line
            new_line = req.to_line

            # 1. Valider la demande
            req.approved          = True
            req.decision_date     = timezone.now()
            req.decision_by       = user
            req.notification_seen = False
            req.save()

            # 2. Mettre à jour ligne principale + manager
            employee.line    = new_line
            employee.manager = req.new_manager
            employee.save()

            # 3. Mettre à jour visible_lines
            employee.visible_lines.add(new_line)

            old_line_kept = False
            if old_line:
                has_trainings_on_old = TrainingRecord.objects.filter(
                    employee=employee,
                    lines=old_line
                ).exists()

                if has_trainings_on_old:
                    old_line_kept = True
                else:
                    employee.visible_lines.remove(old_line)

            # 4. Enregistrer l'historique
            LineTransfer.objects.create(
                employee=employee,
                from_line=old_line,
                to_line=new_line,
                transfer_date=timezone.now().date(),
                reason=req.reason,
                transferred_by=user,
            )

            # 5. Mettre à jour les MatrixEntry
            new_tasks = Task.objects.filter(lines=new_line)
            MatrixEntry.objects.filter(
                employee=employee
            ).exclude(task__in=new_tasks).delete()

            for task in new_tasks:
                MatrixEntry.objects.get_or_create(
                    employee=employee,
                    task=task,
                    defaults={
                        'level':         TrainingLevel.IN_TRAINING,
                        'transfer_date': timezone.now().date(),
                    }
                )

            # 6. Message
            visible_count = employee.visible_lines.count()
            visible_names = ", ".join(
                employee.visible_lines.values_list('name', flat=True)
            )
            chef_name = (
                req.new_manager.get_full_name() or req.new_manager.username
                if req.new_manager else "N/A"
            )

            base_msg = (
                f"✅ Transfert approuvé : "
                f"{employee.first_name} {employee.last_name} "
                f"→ {new_line.name} (Chef: {chef_name}). "
                f"Visible dans {visible_count} matrice(s) : {visible_names}."
            )

            if old_line_kept:
                base_msg += (
                    f" ℹ️ Visibilité conservée sur '{old_line.name}' "
                    f"(formations existantes)."
                )

            messages.success(request, base_msg)

        # ══════════════════════════════════════════════════════════
        # REFUS
        # ══════════════════════════════════════════════════════════
        elif action == "reject":
            decision_notes = request.POST.get("decision_notes", "")

            req.approved          = False
            req.decision_date     = timezone.now()
            req.decision_by       = user
            req.decision_notes    = decision_notes
            req.notification_seen = False
            req.save()

            messages.warning(
                request,
                f"❌ Transfert refusé : "
                f"{req.employee.first_name} {req.employee.last_name} "
                f"→ {req.to_line.name}. "
                f"Le chef d'équipe sera notifié."
            )

        return redirect("rh_transfer_approvals")

    # ═══════════════════════════════════════════════════════════════
    # GET : Données pour le template
    # ═══════════════════════════════════════════════════════════════

    pending = LineTransferApprovalRequest.objects.filter(
        approved__isnull=True
    ).select_related(
        'employee', 'employee__line',
        'from_line', 'to_line',
        'requested_by', 'new_manager',
    ).prefetch_related(
        'employee__visible_lines'
    ).order_by("-date_requested")

    # Enrichir les demandes
    pending_enriched = []
    for req in pending:
        emp = req.employee

        trainings_on_from = 0
        if req.from_line:
            trainings_on_from = TrainingRecord.objects.filter(
                employee=emp,
                lines=req.from_line
            ).count()

        current_visible = list(
            emp.visible_lines.values('id', 'name', 'code')
        )

        future_visible_count = emp.visible_lines.count()
        if req.to_line not in emp.visible_lines.all():
            future_visible_count += 1
        if (req.from_line and
                req.from_line in emp.visible_lines.all() and
                trainings_on_from == 0):
            future_visible_count -= 1

        # Nom du chef destinataire
        chef_name = ""
        if req.new_manager:
            chef_name = req.new_manager.get_full_name() or req.new_manager.username

        pending_enriched.append({
            'req':                   req,
            'trainings_on_from':     trainings_on_from,
            'current_visible':       current_visible,
            'will_keep_old_line':    trainings_on_from > 0,
            'future_visible_count':  future_visible_count,
            'is_currently_multiline': emp.visible_lines.count() > 1,
            'target_chef_name':      chef_name,
        })

    history = LineTransferApprovalRequest.objects.filter(
        approved__isnull=False
    ).select_related(
        'employee', 'from_line', 'to_line',
        'requested_by', 'decision_by', 'new_manager',
    ).prefetch_related(
        'employee__visible_lines'
    ).order_by("-decision_date")[:20]

    # Statistiques
    from django.db.models import Count

    approved_count = LineTransferApprovalRequest.objects.filter(approved=True).count()
    rejected_count = LineTransferApprovalRequest.objects.filter(approved=False).count()
    total_count    = LineTransferApprovalRequest.objects.count()

    multiline_employees_count = Employee.objects.annotate(
        vl_count=Count('visible_lines')
    ).filter(vl_count__gte=2).count()

    top_lines = ProductionLine.objects.annotate(
        visible_count=Count('visible_employees')
    ).order_by('-visible_count')[:5]

    context = {
        "pending":                   pending,
        "pending_enriched":          pending_enriched,
        "history":                   history,
        "approved_count":            approved_count,
        "rejected_count":            rejected_count,
        "total_count":               total_count,
        "multiline_employees_count": multiline_employees_count,
        "top_lines":                 top_lines,
    }

    return render(request, "matrix/rh_transfer_approvals.html", context)


# ── API : Lignes d'un chef d'équipe ──────────────────────────────────────────

@login_required
def get_chef_lines(request):
    """
    API JSON : retourne les lignes gérées par un chef d'équipe.
    Utilisé par le formulaire de transfert en AJAX.
    """
    chef_id = request.GET.get('chef_id')

    if not chef_id:
        return JsonResponse({'lines': []})

    try:
        chef = User.objects.get(id=int(chef_id))
    except (User.DoesNotExist, ValueError):
        return JsonResponse({'lines': []})

    lines = ProductionLine.objects.filter(
        team_leaders=chef
    ).order_by('code')

    lines_data = []
    for line in lines:
        operators_count = Employee.objects.filter(
            visible_lines=line
        ).count()

        lines_data.append({
            'id':              line.id,
            'code':            line.code,
            'name':            line.name,
            'operators_count': operators_count,
        })

    return JsonResponse({
        'lines': lines_data,
        'chef_name': chef.get_full_name() or chef.username,
    })


@login_required
def rh_operators_overview(request):
    """
    Vue RH pour afficher tous les opérateurs et chefs d'équipe
    avec filtrage par ligne, chef d'équipe et niveau
    Inclut l'option d'afficher l'historique des employés par ligne
    """
    user = request.user
    
    # Vérifier que l'utilisateur est RH

    
    # ═══════════════════════════════════════════════════════════════════
    # RÉCUPÉRATION DES FILTRES
    # ═══════════════════════════════════════════════════════════════════
    selected_line_id = request.GET.get('line', '')
    selected_manager_id = request.GET.get('manager', '')
    selected_level = request.GET.get('level', '')
    search_query = request.GET.get('q', '').strip()
    employee_type = request.GET.get('type', '')  # 'operator', 'leader', ou ''
    include_history = request.GET.get('history', '') == '1'  # ✅ NOUVEAU: Inclure l'historique
    
    # ═══════════════════════════════════════════════════════════════════
    # DONNÉES DE BASE
    # ═══════════════════════════════════════════════════════════════════
    all_lines = ProductionLine.objects.all().order_by('code')
    all_managers = User.objects.filter(
        managed_lines__isnull=False
    ).distinct().order_by('last_name', 'first_name')
    
    # ═══════════════════════════════════════════════════════════════════
    # REQUÊTE DE BASE
    # ═══════════════════════════════════════════════════════════════════
    employees = Employee.objects.select_related(
        'line', 'manager', 'user'
    ).order_by('line__code', 'last_name', 'first_name')
    
    # ═══════════════════════════════════════════════════════════════════
    # ✅ NOUVEAU: Gestion de l'historique par ligne
    # ═══════════════════════════════════════════════════════════════════
    historical_employee_ids = set()
    selected_line = None
    
    if selected_line_id:
        try:
            line_id = int(selected_line_id)
            selected_line = ProductionLine.objects.get(id=line_id)
            
            if include_history:
                # ═══════════════════════════════════════════════════════
                # RECHERCHER TOUS LES EMPLOYÉS AYANT UN LIEN AVEC CETTE LIGNE
                # ═══════════════════════════════════════════════════════
                
                # 1. Employés actuellement sur cette ligne
                current_employee_ids = set(
                    Employee.objects.filter(line_id=line_id).values_list('id', flat=True)
                )
                
                # 2. Employés ayant des formations sur cette ligne
                training_employee_ids = set(
                    TrainingRecord.objects.filter(
                        lines__id=line_id
                    ).values_list('employee_id', flat=True).distinct()
                )
                
                # 3. Employés ayant été transférés VERS cette ligne
                transferred_to_ids = set(
                    LineTransfer.objects.filter(
                        to_line_id=line_id
                    ).values_list('employee_id', flat=True).distinct()
                )
                
                # 4. Employés ayant été transférés DEPUIS cette ligne
                transferred_from_ids = set(
                    LineTransfer.objects.filter(
                        from_line_id=line_id
                    ).values_list('employee_id', flat=True).distinct()
                )
                
                # Combiner tous les IDs
                all_related_ids = current_employee_ids | training_employee_ids | transferred_to_ids | transferred_from_ids
                
                # Identifier ceux qui ne sont plus sur la ligne (historique)
                historical_employee_ids = all_related_ids - current_employee_ids
                
                # Filtrer les employés
                employees = employees.filter(id__in=all_related_ids)
            else:
                # Filtre standard: seulement les employés actuels
                employees = employees.filter(line_id=line_id)
                
        except (ValueError, TypeError, ProductionLine.DoesNotExist):
            pass
    
    # ═══════════════════════════════════════════════════════════════════
    # AUTRES FILTRES
    # ═══════════════════════════════════════════════════════════════════
    
    # Filtre par manager
    if selected_manager_id:
        try:
            employees = employees.filter(manager_id=int(selected_manager_id))
        except (ValueError, TypeError):
            pass
    
    # Filtre par recherche
    if search_query:
        employees = employees.filter(
            Q(first_name__icontains=search_query) |
            Q(last_name__icontains=search_query) |
            Q(matricule__icontains=search_query)
        )
    
    # Filtre par type (opérateur ou chef)
    if employee_type == 'operator':
        leader_user_ids = User.objects.filter(managed_lines__isnull=False).values_list('id', flat=True)
        employees = employees.exclude(user_id__in=leader_user_ids)
    elif employee_type == 'leader':
        leader_user_ids = User.objects.filter(managed_lines__isnull=False).values_list('id', flat=True)
        employees = employees.filter(user_id__in=leader_user_ids)
    
    # ═══════════════════════════════════════════════════════════════════
    # CALCUL DES NIVEAUX POUR CHAQUE EMPLOYÉ
    # ═══════════════════════════════════════════════════════════════════
    employees_list = list(employees)
    employees_with_data = []
    
    all_employee_ids = [e.id for e in employees_list]
    
    # ═══════════════════════════════════════════════════════════════════
    # ✅ NOUVEAU: Récupérer l'historique de présence sur la ligne sélectionnée
    # ═══════════════════════════════════════════════════════════════════
    line_history_data = {}
    
    if selected_line_id and include_history:
        line_id = int(selected_line_id)
        
        # Récupérer les dates de transfert pour chaque employé
        for emp_id in all_employee_ids:
            history_info = {
                'is_current': False,
                'periods': [],
                'first_date': None,
                'last_date': None,
                'total_days': 0,
                'training_count': 0,
            }
            
            # Vérifier si actuellement sur la ligne
            emp = next((e for e in employees_list if e.id == emp_id), None)
            if emp and emp.line_id == line_id:
                history_info['is_current'] = True
            
            # Récupérer les transferts liés à cette ligne
            transfers_to = LineTransfer.objects.filter(
                employee_id=emp_id,
                to_line_id=line_id
            ).order_by('transfer_date')
            
            transfers_from = LineTransfer.objects.filter(
                employee_id=emp_id,
                from_line_id=line_id
            ).order_by('transfer_date')
            
            # Construire les périodes de présence
            entries = list(transfers_to.values_list('transfer_date', flat=True))
            exits = list(transfers_from.values_list('transfer_date', flat=True))
            
            if entries:
                history_info['first_date'] = min(entries)
            
            if exits:
                history_info['last_date'] = max(exits)
            elif history_info['is_current'] and entries:
                history_info['last_date'] = timezone.now().date()
            
            # Compter les formations sur cette ligne
            history_info['training_count'] = TrainingRecord.objects.filter(
                employee_id=emp_id,
                lines__id=line_id
            ).count()
            
            line_history_data[emp_id] = history_info
    
    # Récupérer toutes les formations
    all_records = TrainingRecord.objects.filter(
        employee_id__in=all_employee_ids
    ).select_related('task', 'employee').order_by('employee', 'task', '-date')
    
    # ✅ Si on filtre par ligne avec historique, récupérer les formations de cette ligne
    if selected_line_id and include_history:
        line_specific_records = TrainingRecord.objects.filter(
            employee_id__in=all_employee_ids,
            lines__id=int(selected_line_id)
        ).select_related('task', 'employee').order_by('employee', 'task', '-date')
    else:
        line_specific_records = all_records
    
    # Organiser par employé
    records_by_employee = {}
    for record in all_records:
        if record.employee_id not in records_by_employee:
            records_by_employee[record.employee_id] = []
        records_by_employee[record.employee_id].append(record)
    
    # ✅ Formations spécifiques à la ligne sélectionnée
    line_records_by_employee = {}
    if selected_line_id and include_history:
        for record in line_specific_records:
            if record.employee_id not in line_records_by_employee:
                line_records_by_employee[record.employee_id] = []
            line_records_by_employee[record.employee_id].append(record)
    
    # Identifier les chefs d'équipe
    team_leader_user_ids = set(
        User.objects.filter(managed_lines__isnull=False).values_list('id', flat=True)
    )
    
    for emp in employees_list:
        emp_records = records_by_employee.get(emp.id, [])
        
        # ✅ Si on affiche l'historique, utiliser les formations de la ligne
        if selected_line_id and include_history:
            emp_line_records = line_records_by_employee.get(emp.id, [])
        else:
            emp_line_records = emp_records
        
        # Calculer les niveaux par tâche
        task_levels = {}
        for record in emp_records:
            if record.task_id not in task_levels:
                task_levels[record.task_id] = record.level
        
        # ✅ Niveaux spécifiques à la ligne sélectionnée
        line_task_levels = {}
        for record in emp_line_records:
            if record.task_id not in line_task_levels:
                line_task_levels[record.task_id] = record.level
        
        # Compter les niveaux (global)
        level_counts = {0: 0, 1: 0, 3: 0}
        for level in task_levels.values():
            if level in level_counts:
                level_counts[level] += 1
        
        # ✅ Compter les niveaux sur la ligne spécifique
        line_level_counts = {0: 0, 1: 0, 3: 0}
        for level in line_task_levels.values():
            if level in line_level_counts:
                line_level_counts[level] += 1
        
        total_tasks = len(task_levels)
        line_total_tasks = len(line_task_levels)
        
        # Déterminer le niveau dominant
        if level_counts[3] > 0:
            dominant_level = 3
            dominant_level_display = "U - Formateur"
        elif level_counts[1] > 0:
            dominant_level = 1
            dominant_level_display = "L - Autonome"
        elif level_counts[0] > 0:
            dominant_level = 0
            dominant_level_display = "I - En formation"
        else:
            dominant_level = None
            dominant_level_display = "Non formé"
        
        # Vérifier si c'est un chef d'équipe
        is_team_leader = emp.user_id in team_leader_user_ids if emp.user_id else False
        
        # Lignes gérées si chef d'équipe
        managed_lines = []
        if is_team_leader and emp.user:
            managed_lines = list(ProductionLine.objects.filter(team_leaders=emp.user).values_list('code', flat=True))
        
        # Calculer le score de progression
        if emp.line:
            line_tasks_count = Task.objects.filter(lines=emp.line).count()
        else:
            line_tasks_count = 0
        
        if line_tasks_count > 0:
            progress_score = round(
                ((level_counts[0] * 33 + level_counts[1] * 66 + level_counts[3] * 100) / line_tasks_count), 1
            )
            coverage = round((total_tasks / line_tasks_count) * 100, 1)
        else:
            progress_score = 0
            coverage = 0
        
        # ✅ NOUVEAU: Informations sur la présence historique
        is_historical = emp.id in historical_employee_ids
        history_info = line_history_data.get(emp.id, {})
        
        emp_data = {
            'employee': emp,
            'is_team_leader': is_team_leader,
            'managed_lines': managed_lines,
            'level_counts': level_counts,
            'line_level_counts': line_level_counts,  # ✅ Niveaux sur la ligne spécifique
            'total_tasks': total_tasks,
            'line_total_tasks': line_total_tasks,  # ✅ Tâches sur la ligne spécifique
            'line_tasks_count': line_tasks_count,
            'dominant_level': dominant_level,
            'dominant_level_display': dominant_level_display,
            'progress_score': min(100, progress_score),
            'coverage': min(100, coverage),
            # ✅ NOUVEAU: Données historiques
            'is_historical': is_historical,
            'is_current_on_line': not is_historical and selected_line_id and str(emp.line_id) == selected_line_id,
            'history_info': history_info,
        }
        
        # Filtre par niveau si demandé
        if selected_level:
            try:
                level_int = int(selected_level)
                # ✅ Si historique activé, vérifier les niveaux sur la ligne
                if include_history and selected_line_id:
                    if line_level_counts.get(level_int, 0) == 0:
                        continue
                else:
                    if level_counts.get(level_int, 0) == 0:
                        continue
            except (ValueError, TypeError):
                pass
        
        employees_with_data.append(emp_data)
    
    # ═══════════════════════════════════════════════════════════════════
    # STATISTIQUES
    # ═══════════════════════════════════════════════════════════════════
    total_employees = len(employees_with_data)
    total_operators = len([e for e in employees_with_data if not e['is_team_leader']])
    total_leaders = len([e for e in employees_with_data if e['is_team_leader']])
    
    # ✅ NOUVEAU: Stats historiques
    total_current = len([e for e in employees_with_data if e.get('is_current_on_line')])
    total_historical = len([e for e in employees_with_data if e.get('is_historical')])
    
    # Moyennes
    avg_progress = round(
        sum(e['progress_score'] for e in employees_with_data) / total_employees, 1
    ) if total_employees > 0 else 0
    
    avg_coverage = round(
        sum(e['coverage'] for e in employees_with_data) / total_employees, 1
    ) if total_employees > 0 else 0
    
    # Comptage par niveau dominant
    level_distribution = {
        'trainers': len([e for e in employees_with_data if e['dominant_level'] == 3]),
        'autonomous': len([e for e in employees_with_data if e['dominant_level'] == 1]),
        'in_training': len([e for e in employees_with_data if e['dominant_level'] == 0]),
        'not_trained': len([e for e in employees_with_data if e['dominant_level'] is None]),
    }
    
    stats = {
        'total_employees': total_employees,
        'total_operators': total_operators,
        'total_leaders': total_leaders,
        'total_current': total_current,
        'total_historical': total_historical,
        'avg_progress': avg_progress,
        'avg_coverage': avg_coverage,
        'level_distribution': level_distribution,
        'total_lines': all_lines.count(),
    }
    
    # ═══════════════════════════════════════════════════════════════════
    # CONTEXT
    # ═══════════════════════════════════════════════════════════════════
    context = {
        'employees': employees_with_data,
        'all_lines': all_lines,
        'all_managers': all_managers,
        'selected_line_id': selected_line_id,
        'selected_line': selected_line,
        'selected_manager_id': selected_manager_id,
        'selected_level': selected_level,
        'search_query': search_query,
        'employee_type': employee_type,
        'include_history': include_history,  # ✅ NOUVEAU
        'stats': stats,
        'level_choices': [
            {'value': '0', 'label': 'I - En formation'},
            {'value': '1', 'label': 'L - Autonome'},
            {'value': '3', 'label': 'U - Formateur'},
        ],
    }
    
    return render(request, 'matrix/rh_operators_overview.html', context)



# views.py

@login_required
def rh_train_team_leader(request):
    """
    Vue RH pour former les chefs d'équipe.
    """
    user = request.user
    
    if request.method == 'POST':
        form = RHTeamLeaderTrainingForm(request.POST)
        
        if form.is_valid():
            record = form.save(commit=False)
            
            # ═══════════════════════════════════════════════════════════════
            # Assigner automatiquement le RH connecté comme formateur
            # ═══════════════════════════════════════════════════════════════
            rh_employee = Employee.objects.filter(
                Q(user=user) |
                Q(first_name__iexact=user.first_name, last_name__iexact=user.last_name) |
                Q(matricule__iexact=user.username)
            ).first()
            
            if rh_employee:
                record.trainer = rh_employee
            
            record.save()
            
            # ═══════════════════════════════════════════════════════════════
            # ✅ IMPORTANT : Ajouter la ligne au ManyToMany
            # ═══════════════════════════════════════════════════════════════
            line = form.cleaned_data.get('line')
            if line:
                record.lines.add(line)
            
            # Mettre à jour MatrixEntry
            MatrixEntry.objects.update_or_create(
                employee=record.employee,
                task=record.task,
                defaults={'level': record.level}
            )
            
            employee = record.employee
            task = record.task
            level_display = record.get_level_display()
            
            for warning in form.get_warnings():
                messages.warning(request, warning)
            
            trainer_name = f"{rh_employee.first_name} {rh_employee.last_name}" if rh_employee else user.get_full_name() or user.username
            
            messages.success(
                request,
                f"✅ Formation enregistrée avec succès !\n"
                f"Chef d'équipe : {employee.first_name} {employee.last_name}\n"
                f"Tâche : {task.title}\n"
                f"Niveau : {level_display}\n"
                f"Ligne : {line.name if line else 'N/A'}\n"
                f"Formateur : {trainer_name}\n"
                f"Date : {record.date.strftime('%d/%m/%Y')}"
            )
            
            return redirect('rh_train_team_leader')
        else:
            messages.error(request, "❌ Veuillez corriger les erreurs dans le formulaire.")
    else:
        form = RHTeamLeaderTrainingForm()
    
    # ═══════════════════════════════════════════════════════════════════
    # Récupérer les chefs d'équipe
    # ═══════════════════════════════════════════════════════════════════
    team_leader_user_ids = User.objects.filter(
        managed_lines__isnull=False
    ).distinct().values_list('id', flat=True)
    
    leaders_data = []
    
    for tl_user in User.objects.filter(id__in=team_leader_user_ids):
        leader_emp = Employee.objects.filter(
            Q(user=tl_user) |
            Q(first_name__iexact=tl_user.first_name, last_name__iexact=tl_user.last_name) |
            Q(matricule__iexact=tl_user.username)
        ).first()
        
        if leader_emp:
            managed_lines = list(
                ProductionLine.objects.filter(team_leaders=tl_user).values_list('code', flat=True)
            )
            
            training_count = TrainingRecord.objects.filter(employee=leader_emp).count()
            last_training = TrainingRecord.objects.filter(employee=leader_emp).order_by('-date').first()
            
            leaders_data.append({
                'employee': leader_emp,
                'user': tl_user,
                'managed_lines': managed_lines,
                'training_count': training_count,
                'last_training': last_training,
            })
    
    leader_employee_ids = [l['employee'].id for l in leaders_data]
    
    recent_trainings = TrainingRecord.objects.filter(
        employee_id__in=leader_employee_ids
    ).select_related('employee', 'task', 'trainer').prefetch_related('lines').order_by('-date')[:15]
    
    # ═══════════════════════════════════════════════════════════════════
    # Données pour le JS
    # ═══════════════════════════════════════════════════════════════════
    lines_data = []
    for line in ProductionLine.objects.all():
        task_ids = list(Task.objects.filter(lines=line).values_list('id', flat=True))
        lines_data.append({
            'id': line.id,
            'code': line.code,
            'name': line.name,
            'task_ids': task_ids,
        })
    
    leaders_lines_data = []
    for item in leaders_data:
        emp = item['employee']
        user_obj = item['user']
        
        managed_line_ids = list(
            ProductionLine.objects.filter(team_leaders=user_obj).values_list('id', flat=True)
        )
        
        leaders_lines_data.append({
            'employee_id': emp.id,
            'name': f"{emp.first_name} {emp.last_name}",
            'managed_line_ids': managed_line_ids,
        })
    
    leaders_training_history = {}
    for item in leaders_data:
        emp_id = item['employee'].id
        history = TrainingRecord.objects.filter(
            employee_id=emp_id
        ).select_related('task').prefetch_related('lines').order_by('-date')
        
        history_data = []
        for record in history:
            line_ids = list(record.lines.values_list('id', flat=True))
            history_data.append({
                'task_id': record.task_id,
                'level': record.level,
                'date': record.date.isoformat(),
                'line_ids': line_ids,
            })
        
        leaders_training_history[emp_id] = history_data
    
    # ═══════════════════════════════════════════════════════════════════
    # ✅ NOUVEAU : Mapping Employee → Lignes gérées (pour auto-sélection)
    # ═══════════════════════════════════════════════════════════════════
    employee_lines_data = []
    for item in leaders_data:
        emp = item['employee']
        user_obj = item['user']
        
        managed_line_ids = list(
            ProductionLine.objects.filter(team_leaders=user_obj).values_list('id', flat=True)
        )
        
        employee_lines_data.append({
            'employee_id': emp.id,
            'managed_line_ids': managed_line_ids
        })
    
    context = {
        'form': form,
        'leaders_data': leaders_data,
        'recent_trainings': recent_trainings,
        'lines_data_json': json.dumps(lines_data),
        'leaders_lines_json': json.dumps(leaders_lines_data),
        'leaders_history_json': json.dumps(leaders_training_history),
        'employee_lines_json': json.dumps(employee_lines_data),  # ✅ NOUVEAU
    }
    
    return render(request, 'matrix/rh_train_team_leader.html', context)


@login_required
def get_tasks_by_line_for_rh(request):
    """
    API pour récupérer les tâches d'une ligne spécifique (pour le formulaire RH)
    """
    line_id = request.GET.get('line_id')
    
    if not line_id:
        return JsonResponse({'tasks': []})
    
    try:
        tasks = Task.objects.filter(lines__id=int(line_id)).order_by('order', 'code').values(
            'id', 'title', 'code'
        )
        return JsonResponse({'tasks': list(tasks)})
    except (ValueError, TypeError):
        return JsonResponse({'tasks': []})

# ***********************************************************************************************************
# matrix/views.py

from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from .forms import UserManagementForm, QuickTeamLeaderAssignmentForm

def is_superuser(user):
    return user.is_superuser

# matrix/views.py

@login_required
@user_passes_test(is_superuser)
def rh_manage_users(request):
    """
    Vue principale pour gérer les utilisateurs (super admins et chefs d'équipe)
    """
    
    # ✅ CORRECTION : Récupérer TOUS les utilisateurs qui sont :
    # - Super admins (is_superuser=True)
    # - OU qui gèrent au moins une ligne (chefs d'équipe)
    # - OU qui ont is_staff=True
    
    from django.db.models import Q
    
    all_users = User.objects.filter(
        Q(is_superuser=True) |  # Super admins
        Q(is_staff=True) |  # Staff
        Q(managed_lines__isnull=False)  # Chefs d'équipe (gèrent des lignes)
    ).distinct().prefetch_related('managed_lines')
    
    # Ajouter le profil employé pour chaque utilisateur
    for user in all_users:
        try:
            user.employee_profile = Employee.objects.get(user=user)
        except Employee.DoesNotExist:
            user.employee_profile = None
    
    # Statistiques
    total_users = all_users.count()
    superusers_count = all_users.filter(is_superuser=True).count()
    
    # ✅ Chefs d'équipe = ceux qui gèrent au moins une ligne ET ne sont pas super admin
    team_leaders = all_users.filter(
        managed_lines__isnull=False,
        is_superuser=False
    ).distinct()
    team_leaders_count = team_leaders.count()
    
    # Utilisateurs sans profil employé
    user_ids_with_profile = Employee.objects.filter(user__isnull=False).values_list('user_id', flat=True)
    no_profile_count = all_users.exclude(pk__in=user_ids_with_profile).count()
    
    # Filtrer selon le paramètre GET
    user_filter = request.GET.get('filter', 'all')
    
    if user_filter == 'superusers':
        users = all_users.filter(is_superuser=True)
    elif user_filter == 'team_leaders':
        users = team_leaders
    elif user_filter == 'no_employee':
        users = all_users.exclude(pk__in=user_ids_with_profile)
    else:
        users = all_users
    
    users = users.order_by('-is_superuser', 'last_name', 'first_name')
    
    # Ré-ajouter le profil employé après le filtrage
    users_list = list(users)
    for user in users_list:
        try:
            user.employee_profile = Employee.objects.get(user=user)
        except Employee.DoesNotExist:
            user.employee_profile = None
    
    context = {
        'users': users_list,
        'total_users': total_users,
        'superusers_count': superusers_count,
        'team_leaders_count': team_leaders_count,
        'no_profile_count': no_profile_count,
        'user_filter': user_filter,
    }
    
    return render(request, 'matrix/rh_manage_users.html', context)


@login_required

def rh_user_create(request):
    """Créer un nouvel utilisateur (super admin ou chef d'équipe)"""
    
    all_lines = ProductionLine.objects.all().order_by('code')
    available_employees = Employee.objects.filter(user__isnull=True).order_by('last_name', 'first_name')
    
    if request.method == 'POST':
        form = UserManagementForm(request.POST)
        
        # ✅ Récupérer les lignes sélectionnées depuis POST
        selected_line_ids = request.POST.getlist('managed_lines')
        user_type = request.POST.get('user_type', 'team_leader')
        
        # ✅ Validation personnalisée : chef d'équipe doit avoir au moins une ligne
        if user_type == 'team_leader' and not selected_line_ids:
            form.add_error(None, "Un chef d'équipe doit gérer au moins une ligne de production.")
        
        if form.is_valid() and not form.errors:
            user = form.save()
            
            # ✅ Affecter les lignes au chef d'équipe
            if user_type == 'team_leader' and selected_line_ids:
                lines = ProductionLine.objects.filter(pk__in=selected_line_ids)
                user.managed_lines.set(lines)
            else:
                user.managed_lines.clear()
            
            # ✅ Gérer le profil employé
            profile_option = request.POST.get('profile_option', 'none')
            
            if profile_option == 'link':
                # Lier à un employé existant
                employee_id = request.POST.get('link_to_employee')
                if employee_id:
                    try:
                        employee = Employee.objects.get(pk=employee_id, user__isnull=True)
                        employee.user = user
                        employee.save()
                    except Employee.DoesNotExist:
                        pass
            
            elif profile_option == 'create':
                # Créer un nouveau profil employé
                matricule = request.POST.get('matricule', f'USR-{user.pk}')
                
                # Déterminer la ligne par défaut
                line = None
                manager = None
                if selected_line_ids:
                    line = ProductionLine.objects.filter(pk__in=selected_line_ids).first()
                    if line:
                        # Le manager = lui-même ou un autre chef
                        other_leaders = line.team_leaders.exclude(pk=user.pk)
                        manager = other_leaders.first() or user
                
                Employee.objects.create(
                    user=user,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    matricule=matricule,
                    line=line,
                    manager=manager,
                )
            
            user_type_label = "Super Admin" if user.is_superuser else "Chef d'équipe"
            messages.success(
                request,
                f"✅ Utilisateur {user_type_label} créé avec succès : {user.get_full_name()} ({user.username})"
            )
            
            return redirect('rh_manage_users')
        
        # Si erreur, garder les lignes sélectionnées
        selected_lines = [int(x) for x in selected_line_ids]
    else:
        form = UserManagementForm()
        selected_lines = []
    
    context = {
        'form': form,
        'title': 'Créer un utilisateur',
        'submit_text': 'Créer l\'utilisateur',
        'all_lines': all_lines,
        'available_employees': available_employees,
        'selected_lines': selected_lines,
    }
    
    return render(request, 'matrix/rh_user_form.html', context)

# matrix/views.py

@login_required

def rh_user_update(request, user_id):
    """Modifier un utilisateur existant"""
    
    user_to_edit = get_object_or_404(User, pk=user_id)
    
    # ✅ Récupérer toutes les lignes de production
    all_lines = ProductionLine.objects.all().order_by('code')
    
    # ✅ Récupérer les employés sans compte (pour liaison possible)
    # + inclure celui déjà lié à cet utilisateur s'il existe
    available_employees = Employee.objects.filter(user__isnull=True).order_by('last_name', 'first_name')
    
    # ✅ Récupérer le profil employé actuel s'il existe
    current_employee_profile = None
    try:
        current_employee_profile = Employee.objects.get(user=user_to_edit)
    except Employee.DoesNotExist:
        pass
    
    # ✅ Lignes actuellement gérées par cet utilisateur
    selected_lines = list(user_to_edit.managed_lines.values_list('pk', flat=True))
    
    if request.method == 'POST':
        form = UserManagementForm(request.POST, instance=user_to_edit, instance_id=user_id)
        
        # ✅ Récupérer les lignes sélectionnées depuis POST
        selected_line_ids = request.POST.getlist('managed_lines')
        user_type = request.POST.get('user_type', 'team_leader')
        
        # ✅ Validation : chef d'équipe doit avoir au moins une ligne
        if user_type == 'team_leader' and not selected_line_ids:
            form.add_error(None, "Un chef d'équipe doit gérer au moins une ligne de production.")
        
        if form.is_valid() and not form.errors:
            user = form.save()
            
            # ✅ Mettre à jour les permissions selon le type
            if user_type == 'superuser':
                user.is_superuser = True
                user.is_staff = True
                user.managed_lines.clear()  # Super admin n'a pas de lignes spécifiques
            else:
                user.is_superuser = False
                user.is_staff = True
                # Affecter les lignes sélectionnées
                if selected_line_ids:
                    lines = ProductionLine.objects.filter(pk__in=selected_line_ids)
                    user.managed_lines.set(lines)
                else:
                    user.managed_lines.clear()
            
            user.save()
            
            # ✅ Gérer le profil employé
            profile_option = request.POST.get('profile_option', 'none')
            
            if profile_option == 'link':
                # Supprimer l'ancienne liaison si elle existe
                if current_employee_profile:
                    current_employee_profile.user = None
                    current_employee_profile.save()
                
                # Lier à un nouvel employé
                employee_id = request.POST.get('link_to_employee')
                if employee_id:
                    try:
                        employee = Employee.objects.get(pk=employee_id, user__isnull=True)
                        employee.user = user
                        employee.save()
                    except Employee.DoesNotExist:
                        pass
            
            elif profile_option == 'create':
                # Créer un nouveau profil seulement s'il n'en existe pas
                if not current_employee_profile:
                    matricule = request.POST.get('matricule', f'USR-{user.pk}')
                    
                    line = None
                    manager = None
                    if selected_line_ids:
                        line = ProductionLine.objects.filter(pk__in=selected_line_ids).first()
                        if line:
                            other_leaders = line.team_leaders.exclude(pk=user.pk)
                            manager = other_leaders.first() or user
                    
                    Employee.objects.create(
                        user=user,
                        first_name=user.first_name,
                        last_name=user.last_name,
                        matricule=matricule,
                        line=line,
                        manager=manager,
                    )
                else:
                    # Mettre à jour le profil existant
                    current_employee_profile.first_name = user.first_name
                    current_employee_profile.last_name = user.last_name
                    new_matricule = request.POST.get('matricule')
                    if new_matricule:
                        current_employee_profile.matricule = new_matricule
                    current_employee_profile.save()
            
            elif profile_option == 'none':
                # Supprimer la liaison si elle existe
                if current_employee_profile:
                    current_employee_profile.user = None
                    current_employee_profile.save()
            
            messages.success(
                request,
                f"✅ Utilisateur modifié avec succès : {user.get_full_name()}"
            )
            
            return redirect('rh_manage_users')
        
        # Si erreur, garder les lignes sélectionnées
        selected_lines = [int(x) for x in selected_line_ids] if selected_line_ids else []
    else:
        form = UserManagementForm(instance=user_to_edit, instance_id=user_id)
    
    # ✅ Déterminer l'option de profil actuelle
    current_profile_option = 'none'
    if current_employee_profile:
        current_profile_option = 'create'  # Profil existant = option "create" sélectionnée
    
    context = {
        'form': form,
        'title': f'Modifier {user_to_edit.get_full_name() or user_to_edit.username}',
        'submit_text': 'Enregistrer les modifications',
        'user_to_edit': user_to_edit,
        'all_lines': all_lines,  # ✅ AJOUTÉ
        'available_employees': available_employees,  # ✅ AJOUTÉ
        'selected_lines': selected_lines,  # ✅ AJOUTÉ
        'current_employee_profile': current_employee_profile,  # ✅ AJOUTÉ
        'current_profile_option': current_profile_option,  # ✅ AJOUTÉ
    }
    
    return render(request, 'matrix/rh_user_form.html', context)


@login_required

def rh_user_delete(request, user_id):
    """Supprimer un utilisateur"""
    
    user_to_delete = get_object_or_404(User, pk=user_id)
    
    # Empêcher de supprimer le dernier super admin
    if user_to_delete.is_superuser:
        remaining_superusers = User.objects.filter(is_superuser=True).exclude(pk=user_id).count()
        if remaining_superusers == 0:
            messages.error(request, "❌ Impossible de supprimer le dernier super admin !")
            return redirect('rh_manage_users')
    
    if request.method == 'POST':
        username = user_to_delete.username
        full_name = user_to_delete.get_full_name()
        
        user_to_delete.delete()
        
        messages.success(request, f"✅ Utilisateur supprimé : {full_name} ({username})")
        return redirect('rh_manage_users')
    
    context = {
        'user_to_delete': user_to_delete,
    }
    
    return render(request, 'matrix/rh_user_confirm_delete.html', context)


@login_required
@user_passes_test(is_superuser)
def rh_assign_team_leader(request):
    """Formulaire rapide pour affecter des lignes à un chef d'équipe"""
    
    if request.method == 'POST':
        form = QuickTeamLeaderAssignmentForm(request.POST)
        
        if form.is_valid():
            user = form.cleaned_data['user']
            lines = form.cleaned_data['lines']
            
            user.managed_lines.set(lines)
            
            messages.success(
                request,
                f"✅ {user.get_full_name()} affecté à {lines.count()} ligne(s)"
            )
            
            return redirect('rh_manage_users')
    else:
        form = QuickTeamLeaderAssignmentForm()
    
    context = {
        'form': form,
    }
    
    return render(request, 'matrix/rh_assign_team_leader.html', context)


# matrix/views.py  ← AJOUTER À LA FIN

# ═══════════════════════════════════════════════════════════════════
# ESPACE QUALITÉ — LECTURE SEULE
# Utilise uniquement vos modèles existants, zéro modification
# ═══════════════════════════════════════════════════════════════════

from .decorators import quality_required
from .models import QualityProfile

# ──────────────────────────────────────────────────────────────────
# DASHBOARD QUALITÉ
# ──────────────────────────────────────────────────────────────────

@quality_required
def quality_dashboard(request):
    """
    Dashboard principal Qualité.
    """

    # ── KPIs ──────────────────────────────────────────────────────
    total_employees = Employee.objects.count()
    total_lines     = ProductionLine.objects.count()
    total_tasks     = Task.objects.count()

    # ── Répartition des niveaux ───────────────────────────────────
    level_stats = {

        # Niveau I : En formation
        'in_training': TrainingRecord.objects.filter(
            level=TrainingLevel.IN_TRAINING
        ).count(),

        # Niveau L : Approuvé
        'supervised': TrainingRecord.objects.filter(
            level=TrainingLevel.SUPERVISED
        ).count(),

        # Niveau U : Formateur
        'trainer': TrainingRecord.objects.filter(
            level=TrainingLevel.TRAINER
        ).count(),

    }

    # ── Demandes en attente ───────────────────────────────────────
    pending_training  = TrainingApprovalRequest.objects.filter(
        approved__isnull=True
    ).count()

    pending_requalif  = RequalificationApprovalRequest.objects.filter(
        approved__isnull=True
    ).count()

    pending_transfers = LineTransferApprovalRequest.objects.filter(
        approved__isnull=True
    ).count()

    # ── Opérateurs par ligne ──────────────────────────────────────
    # ✅ CORRECTION :
    #   'primary_employees' → employés dont c'est la ligne principale (FK)
    #   'visible_employees' → employés qui voient cette ligne (M2M)
    lines_stats = ProductionLine.objects.annotate(
        nb_employees=Count('primary_employees', distinct=True),  # ✅
        nb_tasks=Count('tasks', distinct=True),
    ).order_by('-nb_employees')

    # ── Activités récentes ────────────────────────────────────────
    recent_transfers = LineTransfer.objects.select_related(
        'employee', 'from_line', 'to_line'
    ).order_by('-transfer_date')[:6]

    recent_requalifs = RequalificationRecord.objects.select_related(
        'employee', 'task', 'line'
    ).order_by('-requalification_date')[:6]

    context = {
        'total_employees':   total_employees,
        'total_lines':       total_lines,
        'total_tasks':       total_tasks,
        'level_stats':       level_stats,
        'pending_training':  pending_training,
        'pending_requalif':  pending_requalif,
        'pending_transfers': pending_transfers,
        'lines_stats':       lines_stats,
        'recent_transfers':  recent_transfers,
        'recent_requalifs':  recent_requalifs,
        'TrainingLevel':     TrainingLevel,
    }
    return render(request, 'matrix/quality_dashboard.html', context)


# ──────────────────────────────────────────────────────────────────
# LISTE DES OPÉRATEURS
# ──────────────────────────────────────────────────────────────────

@quality_required
def quality_operators(request):
    """
    Liste des opérateurs avec filtres.
    Lecture seule — aucune modification possible.
    """

    employees = Employee.objects.select_related(
        'line', 'manager'
    ).order_by('last_name', 'first_name')

    # ── Filtres ───────────────────────────────────────────────────
    search  = request.GET.get('search', '').strip()
    line_id = request.GET.get('line', '').strip()

    if search:
        employees = employees.filter(
            Q(first_name__icontains=search) |
            Q(last_name__icontains=search)  |
            Q(matricule__icontains=search)
        )

    if line_id:
        employees = employees.filter(line_id=line_id)

    # ── Enrichir avec les stats de la matrice ─────────────────────
    employees_data = []
    for emp in employees:
        entries = MatrixEntry.objects.filter(employee=emp)
        nb_tasks = entries.count()

        employees_data.append({
            'employee':      emp,
            'nb_tasks':      nb_tasks,
            'nb_I': entries.filter(level=TrainingLevel.IN_TRAINING).count(),
            'nb_L': entries.filter(level=TrainingLevel.SUPERVISED).count(),
            'nb_U': entries.filter(level=TrainingLevel.TRAINER).count(),
        })

    lines = ProductionLine.objects.all().order_by('name')

    context = {
        'employees_data':  employees_data,
        'lines':           lines,
        'search':          search,
        'selected_line':   line_id,
        'total':           len(employees_data),
    }
    return render(request, 'matrix/quality_operators.html', context)


# ──────────────────────────────────────────────────────────────────
# LISTE DES CHEFS D'ÉQUIPE
# ──────────────────────────────────────────────────────────────────

@quality_required
def quality_team_leaders(request):
    """
    Liste des chefs d'équipe avec leurs lignes et employés.
    Lecture seule.
    """
    from django.contrib.auth.models import User as AuthUser

    # Users qui gèrent au moins une ligne
    team_leaders = AuthUser.objects.filter(
        managed_lines__isnull=False
    ).prefetch_related(
        'managed_lines',
        'managed_employees'
    ).distinct().order_by('last_name', 'first_name')

    leaders_data = []
    for leader in team_leaders:
        lines        = leader.managed_lines.all()
        nb_employees = leader.managed_employees.count()

        # Taux de formateurs parmi ses employés
        emp_ids = leader.managed_employees.values_list('id', flat=True)
        entries = MatrixEntry.objects.filter(employee_id__in=emp_ids)
        nb_entries = entries.count()
        nb_trainer  = entries.filter(level=TrainingLevel.TRAINER).count()
        taux = round((nb_trainer * 100 / nb_entries) if nb_entries > 0 else 0)

        # Demandes en attente soumises par ce chef
        pending = TrainingApprovalRequest.objects.filter(
            requested_by=leader,
            approved__isnull=True
        ).count()

        leaders_data.append({
            'user':         leader,
            'lines':        lines,
            'nb_lines':     lines.count(),
            'nb_employees': nb_employees,
            'taux_trainer': taux,
            'pending':      pending,
        })

    context = {
        'leaders_data': leaders_data,
        'total':        len(leaders_data),
    }
    return render(request, 'matrix/quality_team_leaders.html', context)


# ──────────────────────────────────────────────────────────────────
# DÉTAIL D'UN OPÉRATEUR
# ──────────────────────────────────────────────────────────────────

# matrix/views.py  ← REMPLACER quality_operator_detail


@quality_required
def quality_operator_detail(request, employee_id):
    """
    Fiche complète d'un opérateur avec :

    - Tous les niveaux historiques I, L et U.
    - Une matrice regroupée par tâche.
    - Un historique chronologique des formations.
    - Les requalifications et les transferts.
    - Les demandes en attente.
    - Des statistiques cohérentes avec la matrice affichée.
    """

    # =====================================================
    # 1. RÉCUPÉRATION DE L'OPÉRATEUR
    # =====================================================

    employee = get_object_or_404(
        Employee.objects.select_related(
            'line',
            'manager'
        ),
        pk=employee_id
    )

    # =====================================================
    # 2. MATRICE ACTUELLE
    # =====================================================

    entries = MatrixEntry.objects.filter(
        employee=employee
    ).select_related(
        'task'
    ).order_by(
        'task__group_title',
        'task__order',
        'task__task_number'
    )

    # Regrouper les entrées par groupe de tâches.
    groups = {}

    for entry in entries:

        group = entry.task.group_title or "Général"

        groups.setdefault(
            group,
            []
        ).append(entry)

    # =====================================================
    # 3. RÉCUPÉRER TOUTES LES FORMATIONS
    # =====================================================

    all_training_records = TrainingRecord.objects.filter(
        employee=employee
    ).select_related(
        'task',
        'trainer'
    ).prefetch_related(
        'lines'
    ).order_by(
        'task__group_title',
        'task__order',
        '-date'
    )

    # =====================================================
    # 4. REGROUPER LES FORMATIONS PAR TÂCHE
    # =====================================================

    training_by_task = {}

    for rec in all_training_records:

        task_id = rec.task_id

        # Initialiser une tâche si elle n'existe pas.
        if task_id not in training_by_task:

            training_by_task[task_id] = {

                'task': rec.task,

                'group_title': (
                    rec.task.group_title or 'Général'
                ),

                'records': [],

                'record_I': None,

                'record_L': None,

                'record_U': None,

                'lines': set(),

            }

        data = training_by_task[task_id]

        # Ajouter l'enregistrement à son historique.
        data['records'].append(rec)

        # -------------------------------------------------
        # Garder le dernier enregistrement de chaque niveau.
        #
        # Le QuerySet est trié par date décroissante
        # à l'intérieur de chaque tâche.
        # -------------------------------------------------

        if rec.level == TrainingLevel.IN_TRAINING:

            if data['record_I'] is None:

                data['record_I'] = rec

        elif rec.level == TrainingLevel.SUPERVISED:

            if data['record_L'] is None:

                data['record_L'] = rec

        elif rec.level == TrainingLevel.TRAINER:

            if data['record_U'] is None:

                data['record_U'] = rec

        # -------------------------------------------------
        # Récupérer les lignes liées à la formation.
        # -------------------------------------------------

        for line in rec.lines.all():

            data['lines'].add(line)

    # =====================================================
    # 5. CONSTRUIRE LES GROUPES DE FORMATION
    # =====================================================

    training_groups = {}

    for task_id, data in training_by_task.items():

        group = data['group_title']

        if group not in training_groups:

            training_groups[group] = []

        # Déterminer le niveau le plus élevé enregistré.
        highest = None

        if data['record_U'] is not None:

            highest = 'U'

        elif data['record_L'] is not None:

            highest = 'L'

        elif data['record_I'] is not None:

            highest = 'I'

        # Ajouter la tâche au groupe.
        training_groups[group].append({

            'task': data['task'],

            'record_I': data['record_I'],

            'record_L': data['record_L'],

            'record_U': data['record_U'],

            'lines': sorted(
                data['lines'],
                key=lambda line: line.name
            ),

            'highest': highest,

            'total_records': len(data['records']),

        })

    # =====================================================
    # 6. HISTORIQUE CHRONOLOGIQUE DES FORMATIONS
    # =====================================================

    training_timeline = TrainingRecord.objects.filter(
        employee=employee
    ).select_related(
        'task',
        'trainer'
    ).prefetch_related(
        'lines'
    ).order_by(
        '-date'
    )

    timeline_data = []

    for rec in training_timeline:

        level_label = ''

        level_class = ''

        # Niveau I
        if rec.level == TrainingLevel.IN_TRAINING:

            level_label = 'I'

            level_class = 'warning'

        # Niveau L
        elif rec.level == TrainingLevel.SUPERVISED:

            level_label = 'L'

            level_class = 'success'

        # Niveau U
        elif rec.level == TrainingLevel.TRAINER:

            level_label = 'U'

            level_class = 'primary'

        # Préparer la date.
        date_str = (
            rec.date.strftime('%d/%m/%Y')
            if rec.date
            else '—'
        )

        # Ajouter l'événement.
        timeline_data.append({

            'record': rec,

            'level_label': level_label,

            'level_class': level_class,

            'date_str': date_str,

            'lines': list(rec.lines.all()),

        })

    # =====================================================
    # 7. HISTORIQUE DES REQUALIFICATIONS
    # =====================================================

    # QuerySet complet.
    requalif_queryset = RequalificationRecord.objects.filter(
        employee=employee
    ).select_related(
        'task',
        'line'
    )

    # Nombre TOTAL de requalifications.
    # Important : calculer avant la limitation à 15.
    total_requalifs = requalif_queryset.count()

    # Afficher uniquement les 15 plus récentes.
    requalif_history = requalif_queryset.order_by(
        '-requalification_date'
    )[:15]

    # =====================================================
    # 8. HISTORIQUE DES TRANSFERTS
    # =====================================================

    transfer_history = LineTransfer.objects.filter(
        employee=employee
    ).select_related(
        'from_line',
        'to_line',
        'transferred_by'
    ).order_by(
        '-transfer_date'
    )

    # Nombre total de transferts.
    total_transfers = transfer_history.count()

    # =====================================================
    # 9. DEMANDES DE FORMATION EN ATTENTE
    # =====================================================

    pending_training_req = (
        TrainingApprovalRequest.objects.filter(
            employee=employee,
            approved__isnull=True
        )
        .select_related(
            'task',
            'requested_by'
        )
        .order_by(
            '-date_requested'
        )
    )

    # Nombre de demandes de formation.
    total_pending_training = pending_training_req.count()

    # =====================================================
    # 10. DEMANDES DE REQUALIFICATION EN ATTENTE
    # =====================================================

    pending_requalif_req = (
        RequalificationApprovalRequest.objects.filter(
            employee=employee,
            approved__isnull=True
        )
        .select_related(
            'task'
        )
        .order_by(
            '-date_requested'
        )
    )

    # Nombre de demandes de requalification.
    total_pending_requalif = pending_requalif_req.count()

    # =====================================================
    # 11. CORRECTION DES STATISTIQUES I / L / U
    # =====================================================

    # IMPORTANT :
    #
    # On utilise TrainingRecord via training_by_task.
    #
    # On ne compte plus les niveaux depuis MatrixEntry,
    # puisque le tableau affiche les niveaux historiques.
    #
    # Une même tâche peut avoir :
    # I = 1, L = 1 et U = 1.
    #
    # Chaque niveau est compté une seule fois par tâche.

    # -------------------------------------------------
    # NIVEAU I
    # -------------------------------------------------

    nb_I = sum(
        1
        for data in training_by_task.values()
        if data['record_I'] is not None
    )

    # -------------------------------------------------
    # NIVEAU L
    # -------------------------------------------------

    nb_L = sum(
        1
        for data in training_by_task.values()
        if data['record_L'] is not None
    )

    # -------------------------------------------------
    # NIVEAU U
    # -------------------------------------------------

    nb_U = sum(
        1
        for data in training_by_task.values()
        if data['record_U'] is not None
    )

    # =====================================================
    # 12. CALCUL DES TOTAUX
    # =====================================================

    # Nombre de tâches distinctes possédant
    # au moins un historique de formation.
    total_tasks = len(training_by_task)

    # Nombre total d'enregistrements de formation.
    # Le QuerySet a déjà été évalué dans la boucle.
    total_trainings = len(all_training_records)

    # Somme des niveaux historiques.
    total_levels = nb_I + nb_L + nb_U

    # Nombre total de demandes en attente.
    total_pending = (
        total_pending_training
        + total_pending_requalif
    )

    # =====================================================
    # 13. STATISTIQUES FINALES
    # =====================================================

    stats = {

        # Nombre de tâches avec formation.
        'total_tasks': total_tasks,

        # Compteurs de compétences.
        'nb_I': nb_I,

        'nb_L': nb_L,

        'nb_U': nb_U,

        # Somme des niveaux I + L + U.
        'total_levels': total_levels,

        # Nombre total de formations enregistrées.
        'total_trainings': total_trainings,

        # Nombre total de transferts.
        'nb_transfers': total_transfers,

        # Nombre total de requalifications.
        'nb_requalifs': total_requalifs,

        # Demandes en attente.
        'nb_pending': total_pending,

        # Détail des demandes.
        'nb_pending_training': total_pending_training,

        'nb_pending_requalif': total_pending_requalif,

    }

    # =====================================================
    # 14. CONTEXTE ENVOYÉ AU TEMPLATE
    # =====================================================

    context = {

        # Opérateur.
        'employee': employee,

        # Matrice actuelle.
        'groups': groups,

        # Niveaux historiques par tâche.
        'training_groups': training_groups,

        # Historique chronologique.
        'training_timeline': timeline_data,

        # Requalifications.
        'requalif_history': requalif_history,

        # Transferts.
        'transfer_history': transfer_history,

        # Demandes en attente.
        'pending_training_req': pending_training_req,

        'pending_requalif_req': pending_requalif_req,

        # Statistiques.
        'stats': stats,

        # Enum des niveaux.
        'TrainingLevel': TrainingLevel,

        # Lignes disponibles.
        'all_lines': ProductionLine.objects.all().order_by(
            'name'
        ),

    }

    # =====================================================
    # 15. AFFICHAGE DE LA PAGE
    # =====================================================

    return render(
        request,
        'matrix/quality_operator_detail.html',
        context
    )



@quality_required
def quality_line_detail(request, line_id):
    """
    Affiche la matrice d'une ligne — même logique que matrix_view
    mais en lecture seule pour le service Qualité.
    Compatible avec le template quality_line_detail.html
    """
    from django.db.models import Q

    line  = get_object_or_404(ProductionLine, id=line_id)
    today = timezone.now().date()

    # ── Chefs d'équipe de cette ligne ─────────────────────────────
    team_leaders = line.team_leaders.all()

    # ── Tâches de cette ligne ─────────────────────────────────────
    tasks = Task.objects.filter(
        lines=line
    ).distinct().order_by("order")

    # ── IDs des chefs d'équipe ────────────────────────────────────
    team_leader_user_ids = User.objects.filter(
        managed_lines__isnull=False
    ).distinct().values_list('id', flat=True)

    team_leader_emp_ids = set(
        Employee.objects.filter(
            user_id__in=team_leader_user_ids
        ).values_list('id', flat=True)
    )

    # ── Helper : trouver l'Employee d'un chef ─────────────────────
    def get_leader_employee(leader):
        try:
            if hasattr(leader, 'employee_profile'):
                return leader.employee_profile
        except Exception:
            pass
        return Employee.objects.filter(
            Q(first_name__iexact=leader.first_name,
              last_name__iexact=leader.last_name) |
            Q(matricule__iexact=leader.username)
        ).first()

    # ── Helper : construire training_dict ─────────────────────────
    def build_training_dict(employee_ids):
        records = TrainingRecord.objects.filter(
            employee_id__in=employee_ids,
            task__in=tasks,
            lines=line
        ).select_related('employee', 'task', 'trainer').distinct()

        td = {}
        for rec in records:
            key = (rec.employee_id, rec.task_id, rec.level)
            if key not in td:
                td[key] = rec
            else:
                existing = td[key].date
                new      = rec.date
                if new and existing and new > existing:
                    td[key] = rec
                elif new and not existing:
                    td[key] = rec
        return td

    # ── Helper : construire approval_dict ─────────────────────────
    def build_approval_dict(employee_ids):
        req_on_line = TrainingApprovalRequest.objects.filter(
            employee_id__in=employee_ids,
            task__in=tasks,
            approved__isnull=True,
            line=line
        ).select_related('employee', 'task', 'line')

        req_fallback = TrainingApprovalRequest.objects.filter(
            employee_id__in=employee_ids,
            task__in=tasks,
            approved__isnull=True,
            line__isnull=True,
            employee__line=line
        ).select_related('employee', 'task')

        ad = {}
        for req in list(req_on_line) + list(req_fallback):
            key = (req.employee_id, req.task_id, req.requested_level)
            if key not in ad:
                ad[key] = req
            elif req.date_requested > ad[key].date_requested:
                ad[key] = req
        return ad

    # ── Helper : construire les cellules ──────────────────────────
    def build_cell(emp, task, training_dict, approval_dict,
                   is_team_leader_emp):
        rec_I = training_dict.get((emp.id, task.id, TrainingLevel.IN_TRAINING))
        rec_L = training_dict.get((emp.id, task.id, TrainingLevel.SUPERVISED))
        rec_U = training_dict.get((emp.id, task.id, TrainingLevel.TRAINER))

        pending_L = approval_dict.get(
            (emp.id, task.id, TrainingLevel.SUPERVISED)
        )
        pending_U = approval_dict.get(
            (emp.id, task.id, TrainingLevel.TRAINER)
        )

        # Niveau le plus élevé
        highest = None
        if rec_U:   highest = 3
        elif rec_L: highest = 1
        elif rec_I: highest = 0

        team_leader_name = None
        if emp.manager:
            team_leader_name = (
                f"{emp.manager.first_name} {emp.manager.last_name}".strip()
                or emp.manager.username
            )

        emp_visible_lines = emp.visible_lines.exclude(id=line.id)

        return {
            'employee':         emp,
            'is_team_leader':   is_team_leader_emp,
            'team_leader':      emp.manager,
            'team_leader_name': team_leader_name,
            'other_lines':      emp_visible_lines,
            'is_multiline':     emp_visible_lines.exists(),
            'highest_level':    highest,

            # Niveaux pour matrix_view (cell.I, cell.L, cell.U)
            'I': rec_I,
            'L': rec_L,
            'U': rec_U,

            # Niveaux pour le template qualité (cell.has_I, cell.date_I ...)
            'has_I':  rec_I is not None,
            'date_I': rec_I.date.strftime('%d/%m/%y') if rec_I and rec_I.date else '',
            'has_L':  rec_L is not None,
            'date_L': rec_L.date.strftime('%d/%m/%y') if rec_L and rec_L.date else '',
            'has_U':  rec_U is not None,
            'date_U': rec_U.date.strftime('%d/%m/%y') if rec_U and rec_U.date else '',

            # Pending
            'pending': {1: pending_L, 3: pending_U},
        }

    # ── Helper : construire les lignes de la matrice ──────────────
    def build_matrix_rows(employees_list, training_dict,
                          approval_dict, leader_employee):
        rows          = []
        current_group = None

        for task in tasks:
            group_changed = (task.group_title != current_group)
            if group_changed:
                current_group = task.group_title

            cells = []
            for emp in employees_list:
                is_tl = (
                    leader_employee and
                    emp.id == leader_employee.id
                )
                cell = build_cell(
                    emp, task, training_dict, approval_dict, is_tl
                )
                cells.append(cell)

            rows.append({
                'task':          task,
                'cells':         cells,
                'group_title':   task.group_title or '',
                'group_changed': group_changed,
            })

        return rows

    # ── Helper : alertes de développement ────────────────────────
    def build_alerts(employees_list, training_dict,
                     approval_dict, leader_employee):
        THRESHOLD_I_TO_L = 7
        THRESHOLD_L_TO_U = 21
        alerts_list      = []

        for emp in employees_list:
            is_leader = (
                leader_employee and emp.id == leader_employee.id
            )
            if is_leader:
                continue

            emp_alerts = []

            for task in tasks:
                rec_I = training_dict.get(
                    (emp.id, task.id, TrainingLevel.IN_TRAINING)
                )
                rec_L = training_dict.get(
                    (emp.id, task.id, TrainingLevel.SUPERVISED)
                )
                rec_U = training_dict.get(
                    (emp.id, task.id, TrainingLevel.TRAINER)
                )

                if not rec_I and not rec_L and not rec_U:
                    continue

                has_pending_L = approval_dict.get(
                    (emp.id, task.id, TrainingLevel.SUPERVISED)
                )
                has_pending_U = approval_dict.get(
                    (emp.id, task.id, TrainingLevel.TRAINER)
                )

                # I → L
                if rec_I and not rec_L and not has_pending_L and rec_I.date:
                    days = (today - rec_I.date).days
                    if days >= THRESHOLD_I_TO_L:
                        urgency = (
                            'critical' if days > 30 else
                            'warning'  if days > 14 else
                            'info'
                        )
                        emp_alerts.append({
                            'task':          task,
                            'current_level': 'I',
                            'next_level':    'L',
                            'days_since':    days,
                            'threshold':     THRESHOLD_I_TO_L,
                            'days_overdue':  days - THRESHOLD_I_TO_L,
                            'urgency':       urgency,
                            'training_date': rec_I.date,
                            'message': (
                                f"Niveau I depuis {days} jours → Prêt pour L"
                            ),
                        })

                # L → U
                if rec_L and not rec_U and not has_pending_U and rec_L.date:
                    days = (today - rec_L.date).days
                    if days >= THRESHOLD_L_TO_U:
                        urgency = (
                            'critical' if days > 60 else
                            'warning'  if days > 30 else
                            'info'
                        )
                        emp_alerts.append({
                            'task':          task,
                            'current_level': 'L',
                            'next_level':    'U',
                            'days_since':    days,
                            'threshold':     THRESHOLD_L_TO_U,
                            'days_overdue':  days - THRESHOLD_L_TO_U,
                            'urgency':       urgency,
                            'training_date': rec_L.date,
                            'message': (
                                f"Niveau L depuis {days} jours → Prêt pour U"
                            ),
                        })

            if emp_alerts:
                urgency_order = {'critical': 0, 'warning': 1, 'info': 2}
                emp_alerts.sort(key=lambda x: (
                    urgency_order.get(x['urgency'], 3),
                    -x['days_since']
                ))
                alerts_list.append({
                    'employee':       emp,
                    'alerts':         emp_alerts,
                    'total_alerts':   len(emp_alerts),
                    'critical_count': len([
                        a for a in emp_alerts if a['urgency'] == 'critical'
                    ]),
                    'warning_count':  len([
                        a for a in emp_alerts if a['urgency'] == 'warning'
                    ]),
                    'ready_count':    len([
                        a for a in emp_alerts if a['urgency'] == 'info'
                    ]),
                    'max_urgency':    emp_alerts[0]['urgency'],
                })

        alerts_list.sort(key=lambda x: (
            {'critical': 0, 'warning': 1, 'info': 2}.get(
                x['max_urgency'], 3
            ),
            -x['total_alerts']
        ))
        return alerts_list

    # ══════════════════════════════════════════════════════════════
    # MATRICE PAR CHEF D'ÉQUIPE
    # ══════════════════════════════════════════════════════════════
    matrices_by_leader    = []
    team_leader_employees = {}   # {leader.id: Employee}

    for leader in team_leaders:
        leader_employee = get_leader_employee(leader)
        if leader_employee:
            team_leader_employees[leader.id] = leader_employee

        # Même filtre que matrix_view
        line_employees = Employee.objects.filter(
            visible_lines=line,
            manager=leader
        ).exclude(
            id__in=team_leader_emp_ids
        ).select_related('manager', 'line').order_by(
            "last_name", "first_name"
        )

        employees_list = list(line_employees)

        if leader_employee:
            if leader_employee.id not in [e.id for e in employees_list]:
                employees_list.insert(0, leader_employee)

        employee_ids  = [e.id for e in employees_list]
        training_dict = build_training_dict(employee_ids)
        approval_dict = build_approval_dict(employee_ids)

        # ✅ matrix_rows (nom attendu par le template)
        matrix_rows = build_matrix_rows(
            employees_list, training_dict,
            approval_dict, leader_employee
        )

        development_alerts = build_alerts(
            employees_list, training_dict,
            approval_dict, leader_employee
        )

        # Taux de complétion
        total_cells  = len(employees_list) * tasks.count() * 3
        filled_cells = sum(
            1
            for task in tasks
            for emp in employees_list
            for level in [
                TrainingLevel.IN_TRAINING,
                TrainingLevel.SUPERVISED,
                TrainingLevel.TRAINER,
            ]
            if training_dict.get((emp.id, task.id, level))
        )
        completion_rate = (
            round(filled_cells / total_cells * 100, 1)
            if total_cells > 0 else 0
        )

        total_alerts   = sum(a['total_alerts']   for a in development_alerts)
        total_critical = sum(a['critical_count'] for a in development_alerts)
        total_warning  = sum(a['warning_count']  for a in development_alerts)
        total_ready    = sum(a['ready_count']    for a in development_alerts)

        operator_count = len([
            e for e in employees_list
            if not (leader_employee and e.id == leader_employee.id)
        ])

        matrices_by_leader.append({
            'leader':             leader,
            'leader_name':        leader.get_full_name() or leader.username,
            'leader_employee':    leader_employee,
            'employees':          employees_list,
            'matrix_rows':        matrix_rows,      # ✅ nom correct pour template
            'development_alerts': development_alerts,
            'total_alerts':       total_alerts,
            'total_critical':     total_critical,
            'total_warning':      total_warning,
            'total_ready':        total_ready,
            'completion_rate':    completion_rate,
            'operator_count':     operator_count,
        })

    # ══════════════════════════════════════════════════════════════
    # VUE GLOBALE (onglet "Vue Globale" du template)
    # Tous les employés de toutes les équipes
    # ══════════════════════════════════════════════════════════════
    all_employees = []
    seen_ids      = set()

    for item in matrices_by_leader:
        for emp in item['employees']:
            if emp.id not in seen_ids:
                all_employees.append(emp)
                seen_ids.add(emp.id)

    all_employee_ids    = [e.id for e in all_employees]
    global_training     = build_training_dict(all_employee_ids)
    global_approval     = build_approval_dict(all_employee_ids)

    # leader_employee de référence pour la vue globale (premier chef)
    first_leader_emp = None
    if matrices_by_leader:
        first_leader_emp = matrices_by_leader[0].get('leader_employee')

    global_matrix_rows = build_matrix_rows(
        all_employees, global_training,
        global_approval, first_leader_emp
    )

    # ── Stats globales ─────────────────────────────────────────────
    total_operators = sum(
        m['operator_count'] for m in matrices_by_leader
    )
    global_total_cells  = len(all_employees) * tasks.count() * 3
    global_filled_cells = sum(
        1
        for task in tasks
        for emp in all_employees
        for level in [
            TrainingLevel.IN_TRAINING,
            TrainingLevel.SUPERVISED,
            TrainingLevel.TRAINER,
        ]
        if global_training.get((emp.id, task.id, level))
    )
    global_rate = (
        round(global_filled_cells / global_total_cells * 100, 1)
        if global_total_cells > 0 else 0
    )

    context = {
        # ── Données de base ────────────────────────────────────────
        "line":                  line,
        "team_leaders":          team_leaders,
        "tasks":                 tasks,
        "today":                 today,

        # ── Vue globale (onglet 1 du template) ────────────────────
        "employees":             all_employees,        # ✅ pour le template
        "global_matrix_rows":    global_matrix_rows,   # ✅
        "team_leader_employees": team_leader_employees, # ✅ {leader.id: Employee}

        # ── Vue par chef (onglets suivants) ───────────────────────
        "matrices_by_leader":    matrices_by_leader,   # ✅

        # ── Infos complémentaires ─────────────────────────────────
        "has_multiple_leaders":  team_leaders.count() > 1,
        "all_lines":             ProductionLine.objects.all().order_by('name'),
        "is_readonly":           True,

        # ── Stats ──────────────────────────────────────────────────
        "stats": {
            "total_employees":  len(all_employees),
            "total_operators":  total_operators,
            "total_leaders":    team_leaders.count(),
            "total_tasks":      tasks.count(),
            "completion_rate":  global_rate,
            "total_alerts":     sum(
                m['total_alerts'] for m in matrices_by_leader
            ),
            "total_critical":   sum(
                m['total_critical'] for m in matrices_by_leader
            ),
            "total_warning":    sum(
                m['total_warning'] for m in matrices_by_leader
            ),
        },
    }

    return render(request, "matrix/quality_line_detail.html", context)



from django.http import JsonResponse
from django.db.models import Q
from .models import TrainingQuestion, TrainingLevel


def get_questionnaire_by_task(request):
    """
    Retourne les questions RH actives pour une tâche + une ligne + un niveau spécifique.
    """
    task_id = request.GET.get("task_id")
    line_id = request.GET.get("line_id")
    level_id = request.GET.get("level")  # ✅ Récupération dynamique du niveau envoyé en paramètre

    try:
        task_id = int(task_id)
        line_id = int(line_id)
        level_id = int(level_id)  # ✅ Conversion en entier (0 pour I, 1 pour L, 3 pour U)
    except (TypeError, ValueError):
        return JsonResponse({"questions": []})

    # Filtrage dynamique par niveau
    questions = (
        TrainingQuestion.objects.filter(
            is_active=True,
            level=level_id,  # ✅ Filtre appliqué dynamiquement
        )
        .filter(Q(task__isnull=True) | Q(task_id=task_id))
        .filter(Q(line__isnull=True) | Q(line_id=line_id))
        .order_by("order", "id")
    )

    data = [
        {
            "id": q.pk,
            "text": q.text,
            "help_text": q.help_text or "",
            "answer_type": q.answer_type,   # "YES_NO" ou "TEXT"
            "is_required": q.is_required,
            "is_blocking": q.is_blocking,
            "requires_comment_if_no": q.requires_comment_if_no,
        }
        for q in questions
    ]

    return JsonResponse({"questions": data})
