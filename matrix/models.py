from django.utils import timezone  
from django.db import models
from django.db import transaction
import logging
from django.contrib.auth.models import User
from django.db.models import Q
from django.conf import settings
from django.core.exceptions import ValidationError
from pathlib import Path
from uuid import uuid4
from django.db import models
from django.utils import timezone

def training_plan_upload_to(instance, filename):
    """A unique name prevents a replacement from overwriting the reviewed file."""
    return f"training_plans/{instance.pk}/{uuid4().hex}{Path(filename).suffix.lower()}"


def requalification_plan_upload_to(instance, filename):
    # The request has no primary key yet during its initial upload.
    return f"requalification_plans/{uuid4().hex}{Path(filename).suffix.lower()}"


def delete_unused_requalification_plan(storage, name):
    if name:
        try:
            storage.delete(name)
        except OSError:
            logging.getLogger(__name__).exception("Could not remove unused requalification plan %s", name)


class RequalificationQuerySet(models.QuerySet):
    def update(self, **kwargs):
        protected = {"approved", "training_plan_image", "requested_by", "requested_by_id", "approval_request",
                     "approval_request_id", "training_record", "training_record_id"}
        if protected.intersection(kwargs):
            raise ValidationError("Utilisez le workflow de requalification pour cette modification.")
        return super().update(**kwargs)

    def bulk_create(self, *args, **kwargs):
        raise ValidationError("Utilisez le formulaire de requalification avec un plan valide.")

    def _record_decision(self, **kwargs):
        """Internal conditional write used by the transactional decision service."""
        return super().update(**kwargs)


class ProductionLine(models.Model):
    code = models.CharField(max_length=50, unique=True, default="LINE001")
    name = models.CharField(max_length=100)
    team_leaders = models.ManyToManyField(
        User,
        blank=True,
        related_name="managed_lines",
        help_text="Chefs d'équipe assignés à cette ligne"
    )

    def __str__(self):
        return self.name





# models.py

class Employee(models.Model):
    first_name = models.CharField(max_length=100)
    last_name  = models.CharField(max_length=100)
    matricule  = models.CharField(max_length=50, unique=True)

    line = models.ForeignKey(
        ProductionLine,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='primary_employees'
    )

    visible_lines = models.ManyToManyField(
        ProductionLine,
        blank=True,
        related_name='visible_employees',
        verbose_name="Lignes de visibilité",
        help_text=(
            "L'opérateur apparaîtra dans la matrice "
            "de chacune de ces lignes"
        )
    )

    manager = models.ForeignKey(
        User, null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='managed_employees'
    )
    hire_date = models.DateField(null=True, blank=True)
    user      = models.OneToOneField(
        User, null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='employee_profile'
    )

    # ✅ NOUVEAU
    is_registered = models.BooleanField(
        default=False,
        verbose_name="Pré-enregistré par RH",
        help_text=(
            "Si True, cet employé a été pré-enregistré par RH "
            "et peut être retrouvé par matricule par les chefs."
        )
    )

    def __str__(self):
        return (
            f"{self.last_name} {self.first_name} ({self.matricule})"
        )

    def is_team_leader(self):
        if self.user:
            return ProductionLine.objects.filter(
                team_leaders=self.user
            ).exists()
        return False

    def get_managed_lines(self):
        if self.user:
            return ProductionLine.objects.filter(
                team_leaders=self.user
            )
        return ProductionLine.objects.none()


# ********************************************
# models.py
class PendingEmployee(models.Model):
    """
    Employé pré-enregistré par RH.
    N'est PAS encore un opérateur.
    Devient Employee uniquement quand le chef l'ajoute dans sa matrice.
    """
    matricule  = models.CharField(max_length=50, unique=True)
    first_name = models.CharField(max_length=100)
    last_name  = models.CharField(max_length=100)
    hire_date  = models.DateField(null=True, blank=True)

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='pending_employees_created',
        verbose_name="Créé par RH"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = "Employé en attente"
        verbose_name_plural = "Employés en attente"
        ordering            = ['-created_at']

    def __str__(self):
        return f"{self.matricule} — {self.first_name} {self.last_name}"
# ******************************************

class Task(models.Model):
    code = models.CharField(max_length=50, unique=True)  
    task_number = models.PositiveIntegerField(default=0)  
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=0)
    group_title = models.CharField(max_length=200, blank=True, default="")
    
    lines = models.ManyToManyField(
        "ProductionLine",
        related_name="tasks",
        blank=True
    )

    def __str__(self):
        return f"{self.task_number} - {self.title}"




class TrainingLevel(models.IntegerChoices):
    IN_TRAINING = 0, "I - En période de formation"
    SUPERVISED = 1, "L -Approuvé"
   
    TRAINER = 3, "U - Préparé pour former d'autres"

