from asyncio import Task
from datetime import date, timedelta
from django import forms

from training_matrix import settings
from .models import Employee, PendingEmployee, ProductionLine, TrainingApprovalRequest, TrainingLevel, TrainingQuestionAnswer
from .models import TrainingRecord
import datetime
from django.utils import timezone

from matrix import models  
from django.db.models import Q
from .models import Task, Employee, ProductionLine, MatrixEntry
from django.core.mail import send_mail
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from .models import Task as TaskModel, ProductionLine as LineModel, Employee

from django.contrib.auth.models import User

class TrainingPlanImageForm(forms.Form):
    """Only the plan is editable; training creation remains independent."""

    training_plan_image = forms.ImageField(
        label="Image du plan de formation",
        help_text="JPEG, PNG ou WEBP — 10 Mo maximum, 25 millions de pixels maximum.",
        widget=forms.FileInput(attrs={
            "accept": "image/jpeg,image/png,image/webp",
            "class": "block w-full rounded-lg border border-gray-300 p-3 text-sm",
        }),
        error_messages={
            "required": "Sélectionnez une image du plan de formation.",
            "invalid_image": "Le fichier doit être une image JPEG, PNG ou WEBP valide.",
        },
    )

    def clean_training_plan_image(self):
        uploaded = self.cleaned_data["training_plan_image"]
        if uploaded.size > 10 * 1024 * 1024:
            raise ValidationError("L’image ne doit pas dépasser 10 Mo.")
        extensions = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
        extension = extensions.get(uploaded.image.format)
        if not extension:
            raise ValidationError("Formats acceptés : JPEG, PNG et WEBP.")
        width, height = uploaded.image.size
        if width * height > 25_000_000:
            raise ValidationError("L’image ne doit pas dépasser 25 millions de pixels.")
        # Use the format verified by Pillow, never the client-provided extension.
        uploaded.name = "plan" + extension
        return uploaded


# forms.py

# ══════════════════════════════════════════════════════════════════════
# Formulaire RH — Pré-enregistrement
# ══════════════════════════════════════════════════════════════════════
class EmployeeRHRegisterForm(forms.Form):
    """
    Formulaire RH — stocke dans PendingEmployee, pas Employee.
    """
    matricule  = forms.CharField(
        max_length=50,
        label="Matricule",
        widget=forms.TextInput(attrs={
            'class':       'form-control',
            'placeholder': 'Ex: EMP001',
        })
    )
    first_name = forms.CharField(
        max_length=100,
        label="Prénom",
        widget=forms.TextInput(attrs={
            'class':       'form-control',
            'placeholder': 'Prénom',
        })
    )
    last_name  = forms.CharField(
        max_length=100,
        label="Nom",
        widget=forms.TextInput(attrs={
            'class':       'form-control',
            'placeholder': 'Nom',
        })
    )
    hire_date  = forms.DateField(
        required=False,
        label="Date d'embauche",
        widget=forms.DateInput(attrs={
            'type':  'date',
            'class': 'form-control',
        })
    )

    def clean_matricule(self):
        matricule = self.cleaned_data.get(
            'matricule', ''
        ).strip().upper()

        # ✅ Vérifier dans PendingEmployee ET Employee
        if PendingEmployee.objects.filter(matricule=matricule).exists():
            raise forms.ValidationError(
                f"❌ '{matricule}' est déjà pré-enregistré "
                f"(en attente d'un chef d'équipe)."
            )
        if Employee.objects.filter(matricule=matricule).exists():
            raise forms.ValidationError(
                f"❌ '{matricule}' est déjà un opérateur actif."
            )
        return matricule


