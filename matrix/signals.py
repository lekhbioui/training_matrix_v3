import logging

from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import TrainingRecord, Employee, LineTransfer
from django.utils import timezone

logger = logging.getLogger(__name__)


@receiver(post_save, sender=TrainingRecord)
def assign_employee_line_to_training(sender, instance, created, raw, **kwargs):
    """
    Assigne la ligne de l'employé SEULEMENT si aucune ligne n'est déjà assignée.
    Pour les chefs d'équipe, la ligne doit être spécifiée dans l'Excel.
    """
    if raw:
        return
    
    if getattr(instance, '_skip_line_signal', False):
        return
    
    # ✅ Ne rien faire si des lignes sont déjà assignées
    # (elles ont été assignées par after_import_row)
    if instance.lines.count() > 0:
        return
    
    # Seulement si aucune ligne → utiliser celle de l'employé
    if instance.employee and instance.employee.line:
        instance._skip_line_signal = True
        instance.lines.set([instance.employee.line])
        logger.info(
            "TrainingRecord line signal: training_record_id=%s line_id=%s",
            instance.pk,
            instance.employee.line_id,
        )


@receiver(post_save, sender=Employee)
def track_employee_line_change(sender, instance, created, **kwargs):
    if created:
        if instance.line:
            LineTransfer.objects.create(
                employee=instance,
                from_line=None,
                to_line=instance.line,
                transfer_date=instance.hire_date or timezone.now().date(),
                reason="Embauche initiale"
            )
        return
    
    if hasattr(instance, '_line_transfer_info'):
        info = instance._line_transfer_info
        LineTransfer.objects.create(
            employee=instance,
            from_line=info['from_line'],
            to_line=info['to_line'],
            transfer_date=info['transfer_date'],
            reason=info.get('reason', ''),
            transferred_by=info.get('transferred_by')
        )
        del instance._line_transfer_info
