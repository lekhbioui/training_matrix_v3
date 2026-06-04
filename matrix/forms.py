from asyncio import Task
from datetime import date, timedelta
from django import forms

from training_matrix import settings
from .models import Employee, PendingEmployee, ProductionLine, TrainingApprovalRequest, TrainingLevel
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


class TrainingRecordForm(forms.ModelForm):

    line = forms.ModelChoiceField(
        queryset=ProductionLine.objects.none(),
        required=False,
        label="Ligne de production",
        help_text="Sélectionnez la ligne sur laquelle cette formation est réalisée.",
        widget=forms.Select(attrs={
            'id': 'id_line',
            'class': 'form-select'
        })
    )

    date = forms.DateField(
        required=True,
        label="Date de formation",
        widget=forms.DateInput(attrs={
            'type': 'date',
            'class': 'form-control',
        }),
        help_text="Sélectionnez la date à laquelle la formation a eu lieu"
    )

    class Meta:
        model  = TrainingRecord
        fields = ['employee', 'task', 'level', 'date', 'trainer']
        labels = {
            "employee": "Employé",
            "task":     "Tâche",
            "level":    "Niveau",
            "date":     "Date de formation",
            "trainer":  "Formateur",
        }
        widgets = {
            'employee': forms.Select(attrs={
                'id': 'id_employee', 'class': 'form-select'
            }),
            'task': forms.Select(attrs={
                'id': 'id_task', 'class': 'form-select'
            }),
            'level': forms.Select(attrs={
                'class': 'form-select'
            }),
            'date': forms.DateInput(attrs={
                'type': 'date', 'class': 'form-control'
            }),
            'trainer': forms.Select(attrs={
                'class': 'form-select'
            }),
        }

    def __init__(self, *args, **kwargs):
        self.user             = kwargs.pop('user', None)
        self.selected_line_id = kwargs.pop('selected_line_id', None)

        self.rh_request_created   = False
        self.rh_request_info      = None
        self.team_leader_employee = None
        self.user_lines           = ProductionLine.objects.none()
        # ✅ MODIFIÉ : show_line_field = True dès qu'on a plusieurs lignes
        self.show_line_field      = False

        super().__init__(*args, **kwargs)

        today = timezone.now().date()
        self.fields['date'].widget.attrs['max'] = today.isoformat()

        if self.user:
            self.user_lines = ProductionLine.objects.filter(
                team_leaders=self.user
            )

            # Profil Employee du chef
            if hasattr(self.user, 'employee_profile'):
                try:
                    self.team_leader_employee = self.user.employee_profile
                except Exception:
                    pass

            if not self.team_leader_employee:
                self.team_leader_employee = Employee.objects.filter(
                    Q(first_name__iexact=self.user.first_name,
                      last_name__iexact=self.user.last_name) |
                    Q(matricule__iexact=self.user.username)
                ).first()

            if not self.user.is_superuser:
                # IDs des chefs d'équipe à exclure
                all_tl_user_ids = User.objects.filter(
                    managed_lines__isnull=False
                ).distinct().values_list('id', flat=True)

                tl_emp_ids = set(
                    Employee.objects.filter(
                        user_id__in=all_tl_user_ids
                    ).values_list('id', flat=True)
                )
                for tl_user in User.objects.filter(id__in=all_tl_user_ids):
                    match = Employee.objects.filter(
                        Q(first_name__iexact=tl_user.first_name,
                          last_name__iexact=tl_user.last_name) |
                        Q(matricule__iexact=tl_user.username)
                    ).first()
                    if match:
                        tl_emp_ids.add(match.id)

                # Employés visibles sur les lignes du chef
                employees_qs = Employee.objects.filter(
                    visible_lines__in=self.user_lines,
                    manager=self.user
                ).exclude(id__in=tl_emp_ids).distinct()

                self.fields['employee'].queryset = employees_qs.order_by(
                    'last_name', 'first_name'
                )

                # ✅ Ligne : toujours proposer les lignes du chef
                self.fields['line'].queryset  = self.user_lines
                self.fields['line'].required  = True   # ← Toujours obligatoire
                self.show_line_field          = True   # ← Toujours affiché

                # Pré-sélection si une ligne est déjà dans le contexte
                if self.selected_line_id:
                    try:
                        self.fields['line'].initial = int(self.selected_line_id)
                    except (ValueError, TypeError):
                        pass

                # ✅ Tâches : filtrées par ligne sélectionnée OU toutes les lignes
                if self.selected_line_id:
                    self.fields['task'].queryset = Task.objects.filter(
                        lines__id=self.selected_line_id
                    ).distinct().order_by('order', 'task_number')
                else:
                    self.fields['task'].queryset = Task.objects.filter(
                        lines__in=self.user_lines
                    ).distinct().order_by('order', 'task_number')

                self.fields['trainer'].required = False
                self.fields['trainer'].widget   = forms.HiddenInput()
                self.fields['trainer'].queryset = Employee.objects.none()

            else:
                # RH / superuser
                self.fields['line'].queryset     = ProductionLine.objects.all()
                self.fields['line'].required     = True
                self.fields['employee'].queryset = Employee.objects.all().order_by(
                    'last_name', 'first_name'
                )
                self.fields['task'].queryset     = Task.objects.all()
                self.fields['trainer'].queryset  = Employee.objects.all()
                self.show_line_field             = True
    def clean(self):
        cleaned_data  = super().clean()
        employee      = cleaned_data.get("employee")
        task          = cleaned_data.get("task")
        level         = cleaned_data.get("level")
        line          = cleaned_data.get("line")
        training_date = cleaned_data.get("date")

        # ── Validations date ────────────────────────────────────────────
        if not training_date:
            raise ValidationError({'date': "❌ La date de formation est obligatoire."})

        today    = timezone.now().date()
        days_ago = (today - training_date).days

        if training_date > today:
            raise ValidationError({'date': "❌ La date ne peut pas être dans le futur."})

        

        if not all([employee, task, level is not None]):
            return cleaned_data

        if not line:
            raise ValidationError({'line': "❌ Veuillez sélectionner la ligne de production."})

        effective_line = line
        new_level      = int(level)

        # ── Empêcher le chef de se former lui-même ──────────────────────
        if (self.team_leader_employee and
                employee.id == self.team_leader_employee.id):
            raise ValidationError(
                "❌ Vous ne pouvez pas enregistrer une formation pour vous-même."
            )

        # ── Vérifier si c'est un chef d'équipe ──────────────────────────
        target_is_tl = False
        if employee.user:
            target_is_tl = ProductionLine.objects.filter(
                team_leaders=employee.user
            ).exists()
        if not target_is_tl:
            match_user = User.objects.filter(
                Q(first_name__iexact=employee.first_name,
                last_name__iexact=employee.last_name) |
                Q(username__iexact=employee.matricule)
            ).first()
            if match_user:
                target_is_tl = ProductionLine.objects.filter(
                    team_leaders=match_user
                ).exists()
        if target_is_tl:
            raise ValidationError(
                f"❌ {employee.first_name} {employee.last_name} est un chef d'équipe."
            )

        # ── Vérifier visibilité sur la ligne ────────────────────────────
        if (not self.user.is_superuser and
                not employee.visible_lines.filter(pk=effective_line.pk).exists()):
            raise ValidationError(
                f"❌ {employee.first_name} {employee.last_name} "
                f"n'est pas visible sur la ligne '{effective_line.name}'."
            )

        # ── Tâche appartient à la ligne ──────────────────────────────────
        if not task.lines.filter(pk=effective_line.pk).exists():
            raise ValidationError(
                f"❌ La tâche '{task}' n'appartient pas à la ligne '{effective_line.name}'."
            )

        # ════════════════════════════════════════════════════════════════
        # ✅ LOGIQUE DE PROGRESSION PAR LIGNE
        #
        # La progression I → L → U est INDÉPENDANTE par ligne.
        #
        #   Ligne X : I → L → U  (progression propre à Ligne X)
        #   Ligne Y : I → L → U  (progression propre à Ligne Y)
        #
        # Pour enregistrer L sur Ligne Y :
        #   → Il FAUT un niveau I sur Ligne Y (pas Ligne X)
        #
        # Pour enregistrer U sur Ligne Y :
        #   → Il FAUT un niveau L sur Ligne Y (pas Ligne X)
        # ════════════════════════════════════════════════════════════════

        if new_level == TrainingLevel.IN_TRAINING:
            # ── Niveau I ────────────────────────────────────────────────
            # Vérifier le doublon sur CETTE ligne
            if not self.instance.pk:
                if TrainingRecord.objects.filter(
                    employee=employee,
                    task=task,
                    level=TrainingLevel.IN_TRAINING,
                    lines=effective_line
                ).exists():
                    raise ValidationError(
                        f"❌ Un niveau I existe déjà pour la tâche '{task}' "
                        f"sur la ligne '{effective_line.name}'."
                    )

            # ── 3 jours d'ancienneté ─────────────────────────────────────
            if getattr(employee, 'hire_date', None):
                days_since_hire = (training_date - employee.hire_date).days
                if days_since_hire < 3:
                    raise ValidationError(
                        f"❌ Ancienneté insuffisante : {days_since_hire} jour(s). "
                        f"Minimum : 3 jours."
                    )

            return cleaned_data

        elif new_level == TrainingLevel.SUPERVISED:
            # ════════════════════════════════════════════════════════════
            # Niveau L → chercher niveau I SUR CETTE MÊME LIGNE
            # ════════════════════════════════════════════════════════════

            # ✅ Chercher I uniquement sur effective_line
            record_I_this_line = TrainingRecord.objects.filter(
                employee=employee,
                task=task,
                level=TrainingLevel.IN_TRAINING,
                lines=effective_line,           # ← MÊME LIGNE UNIQUEMENT
            ).exclude(date__isnull=True).order_by('date').first()

            if not record_I_this_line:
                # ✅ Vérifier si le I existe sur D'AUTRES lignes
                # pour donner un message d'erreur plus précis
                record_I_other_lines = TrainingRecord.objects.filter(
                    employee=employee,
                    task=task,
                    level=TrainingLevel.IN_TRAINING,
                ).exclude(
                    lines=effective_line
                ).exclude(date__isnull=True)

                other_lines_with_I = []
                for rec in record_I_other_lines:
                    for l in rec.lines.all():
                        if l.name not in other_lines_with_I:
                            other_lines_with_I.append(l.name)

                if other_lines_with_I:
                    # L'opérateur a un I mais sur une autre ligne
                    raise ValidationError(
                        f"❌ Impossible d'enregistrer le niveau L sur "
                        f"'{effective_line.name}'.\n\n"
                        f"L'opérateur {employee.first_name} {employee.last_name} "
                        f"n'a pas de niveau I pour la tâche '{task}' "
                        f"sur la ligne '{effective_line.name}'.\n\n"
                        f"Le niveau I existe sur : "
                        f"{', '.join(other_lines_with_I)}\n\n"
                        f"💡 Enregistrez d'abord un niveau I sur "
                        f"'{effective_line.name}' avant de passer au niveau L."
                    )
                else:
                    # Pas de I sur aucune ligne
                    raise ValidationError(
                        f"❌ Impossible d'enregistrer le niveau L sur "
                        f"'{effective_line.name}'.\n\n"
                        f"L'opérateur {employee.first_name} {employee.last_name} "
                        f"n'a pas encore de niveau I pour la tâche '{task}' "
                        f"sur cette ligne.\n\n"
                        f"💡 Enregistrez d'abord un niveau I sur "
                        f"'{effective_line.name}'."
                    )

            # ── Doublon L sur cette ligne ─────────────────────────────────
            if not self.instance.pk:
                if TrainingRecord.objects.filter(
                    employee=employee,
                    task=task,
                    level=TrainingLevel.SUPERVISED,
                    lines=effective_line
                ).exists():
                    raise ValidationError(
                        f"❌ Un niveau L existe déjà pour la tâche '{task}' "
                        f"sur la ligne '{effective_line.name}'."
                    )

            # ── Délai 7 jours depuis le I sur CETTE ligne ────────────────
            days_since_I = (training_date - record_I_this_line.date).days

            if days_since_I < 7:
                raise ValidationError(
                    f"⏳ Attendre 7 jours minimum après le niveau I.\n"
                    f"Date niveau I  : "
                    f"{record_I_this_line.date.strftime('%d/%m/%Y')} "
                    f"(ligne '{effective_line.name}')\n"
                    f"Date saisie    : {training_date.strftime('%d/%m/%Y')}\n"
                    f"Jours écoulés  : {days_since_I}/7\n"
                    f"Date possible  : "
                    f"{(record_I_this_line.date + timedelta(days=7)).strftime('%d/%m/%Y')}"
                )

            # ── Créer demande RH ──────────────────────────────────────────
            self._create_rh_request(
                employee, task, new_level,
                days_since_I, training_date, effective_line
            )
            self.rh_request_created = True
            self.rh_request_info    = {
                'employee':   employee,
                'task':       task,
                'level':      'L (Supervisé)',
                'level_code': 'L',
                'line':       effective_line,
            }
            raise ValidationError("RH_REQUEST_CREATED", code='rh_request_pending')

        elif new_level == TrainingLevel.TRAINER:
            # ════════════════════════════════════════════════════════════
            # Niveau U → chercher niveau L SUR CETTE MÊME LIGNE
            # ════════════════════════════════════════════════════════════

            # ✅ Chercher L uniquement sur effective_line
            record_L_this_line = TrainingRecord.objects.filter(
                employee=employee,
                task=task,
                level=TrainingLevel.SUPERVISED,
                lines=effective_line,           # ← MÊME LIGNE UNIQUEMENT
            ).exclude(date__isnull=True).order_by('date').first()

            if not record_L_this_line:
                # Vérifier si L existe sur d'autres lignes
                record_L_other_lines = TrainingRecord.objects.filter(
                    employee=employee,
                    task=task,
                    level=TrainingLevel.SUPERVISED,
                ).exclude(
                    lines=effective_line
                ).exclude(date__isnull=True)

                other_lines_with_L = []
                for rec in record_L_other_lines:
                    for l in rec.lines.all():
                        if l.name not in other_lines_with_L:
                            other_lines_with_L.append(l.name)

                if other_lines_with_L:
                    raise ValidationError(
                        f"❌ Impossible d'enregistrer le niveau U sur "
                        f"'{effective_line.name}'.\n\n"
                        f"L'opérateur {employee.first_name} {employee.last_name} "
                        f"n'a pas de niveau L pour la tâche '{task}' "
                        f"sur la ligne '{effective_line.name}'.\n\n"
                        f"Le niveau L existe sur : "
                        f"{', '.join(other_lines_with_L)}\n\n"
                        f"💡 Enregistrez d'abord un niveau L sur "
                        f"'{effective_line.name}' avant de passer au niveau U."
                    )
                else:
                    raise ValidationError(
                        f"❌ Impossible d'enregistrer le niveau U sur "
                        f"'{effective_line.name}'.\n\n"
                        f"L'opérateur {employee.first_name} {employee.last_name} "
                        f"n'a pas encore de niveau L pour la tâche '{task}' "
                        f"sur cette ligne.\n\n"
                        f"💡 Enregistrez d'abord un niveau L sur "
                        f"'{effective_line.name}'."
                    )

            # ── Doublon U sur cette ligne ─────────────────────────────────
            if not self.instance.pk:
                if TrainingRecord.objects.filter(
                    employee=employee,
                    task=task,
                    level=TrainingLevel.TRAINER,
                    lines=effective_line
                ).exists():
                    raise ValidationError(
                        f"❌ Un niveau U existe déjà pour la tâche '{task}' "
                        f"sur la ligne '{effective_line.name}'."
                    )

            # ── Délai 21 jours depuis le L sur CETTE ligne ───────────────
            days_since_L = (training_date - record_L_this_line.date).days

            if days_since_L < 21:
                raise ValidationError(
                    f"⏳ Attendre 21 jours minimum après le niveau L.\n"
                    f"Date niveau L  : "
                    f"{record_L_this_line.date.strftime('%d/%m/%Y')} "
                    f"(ligne '{effective_line.name}')\n"
                    f"Date saisie    : {training_date.strftime('%d/%m/%Y')}\n"
                    f"Jours écoulés  : {days_since_L}/21\n"
                    f"Date possible  : "
                    f"{(record_L_this_line.date + timedelta(days=21)).strftime('%d/%m/%Y')}"
                )

            # ── Créer demande RH ──────────────────────────────────────────
            self._create_rh_request(
                employee, task, new_level,
                days_since_L, training_date, effective_line
            )
            self.rh_request_created = True
            self.rh_request_info    = {
                'employee':   employee,
                'task':       task,
                'level':      'U (Formateur)',
                'level_code': 'U',
                'line':       effective_line,
            }
            raise ValidationError("RH_REQUEST_CREATED", code='rh_request_pending')

        return cleaned_data


    def _create_rh_request(
        self, employee, task, new_level,
        days_since_last, training_date, line=None
    ):
        """
        ✅ Vérifie le doublon PAR LIGNE avant de créer la demande.
        """
        if line:
            exists = TrainingApprovalRequest.objects.filter(
                employee=employee,
                task=task,
                requested_level=new_level,
                approved__isnull=True,
                line=line              # ✅ FILTRE PAR LIGNE
            ).exists()
        else:
            exists = TrainingApprovalRequest.objects.filter(
                employee=employee,
                task=task,
                requested_level=new_level,
                approved__isnull=True,
                line__isnull=True
            ).exists()

        if exists:
            return

        request_obj = TrainingApprovalRequest.objects.create(
            employee=employee,
            task=task,
            requested_level=new_level,
            requested_by=self.user,
            training_date=training_date,
            line=line,                 # ✅ LIGNE STOCKÉE
        )

        self._send_professional_email(
            employee=employee,
            task=task,
            new_level=new_level,
            days_since_last=days_since_last,
            training_date=training_date,
            request_obj=request_obj,
            line=line
        )

    def _send_professional_email(self, employee, task, new_level, days_since_last, training_date, request_obj, line=None):
        """Envoi d'email HTML professionnel avec logo image intégré"""
        
        from django.core.mail import EmailMultiAlternatives
        from django.template.loader import render_to_string
        from django.utils.html import strip_tags
        from django.conf import settings
        import logging
        
        logger = logging.getLogger(__name__)
        
        LEVEL_CONFIG = {
            TrainingLevel.SUPERVISED: {
                'name': 'L (Supervisé)',
                'color': '#3b82f6',
                'color_dark': '#1d4ed8',
                'required_days': '7 jours',
                'emoji': '📘'
            },
            TrainingLevel.TRAINER: {
                'name': 'U (Formateur)',
                'color': '#8b5cf6',
                'color_dark': '#6d28d9',
                'required_days': '21 jours',
                'emoji': '🎓'
            }
        }
        
        level_config = LEVEL_CONFIG.get(new_level, {
            'name': str(new_level),
            'color': '#6b7280',
            'color_dark': '#4b5563',
            'required_days': 'N/A',
            'emoji': '📋'
        })

        base_url = getattr(settings, 'APP_BASE_URL', 'http://localhost:8000')
        
        if line:
            production_line_display = f"{line.name} ({line.code})"
        elif employee.line:
            production_line_display = str(employee.line)
        else:
            production_line_display = 'Non assignée'
        
        context = {
            'company_name': getattr(settings, 'COMPANY_NAME', 'GRUPO PREMO'),
            'company_website': getattr(settings, 'COMPANY_WEBSITE', 'https://www.grupopremo.com'),
            'support_email': getattr(settings, 'SUPPORT_EMAIL', 'support@grupopremo.com'),
            'current_year': training_date.year,
            'request_id': request_obj.pk,
            'employee_name': f"{employee.first_name} {employee.last_name}",
            'employee_matricule': employee.matricule if hasattr(employee, 'matricule') else 'N/A',
            'task_name': str(task),
            'production_line': production_line_display,
            'requester_name': self.user.get_full_name() or self.user.username,
            'request_date': training_date.strftime('%d %B %Y'),  # ✅ Date de formation
            'training_date': training_date.strftime('%d/%m/%Y'),  # ✅ NOUVEAU
            'level_name': level_config['name'],
            'level_color': level_config['color'],
            'level_color_dark': level_config['color_dark'],
            'required_days': level_config['required_days'],
            'level_emoji': level_config['emoji'],
            'days_elapsed': days_since_last,
            'approval_url': f"{base_url}/matrix/rh/approve/{request_obj.pk}/",
            'reject_url': f"{base_url}/matrix/rh/reject/{request_obj.pk}/",
            'dashboard_url': f"{base_url}/matrix/rh/pending/",
        }

        subject = (
            f"📋 [Action Requise] Formation {level_config['name']} - "
            f"{employee.first_name} {employee.last_name}"
        )

        try:
            html_content = render_to_string('emails/rh_request.html', context)
            text_content = self._get_fallback_text(context)
        except Exception as e:
            logger.warning(f"⚠️ Template non trouvé, utilisation du fallback: {e}")
            html_content = self._get_fallback_html(context)
            text_content = self._get_fallback_text(context)

        try:
            rh_emails = getattr(settings, 'RH_EMAILS', [])
            if not rh_emails:
                logger.error("❌ RH_EMAILS non configuré dans settings.py")
                return
            
            email = EmailMultiAlternatives(
                subject=subject,
                body=text_content,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=rh_emails,
            )
            
            email.attach_alternative(html_content, "text/html")
            email.mixed_subtype = 'related'
            
            logo_attached = self._attach_logo(email, settings)
            
            if logo_attached:
                logger.info("✅ Logo image attaché à l'email")
            else:
                logger.warning("⚠️ Email sera envoyé sans logo")

            email.send(fail_silently=False)
            logger.info(
                f"✅ Email RH envoyé avec succès pour {employee} "
                f"(Demande #{request_obj.pk})"
            )
            
        except Exception as e:
            logger.error(f"❌ Erreur envoi email RH: {e}")

    def _attach_logo(self, email, settings):
        """Attache le logo de l'entreprise à l'email."""
        import os
        from email.mime.image import MIMEImage
        import logging
        
        logger = logging.getLogger(__name__)
        
        possible_paths = [
            os.path.join(settings.BASE_DIR, 'matrix', 'static', 'images', 'logo.png'),
            os.path.join(settings.BASE_DIR, 'matrix', 'static', 'images', 'logo.jpg'),
            os.path.join(settings.BASE_DIR, 'matrix', 'static', 'images', 'logo.jpeg'),
            os.path.join(settings.BASE_DIR, 'matrix', 'static', 'img', 'logo.png'),
            os.path.join(settings.BASE_DIR, 'static', 'images', 'logo.png'),
            os.path.join(settings.BASE_DIR, 'static', 'images', 'logo.jpg'),
            os.path.join(settings.BASE_DIR, 'static', 'img', 'logo.png'),
            os.path.join(settings.BASE_DIR, 'staticfiles', 'images', 'logo.png'),
        ]
        
        for logo_path in possible_paths:
            if os.path.exists(logo_path):
                try:
                    with open(logo_path, 'rb') as f:
                        logo_data = f.read()
                    
                    if logo_path.lower().endswith('.png'):
                        subtype = 'png'
                    elif logo_path.lower().endswith(('.jpg', '.jpeg')):
                        subtype = 'jpeg'
                    else:
                        subtype = 'png'
                    
                    logo_image = MIMEImage(logo_data, _subtype=subtype)
                    logo_image.add_header('Content-ID', '<company_logo>')
                    logo_image.add_header(
                        'Content-Disposition', 
                        'inline', 
                        filename=f'logo.{subtype}'
                    )
                    
                    email.attach(logo_image)
                    logger.info(f"✅ Logo trouvé et attaché: {logo_path}")
                    return True
                    
                except Exception as e:
                    logger.warning(f"⚠️ Erreur lecture logo {logo_path}: {e}")
                    continue
        
        logger.warning(f"⚠️ Aucun logo trouvé.")
        return False

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

