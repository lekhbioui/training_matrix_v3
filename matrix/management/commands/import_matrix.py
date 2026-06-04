import csv
from django.core.management.base import BaseCommand
from matrix.models import Employee, Task, MatrixEntry, TrainingRecord, TrainingLevel

class Command(BaseCommand):
    help = "Import matrix from CSV"

    def add_arguments(self, parser):
        parser.add_argument("csv_file", type=str)

    def handle(self, *args, **options):
        with open(options["csv_file"], newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                emp, _ = Employee.objects.get_or_create(matricule=row["matricule"], defaults={"first_name": row["first_name"], "last_name": row["last_name"]})
                task, _ = Task.objects.get_or_create(code=row["task_code"], defaults={"title": row["task_title"]})
                level = int(row["level"])
                entry, created = MatrixEntry.objects.update_or_create(
                    employee=emp, task=task,
                    defaults={"level": level}
                )
                TrainingRecord.objects.create(employee=emp, task=task, level=level)
        self.stdout.write(self.style.SUCCESS("Import terminé !"))