class MatrixEntry(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE)
    task = models.ForeignKey(Task, on_delete=models.CASCADE)
    level = models.IntegerField(choices=TrainingLevel.choices, default=TrainingLevel.IN_TRAINING)
    last_trained = models.DateField(null=True, blank=True)
    trainer = models.ForeignKey(
        Employee,
        null=True,
        blank=True,
        related_name="trained_entries",
        on_delete=models.SET_NULL
    )
    note = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    transfer_date = models.DateField(default=timezone.now)  

    class Meta:
        unique_together = ('employee', 'task')

    def __str__(self):
        return f"{self.employee} - {self.task} : {self.get_level_display()}"

class TrainingRecord(models.Model):
    """
    Représente un niveau de formation attribué à un employé
    pour une tâche et une ou plusieurs lignes de production.

    Le niveau n'est pris en compte dans la matrice que lorsque
    approval_status = APPROVED.
    """

    class ApprovalStatus(models.TextChoices):
        PENDING = "pending", "En attente RH"
        APPROVED = "approved", "Approuvé"
        REJECTED = "rejected", "Refusé"

    employee = models.ForeignKey(
        "Employee",
        on_delete=models.CASCADE,
        related_name="training_records",
        verbose_name="Employé",
    )

    task = models.ForeignKey(
        "Task",
        on_delete=models.CASCADE,
        related_name="training_records",
        verbose_name="Tâche",
    )

    level = models.IntegerField(
        choices=TrainingLevel.choices,
        verbose_name="Niveau",
    )

    date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Date de formation",
)

    trainer = models.ForeignKey(
        "Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="trainings_given",
        verbose_name="Formateur",
    )

    lines = models.ManyToManyField(
        "ProductionLine",
        related_name="training_records",
        verbose_name="Lignes de production",
    )

    note = models.TextField(
        blank=True,
        default="",
        verbose_name="Note",
    )

    # ============================================================
    # VALIDATION RH
    # ============================================================

    approval_status = models.CharField(
        max_length=20,
        choices=ApprovalStatus.choices,
        default=ApprovalStatus.APPROVED,
        db_index=True,
        verbose_name="Statut d'approbation",
        help_text=(
            "Les anciennes formations sont considérées comme "
            "approuvées. Un nouveau niveau I doit être créé avec "
            "le statut En attente RH."
        ),
    )

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_training_records",
        verbose_name="Approuvé ou refusé par",
    )

    approved_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Date de décision RH",
    )

    rejection_reason = models.TextField(
        blank=True,
        default="",
        verbose_name="Motif du refus",
    )

    # ============================================================
    # AUDIT
    # ============================================================

    created_at = models.DateTimeField(
        default=timezone.now,
        editable=False,
        verbose_name="Créé le",
    )

    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="Modifié le",
    )

    class Meta:
        verbose_name = "Enregistrement de formation"
        verbose_name_plural = "Enregistrements de formation"

        ordering = [
            "-date",
            "-created_at",
        ]

        indexes = [
            models.Index(
                fields=[
                    "employee",
                    "task",
                    "level",
                ],
                name="training_emp_task_lvl_idx",
            ),
            models.Index(
                fields=[
                    "approval_status",
                    "date",
                ],
                name="training_status_date_idx",
            ),
        ]

    def __str__(self):
        level_display = self.get_level_display()

        return (
            f"{self.employee} - "
            f"{self.task} - "
            f"{level_display}"
        )

    def clean(self):
        """
        Vérifications de cohérence du statut RH.
        """

        super().clean()

        if (
            self.approval_status
            == self.ApprovalStatus.REJECTED
            and not self.rejection_reason.strip()
        ):
            raise ValidationError(
                {
                    "rejection_reason": (
                        "Le motif du refus est obligatoire "
                        "lorsque la formation est refusée."
                    )
                }
            )

        if (
            self.approval_status
            == self.ApprovalStatus.PENDING
            and self.approved_at
        ):
            raise ValidationError(
                {
                    "approved_at": (
                        "Une formation en attente ne peut pas "
                        "avoir de date de décision RH."
                    )
                }
            )

    @property
    def is_pending(self):
        return (
            self.approval_status
            == self.ApprovalStatus.PENDING
        )

    @property
    def is_approved(self):
        return (
            self.approval_status
            == self.ApprovalStatus.APPROVED
        )

    @property
    def is_rejected(self):
        return (
            self.approval_status
            == self.ApprovalStatus.REJECTED
        )

    def mark_as_pending(self):
        """
        Place la formation en attente de validation RH.
        """

        self.approval_status = self.ApprovalStatus.PENDING
        self.approved_by = None
        self.approved_at = None
        self.rejection_reason = ""

        self.save(
            update_fields=[
                "approval_status",
                "approved_by",
                "approved_at",
                "rejection_reason",
                "updated_at",
            ]
        )

    def approve(self, user):
        """
        Approuve la formation.

        La mise à jour de MatrixEntry doit être effectuée
        dans la vue ou dans un service transactionnel.
        """

        approval_request = TrainingApprovalRequest.objects.filter(training_record=self).first()
        if approval_request:
            approval_request.validate_training_plan_for_approval()
        self.approval_status = self.ApprovalStatus.APPROVED
        self.approved_by = user
        self.approved_at = timezone.now()
        self.rejection_reason = ""

        self.save(
            update_fields=[
                "approval_status",
                "approved_by",
                "approved_at",
                "rejection_reason",
                "updated_at",
            ]
        )

    def reject(self, user, reason):
        """
        Refuse la formation.
        """

        reason = str(reason or "").strip()

        if not reason:
            raise ValidationError(
                "Le motif du refus est obligatoire."
            )

        self.approval_status = self.ApprovalStatus.REJECTED
        self.approved_by = user
        self.approved_at = timezone.now()
        self.rejection_reason = reason

        self.save(
            update_fields=[
                "approval_status",
                "approved_by",
                "approved_at",
                "rejection_reason",
                "updated_at",
            ]
        )