# ══════════════════════════════════════════════════════════════════════
# Formulaire Chef d'équipe — Compléter via matricule
# ══════════════════════════════════════════════════════════════════════
class EmployeeForm(forms.ModelForm):

    matricule = forms.CharField(
        max_length=50,
        label="Matricule",
        widget=forms.TextInput(attrs={
            'class':        'form-control',
            'placeholder':  'Ex: EMP001',
            'id':           'id_matricule_input',
            'autocomplete': 'off',
        }),
    )

    visible_lines = forms.ModelMultipleChoiceField(
        queryset=ProductionLine.objects.none(),
        required=True,
        label="Afficher dans les matrices de",
        widget=forms.CheckboxSelectMultiple()
    )

    class Meta:
        model  = Employee
        fields = [
            'matricule', 'first_name', 'last_name',
            'hire_date', 'line', 'visible_lines'
        ]
        widgets = {
            'first_name': forms.TextInput(attrs={
                'class':    'form-control',
                'id':       'id_first_name',
                'readonly': True,
            }),
            'last_name': forms.TextInput(attrs={
                'class':    'form-control',
                'id':       'id_last_name',
                'readonly': True,
            }),
            'hire_date': forms.DateInput(attrs={
                'type':     'date',
                'class':    'form-control',
                'id':       'id_hire_date',
                'readonly': True,
            }),
            'line': forms.Select(attrs={
                'class': 'form-control',
                'id':    'id_line_main',
            }),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user             = user
        self._found_pending   = None  # ← PendingEmployee trouvé

        if user and not user.is_superuser:
            managed_lines = ProductionLine.objects.filter(
                team_leaders=user
            )
            self.fields['line'].queryset    = managed_lines
            self.fields['line'].required    = True
            self.fields['line'].empty_label = "-- Ligne principale --"
            self.fields['visible_lines'].queryset = managed_lines

            if self.instance and self.instance.pk:
                self.initial['visible_lines'] = (
                    self.instance.visible_lines.filter(
                        team_leaders=user
                    )
                )
            else:
                self.initial['visible_lines'] = managed_lines
        else:
            all_lines = ProductionLine.objects.all()
            self.fields['line'].queryset          = all_lines
            self.fields['visible_lines'].queryset = all_lines
            self.fields['line'].empty_label = (
                "-- Sélectionnez une ligne --"
            )

    def validate_unique(self):
        """
        ✅ Ne pas vérifier l'unicité du matricule ici.
        On travaille sur un PendingEmployee existant,
        pas sur un nouvel Employee.
        """
        pass

    def clean_matricule(self):
        matricule = self.cleaned_data.get(
            'matricule', ''
        ).strip().upper()

        if not matricule:
            raise forms.ValidationError(
                "❌ Le matricule est obligatoire."
            )

        # ✅ Chercher dans PendingEmployee
        try:
            pending = PendingEmployee.objects.get(matricule=matricule)
            # ✅ Stocker pour save()
            self._found_pending = pending
            return matricule

        except PendingEmployee.DoesNotExist:
            pass

        # ✅ Vérifier si déjà un opérateur actif
        try:
            existing_emp = Employee.objects.get(matricule=matricule)

            # Déjà assigné à un autre chef
            if (existing_emp.manager and
                    existing_emp.manager != self.user):
                raise forms.ValidationError(
                    f"❌ '{matricule}' est déjà assigné à "
                    f"{existing_emp.manager.get_full_name() or existing_emp.manager.username}."
                )

            # Déjà assigné à ce même chef
            if existing_emp.manager == self.user:
                raise forms.ValidationError(
                    f"❌ '{matricule}' est déjà dans votre équipe."
                )

            # Existe mais sans manager (cas rare)
            self._found_pending = None
            return matricule

        except Employee.DoesNotExist:
            pass

        # ✅ Introuvable nulle part
        raise forms.ValidationError(
            f"❌ Aucun employé trouvé avec le matricule '{matricule}'. "
            f"Contactez RH pour l'enregistrer d'abord."
        )

    def clean(self):
        cleaned_data  = super().clean()
        line          = cleaned_data.get('line')
        visible_lines = cleaned_data.get('visible_lines')

        if not visible_lines:
            raise forms.ValidationError(
                "Veuillez sélectionner au moins une ligne."
            )

        # ✅ S'assurer que la ligne principale est dans visible_lines
        if line and line not in visible_lines:
            lines_list = list(visible_lines)
            lines_list.append(line)
            cleaned_data['visible_lines'] = lines_list

        return cleaned_data

    def save(self, commit=True):
        """
        ✅ Convertit PendingEmployee en Employee (opérateur actif).
        ✅ Supprime le PendingEmployee après conversion.
        ✅ Ne crée PAS de doublon.
        """
        pending = self._found_pending

        if pending:
            # ✅ Créer l'Employee depuis le PendingEmployee
            instance, created = Employee.objects.get_or_create(
                matricule=pending.matricule,
                defaults={
                    'first_name': pending.first_name,
                    'last_name':  pending.last_name,
                    'hire_date':  pending.hire_date,
                }
            )
        else:
            # Fallback : employé existant sans manager
            matricule = self.cleaned_data.get('matricule', '')
            try:
                instance = Employee.objects.get(matricule=matricule)
            except Employee.DoesNotExist:
                instance = super().save(commit=False)

        # ✅ Assigner le chef d'équipe
        instance.manager = self.user

        # ✅ Assigner la ligne principale
        instance.line = self.cleaned_data.get('line')

        if commit:
            instance.save()

            # ✅ Assigner les visible_lines
            visible_lines = self.cleaned_data.get('visible_lines', [])
            main_line     = self.cleaned_data.get('line')
            lines_set     = set(visible_lines)
            if main_line:
                lines_set.add(main_line)
            instance.visible_lines.set(lines_set)

            # ✅ Supprimer le PendingEmployee
            # → il est maintenant un opérateur actif
            if pending:
                pending.delete()

        return instance


import openpyxl
from django import forms
from .models import Employee


class EmployeeExcelImportForm(forms.Form):
    """
    Formulaire pour importer plusieurs employés via un fichier Excel.
    Colonnes attendues : matricule | prenom | nom | date_embauche (optionnel)
    """
    fichier_excel = forms.FileField(
        label="Fichier Excel (.xlsx)",
        widget=forms.FileInput(attrs={
            'class':  'form-control',
            'accept': '.xlsx,.xls',
        }),
        help_text=(
            "Colonnes requises : matricule, prenom, nom. "
            "Colonne optionnelle : date_embauche (format JJ/MM/AAAA)"
        )
    )

    def clean_fichier_excel(self):
        fichier = self.cleaned_data.get('fichier_excel')

        if not fichier:
            raise forms.ValidationError("❌ Aucun fichier sélectionné.")

        # Vérifier l'extension
        name = fichier.name.lower()
        if not (name.endswith('.xlsx') or name.endswith('.xls')):
            raise forms.ValidationError(
                "❌ Format invalide. Utilisez un fichier .xlsx ou .xls"
            )

        # Vérifier la taille (max 5 Mo)
        if fichier.size > 5 * 1024 * 1024:
            raise forms.ValidationError(
                "❌ Fichier trop volumineux (max 5 Mo)."
            )

        return fichier

    def parse_excel(self):
        from datetime import date as date_type
        import datetime

        fichier = self.cleaned_data['fichier_excel']

        try:
            wb = openpyxl.load_workbook(fichier, data_only=True)
            ws = wb.active
        except Exception as e:
            raise forms.ValidationError(f"❌ Impossible de lire le fichier : {e}")

        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise forms.ValidationError("❌ Le fichier est vide.")

        # ── Détecter les colonnes via l'en-tête ──────────────────────────
        header_raw = [
            str(c).strip().lower().replace(' ', '_') if c is not None else ''
            for c in rows[0]
        ]

        COL_ALIASES = {
            'matricule':  ['matricule', 'id', 'mat', 'identifiant'],
            'first_name': ['prenom', 'prénom', 'first_name', 'firstname'],
            'last_name':  ['nom', 'last_name', 'lastname', 'family_name'],
            'hire_date':  ['date_embauche', "date_d'embauche", 'hire_date',
                        'embauche', 'date_entrée', 'date_entree'],
        }

        col_index = {}
        for field, aliases in COL_ALIASES.items():
            for i, h in enumerate(header_raw):
                if h in aliases:
                    col_index[field] = i
                    break

        missing = [
            f for f in ['matricule', 'first_name', 'last_name']
            if f not in col_index
        ]
        if missing:
            raise forms.ValidationError(
                f"❌ Colonnes manquantes : {', '.join(missing)}. "
                f"En-têtes trouvés : {', '.join(header_raw)}"
            )

        # ── Parser les lignes ─────────────────────────────────────────────
        employees_data = []
        errors = []

        for row_num, row in enumerate(rows[1:], start=2):

            if all(c is None or str(c).strip() == '' for c in row):
                continue

            # ── Helpers ──────────────────────────────────────────────────
            def get_col(field):
                """Valeur en string (pour texte)"""
                idx = col_index.get(field)
                if idx is None or idx >= len(row):
                    return None
                val = row[idx]
                return str(val).strip() if val is not None else None

            def get_col_raw(field):
                """Valeur brute sans conversion (pour les dates)"""
                idx = col_index.get(field)
                if idx is None or idx >= len(row):
                    return None
                return row[idx]

            # ── Récupération des champs texte ─────────────────────────────
            matricule  = get_col('matricule')
            first_name = get_col('first_name')
            last_name  = get_col('last_name')

            # ── Validations ───────────────────────────────────────────────
            row_errors = []
            if not matricule:
                row_errors.append("Matricule manquant")
            else:
                matricule = matricule.upper()

            if not first_name:
                row_errors.append("Prénom manquant")
            if not last_name:
                row_errors.append("Nom manquant")

            if row_errors:
                errors.append(f"Ligne {row_num} : {', '.join(row_errors)}")
                continue

            # ── Parsing de la date (valeur brute préservée) ───────────────
            hire_date_raw = get_col_raw('hire_date')
            hire_date = None

            if hire_date_raw is not None and str(hire_date_raw).strip() != '':

                if isinstance(hire_date_raw, datetime.datetime):
                    # openpyxl lit souvent les dates Excel comme datetime
                    hire_date = hire_date_raw.date()

                elif isinstance(hire_date_raw, date_type):
                    hire_date = hire_date_raw

                elif isinstance(hire_date_raw, str):
                    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y',
                                '%d/%m/%y', '%Y/%m/%d'):
                        try:
                            hire_date = datetime.datetime.strptime(
                                hire_date_raw.strip(), fmt
                            ).date()
                            break
                        except ValueError:
                            continue

                    if hire_date is None:
                        errors.append(
                            f"Ligne {row_num} : Date '{hire_date_raw}' non reconnue "
                            f"— importé sans date."
                        )

                elif isinstance(hire_date_raw, (int, float)):
                    # Excel stocke parfois les dates comme numéros de série
                    try:
                        from openpyxl.utils.datetime import from_excel
                        converted = from_excel(hire_date_raw)
                        hire_date = (
                            converted.date()
                            if isinstance(converted, datetime.datetime)
                            else converted
                        )
                    except Exception:
                        errors.append(
                            f"Ligne {row_num} : Date numérique '{hire_date_raw}' "
                            f"non convertible — importé sans date."
                        )

            employees_data.append({
                'matricule':  matricule,
                'first_name': first_name.capitalize(),
                'last_name':  last_name.upper(),
                'hire_date':  hire_date,
                'row_num':    row_num,
            })

        return employees_data, errors


import os
from django import forms
from django.db import transaction
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils import timezone
from django.conf import settings
from django.core.exceptions import ValidationError
from email.mime.image import MIMEImage

from .models import (
    TrainingRecord, Employee, ProductionLine, Task,
    TrainingApprovalRequest, TrainingLevel
)


from datetime import timedelta
import logging

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db.models import Q
from django.utils import timezone

from .models import (
    Employee,
    ProductionLine,
    Task,
    TrainingApprovalRequest,
    TrainingLevel,
    TrainingQuestion,
    TrainingQuestionnaire,
    TrainingRecord,
)


User = get_user_model()
logger = logging.getLogger(__name__)


