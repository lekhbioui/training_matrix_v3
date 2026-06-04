from rest_framework import serializers
from .models import Employee, Task, MatrixEntry, TrainingLevel
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

class EmployeeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Employee
        fields = "__all__"

class EmployeeViewSet(viewsets.ModelViewSet):
    serializer_class = EmployeeSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        # Le chef ne voit que ses employés
        return Employee.objects.filter(manager=user)

    def perform_create(self, serializer):
        # L’employé créé est automatiquement rattaché au chef connecté
        serializer.save(manager=self.request.user)

class TaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = Task
        fields = "__all__"

class MatrixEntrySerializer(serializers.ModelSerializer):
    trainer = serializers.PrimaryKeyRelatedField(
        queryset=Employee.objects.filter(matrixentry__level=TrainingLevel.TRAINER),
        required=False,
        allow_null=True
    )

    class Meta:
        model = MatrixEntry
        fields = "__all__"

    def validate_trainer(self, value):
        if value and not MatrixEntry.objects.filter(employee=value, level=TrainingLevel.TRAINER).exists():
            raise serializers.ValidationError("Le trainer doit être un employé de niveau 'P - Formateur'.")
        return value