# models.py

class TrainingApprovalRequest(models.Model):
    """
    Demande d'approbation envoyée au service RH.

    approved :
    - None  : demande en attente ;
    - True  : demande approuvée ;
    - False : demande refusée.

    Pour le niveau I, training_record contient le TrainingRecord
    créé en attente avec son questionnaire.

    Pour les niveaux L et U, training_record peut rester vide
    jusqu'à l'approbation RH.
    """

    employee = models.ForeignKey(
        "Employee",
        on_delete=models.CASCADE,
        related_name="training_approval_requests",
        verbose_name="Employé",
    )

    task = models.ForeignKey(
        "Task",
        on_delete=models.CASCADE,
        related_name="training_approval_requests",
        verbose_name="Tâche",
    )

    requested_level = models.IntegerField(
        choices=TrainingLevel.choices,
        verbose_name="Niveau demandé",
    )

    line = models.ForeignKey(
        "ProductionLine",
        on_delete=models.CASCADE,
        related_name="training_approval_requests",
        null=True,
        blank=True,
        verbose_name="Ligne de production",
    )

    training_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Date de formation",
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_training_approvals",
        verbose_name="Demandé par",
    )

    date_requested = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name="Date de la demande",
    )

    training_plan_image = models.ImageField(
        upload_to=training_plan_upload_to,
        blank=True,
        null=True,
        verbose_name="Plan de formation",
    )

    # ============================================================
    # LIEN VERS LA FORMATION
    # ============================================================

    training_record = models.OneToOneField(
        TrainingRecord,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="rh_approval_request",
        verbose_name="Formation associée",
        help_text=(
            "Pour le niveau I, la formation est créée en attente "
            "avant la validation RH."
        ),
    )

    # ============================================================
    # DÉCISION RH
    # ============================================================

    approved = models.BooleanField(
        null=True,
        blank=True,
        default=None,
        db_index=True,
        verbose_name="Décision RH",
        help_text=(
            "Vide = en attente, Oui = approuvé, "
            "Non = refusé."
        ),
    )

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_training_approvals",
        verbose_name="Traité par",
    )

    reviewed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Date de traitement",
    )

    rejection_reason = models.TextField(
        blank=True,
        default="",
        verbose_name="Motif du refus",
    )

    notification_seen = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name="Notification consultée",
    )

    class Meta:
        verbose_name = "Demande d'approbation de formation"
        verbose_name_plural = (
            "Demandes d'approbation de formation"
        )

        ordering = [
            "-date_requested",
        ]

        indexes = [
            models.Index(
                fields=[
                    "approved",
                    "date_requested",
                ],
                name="approval_pending_date_idx",
            ),
            models.Index(
                fields=[
                    "employee",
                    "task",
                    "requested_level",
                ],
                name="approval_emp_task_lvl_idx",
            ),
            models.Index(
                fields=[
                    "line",
                    "approved",
                ],
                name="approval_line_status_idx",
            ),
        ]

    def __str__(self):
        status = self.status_label

        return (
            f"{self.employee} - "
            f"{self.task} - "
            f"{self.get_requested_level_display()} - "
            f"{status}"
        )

    def clean(self):
        """
        Contrôle la cohérence de la décision RH.
        """

        super().clean()

        # Preserve historical approvals, but do not allow a new approval in admin.
        if self.approved is True and self.requires_training_plan:
            historical_approval = type(self).objects.filter(
                pk=self.pk, approved=True, requested_level=self.requested_level,
            ).exists()
            if not historical_approval:
                self.validate_training_plan_for_approval()

        if self.approved is None:
            if self.reviewed_by:
                raise ValidationError(
                    {
                        "reviewed_by": (
                            "Une demande en attente ne peut pas "
                            "avoir d'utilisateur RH associé."
                        )
                    }
                )

            if self.reviewed_at:
                raise ValidationError(
                    {
                        "reviewed_at": (
                            "Une demande en attente ne peut pas "
                            "avoir de date de traitement."
                        )
                    }
                )

        if (
            self.approved is False
            and not self.rejection_reason.strip()
        ):
            raise ValidationError(
                {
                    "rejection_reason": (
                        "Le motif du refus est obligatoire."
                    )
                }
            )

        if (
            self.training_record
            and self.training_record.employee_id
            != self.employee_id
        ):
            raise ValidationError(
                {
                    "training_record": (
                        "La formation associée n'appartient pas "
                        "à l'employé sélectionné."
                    )
                }
            )

        if (
            self.training_record
            and self.training_record.task_id
            != self.task_id
        ):
            raise ValidationError(
                {
                    "training_record": (
                        "La tâche de la formation associée ne "
                        "correspond pas à la demande."
                    )
                }
            )

        if (
            self.training_record
            and self.training_record.level
            != self.requested_level
        ):
            raise ValidationError(
                {
                    "training_record": (
                        "Le niveau de la formation associée ne "
                        "correspond pas au niveau demandé."
                    )
                }
            )

    @property
    def is_pending(self):
        return self.approved is None

    @property
    def is_approved(self):
        return self.approved is True

    @property
    def is_rejected(self):
        return self.approved is False

    @property
    def status_label(self):
        if self.approved is True:
            return "Approuvée"

        if self.approved is False:
            return "Refusée"

        return "En attente RH"

    def approve_request(self, user):
        """
        Marque uniquement la demande comme approuvée.

        La création de MatrixEntry doit rester dans la vue ou
        dans un service entouré par transaction.atomic().
        """

        self.validate_training_plan_for_approval()
        self.approved = True
        self.reviewed_by = user
        self.reviewed_at = timezone.now()
        self.rejection_reason = ""

        self.save(
            update_fields=[
                "approved",
                "reviewed_by",
                "reviewed_at",
                "rejection_reason",
            ]
        )

        if self.training_record:
            self.training_record.approve(user)

    @property
    def requires_training_plan(self):
        return self.requested_level in (
            TrainingLevel.SUPERVISED, TrainingLevel.TRAINER,
        )

    @property
    def has_training_plan(self):
        if not self.training_plan_image:
            return False
        try:
            return self.training_plan_image.storage.exists(self.training_plan_image.name)
        except OSError:
            return False

    def validate_training_plan_for_approval(self):
        if self.requires_training_plan and not self.has_training_plan:
            raise ValidationError(
                "Approbation impossible : le plan de formation n’a pas encore été "
                "ajouté par le Chef d’équipe ou le fichier est indisponible."
            )

    def reject_request(self, user, reason):
        """
        Refuse la demande et la formation associée.
        """

        reason = str(reason or "").strip()

        if not reason:
            raise ValidationError(
                "Le motif du refus est obligatoire."
            )

        self.approved = False
        self.reviewed_by = user
        self.reviewed_at = timezone.now()
        self.rejection_reason = reason

        self.save(
            update_fields=[
                "approved",
                "reviewed_by",
                "reviewed_at",
                "rejection_reason",
            ]
        )

        if self.training_record:
            self.training_record.reject(
                user=user,
                reason=reason,
            )




