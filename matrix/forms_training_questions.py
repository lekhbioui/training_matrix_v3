from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from .models import ProductionLine, Task, TrainingQuestion


class TrainingQuestionForm(forms.ModelForm):
    class Meta:
        model = TrainingQuestion
        fields = [
            "text",
            "help_text",
            "level",
            "line",
            "task",
            "answer_type",
            "expected_boolean",
            "is_required",
            "is_blocking",
            "requires_comment_if_no",
            "is_active",
            "order",
        ]
        widgets = {
            "text": forms.Textarea(attrs={"rows": 3}),
            "help_text": forms.TextInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["line"].queryset = ProductionLine.objects.order_by("code", "name")

        line_id = None
        if self.is_bound:
            line_id = self.data.get(self.add_prefix("line"))
        elif self.instance and self.instance.pk:
            line_id = self.instance.line_id

        tasks = Task.objects.all()
        if line_id:
            try:
                tasks = Task.objects.filter(lines__pk=int(line_id)).distinct()
            except (TypeError, ValueError):
                pass

        # Inclure la valeur POST afin que clean() puisse produire l'erreur
        # metier precise si la tache n'appartient pas a la ligne.
        submitted_task_id = self.data.get(self.add_prefix("task")) if self.is_bound else None
        if submitted_task_id:
            try:
                submitted_task_id = int(submitted_task_id)
                if line_id:
                    tasks = Task.objects.filter(
                        Q(lines__pk=int(line_id)) | Q(pk=submitted_task_id)
                    ).distinct()
            except (TypeError, ValueError):
                pass
        self.fields["task"].queryset = tasks.order_by("order", "task_number", "code")

        for field in self.fields.values():
            css_class = "form-check-input" if isinstance(field.widget, forms.CheckboxInput) else "form-control"
            field.widget.attrs["class"] = css_class

    def clean_text(self):
        text = (self.cleaned_data.get("text") or "").strip()
        if not text:
            raise forms.ValidationError("Le texte de la question est obligatoire.")
        return text

    def clean(self):
        cleaned = super().clean()
        text = cleaned.get("text")
        line = cleaned.get("line")
        task = cleaned.get("task")
        level = cleaned.get("level")
        answer_type = cleaned.get("answer_type")
        expected_boolean = cleaned.get("expected_boolean")
        is_required = cleaned.get("is_required")
        is_blocking = cleaned.get("is_blocking")

        if line and task and not task.lines.filter(pk=line.pk).exists():
            self.add_error("task", "La tache selectionnee n'appartient pas a cette ligne.")

        valid_levels = {value for value, _ in TrainingQuestion._meta.get_field("level").choices}
        if level is not None and level not in valid_levels:
            self.add_error("level", "Niveau de formation invalide.")

        valid_types = {value for value, _ in TrainingQuestion.AnswerType.choices}
        if answer_type and answer_type not in valid_types:
            self.add_error("answer_type", "Type de reponse invalide.")

        if is_blocking and answer_type != TrainingQuestion.AnswerType.YES_NO:
            self.add_error("is_blocking", "Une question bloquante doit utiliser une reponse Oui / Non.")

        if answer_type == TrainingQuestion.AnswerType.TEXT and expected_boolean is not None:
            self.add_error("expected_boolean", "La reponse attendue concerne uniquement les questions Oui / Non.")

        if (is_required or is_blocking) and not text:
            self.add_error("text", "Une question obligatoire ou bloquante doit avoir un texte.")

        if text and level is not None:
            duplicates = TrainingQuestion.objects.filter(
                level=level,
                text__iexact=text,
            )
            duplicates = duplicates.filter(line=line) if line else duplicates.filter(line__isnull=True)
            duplicates = duplicates.filter(task=task) if task else duplicates.filter(task__isnull=True)
            if self.instance.pk:
                duplicates = duplicates.exclude(pk=self.instance.pk)
            if duplicates.exists():
                raise forms.ValidationError(
                    "Une question identique existe deja pour cette ligne, cette tache et ce niveau."
                )

        return cleaned


class QuestionTasksField(forms.ModelMultipleChoiceField):
    def clean(self, value):
        # Accept the former empty single-select POST as the all-tasks scope.
        if isinstance(value, (list, tuple)):
            value = [item for item in value if item not in ("", None)]
        return super().clean(value)


class TrainingQuestionCreateForm(forms.Form):
    """Batch creation reuses the single-question form's existing validation."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        single = TrainingQuestionForm(*args, **kwargs)
        self.fields = single.fields
        tasks = Task.objects.all()
        line_id = self.data.get(self.add_prefix("line")) if self.is_bound else self.initial.get("line")
        if line_id:
            try:
                tasks = tasks.filter(lines__pk=int(line_id)).distinct()
            except (TypeError, ValueError):
                tasks = Task.objects.none()
        self.fields["task"] = QuestionTasksField(
            queryset=tasks.order_by("order", "task_number", "code"),
            required=False,
            label="Tâches concernées",
            help_text="Une question sera créée par tâche sélectionnée. Sans sélection : toutes les tâches. Maintenez Ctrl ou Cmd pour sélectionner plusieurs tâches.",
            widget=forms.SelectMultiple(attrs={"class": "form-control", "size": 6}),
            error_messages={
                "invalid_choice": "Une tâche sélectionnée n'appartient pas à cette ligne ou n'existe plus.",
                "invalid_pk_value": "La sélection contient une tâche invalide.",
            },
        )

    def _single_forms(self):
        for task in list(self.cleaned_data["task"]) or [None]:
            data = self.data.copy()
            data[self.add_prefix("task")] = str(task.pk) if task else ""
            yield TrainingQuestionForm(data, prefix=self.prefix)

    def clean(self):
        cleaned = super().clean()
        if not self.errors:
            for single in self._single_forms():
                if not single.is_valid():
                    for field, errors in single.errors.items():
                        for error in errors:
                            self.add_error(None if field == "__all__" else field, error)
        return cleaned

    @transaction.atomic
    def save_questions(self, user):
        if not self.is_valid():
            raise ValueError("Le formulaire d'ajout est invalide.")
        # Revalidate every scope before writing, including duplicates created
        # since the original form validation. Never partially accept a batch.
        singles = list(self._single_forms())
        for single in singles:
            if not single.is_valid():
                raise ValidationError([error for errors in single.errors.values() for error in errors])
        questions = []
        for single in singles:
            question = single.save(commit=False)
            question.created_by = user
            question.save()
            questions.append(question)
        return questions