class TrainingRecordForm(forms.ModelForm):
    """
    Formulaire utilisé par les chefs d'équipe pour enregistrer
    une formation I, L ou U.

    Pour TOUS les niveaux (I, L, U) :
    - Affiche dynamiquement les questions configurées par RH pour le niveau choisi.
    - Vérifie les réponses obligatoires et bloque l'enregistrement si une réponse bloquante est incorrecte.

    Pour les niveaux L et U :
    - Crée un TrainingRecord en statut 'PENDING' pour attacher le questionnaire.
    - Crée une demande d'approbation RH (TrainingApprovalRequest).
    """

    # Mettre False si tu souhaites autoriser un niveau I
    # même lorsqu'aucune question RH n'est configurée.
    REQUIRE_QUESTIONNAIRE_FOR_LEVEL_I = True

    line = forms.ModelChoiceField(
        queryset=ProductionLine.objects.none(),
        required=False,
        label="Ligne de production",
        help_text="Sélectionnez la ligne sur laquelle cette formation est réalisée.",
        widget=forms.Select(attrs={"id": "id_line", "class": "form-select"}),
    )

    date = forms.DateField(
        required=True,
        label="Date de formation",
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        help_text="Sélectionnez la date à laquelle la formation a eu lieu.",
    )

    class Meta:
        model = TrainingRecord
        fields = ["employee", "task", "level", "date", "trainer"]
        labels = {
            "employee": "Employé",
            "task": "Tâche",
            "level": "Niveau",
            "date": "Date de formation",
            "trainer": "Formateur",
        }
        widgets = {
            "employee": forms.Select(attrs={"id": "id_employee", "class": "form-select"}),
            "task": forms.Select(attrs={"id": "id_task", "class": "form-select"}),
            "level": forms.Select(attrs={"id": "id_level", "class": "form-select"}),
            "trainer": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        self.selected_line_id = kwargs.pop("selected_line_id", None)

        self.rh_request_created = False
        self.rh_request_info = None
        self.team_leader_employee = None
        self.user_lines = ProductionLine.objects.none()
        self.show_line_field = False

        self.training_questions = []

        super().__init__(*args, **kwargs)

        today = timezone.now().date()
        self.fields["date"].widget.attrs["max"] = today.isoformat()

        if not self.is_bound and not self.instance.pk:
            self.fields["date"].initial = today

        self._configure_user_fields()
        self._load_questionnaire_fields()

    # ================================================================
    # CONFIGURATION DU FORMULAIRE ET UTILS PROFIL
    # ================================================================

    def _configure_user_fields(self):
        if not self.user:
            self.fields["employee"].queryset = Employee.objects.all().order_by("last_name", "first_name")
            self.fields["line"].queryset = ProductionLine.objects.all().order_by("code")
            self.fields["task"].queryset = Task.objects.all().order_by("order", "task_number")
            self.fields["trainer"].queryset = Employee.objects.all().order_by("last_name", "first_name")
            self.fields["line"].required = True
            self.show_line_field = True
            return

        self.user_lines = ProductionLine.objects.filter(team_leaders=self.user).order_by("code")
        self.team_leader_employee = self._find_employee_profile_for_user(self.user)

        if not self.user.is_superuser:
            team_leader_employee_ids = self._get_all_team_leader_employee_ids()

            employees_queryset = (
                Employee.objects.filter(
                    visible_lines__in=self.user_lines,
                    manager=self.user,
                )
                .exclude(id__in=team_leader_employee_ids)
                .distinct()
                .order_by("last_name", "first_name")
            )

            self.fields["employee"].queryset = employees_queryset
            self.fields["line"].queryset = self.user_lines
            self.fields["line"].required = True
            self.show_line_field = True

            if self.selected_line_id:
                try:
                    self.fields["line"].initial = int(self.selected_line_id)
                except (ValueError, TypeError):
                    pass

            selected_line_id = self._get_selected_field_value("line") or self.selected_line_id

            if selected_line_id:
                self.fields["task"].queryset = (
                    Task.objects.filter(lines__id=selected_line_id)
                    .distinct()
                    .order_by("order", "task_number")
                )
            else:
                self.fields["task"].queryset = (
                    Task.objects.filter(lines__in=self.user_lines)
                    .distinct()
                    .order_by("order", "task_number")
                )

            self.fields["trainer"].required = False
            self.fields["trainer"].widget = forms.HiddenInput()
            self.fields["trainer"].queryset = Employee.objects.none()

        else:
            self.fields["line"].queryset = ProductionLine.objects.all().order_by("code")
            self.fields["line"].required = True
            self.fields["employee"].queryset = Employee.objects.all().order_by("last_name", "first_name")

            selected_line_id = self._get_selected_field_value("line") or self.selected_line_id

            if selected_line_id:
                self.fields["task"].queryset = (
                    Task.objects.filter(lines__id=selected_line_id)
                    .distinct()
                    .order_by("order", "task_number")
                )
            else:
                self.fields["task"].queryset = Task.objects.all().order_by("order", "task_number")

            self.fields["trainer"].queryset = Employee.objects.all().order_by("last_name", "first_name")
            self.show_line_field = True

    def _find_employee_profile_for_user(self, user):
        """Trouve le profil Employee correspondant à un User."""
        if not user:
            return None
        try:
            if hasattr(user, "employee_profile"):
                return user.employee_profile
        except Exception:
            pass
        return Employee.objects.filter(
            Q(user=user)
            | Q(first_name__iexact=user.first_name, last_name__iexact=user.last_name)
            | Q(matricule__iexact=user.username)
        ).first()

    def _get_all_team_leader_employee_ids(self):
        """Retourne les IDs Employee correspondant aux chefs d'équipe."""
        team_leader_users = User.objects.filter(managed_lines__isnull=False).distinct()
        employee_ids = set(
            Employee.objects.filter(user__in=team_leader_users).values_list("id", flat=True)
        )
        for team_leader_user in team_leader_users:
            employee = self._find_employee_profile_for_user(team_leader_user)
            if employee:
                employee_ids.add(employee.id)
        return employee_ids

    def _get_selected_field_value(self, field_name):
        value = None
        if self.is_bound:
            value = self.data.get(field_name)
        else:
            value = self.initial.get(field_name)
            if value is None and self.instance.pk:
                value = getattr(self.instance, f"{field_name}_id", None)
        if hasattr(value, "pk"):
            value = value.pk
        return value

    # ================================================================
    # CHARGEMENT ET SAUVEGARDE DU QUESTIONNAIRE (TOUS NIVEAUX)
    # ================================================================

    def _load_questionnaire_fields(self):
        selected_task_id = self._get_selected_field_value("task")
        selected_line_id = self._get_selected_field_value("line") or self.selected_line_id
        selected_level = self._get_selected_field_value("level")

        try:
            selected_task_id = int(selected_task_id)
            selected_line_id = int(selected_line_id)
            selected_level = int(selected_level)
        except (ValueError, TypeError):
            return

        questions = (
            TrainingQuestion.objects.filter(
                is_active=True,
                level=selected_level,  # Filtrage dynamique par niveau
            )
            .filter(Q(task__isnull=True) | Q(task_id=selected_task_id))
            .filter(Q(line__isnull=True) | Q(line_id=selected_line_id))
            .select_related("task", "line")
            .order_by("order", "id")
        )

        existing_answers = {}
        existing_general_comment = ""

        if self.instance.pk:
            try:
                questionnaire = self.instance.questionnaire
                existing_answers = {
                    answer.question_id: answer
                    for answer in questionnaire.answers.all()
                }
                existing_general_comment = questionnaire.general_comment
            except TrainingQuestionnaire.DoesNotExist:
                pass

        for question in questions:
            answer_field_name = f"training_question_{question.pk}"
            comment_field_name = f"training_question_{question.pk}_comment"

            if question.answer_type == TrainingQuestion.AnswerType.YES_NO:
                self.fields[answer_field_name] = forms.TypedChoiceField(
                    label=question.text,
                    required=False,
                    choices=[("1", "Oui"), ("0", "Non")],
                    coerce=lambda value: value == "1",
                    empty_value=None,
                    widget=forms.RadioSelect(
                        attrs={"class": "form-check-input training-question-answer"}
                    ),
                    help_text=question.help_text,
                )

            elif question.answer_type == TrainingQuestion.AnswerType.TEXT:
                self.fields[answer_field_name] = forms.CharField(
                    label=question.text,
                    required=False,
                    help_text=question.help_text,
                    widget=forms.Textarea(
                        attrs={
                            "class": "form-control",
                            "rows": 2,
                            "placeholder": "Saisissez votre réponse...",
                        }
                    ),
                )

            self.fields[comment_field_name] = forms.CharField(
                label="Commentaire",
                required=False,
                widget=forms.Textarea(
                    attrs={
                        "class": "form-control training-question-comment",
                        "rows": 2,
                        "placeholder": "Commentaire ou action corrective...",
                    }
                ),
            )

            existing_answer = existing_answers.get(question.pk)
            if existing_answer:
                if question.answer_type == TrainingQuestion.AnswerType.YES_NO:
                    if existing_answer.boolean_answer is True:
                        self.fields[answer_field_name].initial = "1"
                    elif existing_answer.boolean_answer is False:
                        self.fields[answer_field_name].initial = "0"
                else:
                    self.fields[answer_field_name].initial = existing_answer.text_answer

                self.fields[comment_field_name].initial = existing_answer.comment

            self.training_questions.append({
                "question": question,
                "answer_field": answer_field_name,
                "comment_field": comment_field_name,
            })

        if self.training_questions:
            self.fields["questionnaire_general_comment"] = forms.CharField(
                label="Commentaire général",
                required=False,
                initial=existing_general_comment,
                widget=forms.Textarea(
                    attrs={
                        "class": "form-control",
                        "rows": 3,
                        "placeholder": "Observations générales sur la formation...",
                    }
                ),
            )

    @property
    def questionnaire_fields(self):
        result = []
        for item in self.training_questions:
            result.append({
                "question": item["question"],
                "answer": self[item["answer_field"]],
                "comment": self[item["comment_field"]],
            })
        return result

    # ================================================================
    # VALIDATION ET LOGIQUE DE DEMANDES RH
    # ================================================================

    def _validate_questionnaire(self, cleaned_data, level):
        level_names = {
            TrainingLevel.IN_TRAINING: "I",
            TrainingLevel.SUPERVISED: "L",
            TrainingLevel.TRAINER: "U",
        }
        level_label = level_names.get(level, str(level))

        if not self.training_questions:
            if level == TrainingLevel.IN_TRAINING and self.REQUIRE_QUESTIONNAIRE_FOR_LEVEL_I:
                raise ValidationError(
                    f"❌ Aucun questionnaire actif n'est configuré pour le niveau {level_label}. "
                    "Contactez le service RH."
                )
            return

        for item in self.training_questions:
            question = item["question"]
            answer_field_name = item["answer_field"]
            comment_field_name = item["comment_field"]

            answer = cleaned_data.get(answer_field_name)
            comment = (cleaned_data.get(comment_field_name, "") or "").strip()

            if question.is_required:
                missing_answer = False
                if question.answer_type == TrainingQuestion.AnswerType.YES_NO:
                    missing_answer = answer is None
                elif question.answer_type == TrainingQuestion.AnswerType.TEXT:
                    missing_answer = not str(answer or "").strip()

                if missing_answer:
                    self.add_error(answer_field_name, "Cette question est obligatoire.")
                    continue

            if question.answer_type == TrainingQuestion.AnswerType.YES_NO:
                if answer is None:
                    continue

                if answer is False and question.requires_comment_if_no and not comment:
                    self.add_error(
                        comment_field_name,
                        "Un commentaire est obligatoire lorsque la réponse est Non.",
                    )

                if (
                    question.is_blocking
                    and question.expected_boolean is not None
                    and answer != question.expected_boolean
                ):
                    expected_label = "Oui" if question.expected_boolean else "Non"
                    self.add_error(
                        answer_field_name,
                        f"Réponse non conforme. La réponse attendue est « {expected_label} ». "
                        f"Le niveau {level_label} ne peut pas être enregistré.",
                    )

    def clean(self):
        cleaned_data = super().clean()

        employee = cleaned_data.get("employee")
        task = cleaned_data.get("task")
        level = cleaned_data.get("level")
        line = cleaned_data.get("line")
        training_date = cleaned_data.get("date")

        if not training_date:
            self.add_error("date", "❌ La date de formation est obligatoire.")
            return cleaned_data

        today = timezone.now().date()
        if training_date > today:
            self.add_error("date", "❌ La date ne peut pas être dans le futur.")

        if not employee or not task or level is None:
            return cleaned_data

        if not line:
            self.add_error("line", "❌ Veuillez sélectionner la ligne de production.")
            return cleaned_data

        try:
            new_level = int(level)
        except (ValueError, TypeError):
            self.add_error("level", "❌ Niveau de formation invalide.")
            return cleaned_data

        effective_line = line

        if self.team_leader_employee and employee.id == self.team_leader_employee.id:
            raise ValidationError("❌ Vous ne pouvez pas enregistrer une formation pour vous-même.")

        if self._employee_is_team_leader(employee):
            raise ValidationError(f"❌ {employee.first_name} {employee.last_name} est un chef d'équipe.")

        if (
            self.user
            and not self.user.is_superuser
            and not employee.visible_lines.filter(pk=effective_line.pk).exists()
        ):
            raise ValidationError(
                f"❌ {employee.first_name} {employee.last_name} n'est pas visible "
                f"sur la ligne « {effective_line.name} »."
            )

        if not task.lines.filter(pk=effective_line.pk).exists():
            raise ValidationError(f"❌ La tâche « {task} » n'appartient pas à la ligne « {effective_line.name} ».")

        # ============================================================
        # NIVEAU I
        # ============================================================
        if new_level == TrainingLevel.IN_TRAINING:
            duplicate_query = TrainingRecord.objects.filter(
                employee=employee, task=task, level=TrainingLevel.IN_TRAINING, lines=effective_line
            ).exclude(
                approval_status=TrainingRecord.ApprovalStatus.REJECTED
            )
            if self.instance.pk:
                duplicate_query = duplicate_query.exclude(pk=self.instance.pk)
            if duplicate_query.exists():
                raise ValidationError(f"❌ Un niveau I existe déjà pour cette tâche sur la ligne « {effective_line.name} ».")

            if employee.hire_date:
                days_since_hire = (training_date - employee.hire_date).days
                if days_since_hire < 3:
                    raise ValidationError(f"❌ Ancienneté insuffisante : {days_since_hire} jour(s). Minimum requis : 3 jours.")

            self._validate_questionnaire(cleaned_data, TrainingLevel.IN_TRAINING)
            return cleaned_data

        # ============================================================
        # NIVEAU L
        # ============================================================
        if new_level == TrainingLevel.SUPERVISED:
            record_i = (
                TrainingRecord.objects.filter(
                    employee=employee, task=task, level=TrainingLevel.IN_TRAINING, lines=effective_line,
                    approval_status=TrainingRecord.ApprovalStatus.APPROVED,
                )
                .exclude(date__isnull=True)
                .order_by("date")
                .first()
            )

            if not record_i:
                unapproved_record_i = (
                    TrainingRecord.objects.filter(
                        employee=employee, task=task, level=TrainingLevel.IN_TRAINING, lines=effective_line
                    )
                    .order_by("-date", "-created_at", "-id")
                    .first()
                )

                if unapproved_record_i:
                    if unapproved_record_i.approval_status == TrainingRecord.ApprovalStatus.PENDING:
                        raise ValidationError("Impossible de demander le niveau L : le niveau I correspondant est en attente d'approbation RH.")
                    raise ValidationError("Impossible de demander le niveau L : le niveau I correspondant a été refusé par RH.")

                other_lines = self._get_other_lines_for_level(employee, task, TrainingLevel.IN_TRAINING, effective_line)
                if other_lines:
                    raise ValidationError(f"❌ Le niveau I existe déjà sur : {', '.join(other_lines)}. Enregistrez d'abord un niveau I sur « {effective_line.name} ».")
                raise ValidationError(f"❌ Aucun niveau I n'existe pour cette tâche sur la ligne « {effective_line.name} ».")

            duplicate_query = TrainingRecord.objects.filter(
                employee=employee, task=task, level=TrainingLevel.SUPERVISED, lines=effective_line
            ).exclude(
                approval_status=TrainingRecord.ApprovalStatus.REJECTED
            )
            if self.instance.pk:
                duplicate_query = duplicate_query.exclude(pk=self.instance.pk)
            if duplicate_query.exists():
                raise ValidationError(f"❌ Un niveau L existe déjà pour cette tâche sur la ligne « {effective_line.name} ».")

            days_since_i = (training_date - record_i.date).days
            if days_since_i < 7:
                possible_date = record_i.date + timedelta(days=7)
                raise ValidationError(
                    f"⏳ Attendre 7 jours minimum après le niveau I.\n"
                    f"Date niveau I : {record_i.date.strftime('%d/%m/%Y')}\n"
                    f"Date possible : {possible_date.strftime('%d/%m/%Y')}"
                )

            self._validate_questionnaire(cleaned_data, TrainingLevel.SUPERVISED)
            if any(self.errors):
                return cleaned_data

            request_object, created = self._create_rh_request(
                employee=employee, task=task, new_level=new_level,
                days_since_last=days_since_i, training_date=training_date, line=effective_line
            )

            self.rh_request_created = True
            self.rh_request_info = {
                "request": request_object, "created": created,
                "employee": employee, "task": task, "level": "L (Approuvé)", "level_code": "L", "line": effective_line
            }
            raise ValidationError("RH_REQUEST_CREATED", code="rh_request_pending")

        # ============================================================
        # NIVEAU U
        # ============================================================
        if new_level == TrainingLevel.TRAINER:
            record_l = (
                TrainingRecord.objects.filter(
                    employee=employee, task=task, level=TrainingLevel.SUPERVISED, lines=effective_line,
                    approval_status=TrainingRecord.ApprovalStatus.APPROVED,
                )
                .exclude(date__isnull=True)
                .order_by("date")
                .first()
            )

            if not record_l:
                other_lines = self._get_other_lines_for_level(employee, task, TrainingLevel.SUPERVISED, effective_line)
                if other_lines:
                    raise ValidationError(f"❌ Le niveau L existe déjà sur : {', '.join(other_lines)}. Enregistrez d'abord un niveau L sur « {effective_line.name} ».")
                raise ValidationError(f"❌ Aucun niveau L n'existe pour cette tâche sur la ligne « {effective_line.name} ».")

            duplicate_query = TrainingRecord.objects.filter(
                employee=employee, task=task, level=TrainingLevel.TRAINER, lines=effective_line
            ).exclude(
                approval_status=TrainingRecord.ApprovalStatus.REJECTED
            )
            if self.instance.pk:
                duplicate_query = duplicate_query.exclude(pk=self.instance.pk)
            if duplicate_query.exists():
                raise ValidationError(f"❌ Un niveau U existe déjà pour cette tâche sur la ligne « {effective_line.name} ».")

            days_since_l = (training_date - record_l.date).days
            if days_since_l < 21:
                possible_date = record_l.date + timedelta(days=21)
                raise ValidationError(
                    f"⏳ Attendre 21 jours minimum après le niveau L.\n"
                    f"Date niveau L : {record_l.date.strftime('%d/%m/%Y')}\n"
                    f"Date possible : {possible_date.strftime('%d/%m/%Y')}"
                )

            self._validate_questionnaire(cleaned_data, TrainingLevel.TRAINER)
            if any(self.errors):
                return cleaned_data

            request_object, created = self._create_rh_request(
                employee=employee, task=task, new_level=new_level,
                days_since_last=days_since_l, training_date=training_date, line=effective_line
            )

            self.rh_request_created = True
            self.rh_request_info = {
                "request": request_object, "created": created,
                "employee": employee, "task": task, "level": "U (Formateur)", "level_code": "U", "line": effective_line
            }
            raise ValidationError("RH_REQUEST_CREATED", code="rh_request_pending")

        return cleaned_data

    # ================================================================
    # LOGIQUE DE SAUVEGARDE ET UTILITAIRES
    # ================================================================

    def _save_questionnaire_for_record(self, record):
        if not self.training_questions:
            return

        line = record.lines.first() or self.cleaned_data.get("line")
        if not line or not self.user:
            return

        with transaction.atomic():
            questionnaire, created = TrainingQuestionnaire.objects.get_or_create(
                training_record=record,
                defaults={
                    "line": line,
                    "answered_by": self.user,
                    "general_comment": self.cleaned_data.get("questionnaire_general_comment", ""),
                }
            )

            if not created:
                questionnaire.general_comment = self.cleaned_data.get("questionnaire_general_comment", "")
                questionnaire.save()

            questionnaire.answers.all().delete()

            for item in self.training_questions:
                question = item["question"]
                answer_val = self.cleaned_data.get(item["answer_field"])
                comment = self.cleaned_data.get(item["comment_field"], "") or ""

                is_compliant = True
                if (
                    question.answer_type == TrainingQuestion.AnswerType.YES_NO
                    and question.expected_boolean is not None
                    and answer_val is not None
                ):
                    is_compliant = (answer_val == question.expected_boolean)

                TrainingQuestionAnswer.objects.create(
                    questionnaire=questionnaire,
                    question=question,
                    question_text_snapshot=question.text,
                    answer_type_snapshot=question.answer_type,
                    boolean_answer=answer_val if question.answer_type == TrainingQuestion.AnswerType.YES_NO else None,
                    text_answer=str(answer_val or "") if question.answer_type == TrainingQuestion.AnswerType.TEXT else "",
                    comment=comment,
                    is_compliant=is_compliant,
                )

            all_compliant = not questionnaire.answers.filter(is_compliant=False).exists()
            questionnaire.is_compliant = all_compliant
            questionnaire.save(update_fields=["is_compliant"])

    def save(self, commit=True):
        instance = super().save(commit=False)
        if commit:
            instance.save()
            self.save_m2m()
            self._save_questionnaire_for_record(instance)
        return instance

    def _employee_is_team_leader(self, employee):
        if employee.user:
            if ProductionLine.objects.filter(team_leaders=employee.user).exists():
                return True
        matching_user = User.objects.filter(
            Q(first_name__iexact=employee.first_name, last_name__iexact=employee.last_name)
            | Q(username__iexact=employee.matricule)
        ).first()
        if matching_user:
            return ProductionLine.objects.filter(team_leaders=matching_user).exists()
        return False

    def _get_other_lines_for_level(self, employee, task, level, excluded_line):
        records = TrainingRecord.objects.filter(employee=employee, task=task, level=level).exclude(lines=excluded_line).prefetch_related("lines")
        line_names = set()
        for r in records:
            for l in r.lines.all():
                if l.pk != excluded_line.pk:
                    line_names.add(l.name)
        return sorted(line_names)

    def _create_rh_request(self, employee, task, new_level, days_since_last, training_date, line):
        existing_request = TrainingApprovalRequest.objects.filter(
            employee=employee, task=task, requested_level=new_level, approved__isnull=True, line=line
        ).first()

        if existing_request:
            return existing_request, False

        with transaction.atomic():
            record = TrainingRecord.objects.create(
                employee=employee,
                task=task,
                level=new_level,
                date=training_date,
                approval_status=TrainingRecord.ApprovalStatus.PENDING,
                trainer=None,
            )
            record.lines.add(line)

            self._save_questionnaire_for_record(record)

            request_object = TrainingApprovalRequest.objects.create(
                employee=employee,
                task=task,
                requested_level=new_level,
                requested_by=self.user,
                training_date=training_date,
                line=line,
                training_record=record,
            )

        self._send_rh_email(employee, task, new_level, days_since_last, training_date, request_object, line)
        return request_object, True

    def _send_rh_email(self, employee, task, new_level, days_since_last, training_date, request_object, line):
        rh_emails = getattr(settings, "RH_EMAILS", [])
        if not rh_emails:
            logger.warning("RH_EMAILS n'est pas configuré.")
            return

        level_names = {
            TrainingLevel.IN_TRAINING: "I - En formation",
            TrainingLevel.SUPERVISED: "L - Approuvé",
            TrainingLevel.TRAINER: "U - Formateur",
        }
        level_name = level_names.get(new_level, str(new_level))
        requester_name = self.user.get_full_name() or self.user.username if self.user else "Utilisateur inconnu"

        subject = f"Demande de validation formation {level_name} - {employee.first_name} {employee.last_name}"
        message = (
            f"Une nouvelle demande de formation nécessite une validation RH.\n\n"
            f"Demande : #{request_object.pk}\n"
            f"Employé : {employee.first_name} {employee.last_name}\n"
            f"Matricule : {employee.matricule}\n"
            f"Tâche : {task}\n"
            f"Ligne : {line.name if line else 'N/A'}\n"
            f"Niveau demandé : {level_name}\n"
            f"Date de formation : {training_date.strftime('%d/%m/%Y')}\n"
            f"Jours écoulés : {days_since_last}\n"
            f"Demandé par : {requester_name}\n"
        )

        try:
            send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, rh_emails, fail_silently=False)
        except Exception as e:
            logger.exception("Erreur pendant l'envoi de l'email RH : %s", e)

    def _get_fallback_html(self, context):
        """HTML de secours"""
        return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
    </head>
    <body style="margin: 0; padding: 0; background-color: #f4f4f4; font-family: Arial, sans-serif;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #f4f4f4;">
            <tr>
                <td align="center" style="padding: 20px;">
                    <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="background-color: #ffffff; border: 1px solid #dddddd;">
                        
                        <!-- HEADER -->
                        <tr>
                            <td bgcolor="#1a5276" style="background-color: #1a5276; padding: 30px; text-align: center; border-bottom: 4px solid #f39c12;">
                                <img src="cid:company_logo" alt="{context['company_name']}" width="150" style="display: block; margin: 0 auto 15px auto;">
                                <h1 style="color: #ffffff; margin: 0; font-size: 22px;">Demande de Formation</h1>
                                <p style="color: #bdc3c7; margin: 8px 0 0 0; font-size: 14px;">Validation requise</p>
                            </td>
                        </tr>
                        
                        <!-- NIVEAU -->
                        <tr>
                            <td style="padding: 25px; text-align: center;">
                                <table role="presentation" align="center" cellpadding="0" cellspacing="0" border="0">
                                    <tr>
                                        <td bgcolor="{context['level_color']}" style="background-color: {context['level_color']}; padding: 10px 25px; border-radius: 20px;">
                                            <span style="color: #ffffff; font-size: 15px; font-weight: bold;">
                                                Niveau : {context['level_name']}
                                            </span>
                                        </td>
                                    </tr>
                                </table>
                            </td>
                        </tr>
                        
                        <!-- DETAILS -->
                        <tr>
                            <td style="padding: 0 30px 25px 30px;">
                                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border: 2px solid #1a5276;">
                                    <tr>
                                        <td colspan="2" bgcolor="#1a5276" style="background-color: #1a5276; padding: 10px 15px;">
                                            <span style="color: #ffffff; font-weight: bold;">DÉTAILS DE LA DEMANDE</span>
                                        </td>
                                    </tr>
                                    <tr>
                                        <td bgcolor="#f8f9fa" style="padding: 10px 15px; border-bottom: 1px solid #dee2e6; width: 40%;">
                                            <span style="color: #495057; font-weight: bold;">Employé :</span>
                                        </td>
                                        <td bgcolor="#ffffff" style="padding: 10px 15px; border-bottom: 1px solid #dee2e6;">
                                            <span style="color: #212529;">{context['employee_name']}</span>
                                        </td>
                                    </tr>
                                    <tr>
                                        <td bgcolor="#f8f9fa" style="padding: 10px 15px; border-bottom: 1px solid #dee2e6;">
                                            <span style="color: #495057; font-weight: bold;">Matricule :</span>
                                        </td>
                                        <td bgcolor="#ffffff" style="padding: 10px 15px; border-bottom: 1px solid #dee2e6;">
                                            <span style="color: #212529;">{context['employee_matricule']}</span>
                                        </td>
                                    </tr>
                                    <tr>
                                        <td bgcolor="#f8f9fa" style="padding: 10px 15px; border-bottom: 1px solid #dee2e6;">
                                            <span style="color: #495057; font-weight: bold;">Tâche :</span>
                                        </td>
                                        <td bgcolor="#ffffff" style="padding: 10px 15px; border-bottom: 1px solid #dee2e6;">
                                            <span style="color: #212529;">{context['task_name']}</span>
                                        </td>
                                    </tr>
                                    <tr>
                                        <td bgcolor="#f8f9fa" style="padding: 10px 15px; border-bottom: 1px solid #dee2e6;">
                                            <span style="color: #495057; font-weight: bold;">Ligne :</span>
                                        </td>
                                        <td bgcolor="#ffffff" style="padding: 10px 15px; border-bottom: 1px solid #dee2e6;">
                                            <span style="color: #212529;">{context['production_line']}</span>
                                        </td>
                                    </tr>
                                    <tr>
                                        <td bgcolor="#f8f9fa" style="padding: 10px 15px; border-bottom: 1px solid #dee2e6;">
                                            <span style="color: #495057; font-weight: bold;">Date de formation :</span>
                                        </td>
                                        <td bgcolor="#ffffff" style="padding: 10px 15px; border-bottom: 1px solid #dee2e6;">
                                            <span style="color: #212529; font-weight: bold;">{context['training_date']}</span>
                                        </td>
                                    </tr>
                                    <tr>
                                        <td bgcolor="#f8f9fa" style="padding: 10px 15px; border-bottom: 1px solid #dee2e6;">
                                            <span style="color: #495057; font-weight: bold;">Demandé par :</span>
                                        </td>
                                        <td bgcolor="#ffffff" style="padding: 10px 15px; border-bottom: 1px solid #dee2e6;">
                                            <span style="color: #212529;">{context['requester_name']}</span>
                                        </td>
                                    </tr>
                                    <tr>
                                        <td bgcolor="#f8f9fa" style="padding: 10px 15px;">
                                            <span style="color: #495057; font-weight: bold;">Jours écoulés :</span>
                                        </td>
                                        <td bgcolor="#ffffff" style="padding: 10px 15px;">
                                            <span style="color: #28a745; font-weight: bold;">{context['days_elapsed']} jours (min: {context['required_days']})</span>
                                        </td>
                                    </tr>
                                </table>
                            </td>
                        </tr>
                        
                        <!-- FOOTER -->
                        <tr>
                            <td bgcolor="#343a40" style="background-color: #343a40; padding: 25px; text-align: center; border-top: 4px solid #f39c12;">
                                <p style="color: #adb5bd; margin: 0; font-size: 13px;">
                                    &copy; {context['current_year']} {context['company_name']}
                                </p>
                                <p style="color: #adb5bd; margin: 5px 0 0 0; font-size: 13px;">
                                    Système de Gestion des Formations
                                </p>
                                <p style="color: #6c757d; margin: 12px 0 0 0; font-size: 11px;">
                                    Email automatique - Demande #{context['request_id']}
                                </p>
                            </td>
                        </tr>
                        
                    </table>
                </td>
            </tr>
        </table>
    </body>
    </html>
        """

    def _get_fallback_text(self, context):
        """Texte brut de secours"""
        return f"""