class LineTransfer(models.Model):
    """
    Historique des transferts d'employés entre lignes de production.
    """
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='line_transfers',
        verbose_name="Employé"
    )
    
    from_line = models.ForeignKey(
        ProductionLine,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='transfers_from',
        verbose_name="Ancienne ligne"
    )
    
    to_line = models.ForeignKey(
        ProductionLine,
        on_delete=models.CASCADE,
        related_name='transfers_to',
        verbose_name="Nouvelle ligne"
    )
    
    transfer_date = models.DateField(
        default=timezone.now,
        verbose_name="Date de transfert"
    )
    
    reason = models.TextField(
        blank=True,
        verbose_name="Raison du transfert"
    )
    
    transferred_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Transféré par"
    )

    class Meta:
        verbose_name = "Transfert de ligne"
        verbose_name_plural = "Transferts de lignes"
        ordering = ['-transfer_date']
        indexes = [
            models.Index(fields=['employee', '-transfer_date']),
        ]

    def __str__(self):
        from_line_name = self.from_line.name if self.from_line else "Nouvelle embauche"
        return f"{self.employee.last_name} {self.employee.first_name}: {from_line_name} → {self.to_line.name} ({self.transfer_date})"
    
class RequalificationRecord(models.Model):
    """Enregistrement des requalifications après 188 jours"""

    objects = RequalificationQuerySet.as_manager()
    approval_request = models.OneToOneField(
        "RequalificationApprovalRequest", on_delete=models.PROTECT,
        null=True, blank=True, related_name="requalification_record",
    )
    training_record = models.OneToOneField(
        "TrainingRecord", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="requalification_record",
    )

    def save(self, *args, **kwargs):
        if self._state.adding:
            request = RequalificationApprovalRequest.objects.filter(
                pk=self.approval_request_id, approved=True,
            ).first()
            if not request:
                raise ValidationError("Une nouvelle requalification doit provenir d’une demande approuvée.")
            request.validate_training_plan_for_approval()
        else:
            previous = type(self).objects.only("approval_request_id", "training_record_id").get(pk=self.pk)
            if (previous.approval_request_id != self.approval_request_id
                    or previous.training_record_id != self.training_record_id):
                raise ValidationError("Les liens d’origine de la requalification sont en lecture seule.")
        return super().save(*args, **kwargs)
    
    RESULT_CHOICES = [
        ('PASS', 'Réussi - Maintien du niveau'),
        ('UPGRADE', 'Réussi - Montée en niveau'),
        ('RETRAIN', 'À reformer complètement'),
    ]
    
    employee = models.ForeignKey(
        'Employee', 
        on_delete=models.CASCADE, 
        related_name='requalifications'
    )
    
    # Tâche optionnelle
    task = models.ForeignKey(
        'Task', 
        on_delete=models.CASCADE, 
        related_name='requalifications',
        null=True,
        blank=True
    )
    
    line = models.ForeignKey(
        'ProductionLine', 
        on_delete=models.CASCADE, 
        related_name='requalifications',
        null=True,
        blank=True
    )
    
    # Niveau avant requalification (optionnel si pas de tâche)
    previous_level = models.IntegerField(
        choices=TrainingLevel.choices,
        null=True,
        blank=True
    )
    
    # Résultat de la requalification
    result = models.CharField(max_length=20, choices=RESULT_CHOICES)
    
    # Nouveau niveau après requalification (optionnel)
    new_level = models.IntegerField(
        choices=TrainingLevel.choices,
        null=True,
        blank=True
    )
    
    # Date de la dernière formation (pour référence)
    original_training_date = models.DateField(null=True, blank=True)
    
    # Date de la requalification (saisie par l'utilisateur)
    requalification_date = models.DateField()
    
    # Nombre de jours depuis la dernière formation
    days_since_training = models.IntegerField(default=0)
    
    # Évaluateur (chef d'équipe) - lien vers User
    evaluated_by = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='requalifications_evaluated'
    )
    
    # ============================================
    # NOUVEAU CHAMP : Nom du requalificateur
    # ============================================
    requalifier_name = models.CharField(
        max_length=150,
        verbose_name="Requalificateur",
        help_text="Nom de la personne ayant effectué la requalification",
        blank=True,
        null=True
    )
    
    # Notes/commentaires
    notes = models.TextField(blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-requalification_date']
        verbose_name = "Requalification"
        verbose_name_plural = "Requalifications"
    
    def __str__(self):
        task_str = f" - {self.task.title}" if self.task else ""
        return f"{self.employee}{task_str} ({self.get_result_display()})"
    
    def get_result_color(self):
        colors = {
            'PASS': 'success',
            'UPGRADE': 'info',
            'DOWNGRADE': 'warning',
            'RETRAIN': 'danger',
        }
        return colors.get(self.result, 'secondary')
    
    def get_previous_level_display(self):
        if self.previous_level is None:
            return '-'
        level_map = {0: 'I', 1: 'L', 3: 'U'}
        return level_map.get(self.previous_level, str(self.previous_level))
    
    def get_new_level_display(self):
        if self.new_level is None:
            return '-'
        level_map = {0: 'I', 1: 'L', 3: 'U'}
        return level_map.get(self.new_level, str(self.new_level))
    
    def get_requalifier_display(self):
        """Retourne le nom du requalificateur (champ texte ou User)"""
        if self.requalifier_name:
            return self.requalifier_name
        elif self.evaluated_by:
            return self.evaluated_by.get_full_name() or self.evaluated_by.username
        return '-'
    

class RequalificationApprovalRequest(models.Model):
    """Demande d'approbation de requalification par RH"""

    objects = RequalificationQuerySet.as_manager()
    training_plan_image = models.ImageField(
        upload_to=requalification_plan_upload_to, blank=True, null=True,
        verbose_name="Plan de formation",
    )

    @property
    def has_training_plan(self):
        plan = self.training_plan_image
        if not plan:
            return False
        try:
            if not plan.storage.exists(plan.name):
                return False
            with plan.storage.open(plan.name, "rb") as stream:
                stream.read(1)
            return True
        except (OSError, ValueError):
            return False

    @property
    def requires_training_plan(self):
        from .services_requalification import is_requalification_management
        # Keep checking an existing initial document, even if the author changes role.
        return bool(self.training_plan_image) or not is_requalification_management(self.requested_by)

    def validate_training_plan_for_approval(self):
        if self.requires_training_plan and not self.has_training_plan:
            raise ValidationError("Approbation impossible : le plan de formation est indisponible.")

    def save(self, *args, **kwargs):
        new_upload = self._state.adding and not self.training_plan_image._committed
        if self._state.adding:
            if self.approved is not None:
                raise ValidationError("Une nouvelle demande doit passer par le workflow de décision RH.")
            # Also protect ordinary ORM creation outside the public form.
            from .forms import TrainingPlanImageForm
            from .services_requalification import can_add_requalification_plan
            if self.training_plan_image and not can_add_requalification_plan(self.requested_by):
                raise ValidationError({"training_plan_image": "Seul un chef d’équipe peut joindre un plan de requalification."})
            if self.requires_training_plan and not self.training_plan_image:
                raise ValidationError({"training_plan_image": "Le plan de formation est obligatoire pour créer une requalification."})
            try:
                if self.training_plan_image:
                    plan_form = TrainingPlanImageForm(files={"training_plan_image": self.training_plan_image.file})
                    if not plan_form.is_valid():
                        raise ValidationError(dict(plan_form.errors))
                    if new_upload:
                        self.training_plan_image = plan_form.cleaned_data["training_plan_image"]
                    self.training_plan_image.file.seek(0)
            except OSError as exc:
                raise ValidationError("Le plan de formation est indisponible.") from exc
        else:
            previous = type(self).objects.only("approved", "training_plan_image", "requested_by_id").get(pk=self.pk)
            if previous.approved != self.approved:
                raise ValidationError("Utilisez le workflow de décision RH.")
            if (not self.training_plan_image._committed
                    or (previous.training_plan_image.name or "") != (self.training_plan_image.name or "")):
                raise ValidationError("Le plan initial de la requalification est en lecture seule.")
            if previous.requested_by_id != self.requested_by_id:
                raise ValidationError("Le demandeur d’origine est en lecture seule.")
            # A concurrent metadata/notification edit must never overwrite a
            # decision or the original document with an older in-memory value.
            immutable = {"approved", "training_plan_image", "requested_by", "requested_by_id",
                         "decision_by", "decision_by_id", "decision_date"}
            fields = kwargs.get("update_fields")
            if fields is None:
                fields = [field.name for field in self._meta.concrete_fields if not field.primary_key]
            kwargs["update_fields"] = [field for field in fields if field not in immutable]
        if new_upload:
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except Exception:
                plan = self.training_plan_image
                plan.close()
                if plan._committed:
                    delete_unused_requalification_plan(plan.storage, plan.name)
                raise
        return super().save(*args, **kwargs)
    
    RESULT_CHOICES = [
        ('PASS', 'Réussi - Maintien du niveau'),
        ('UPGRADE', 'Réussi - Montée en niveau'),
        ('DOWNGRADE', 'Échec - Descente en niveau'),
        ('RETRAIN', 'À reformer complètement'),
    ]
    
    employee = models.ForeignKey(
        'Employee', 
        on_delete=models.CASCADE, 
        related_name='requalification_approval_requests'
    )
    task = models.ForeignKey(
        'Task', 
        on_delete=models.CASCADE, 
        null=True, 
        blank=True,
        related_name='requalification_approval_requests'
    )
    line = models.ForeignKey(
        'ProductionLine', 
        on_delete=models.CASCADE, 
        null=True, 
        blank=True
    )
    
    # Niveau actuel
    previous_level = models.IntegerField(
        choices=TrainingLevel.choices, 
        null=True, 
        blank=True
    )
    
    # Résultat demandé
    requested_result = models.CharField(max_length=20, choices=RESULT_CHOICES)
    
    # Nouveau niveau demandé
    requested_new_level = models.IntegerField(
        choices=TrainingLevel.choices, 
        null=True, 
        blank=True
    )
    
    # Dates
    original_training_date = models.DateField(null=True, blank=True)
    requalification_date = models.DateField()
    days_since_training = models.IntegerField(default=0)

    requalifier_name = models.CharField(
        max_length=255, 
        blank=True, 
        null=True,
        verbose_name="Nom du requalificateur"
    )
    
    # Notes du chef d'équipe
    notes = models.TextField(blank=True)
    
    # Demandeur (chef d'équipe)
    requested_by = models.ForeignKey(
        User, 
        on_delete=models.CASCADE, 
        related_name='requalification_requests_made'
    )
    date_requested = models.DateTimeField(auto_now_add=True)
    
    # Décision RH
    approved = models.BooleanField(null=True, blank=True)  # None = en attente
    decision_date = models.DateTimeField(null=True, blank=True)
    decision_by = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='requalification_decisions_made'
    )
    decision_notes = models.TextField(blank=True)
    
    # Notification vue par le chef d'équipe
    notification_seen = models.BooleanField(default=False)
    
    class Meta:
        ordering = ['-date_requested']
        verbose_name = "Demande d'approbation requalification"
        verbose_name_plural = "Demandes d'approbation requalifications"
    
    def __str__(self):
        task_str = f" - {self.task.title}" if self.task else ""
        status = "En attente" if self.approved is None else ("Approuvée" if self.approved else "Refusée")
        return f"{self.employee}{task_str} ({status})"
    
    def get_result_display(self):
        result_map = {
            'PASS': 'Réussi - Maintien',
            'UPGRADE': 'Réussi - Montée',
            'DOWNGRADE': 'Échec - Descente',
            'RETRAIN': 'À reformer',
        }
        return result_map.get(self.requested_result, self.requested_result)
    
    def get_previous_level_display(self):
        if self.previous_level is None:
            return '-'
        level_map = {0: 'I', 1: 'L', 3: 'U'}
        return level_map.get(self.previous_level, str(self.previous_level))
    
    def get_requested_new_level_display(self):
        if self.requested_new_level is None:
            return '-'
        level_map = {0: 'I', 1: 'L', 3: 'U'}
        return level_map.get(self.requested_new_level, str(self.requested_new_level))
    
    def get_result_color(self):
        colors = {
            'PASS': 'success',
            'UPGRADE': 'info',
            'DOWNGRADE': 'warning',
            'RETRAIN': 'danger',
        }
        return colors.get(self.requested_result, 'secondary')