class RequalificationForm(forms.Form):
    """Formulaire pour enregistrer une requalification"""
    
    RESULT_CHOICES = [
        ('PASS', 'Réussi - Maintien du niveau'),
        ('UPGRADE', 'Réussi - Montée en niveau'),
        ('DOWNGRADE', 'Échec - Descente en niveau'),
        ('RETRAIN', 'À reformer complètement'),
    ]
    
    employee_id = forms.IntegerField(widget=forms.HiddenInput())
    task_id = forms.IntegerField(widget=forms.HiddenInput())
    
    # ============================================
    # NOUVEAU CHAMP : Nom du requalificateur
    # ============================================
    requalifier_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Entrez le nom du requalificateur...',
            'id': 'requalifier-name'
        }),
        label="Nom du requalificateur",
        required=False  # Mettez False si optionnel
    )
    
    result = forms.ChoiceField(
        choices=RESULT_CHOICES,
        widget=forms.Select(attrs={
            'class': 'form-select',
            'id': 'result-select'
        }),
        label="Résultat de la requalification"
    )
    
    new_level = forms.ChoiceField(
        choices=TrainingLevel.choices,
        widget=forms.Select(attrs={
            'class': 'form-select',
            'id': 'new-level-select'
        }),
        label="Nouveau niveau"
    )
    
    requalification_date = forms.DateField(
        widget=forms.DateInput(attrs={
            'class': 'form-control',
            'type': 'date',
            'id': 'requalification-date'
        }),
        label="Date de requalification",
        required=False  # Optionnel, utilisera la date du jour par défaut
    )
    
    notes = forms.CharField(
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 3,
            'placeholder': 'Commentaires sur la requalification...'
        }),
        required=False,
        label="Notes"
    )


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