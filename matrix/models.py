from django.utils import timezone  
from django.db import models
from django.contrib.auth.models import User
from django.db.models import Q


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
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE)
    task = models.ForeignKey(Task, on_delete=models.CASCADE)
    lines = models.ManyToManyField(  
        ProductionLine,
        related_name="training_records",
        blank=True,
        help_text="Lignes sur lesquelles la formation a été réalisée."
    )
    level = models.IntegerField(choices=TrainingLevel.choices)
    date = models.DateField(null=True, blank=True)
    trainer = models.ForeignKey(
        Employee,
        null=True,
        blank=True,
        related_name="records_given",
        on_delete=models.SET_NULL,
        limit_choices_to={'matrixentry__level': TrainingLevel.TRAINER}
    )
    note = models.TextField(blank=True)

    def __str__(self):
        return f"{self.date} | {self.employee} - {self.task} ({self.get_level_display()})"




# models.py

class TrainingApprovalRequest(models.Model):
    employee = models.ForeignKey(
        Employee, 
        on_delete=models.CASCADE
    )
    task = models.ForeignKey(
        Task, 
        on_delete=models.CASCADE
    )
    requested_level = models.IntegerField(
        choices=TrainingLevel.choices
    )
    requested_by = models.ForeignKey(
        User, 
        on_delete=models.CASCADE
    )
    date_requested = models.DateTimeField(
        auto_now_add=True
    )
    approved = models.BooleanField(
        null=True, 
        blank=True
    )
    decision_date = models.DateTimeField(
        null=True, 
        blank=True
    )
    comment = models.TextField(blank=True)
    notification_seen = models.BooleanField(default=True)
    
    # Date de formation saisie par le chef d'équipe
    training_date = models.DateField(
        null=True, 
        blank=True,
        verbose_name="Date de formation",
        help_text="Date à laquelle la formation a eu lieu"
    )

    # ✅ NOUVEAU : Ligne sur laquelle la formation a été faite
    # Indispensable pour approuver sur la bonne matrice
    line = models.ForeignKey(
        'ProductionLine',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='training_approval_requests',
        verbose_name="Ligne de production",
        help_text=(
            "Ligne sur laquelle la formation a été enregistrée. "
            "Utilisée pour associer le TrainingRecord à la bonne ligne "
            "lors de l'approbation RH."
        )
    )

    class Meta:
        verbose_name          = "Demande d'approbation de formation"
        verbose_name_plural   = "Demandes d'approbation de formations"
        ordering              = ['-date_requested']

    def __str__(self):
        status = "En attente"
        if self.approved is True:
            status = "Approuvé"
        elif self.approved is False:
            status = "Refusé"
        
        line_info = f" [{self.line.code}]" if self.line else ""
        
        return (
            f"{self.employee} - {self.task}{line_info} → "
            f"{self.get_requested_level_display()} ({status})"
        )

    def get_requested_level_display(self):
        return dict(TrainingLevel.choices).get(
            self.requested_level, 
            str(self.requested_level)
        )

    # ✅ Propriété utile pour les templates
    @property
    def formation_line(self):
        """
        Retourne la ligne de formation.
        Priorité : champ line → ligne principale de l'employé
        """
        return self.line or self.employee.line

    @property
    def status_display(self):
        """Retourne le statut sous forme lisible"""
        if self.approved is None:
            return "En attente"
        return "Approuvé" if self.approved else "Refusé"

    @property
    def status_color(self):
        """Retourne la couleur Bootstrap/Tailwind selon le statut"""
        if self.approved is None:
            return "warning"
        return "success" if self.approved else "danger"




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