class LineTransferApprovalRequest(models.Model):
    """Demande d'approbation de transfert créée par un chef d'équipe"""
    
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='transfer_approval_requests',
        verbose_name="Employé"
    )
    
    from_line = models.ForeignKey(
        ProductionLine,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='transfer_requests_from',
        verbose_name="Ancienne ligne"
    )
    
    to_line = models.ForeignKey(
        ProductionLine,
        on_delete=models.CASCADE,
        related_name='transfer_requests_to',
        verbose_name="Nouvelle ligne demandée"
    )
    
    new_manager = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='transfer_requests_as_manager',
        verbose_name="Nouveau chef d'équipe"
    )
    
    reason = models.TextField(
        blank=True,
        verbose_name="Raison du transfert"
    )
    
    requested_transfer_date = models.DateField(
        default=timezone.now,
        verbose_name="Date de transfert demandée"
    )
    
    # Demandeur (chef d'équipe)
    requested_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='transfer_requests_made',
        verbose_name="Demandé par"
    )
    
    date_requested = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Date de la demande"
    )
    
    # Décision RH
    approved = models.BooleanField(
        null=True,
        blank=True,
        verbose_name="Approuvé"
    )
    
    decision_date = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Date de décision"
    )
    
    decision_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='transfer_decisions_made',
        verbose_name="Décision par"
    )
    
    decision_notes = models.TextField(
        blank=True,
        verbose_name="Notes de la décision"
    )
    
    # Notification
    notification_seen = models.BooleanField(
        default=False,
        verbose_name="Notification vue"
    )
    
    class Meta:
        ordering = ['-date_requested']
        verbose_name = "Demande d'approbation de transfert"
        verbose_name_plural = "Demandes d'approbation de transferts"
    
    def __str__(self):
        status = "En attente" if self.approved is None else ("Approuvée" if self.approved else "Refusée")
        from_line_name = self.from_line.name if self.from_line else "Aucune"
        return f"{self.employee}: {from_line_name} → {self.to_line.name} ({status})"
    
    def get_status_display(self):
        if self.approved is None:
            return "En attente"
        return "Approuvée" if self.approved else "Refusée"
    
    def get_status_color(self):
        if self.approved is None:
            return 'warning'
        return 'success' if self.approved else 'danger'


