# resources.py

from import_export import resources, fields
from import_export.widgets import ForeignKeyWidget, DateWidget
from django.contrib.auth import get_user_model

from .models import Employee, Task, TrainingRecord, ProductionLine

User = get_user_model()


class TrainingRecordResource(resources.ModelResource):

    employee = fields.Field(
        column_name='employee_matricule',
        attribute='employee',
        widget=ForeignKeyWidget(Employee, 'matricule')
    )

    task = fields.Field(
        column_name='task_code',
        attribute='task',
        widget=ForeignKeyWidget(Task, 'code')
    )

    trainer = fields.Field(
        column_name='trainer_username',
        attribute='trainer',
        widget=ForeignKeyWidget(User, 'username')
    )

    date = fields.Field(
        column_name='date',
        attribute='date',
        widget=DateWidget(format='%Y-%m-%d')
    )

    level = fields.Field(
        column_name='level',
        attribute='level'
    )

    note = fields.Field(
        column_name='note',
        attribute='note'
    )

    # ✅ Colonne pour spécifier la ligne
    line_code = fields.Field(
        column_name='line_code',
    )

    # Export only
    employee_first_name = fields.Field(column_name='employee_first_name', readonly=True)
    employee_last_name = fields.Field(column_name='employee_last_name', readonly=True)
    employee_line = fields.Field(column_name='employee_line', readonly=True)
    lines_assigned = fields.Field(column_name='lines_assigned', readonly=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._line_code_cache = {}

    def dehydrate_employee_first_name(self, record):
        return record.employee.first_name if record.employee else ""

    def dehydrate_employee_last_name(self, record):
        return record.employee.last_name if record.employee else ""

    def dehydrate_employee_line(self, record):
        return record.employee.line.code if record.employee and record.employee.line else ""

    def dehydrate_lines_assigned(self, record):
        return ", ".join([l.code for l in record.lines.all()])

    def dehydrate_level(self, record):
        return {0: 'I', 1: 'L', 3: 'U'}.get(record.level, str(record.level))

    def dehydrate_line_code(self, record):
        lines = record.lines.all()
        return ", ".join([l.code for l in lines]) if lines else ""

    def before_import_row(self, row, row_number=None, **kwargs):
        """Normaliser le niveau et stocker line_code"""
        # Normaliser le niveau
        level_mapping = {'I': '0', 'i': '0', 'L': '1', 'l': '1', 'U': '3', 'u': '3'}
        if 'level' in row and str(row['level']).strip() in level_mapping:
            row['level'] = level_mapping[str(row['level']).strip()]
        
        # ✅ Stocker le line_code pour cette ligne
        line_code = str(row.get('line_code', '')).strip() if row.get('line_code') else ''
        if row_number is not None:
            self._line_code_cache[row_number] = line_code
        
        return row

    def after_import_row(self, row, row_result, row_number=None, **kwargs):
        """
        ✅ Assigner la ligne SPÉCIFIÉE dans l'Excel (ou celle de l'employé par défaut)
        """
        if row_result.import_type not in ('new', 'update'):
            return
        
        if not row_result.object_id:
            return
        
        try:
            tr = TrainingRecord.objects.get(pk=row_result.object_id)
        except TrainingRecord.DoesNotExist:
            return
        
        # Récupérer le line_code depuis le cache ou depuis row
        line_code = ''
        if row_number is not None and row_number in self._line_code_cache:
            line_code = self._line_code_cache[row_number]
        elif row and 'line_code' in row:
            line_code = str(row.get('line_code', '')).strip()
        
        print(f"\n{'='*50}")
        print(f"[after_import_row] TrainingRecord #{tr.id}")
        print(f"  Employee: {tr.employee.matricule}")
        print(f"  Task: {tr.task.code}")
        print(f"  line_code from Excel: '{line_code}'")
        
        line_to_assign = None
        
        # ✅ 1. Si line_code est spécifié dans l'Excel, l'utiliser
        if line_code:
            line_to_assign = ProductionLine.objects.filter(code__iexact=line_code).first()
            if line_to_assign:
                print(f"  ✅ Ligne trouvée: {line_to_assign.code}")
            else:
                print(f"  ❌ Ligne '{line_code}' non trouvée!")
                # Lister les lignes disponibles
                all_codes = list(ProductionLine.objects.values_list('code', flat=True))
                print(f"  Lignes disponibles: {all_codes}")
        
        # ✅ 2. Sinon, utiliser la ligne de l'employé
        if not line_to_assign:
            if tr.employee and tr.employee.line:
                line_to_assign = tr.employee.line
                print(f"  ℹ️ Utilisation ligne employé: {line_to_assign.code}")
            else:
                print(f"  ❌ Employé sans ligne!")
        
        # ✅ 3. Assigner la ligne
        if line_to_assign:
            tr.lines.set([line_to_assign])
            print(f"  ✅ ASSIGNÉ: {line_to_assign.code}")
        else:
            print(f"  ❌ AUCUNE LIGNE ASSIGNÉE!")
        
        print(f"{'='*50}\n")

    def import_field(self, field, obj, data, is_m2m=False, **kwargs):
        readonly = {"employee_first_name", "employee_last_name", "employee_line", "lines_assigned", "line_code"}
        if field.column_name in readonly:
            return None
        return super().import_field(field, obj, data, is_m2m=is_m2m, **kwargs)

    def skip_row(self, instance, original, row, import_validation_errors=None):
        if instance.pk:
            return False
        return TrainingRecord.objects.filter(
            employee=instance.employee,
            task=instance.task,
            level=instance.level,
            date=instance.date
        ).exists()

    class Meta:
        model = TrainingRecord
        import_id_fields = ()
        exclude = ('lines',)
        fields = ('id', 'date', 'employee', 'task', 'level', 'trainer', 'note', 'line_code',
                  'employee_first_name', 'employee_last_name', 'employee_line', 'lines_assigned')
        skip_unchanged = True
        report_skipped = True