════════════════════════════════════════════════════════════════════
                    {context['company_name']}
                    DEMANDE DE FORMATION
════════════════════════════════════════════════════════════════════

Bonjour,

Une nouvelle demande de formation nécessite votre validation.

────────────────────────────────────────────────────────────────────
📌 DÉTAILS DE LA DEMANDE
────────────────────────────────────────────────────────────────────

    • Employé           : {context['employee_name']}
    • Matricule         : {context['employee_matricule']}
    • Tâche             : {context['task_name']}
    • Ligne             : {context['production_line']}
    • Niveau demandé    : {context['level_name']}
    • Date de formation : {context['training_date']}
    • Demandé par       : {context['requester_name']}
    • Jours écoulés     : {context['days_elapsed']} (minimum: {context['required_days']})

────────────────────────────────────────────────────────────────────
✅ Le délai minimum a été respecté.
────────────────────────────────────────────────────────────────────

════════════════════════════════════════════════════════════════════
            © {context['current_year']} {context['company_name']}
              Système de Gestion des Formations
              Demande #{context['request_id']}
════════════════════════════════════════════════════════════════════
        """
    



class RHTeamLeaderTrainingForm(forms.ModelForm):
    """
    Formulaire RH exclusif pour former les chefs d'équipe.
    - La ligne est automatiquement sélectionnée selon le chef d'équipe
    """
    
    line = forms.ModelChoiceField(
        queryset=ProductionLine.objects.all(),
        required=True,
        label="Ligne de production",
        help_text="Ligne gérée par le chef d'équipe (auto-sélectionnée)",
        widget=forms.Select(attrs={
            'id': 'id_line',
            'class': 'form-select'
        })
    )
    
    force_save = forms.BooleanField(
        required=False,
        initial=False,
        label="Forcer l'enregistrement (ignorer les délais)",
        help_text="Cochez cette case pour ignorer les restrictions de délai",
        widget=forms.CheckboxInput(attrs={
            'class': 'form-check-input'
        })
    )
    
    class Meta:
        model = TrainingRecord
        fields = ['employee', 'task', 'level', 'date', 'note']  # ✅ Sans 'trainer'
        labels = {
            "employee": "Chef d'Équipe",
            "task": "Tâche",
            "level": "Niveau",
            "date": "Date de formation",
            "note": "Notes",
        }
        widgets = {
            'employee': forms.Select(attrs={
                'id': 'id_employee',
                'class': 'form-select'
            }),
            'task': forms.Select(attrs={
                'id': 'id_task',
                'class': 'form-select'
            }),
            'level': forms.Select(attrs={
                'class': 'form-select'
            }),
            'date': forms.DateInput(attrs={
                'type': 'date',
                'class': 'form-control'
            }),
            'note': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 3,
                'placeholder': 'Notes ou commentaires...'
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Récupérer les chefs d'équipe
        team_leader_user_ids = User.objects.filter(
            managed_lines__isnull=False
        ).distinct().values_list('id', flat=True)
        
        employees_via_user = Employee.objects.filter(user_id__in=team_leader_user_ids)
        
        q_filters = Q()
        for tl_user in User.objects.filter(id__in=team_leader_user_ids):
            q_filters |= Q(
                first_name__iexact=tl_user.first_name,
                last_name__iexact=tl_user.last_name
            ) | Q(matricule__iexact=tl_user.username)
        
        if q_filters:
            employees_via_match = Employee.objects.filter(q_filters)
            team_leader_employees = (employees_via_user | employees_via_match).distinct()
        else:
            team_leader_employees = employees_via_user
        
        self.fields['employee'].queryset = team_leader_employees.select_related(
            'line', 'manager'
        ).order_by('last_name', 'first_name')
        
        self.fields['line'].queryset = ProductionLine.objects.all().order_by('code')
        self.fields['task'].queryset = Task.objects.all().order_by('order', 'code')
        self.fields['date'].initial = timezone.now().date()
        self.fields['note'].required = False

    def clean(self):
        cleaned_data = super().clean()
        employee = cleaned_data.get('employee')
        task = cleaned_data.get('task')
        level = cleaned_data.get('level')
        line = cleaned_data.get('line')
        training_date = cleaned_data.get('date')
        force_save = cleaned_data.get('force_save', False)
        
        if not all([employee, task, level is not None, line]):
            return cleaned_data
        
        # Date par défaut si non fournie
        if not training_date:
            training_date = timezone.now().date()
            cleaned_data['date'] = training_date
        
        new_level = int(level)
        
        # ═══════════════════════════════════════════════════════════════════
        # Vérifier que la tâche appartient à la ligne sélectionnée
        # ═══════════════════════════════════════════════════════════════════
        if not task.lines.filter(pk=line.pk).exists():
            raise ValidationError(
                f"❌ La tâche '{task.title}' n'est pas associée à la ligne '{line.name}'. "
                f"Veuillez sélectionner une tâche de cette ligne."
            )
        
        # ═══════════════════════════════════════════════════════════════════
        # Vérifier que ce n'est pas un doublon exact
        # ═══════════════════════════════════════════════════════════════════
        duplicate = TrainingRecord.objects.filter(
            employee=employee,
            task=task,
            level=new_level,
            lines=line
        ).exclude(
            approval_status=TrainingRecord.ApprovalStatus.REJECTED
        ).exists()
        
        if duplicate:
            raise ValidationError(
                f"❌ Une formation de niveau {TrainingLevel(new_level).label} existe déjà "
                f"pour {employee} sur la tâche '{task.title}' (ligne {line.code})."
            )
        
        # ═══════════════════════════════════════════════════════════════════
        # Récupérer le dernier enregistrement pour CETTE LIGNE
        # ═══════════════════════════════════════════════════════════════════
        last_record = TrainingRecord.objects.filter(
            employee=employee,
            task=task,
            lines=line
        ).exclude(
            approval_status=TrainingRecord.ApprovalStatus.REJECTED
        ).order_by('-date').first()
        
        # ═══════════════════════════════════════════════════════════════════
        # Premier enregistrement pour cette ligne
        # ═══════════════════════════════════════════════════════════════════
        if last_record is None:
            if new_level != TrainingLevel.IN_TRAINING:
                if not force_save:
                    raise ValidationError(
                        f"⚠️ Le premier niveau sur la ligne {line.name} doit être I (En formation). "
                        f"Cochez 'Forcer l'enregistrement' si vous souhaitez ignorer cette règle."
                    )
                else:
                    # Ajouter un avertissement mais permettre
                    self.add_warning(
                        f"⚠️ Attention : Premier enregistrement directement au niveau "
                        f"{TrainingLevel(new_level).label} (règle ignorée)"
                    )
            return cleaned_data
        
        last_level = int(last_record.level)
        days_since_last = (training_date - last_record.date).days
        
        # ═══════════════════════════════════════════════════════════════════
        # Validation : Pas de rétrogradation
        # ═══════════════════════════════════════════════════════════════════
        if new_level < last_level:
            if not force_save:
                raise ValidationError(
                    f"❌ Impossible de rétrograder le niveau. "
                    f"Niveau actuel : {TrainingLevel(last_level).label}, "
                    f"Niveau demandé : {TrainingLevel(new_level).label}. "
                    f"Cochez 'Forcer l'enregistrement' pour ignorer cette règle."
                )
        
        # ═══════════════════════════════════════════════════════════════════
        # Validation : Pas de saut I → U directement
        # ═══════════════════════════════════════════════════════════════════
        if last_level == TrainingLevel.IN_TRAINING and new_level == TrainingLevel.TRAINER:
            if not force_save:
                raise ValidationError(
                    f"❌ Impossible de passer directement de I (En formation) à U (Formateur). "
                    f"L'employé doit d'abord passer par le niveau L (Autonome). "
                    f"Cochez 'Forcer l'enregistrement' pour ignorer cette règle."
                )
        
        # ═══════════════════════════════════════════════════════════════════
        # Validation : Délai I → L (7 jours minimum)
        # ═══════════════════════════════════════════════════════════════════
        if last_level == TrainingLevel.IN_TRAINING and new_level == TrainingLevel.SUPERVISED:
            if days_since_last < 7:
                if not force_save:
                    raise ValidationError(
                        f"⏳ Délai insuffisant pour passer de I à L. "
                        f"Minimum requis : 7 jours. "
                        f"Jours écoulés : {days_since_last}. "
                        f"Dernière formation : {last_record.date.strftime('%d/%m/%Y')}. "
                        f"Date possible : {(last_record.date + timedelta(days=7)).strftime('%d/%m/%Y')}. "
                        f"Cochez 'Forcer l'enregistrement' pour ignorer cette règle."
                    )
                else:
                    self.add_warning(
                        f"⚠️ Attention : Passage I→L avec seulement {days_since_last}/7 jours (règle ignorée)"
                    )
        
        # ═══════════════════════════════════════════════════════════════════
        # Validation : Délai L → U (21 jours minimum)
        # ═══════════════════════════════════════════════════════════════════
        if last_level == TrainingLevel.SUPERVISED and new_level == TrainingLevel.TRAINER:
            if days_since_last < 21:
                if not force_save:
                    raise ValidationError(
                        f"⏳ Délai insuffisant pour passer de L à U. "
                        f"Minimum requis : 21 jours. "
                        f"Jours écoulés : {days_since_last}. "
                        f"Dernière formation : {last_record.date.strftime('%d/%m/%Y')}. "
                        f"Date possible : {(last_record.date + timedelta(days=21)).strftime('%d/%m/%Y')}. "
                        f"Cochez 'Forcer l'enregistrement' pour ignorer cette règle."
                    )
                else:
                    self.add_warning(
                        f"⚠️ Attention : Passage L→U avec seulement {days_since_last}/21 jours (règle ignorée)"
                    )
        
        # ═══════════════════════════════════════════════════════════════════
        # Validation : Même niveau (mise à jour de date seulement)
        # ═══════════════════════════════════════════════════════════════════
        if new_level == last_level:
            # Permettre la mise à jour si la date est différente
            if training_date <= last_record.date and not force_save:
                raise ValidationError(
                    f"ℹ️ Une formation de niveau {TrainingLevel(new_level).label} existe déjà "
                    f"à la date du {last_record.date.strftime('%d/%m/%Y')}. "
                    f"Veuillez choisir une date ultérieure ou cocher 'Forcer l'enregistrement'."
                )
        
        return cleaned_data

    def add_warning(self, message):
        """Ajoute un avertissement (pas une erreur bloquante)"""
        if not hasattr(self, '_warnings'):
            self._warnings = []
        self._warnings.append(message)
    
    def get_warnings(self):
        """Retourne la liste des avertissements"""
        return getattr(self, '_warnings', [])

    def save(self, commit=True):
        instance = super().save(commit=False)
        
        # S'assurer que la date est définie
        if not instance.date:
            instance.date = timezone.now().date()
        
        if commit:
            instance.save()
            
            # Associer la ligne
            line = self.cleaned_data.get('line')
            if line:
                instance.lines.set([line])
        
        return instance

# *************************************************************************

class TaskForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = ["code","task_number", "title","group_title",  "lines"]
        widgets = {
            "lines": forms.CheckboxSelectMultiple(),  
            "description": forms.Textarea(attrs={"rows": 2}),
        }


class ProductionLineForm(forms.ModelForm):
    class Meta:
        model = ProductionLine
        fields = ["code", "name", "team_leaders"]
        widgets = {
            "team_leaders": forms.CheckboxSelectMultiple(),  
        }


class EmployeeTransferForm(forms.Form):
    employee = forms.ModelChoiceField(
        queryset=Employee.objects.all(),
        label="Employé",
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    
    new_line = forms.ModelChoiceField(
        queryset=ProductionLine.objects.all(),
        label="Nouvelle ligne de production",
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    
    reason = forms.CharField(
        required=False,
        label="Raison du transfert",
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 3,
            'placeholder': 'Ex: Besoin de renforcer cette ligne...'
        })
    )


# ══════════════════════════════════════════════════════════════════════
# Formulaire RH — Création complète (conservé)
# ══════════════════════════════════════════════════════════════════════
class EmployeeFormRH(forms.ModelForm):
    """
    Formulaire RH avec sélection du chef d'équipe.
    Permet aussi de pré-enregistrer un employé complet.
    """

    manager = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        label="Chef d'équipe responsable",
        widget=forms.Select(attrs={
            'class': 'form-control',
            'id':    'id_manager',
        })
    )

    class Meta:
        model  = Employee
        fields = [
            'matricule', 'first_name', 'last_name',
            'hire_date', 'line'
        ]
        widgets = {
            'matricule': forms.TextInput(attrs={
                'class':       'form-control',
                'placeholder': 'Ex: EMP001',
            }),
            'first_name': forms.TextInput(attrs={
                'class':       'form-control',
                'placeholder': 'Prénom',
            }),
            'last_name': forms.TextInput(attrs={
                'class':       'form-control',
                'placeholder': 'Nom',
            }),
            'hire_date': forms.DateInput(attrs={
                'type':  'date',
                'class': 'form-control',
            }),
            'line': forms.Select(attrs={
                'class': 'form-control',
                'id':    'id_line',
            }),
        }
        labels = {
            'matricule':  'Matricule',
            'first_name': 'Prénom',
            'last_name':  'Nom',
            'hire_date':  "Date d'embauche",
            'line':       'Ligne de production',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields['line'].required = True
        self.fields['line'].queryset = (
            ProductionLine.objects.all().order_by('code')
        )

        if not self.instance.pk:
            self.initial['hire_date'] = timezone.now().date()

        if self.instance and self.instance.pk and self.instance.line:
            self.fields['manager'].queryset = (
                self.instance.line.team_leaders.all()
            )
            self.fields['manager'].initial = self.instance.manager

        if 'line' in self.data:
            try:
                line_id = int(self.data.get('line'))
                line    = ProductionLine.objects.get(pk=line_id)
                self.fields['manager'].queryset = line.team_leaders.all()
            except (ValueError, TypeError, ProductionLine.DoesNotExist):
                pass

    def clean_matricule(self):
        matricule = self.cleaned_data.get('matricule', '').strip().upper()

        # Vérifier doublon (sauf modification)
        qs = Employee.objects.filter(matricule=matricule)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(
                f"❌ Le matricule '{matricule}' est déjà utilisé."
            )
        return matricule

    def clean(self):
        cleaned_data  = super().clean()
        line          = cleaned_data.get('line')
        manager       = cleaned_data.get('manager')

        if line:
            team_leaders  = line.team_leaders.all()
            leaders_count = team_leaders.count()

            if leaders_count == 0:
                raise ValidationError(
                    f"La ligne '{line.name}' n'a aucun chef "
                    f"d'équipe assigné."
                )
            elif leaders_count == 1:
                cleaned_data['manager'] = team_leaders.first()
            else:
                if not manager:
                    raise ValidationError(
                        f"La ligne '{line.name}' a {leaders_count} "
                        f"chefs d'équipe. Veuillez en sélectionner un."
                    )
                if manager not in team_leaders:
                    raise ValidationError(
                        f"Le chef sélectionné ne gère pas "
                        f"la ligne '{line.name}'."
                    )
                cleaned_data['manager'] = manager

        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        # ✅ Marquer comme pré-enregistré par RH
        instance.is_registered = True
        if commit:
            instance.save()
        return instance

class RequalificationForm(TrainingPlanImageForm):
    """Validate the existing global/per-task flow before any persistence."""

    employee_id = forms.ModelChoiceField(queryset=Employee.objects.none(), label="Employé")
    line_id = forms.ModelChoiceField(queryset=ProductionLine.objects.all(), required=False, label="Ligne")
    task_id = forms.ModelChoiceField(queryset=Task.objects.all(), required=False, label="Tâche")
    result = forms.ChoiceField(choices=models.RequalificationApprovalRequest.RESULT_CHOICES, label="Résultat")
    new_level = forms.TypedChoiceField(choices=TrainingLevel.choices, coerce=int, empty_value=None, required=False)
    requalification_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    notes = forms.CharField(required=False, widget=forms.Textarea)
    requalifier_name = forms.CharField(max_length=150, required=False)
    field_order = ["employee_id", "line_id", "task_id", "result", "new_level",
                   "requalification_date", "notes", "requalifier_name", "training_plan_image"]

    def __init__(self, *args, user, direct_leader=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.direct_leader = direct_leader
        is_rh = user.is_superuser or user.groups.filter(name="RH").exists()
        employees = Employee.objects.all()
        if direct_leader:
            employees = employees.filter(user__managed_lines__isnull=False).distinct()
        elif not is_rh:
            employees = employees.filter(manager=user)
        self.fields["employee_id"].queryset = employees
        self.fields["line_id"].required = direct_leader
        from .services_requalification import can_add_requalification_plan
        if direct_leader or not can_add_requalification_plan(user):
            # Removing the field also excludes forged uploads from cleaned_data.
            self.fields.pop("training_plan_image")
            return
        plan = self.fields["training_plan_image"]
        plan.label = "Plan de formation"
        plan.required = True
        plan.error_messages["required"] = "Le plan de formation est obligatoire pour créer une requalification."

    def clean(self):
        data = super().clean()
        employee = data.get("employee_id")
        if employee:
            if self.direct_leader and employee.user_id == self.user.pk:
                self.add_error("employee_id", "Vous ne pouvez pas vous requalifier vous-même.")
            if not data.get("line_id") and "line_id" not in self.errors:
                data["line_id"] = employee.line
        if self.direct_leader and data.get("task_id"):
            self.add_error("task_id", "La requalification des chefs est globale par ligne.")
        if self.direct_leader and employee and data.get("line_id"):
            if not data["line_id"].team_leaders.filter(pk=employee.user_id).exists():
                self.add_error("line_id", "Ce chef n’est pas affecté à la ligne sélectionnée.")
        if "requalification_date" not in self.errors and not data.get("requalification_date"):
            data["requalification_date"] = timezone.now().date()
        # The leader/line cooldown is rechecked under a transaction in the
        # service. The operator workflow keeps its existing validation.
        return data


class TeamLeaderTransferForm(forms.Form):
    """Formulaire de transfert pour les chefs d'équipe"""
    
    employee = forms.ModelChoiceField(
        queryset=Employee.objects.none(),
        label="Employé à transférer",
        widget=forms.Select(attrs={'class': 'form-control select2'})
    )
    
    new_line = forms.ModelChoiceField(
        queryset=ProductionLine.objects.all(),
        label="Nouvelle ligne",
        widget=forms.Select(attrs={'class': 'form-control select2'})
    )
    
    reason = forms.CharField(
        required=False,
        label="Raison du transfert",
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 3,
            'placeholder': 'Expliquez la raison de ce transfert...'
        })
    )
    
    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user:
            # Le chef ne voit que ses propres employés
            self.fields['employee'].queryset = Employee.objects.filter(
                manager=user
            ).select_related('line').order_by('last_name', 'first_name')