# **********************************************************************
# matrix/models.py  ← AJOUTER À LA FIN DE VOTRE FICHIER EXISTANT

class QualityProfile(models.Model):
    """
    Profil pour les comptes Qualité - lecture seule.
    Se greffe sur User sans rien modifier d'autre.
    """
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='quality_profile',
        verbose_name="Utilisateur"
    )

    is_active = models.BooleanField(
        default=True,
        verbose_name="Compte actif"
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    class Meta:
        verbose_name = "Compte Qualité"
        verbose_name_plural = "Comptes Qualité"

    def __str__(self):
        return (
            f"{self.user.get_full_name() or self.user.username}"
            f" — Qualité"
        )

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class TrainingQuestion(models.Model):
    """
    Question configurable par RH.

    Une question peut être :
    - globale ;
    - spécifique à une ligne ;
    - spécifique à une tâche ;
    - spécifique à une tâche et une ligne.
    """

    class AnswerType(models.TextChoices):
        YES_NO = "YES_NO", "Oui / Non"
        TEXT = "TEXT", "Réponse texte"

    text = models.TextField(
        verbose_name="Question"
    )

    help_text = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Texte d'aide"
    )

    level = models.IntegerField(
        choices=TrainingLevel.choices,
        default=TrainingLevel.IN_TRAINING,
        verbose_name="Niveau concerné"
    )

    line = models.ForeignKey(
        ProductionLine,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="training_questions",
        verbose_name="Ligne concernée",
        help_text="Laissez vide pour utiliser la question sur toutes les lignes."
    )

    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="training_questions",
        verbose_name="Tâche concernée",
        help_text="Laissez vide pour utiliser la question sur toutes les tâches."
    )

    answer_type = models.CharField(
        max_length=20,
        choices=AnswerType.choices,
        default=AnswerType.YES_NO,
        verbose_name="Type de réponse"
    )

    expected_boolean = models.BooleanField(
        null=True,
        blank=True,
        default=True,
        verbose_name="Réponse attendue",
        help_text="Utilisé uniquement pour les questions Oui / Non."
    )

    is_required = models.BooleanField(
        default=True,
        verbose_name="Réponse obligatoire"
    )

    is_blocking = models.BooleanField(
        default=True,
        verbose_name="Question bloquante",
        help_text=(
            "Si la réponse ne correspond pas à la réponse attendue, "
            "le niveau I ne pourra pas être enregistré."
        )
    )

    requires_comment_if_no = models.BooleanField(
        default=True,
        verbose_name="Commentaire obligatoire si Non"
    )

    is_active = models.BooleanField(
        default=True,
        verbose_name="Question active"
    )

    order = models.PositiveIntegerField(
        default=0,
        verbose_name="Ordre d'affichage"
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="training_questions_created"
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        ordering = [
            "order",
            "id",
        ]
        verbose_name = "Question de formation"
        verbose_name_plural = "Questions de formation"

    def clean(self):
        if (
            self.answer_type != self.AnswerType.YES_NO
            and self.expected_boolean is not None
        ):
            raise ValidationError({
                "expected_boolean": (
                    "La réponse attendue est disponible uniquement "
                    "pour les questions Oui / Non."
                )
            })

    def __str__(self):
        scope = []

        if self.line:
            scope.append(self.line.code)

        if self.task:
            scope.append(self.task.code)

        scope_text = " / ".join(scope) if scope else "Question globale"

        return f"{self.text[:60]} — {scope_text}"


class TrainingQuestionnaire(models.Model):
    """
    Questionnaire rempli lors de l'enregistrement du niveau I.
    """

    training_record = models.OneToOneField(
        TrainingRecord,
        on_delete=models.CASCADE,
        related_name="questionnaire"
    )

    line = models.ForeignKey(
        ProductionLine,
        on_delete=models.PROTECT,
        related_name="completed_training_questionnaires"
    )

    answered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="completed_training_questionnaires"
    )

    is_compliant = models.BooleanField(
        default=True,
        verbose_name="Questionnaire conforme"
    )

    general_comment = models.TextField(
        blank=True,
        verbose_name="Commentaire général"
    )

    completed_at = models.DateTimeField(
        default=timezone.now
    )

    class Meta:
        verbose_name = "Questionnaire de formation"
        verbose_name_plural = "Questionnaires de formation"

    def __str__(self):
        return (
            f"Questionnaire #{self.pk} — "
            f"{self.training_record.employee} — "
            f"{self.training_record.task}"
        )


class TrainingQuestionAnswer(models.Model):
    questionnaire = models.ForeignKey(
        TrainingQuestionnaire,
        on_delete=models.CASCADE,
        related_name="answers"
    )

    question = models.ForeignKey(
        TrainingQuestion,
        on_delete=models.PROTECT,
        related_name="answers"
    )

    # Copie historique du texte.
    # Une modification RH ne change pas les anciens questionnaires.
    question_text_snapshot = models.TextField()

    answer_type_snapshot = models.CharField(
        max_length=20
    )

    boolean_answer = models.BooleanField(
        null=True,
        blank=True
    )

    text_answer = models.TextField(
        blank=True
    )

    comment = models.TextField(
        blank=True
    )

    is_compliant = models.BooleanField(
        default=True
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "questionnaire",
                    "question",
                ],
                name="unique_question_per_training_questionnaire"
            )
        ]
        verbose_name = "Réponse au questionnaire"
        verbose_name_plural = "Réponses aux questionnaires"

    def __str__(self):
        return (
            f"{self.questionnaire} — "
            f"{self.question_text_snapshot[:40]}"
        )
