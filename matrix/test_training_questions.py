from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.db import IntegrityError
from django.http import QueryDict
from unittest.mock import patch

from .forms_training_questions import TrainingQuestionCreateForm

from .models import (
    Employee,
    ProductionLine,
    Task,
    TrainingLevel,
    TrainingQuestion,
    TrainingQuestionAnswer,
    TrainingQuestionnaire,
    TrainingRecord,
)


class TrainingQuestionManagementTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.rh_group = Group.objects.create(name="RH")
        self.rh = user_model.objects.create_user("rh-user", password=None)
        self.rh.groups.add(self.rh_group)
        self.superuser = user_model.objects.create_user(
            "admin-user", email="admin@example.com", password=None, is_staff=True, is_superuser=True
        )
        self.standard = user_model.objects.create_user("standard-user", password=None)
        self.leader = user_model.objects.create_user("leader-user", password=None)
        self.quality = user_model.objects.create_user("quality-user", password=None)

        self.line_a = ProductionLine.objects.create(code="QA", name="Ligne A")
        self.line_b = ProductionLine.objects.create(code="QB", name="Ligne B")
        self.line_a.team_leaders.add(self.leader)
        self.task_a = Task.objects.create(code="TASK-A", title="Tache A", task_number=1)
        self.task_b = Task.objects.create(code="TASK-B", title="Tache B", task_number=2)
        self.task_a.lines.add(self.line_a)
        self.task_b.lines.add(self.line_b)

        self.valid_data = {
            "text": "Les consignes sont-elles comprises ?",
            "help_text": "Verifier avec l'operateur.",
            "level": TrainingLevel.IN_TRAINING,
            "line": self.line_a.pk,
            "task": self.task_a.pk,
            "answer_type": TrainingQuestion.AnswerType.YES_NO,
            "expected_boolean": "True",
            "is_required": "on",
            "is_blocking": "on",
            "requires_comment_if_no": "on",
            "is_active": "on",
            "order": 0,
        }

    def login(self, user):
        self.client.force_login(user)

    def create_question(self, **overrides):
        data = {
            "text": "Question existante",
            "level": TrainingLevel.IN_TRAINING,
            "line": self.line_a,
            "task": self.task_a,
            "answer_type": TrainingQuestion.AnswerType.YES_NO,
            "expected_boolean": True,
            "is_required": True,
            "is_blocking": True,
            "is_active": True,
            "order": 0,
        }
        data.update(overrides)
        return TrainingQuestion.objects.create(**data)

    def test_anonymous_cannot_access_list(self):
        response = self.client.get(reverse("training_question_list"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"{reverse('login')}?next={reverse('training_question_list')}")

    def test_standard_user_is_forbidden(self):
        self.login(self.standard)
        self.assertEqual(self.client.get(reverse("training_question_list")).status_code, 403)

    def test_team_leader_is_forbidden(self):
        self.login(self.leader)
        self.assertEqual(self.client.get(reverse("training_question_list")).status_code, 403)

    def test_rh_can_access_list(self):
        self.login(self.rh)
        self.assertEqual(self.client.get(reverse("training_question_list")).status_code, 200)

    def test_superuser_can_access_list(self):
        self.login(self.superuser)
        self.assertEqual(self.client.get(reverse("training_question_list")).status_code, 200)

    def test_rh_can_create_valid_question(self):
        self.login(self.rh)
        response = self.client.post(reverse("training_question_create"), self.valid_data)
        self.assertRedirects(response, reverse("training_question_list"))
        question = TrainingQuestion.objects.get(text=self.valid_data["text"])
        self.assertEqual(question.created_by, self.rh)

    def test_task_from_another_line_is_rejected(self):
        self.login(self.rh)
        data = {**self.valid_data, "task": self.task_b.pk}
        response = self.client.post(reverse("training_question_create"), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "n&#x27;appartient pas")
        self.assertFalse(TrainingQuestion.objects.exists())

    def test_invalid_question_is_not_saved(self):
        self.login(self.rh)
        data = {**self.valid_data, "text": "", "order": -1}
        response = self.client.post(reverse("training_question_create"), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(TrainingQuestion.objects.exists())

    def test_question_can_be_updated(self):
        question = self.create_question()
        self.login(self.rh)
        data = {**self.valid_data, "text": "Question modifiee", "order": 4}
        response = self.client.post(reverse("training_question_update", args=[question.pk]), data)
        self.assertRedirects(response, reverse("training_question_list"))
        question.refresh_from_db()
        self.assertEqual(question.text, "Question modifiee")
        self.assertEqual(question.order, 4)

    def test_toggle_requires_post_and_toggles(self):
        question = self.create_question()
        self.login(self.rh)
        url = reverse("training_question_toggle_active", args=[question.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        question.refresh_from_db()
        self.assertTrue(question.is_active)

        self.client.post(url)
        question.refresh_from_db()
        self.assertFalse(question.is_active)
        self.client.post(url)
        question.refresh_from_db()
        self.assertTrue(question.is_active)

    def test_question_without_history_can_be_deleted(self):
        question = self.create_question()
        self.login(self.rh)
        self.client.post(reverse("training_question_delete", args=[question.pk]))
        self.assertFalse(TrainingQuestion.objects.filter(pk=question.pk).exists())

    def test_question_with_history_is_deactivated_not_deleted(self):
        question = self.create_question()
        employee = Employee.objects.create(first_name="Test", last_name="Operateur", matricule="Q-001", line=self.line_a)
        record = TrainingRecord.objects.create(employee=employee, task=self.task_a, level=TrainingLevel.IN_TRAINING, date=timezone.localdate())
        record.lines.add(self.line_a)
        questionnaire = TrainingQuestionnaire.objects.create(training_record=record, line=self.line_a, answered_by=self.rh)
        TrainingQuestionAnswer.objects.create(
            questionnaire=questionnaire,
            question=question,
            question_text_snapshot=question.text,
            answer_type_snapshot=question.answer_type,
            boolean_answer=True,
        )
        self.login(self.rh)
        self.client.post(reverse("training_question_delete", args=[question.pk]))
        question.refresh_from_db()
        self.assertFalse(question.is_active)
        self.assertEqual(question.answers.count(), 1)

    def test_tasks_endpoint_only_returns_selected_line_tasks(self):
        self.login(self.rh)
        response = self.client.get(reverse("training_question_tasks_by_line"), {"line_id": self.line_a.pk})
        self.assertEqual(response.status_code, 200)
        ids = {item["id"] for item in response.json()["tasks"]}
        self.assertEqual(ids, {self.task_a.pk})

    def test_unauthorized_user_cannot_use_tasks_endpoint(self):
        self.login(self.standard)
        response = self.client.get(reverse("training_question_tasks_by_line"), {"line_id": self.line_a.pk})
        self.assertEqual(response.status_code, 403)

    def test_rejected_mutations_do_not_change_data(self):
        question = self.create_question()
        initial_count = TrainingQuestion.objects.count()
        self.login(self.standard)
        self.assertEqual(self.client.post(reverse("training_question_create"), self.valid_data).status_code, 403)
        self.assertEqual(self.client.post(reverse("training_question_toggle_active", args=[question.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("training_question_delete", args=[question.pk])).status_code, 403)
        question.refresh_from_db()
        self.assertEqual(TrainingQuestion.objects.count(), initial_count)
        self.assertTrue(question.is_active)


class TrainingQuestionBatchCreationTests(TestCase):
    setUp = TrainingQuestionManagementTests.setUp
    login = TrainingQuestionManagementTests.login
    create_question = TrainingQuestionManagementTests.create_question

    def batch_data(self, **overrides):
        return {**self.valid_data, "task": [self.task_a.pk, self.task_b.pk], **overrides}

    def prepare_batch(self):
        self.task_b.lines.add(self.line_a)
        self.login(self.rh)

    def test_both_creation_routes_create_one_copy_per_task(self):
        self.prepare_batch()
        for route in ("training_question_list", "training_question_create"):
            with self.subTest(route=route):
                response = self.client.post(reverse(route), self.batch_data(text=route))
                self.assertEqual(response.status_code, 302)
                questions = TrainingQuestion.objects.filter(text=route)
                self.assertEqual(set(questions.values_list("task_id", flat=True)), {self.task_a.pk, self.task_b.pk})
                self.assertEqual(questions.count(), 2)
                self.assertTrue(all(q.created_by == self.rh and q.line == self.line_a for q in questions))
                self.assertTrue(all(q.is_required and q.is_blocking and q.requires_comment_if_no for q in questions))
                self.assertTrue(all(q.help_text == self.valid_data["help_text"] for q in questions))

    def test_creation_is_multiple_but_edit_remains_single(self):
        self.prepare_batch()
        question = self.create_question()
        for route in ("training_question_list", "training_question_create"):
            response = self.client.get(reverse(route))
            self.assertTrue(response.context["form"].fields["task"].widget.allow_multiple_selected)
        for url in (reverse("training_question_update", args=[question.pk]),
                    reverse("training_question_list") + f"?edit={question.pk}"):
            response = self.client.get(url)
            self.assertFalse(response.context["form"].fields["task"].widget.allow_multiple_selected)

    def test_no_task_selection_preserves_line_and_global_scopes(self):
        self.prepare_batch()
        for index, values in enumerate(({"task": []}, {"task": ""}, {"task": [], "line": ""})):
            response = self.client.post(reverse("training_question_create"), {
                **self.valid_data, **values, "text": f"All tasks {index}",
            })
            self.assertEqual(response.status_code, 302)
            question = TrainingQuestion.objects.get(text=f"All tasks {index}")
            self.assertIsNone(question.task_id)
            self.assertEqual(question.line_id, None if values.get("line") == "" else self.line_a.pk)

    def test_no_line_allows_tasks_from_different_lines(self):
        self.login(self.rh)
        response = self.client.post(reverse("training_question_create"), self.batch_data(line=""))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TrainingQuestion.objects.filter(line__isnull=True).count(), 2)

    def test_mixed_invalid_task_selection_creates_nothing(self):
        self.login(self.rh)
        for invalid in (self.task_b.pk, 999999, "not-an-id"):
            with self.subTest(invalid=invalid):
                response = self.client.post(reverse("training_question_list"), self.batch_data(task=[self.task_a.pk, invalid]))
                self.assertEqual(response.status_code, 200)
                self.assertIn("task", response.context["form"].errors)
                self.assertFalse(TrainingQuestion.objects.exists())

    def test_duplicate_on_one_task_rejects_whole_batch(self):
        self.prepare_batch()
        existing = self.create_question(text=self.valid_data["text"].upper())
        response = self.client.post(reverse("training_question_list"), self.batch_data())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].non_field_errors())
        self.assertEqual(list(TrainingQuestion.objects.values_list("pk", flat=True)), [existing.pk])

    def test_repeated_task_ids_create_only_one_copy(self):
        self.prepare_batch()
        response = self.client.post(reverse("training_question_create"), self.batch_data(task=[self.task_a.pk, self.task_a.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TrainingQuestion.objects.count(), 1)

    def test_repeated_submission_creates_no_duplicate(self):
        self.prepare_batch()
        url = reverse("training_question_list")
        self.assertEqual(self.client.post(url, self.batch_data()).status_code, 302)
        self.assertEqual(self.client.post(url, self.batch_data()).status_code, 200)
        self.assertEqual(TrainingQuestion.objects.count(), 2)

    def test_global_duplicate_is_still_rejected(self):
        self.prepare_batch()
        self.create_question(text=self.valid_data["text"], task=None)
        response = self.client.post(reverse("training_question_create"), self.batch_data(task=[]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TrainingQuestion.objects.count(), 1)

    def test_save_failure_rolls_back_all_copies_on_both_routes(self):
        self.prepare_batch()
        original_save = TrainingQuestion.save
        for route in ("training_question_list", "training_question_create"):
            with self.subTest(route=route):
                calls = []
                def failing_save(question, *args, **kwargs):
                    calls.append(question.task_id)
                    if len(calls) == 2:
                        raise IntegrityError("simulated failure on second question")
                    return original_save(question, *args, **kwargs)
                with patch.object(TrainingQuestion, "save", failing_save):
                    response = self.client.post(reverse(route), self.batch_data())
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(calls), 2)
                self.assertFalse(TrainingQuestion.objects.exists())

    def test_form_rechecks_duplicates_before_saving(self):
        self.prepare_batch()
        data = QueryDict(mutable=True)
        data.update(self.valid_data)
        data.setlist("task", [str(self.task_a.pk), str(self.task_b.pk)])
        form = TrainingQuestionCreateForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        self.create_question(text=self.valid_data["text"], task=self.task_b)
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            form.save_questions(self.rh)
        self.assertEqual(TrainingQuestion.objects.count(), 1)

    def test_filter_endpoint_supports_cleared_line_for_creation(self):
        self.prepare_batch()
        response = self.client.get(reverse("training_question_tasks_by_line"), {"all_tasks": 1})
        self.assertEqual({item["id"] for item in response.json()["tasks"]}, {self.task_a.pk, self.task_b.pk})
        self.assertEqual(self.client.get(reverse("training_question_tasks_by_line")).json()["tasks"], [])

    def test_copies_are_independent_and_evaluations_keep_task_scoping(self):
        self.prepare_batch()
        self.client.post(reverse("training_question_list"), self.batch_data())
        first = TrainingQuestion.objects.get(task=self.task_a)
        second = TrainingQuestion.objects.get(task=self.task_b)
        response = self.client.post(reverse("training_question_list"), {
            **self.valid_data, "edit_pk": first.pk, "text": "Changed one copy",
        })
        self.assertEqual(response.status_code, 302)
        second.refresh_from_db()
        self.assertEqual(second.text, self.valid_data["text"])
        for task, expected in ((self.task_a, first), (self.task_b, second)):
            response = self.client.get(reverse("ajax_get_questionnaire"), {
                "task_id": task.pk, "line_id": self.line_a.pk, "level": TrainingLevel.IN_TRAINING,
            })
            self.assertEqual([q["id"] for q in response.json()["questions"]], [expected.pk])

    def test_batch_creation_does_not_change_existing_answers_or_training(self):
        self.prepare_batch()
        question = self.create_question()
        employee = Employee.objects.create(matricule="HISTORY", first_name="Test", last_name="History", line=self.line_a)
        record = TrainingRecord.objects.create(employee=employee, task=self.task_a, level=0, date=timezone.localdate())
        questionnaire = TrainingQuestionnaire.objects.create(training_record=record, line=self.line_a, answered_by=self.rh)
        answer = TrainingQuestionAnswer.objects.create(
            questionnaire=questionnaire, question=question, question_text_snapshot=question.text,
            answer_type_snapshot=question.answer_type, boolean_answer=True,
        )
        models = (TrainingRecord, TrainingQuestionnaire, TrainingQuestionAnswer)
        before = {model.__name__: list(model.objects.values()) for model in models}
        self.client.post(reverse("training_question_list"), self.batch_data())
        self.assertEqual({model.__name__: list(model.objects.values()) for model in models}, before)
        self.client.post(reverse("training_question_delete", args=[question.pk]))
        answer.refresh_from_db()
        self.assertEqual(answer.question_text_snapshot, "Question existante")
        question.refresh_from_db()
        self.assertFalse(question.is_active)