# ****************************************************************************
# matrix/forms.py

from django import forms
from django.contrib.auth.models import User, Group
from django.contrib.auth.forms import UserCreationForm
from .models import ProductionLine, Employee

from django.contrib.auth.forms import UserCreationForm, UserChangeForm, SetPasswordForm
from django.contrib.auth import get_user_model

User = get_user_model()


class UserManagementForm(forms.ModelForm):
    """Formulaire unifié pour créer/modifier un utilisateur"""
    
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'email@entreprise.com'
        })
    )
    
    first_name = forms.CharField(
        max_length=30,
        required=True,
        label="Prénom",
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Jean'
        })
    )
    
    last_name = forms.CharField(
        max_length=30,
        required=True,
        label="Nom",
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Dupont'
        })
    )
    
    # Mot de passe uniquement pour la création
    password1 = forms.CharField(
        label="Mot de passe",
        required=False,
        widget=forms.PasswordInput(attrs={'class': 'form-control'}),
        help_text="Laissez vide pour ne pas modifier le mot de passe (modification uniquement)"
    )
    
    password2 = forms.CharField(
        label="Confirmer le mot de passe",
        required=False,
        widget=forms.PasswordInput(attrs={'class': 'form-control'}),
    )
    
    user_type = forms.ChoiceField(
        choices=[
            ('team_leader', "👔 Chef d'équipe"),
            ('superuser', '👑 Super Admin (accès RH complet)'),
        ],
        widget=forms.RadioSelect(attrs={'class': 'form-check-input'}),
        label="Type d'utilisateur"
    )
    
    managed_lines = forms.ModelMultipleChoiceField(
        queryset=ProductionLine.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={'class': 'form-check-input'}),
        label="Lignes de production à gérer",
    )
    
    link_to_employee = forms.ModelChoiceField(
        queryset=Employee.objects.filter(user__isnull=True),
        required=False,
        widget=forms.Select(attrs={'class': 'form-control select2'}),
        label="Lier à un profil employé existant",
    )
    
    create_employee_profile = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        label="Créer automatiquement un profil employé",
    )
    
    matricule = forms.CharField(
        max_length=50,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'MAT-001'
        }),
        label="Matricule",
    )
    
    class Meta:
        model = User
        fields = ('username', 'email', 'first_name', 'last_name')
        widgets = {
            'username': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'nom.utilisateur'
            }),
        }
    
    def __init__(self, *args, **kwargs):
        self.instance_id = kwargs.pop('instance_id', None)
        super().__init__(*args, **kwargs)
        
        is_edit = self.instance and self.instance.pk
        
        if is_edit:
            # ✅ Mot de passe optionnel en modification
            self.fields['password1'].required = False
            self.fields['password2'].required = False
            self.fields['password1'].help_text = "Laissez vide pour conserver le mot de passe actuel."
            
            # Pré-remplir le type
            self.fields['user_type'].initial = (
                'superuser' if self.instance.is_superuser else 'team_leader'
            )
            
            # Pré-sélectionner les lignes
            self.fields['managed_lines'].initial = self.instance.managed_lines.all()
            
            # Pré-remplir le matricule si profil employé existe
            try:
                employee = self.instance.employee_profile
                self.fields['matricule'].initial = employee.matricule
            except Employee.DoesNotExist:
                pass
        else:
            # ✅ Mot de passe obligatoire en création
            self.fields['password1'].required = True
            self.fields['password2'].required = True
    
    def clean_username(self):
        """✅ Validation du username en excluant l'instance actuelle"""
        username = self.cleaned_data.get('username')
        
        if username:
            # Construire la requête en excluant l'utilisateur actuel
            qs = User.objects.filter(username=username)
            
            # Si on modifie un utilisateur existant, l'exclure de la vérification
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            
            if qs.exists():
                raise forms.ValidationError("Un utilisateur avec ce nom d'utilisateur existe déjà.")
        
        return username
    
    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get('password1')
        password2 = cleaned_data.get('password2')
        user_type = cleaned_data.get('user_type')
        managed_lines = cleaned_data.get('managed_lines')
        link_to_employee = cleaned_data.get('link_to_employee')
        create_employee_profile = cleaned_data.get('create_employee_profile')
        
        is_edit = self.instance and self.instance.pk
        
        # ✅ Validation mot de passe
        if not is_edit and not password1:
            self.add_error('password1', "Le mot de passe est obligatoire pour un nouvel utilisateur.")
        
        if password1 or password2:
            if password1 != password2:
                self.add_error('password2', "Les mots de passe ne correspondent pas.")
            elif len(password1) < 8:
                self.add_error('password1', "Le mot de passe doit contenir au moins 8 caractères.")
        
        # Validation : chef d'équipe doit avoir au moins une ligne
        if user_type == 'team_leader' and not managed_lines:
            raise forms.ValidationError(
                "Un chef d'équipe doit gérer au moins une ligne de production."
            )
        
        # Validation : ne pas lier ET créer un profil
        if link_to_employee and create_employee_profile:
            raise forms.ValidationError(
                "Vous ne pouvez pas lier à un profil existant ET créer un nouveau profil."
            )
        
        return cleaned_data
    
    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data['email']
        user.first_name = self.cleaned_data['first_name']
        user.last_name = self.cleaned_data['last_name']
        
        user_type = self.cleaned_data['user_type']
        
        # Configuration du type
        if user_type == 'superuser':
            user.is_superuser = True
            user.is_staff = True
        else:
            user.is_superuser = False
            user.is_staff = True
        
        # ✅ Gestion du mot de passe
        password = self.cleaned_data.get('password1')
        if password:
            user.set_password(password)
        # Si pas de password fourni en modification → conserver l'ancien (déjà en mémoire)
        
        if commit:
            user.save()
            
            # Affecter les lignes
            if user_type == 'team_leader':
                managed_lines = self.cleaned_data.get('managed_lines', [])
                user.managed_lines.set(managed_lines)
            else:
                user.managed_lines.clear()
            
            # Gestion du profil employé
            if self.cleaned_data.get('link_to_employee'):
                employee = self.cleaned_data['link_to_employee']
                employee.user = user
                employee.save()
            
            elif self.cleaned_data.get('create_employee_profile'):
                if not Employee.objects.filter(user=user).exists():
                    line = None
                    manager = None
                    
                    if user_type == 'team_leader':
                        managed_lines = self.cleaned_data.get('managed_lines', [])
                        if managed_lines:
                            line = list(managed_lines)[0]
                            other_leaders = line.team_leaders.exclude(pk=user.pk)
                            manager = other_leaders.first() or user
                    
                    Employee.objects.create(
                        user=user,
                        first_name=user.first_name,
                        last_name=user.last_name,
                        matricule=self.cleaned_data.get('matricule') or f"USR-{user.pk}",
                        line=line,
                        manager=manager,
                    )
        
        return user


class QuickTeamLeaderAssignmentForm(forms.Form):
    """Formulaire rapide pour affecter des chefs d'équipe à des lignes"""
    
    user = forms.ModelChoiceField(
        queryset=User.objects.filter(is_staff=True, is_superuser=False),
        label="Chef d'équipe",
        widget=forms.Select(attrs={'class': 'form-control select2'})
    )
    
    lines = forms.ModelMultipleChoiceField(
        queryset=ProductionLine.objects.all(),
        widget=forms.CheckboxSelectMultiple(attrs={'class': 'form-check-input'}),
        label="Lignes à gérer"
    )
