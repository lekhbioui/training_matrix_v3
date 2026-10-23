from datetime import timedelta
from html.parser import HTMLParser
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import TrainingRecordForm
from .models import (
    Employee, LineTransfer, MatrixEntry, ProductionLine, RequalificationApprovalRequest,
    RequalificationRecord, Task, TrainingApprovalRequest, TrainingLevel,
    TrainingQuestion, TrainingQuestionAnswer, TrainingQuestionnaire, TrainingRecord,
)


class TrainingDateEditTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.owner = users.objects.create_user(username="date-owner", first_name="Chef", last_name="X")
        self.other = users.objects.create_user(username="date-other", first_name="Chef", last_name="Y")
        self.line = ProductionLine.objects.create(code="DATE-A", name="Date A")
        self.line.team_leaders.add(self.owner, self.other)
        self.employee = Employee.objects.create(
            matricule="DATE-1", first_name="Test", last_name="Operator", manager=self.owner,
            line=self.line, hire_date=self.day(-100),
        )
        self.employee.visible_lines.add(self.line)
        self.trainer = Employee.objects.create(matricule="DATE-T", first_name="Other", last_name="Trainer")
        self.task = Task.objects.create(code="DATE-TASK", title="Date task")
        self.task.lines.add(self.line)
        self.records = {}
        for level, offset in ((0, -50), (1, -40), (3, -15)):
            record = TrainingRecord.objects.create(
                employee=self.employee, task=self.task, level=level,
                date=self.day(offset), trainer=self.trainer, note="Keep note",
            )
            record.lines.add(self.line)
            self.records[level] = record
        self.record = self.records[0]
        self.entry = MatrixEntry.objects.create(
            employee=self.employee, task=self.task, level=TrainingLevel.TRAINER,
            last_trained=self.records[3].date, trainer=self.trainer, note="Keep qualification",
        )
        self.request = TrainingApprovalRequest.objects.create(
            employee=self.employee, task=self.task, requested_level=0, line=self.line,
            training_record=self.record, training_date=self.record.date,
            requested_by=self.owner, approved=True, reviewed_at=timezone.now(),
        )
        self.question = TrainingQuestion.objects.create(
            text="Archived question", level=0, is_active=False,
        )
        questionnaire = TrainingQuestionnaire.objects.create(
            training_record=self.record, line=self.line, answered_by=self.owner,
            general_comment="Preserve general comment",
        )
        TrainingQuestionAnswer.objects.create(
            questionnaire=questionnaire, question=self.question,
            question_text_snapshot="Historical text", answer_type_snapshot="yes_no",
            boolean_answer=True, comment="Preserve answer comment",
        )

    def day(self, offset):
        return timezone.localdate() + timedelta(days=offset)

    def post(self, record=None, date=None, **fields):
        record = record or self.record
        self.client.force_login(self.owner)
        return self.client.post(reverse("training_record_update", args=[record.pk]), {
            "date": (date or self.day(-51)).isoformat(), **fields,
        })

    def snapshot(self, record=None):
        record = record or self.record
        training = list(TrainingRecord.objects.order_by('pk').values())
        for row in training:
            if row['id'] == record.pk:
                row.pop('date')
        approvals = list(TrainingApprovalRequest.objects.order_by('pk').values())
        for row in approvals:
            if row['training_record_id'] == record.pk:
                row.pop('training_date')
        entry = MatrixEntry.objects.values().get(pk=self.entry.pk)
        entry.pop('last_trained')
        return (
            training, approvals, entry,
            list(TrainingRecord.lines.through.objects.order_by('pk').values()),
            list(TrainingQuestionnaire.objects.order_by('pk').values()),
            list(TrainingQuestionAnswer.objects.order_by('pk').values()),
            list(Employee.objects.order_by('pk').values()),
            list(RequalificationRecord.objects.order_by('pk').values()),
            list(RequalificationApprovalRequest.objects.order_by('pk').values()),
            list(LineTransfer.objects.order_by('pk').values()),
        )

    def test_valid_date_only_preserves_data_and_syncs_linked_rh_date(self):
        before = self.snapshot()
        response = self.post()
        self.assertEqual(response.status_code, 302)
        self.record.refresh_from_db()
        self.request.refresh_from_db()
        self.entry.refresh_from_db()
        self.assertEqual(self.record.date, self.day(-51))
        self.assertEqual(self.request.training_date, self.record.date)
        self.assertEqual(self.entry.last_trained, self.records[3].date)
        self.assertEqual(self.snapshot(), before)

    def test_forged_protected_fields_and_questionnaire_are_ignored(self):
        before = self.snapshot()
        response = self.post(
            employee=self.trainer.pk, task="99999", line="99999", level=3, trainer="99999",
            approval_status="rejected", note="Forged", questionnaire_general_comment="Forged",
            **{f"answer_{self.question.pk}": "0", f"comment_{self.question.pk}": "Forged"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_empty_future_dates_write_nothing(self):
        for date in ("", "invalid", self.day(1).isoformat()):
            with self.subTest(date=date):
                before = self.snapshot()
                old_date = TrainingRecord.objects.get(pk=self.record.pk).date
                self.client.force_login(self.owner)
                response = self.client.post(reverse("training_record_update", args=[self.record.pk]), {"date": date})
                self.assertEqual(response.status_code, 200)
                self.assertIn('date', response.context['form'].errors)
                self.assertEqual(TrainingRecord.objects.get(pk=self.record.pk).date, old_date)
                self.assertEqual(self.snapshot(), before)

    def test_hire_date_and_both_chronological_directions_are_enforced(self):
        for level, offset in ((0, -99), (0, -45), (1, -44), (1, -35), (3, -20)):
            with self.subTest(level=level, offset=offset):
                record = self.records[level]
                response = self.post(record=record, date=self.day(offset))
                self.assertEqual(response.status_code, 200)
                self.assertIn('date', response.context['form'].errors)
                record.refresh_from_db()
                self.assertEqual(record.date, {0: self.day(-50), 1: self.day(-40), 3: self.day(-15)}[level])

    def test_exact_seven_and_twenty_one_day_boundaries_are_valid(self):
        self.assertEqual(self.post(self.records[1], self.day(-43)).status_code, 302)
        self.assertEqual(self.post(self.records[3], self.day(-22)).status_code, 302)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.level, TrainingLevel.TRAINER)
        self.assertEqual(self.entry.last_trained, self.day(-22))

    def test_pending_successor_request_blocks_inconsistent_lower_date(self):
        self.records[1].delete()
        self.records[3].delete()
        TrainingApprovalRequest.objects.create(
            employee=self.employee, task=self.task, requested_level=1, line=self.line,
            training_date=self.day(-40), requested_by=self.owner,
        )
        response = self.post(date=self.day(-45))
        self.assertEqual(response.status_code, 200)
        self.assertIn('date', response.context['form'].errors)

    def test_latest_date_is_recomputed_without_overwriting_level(self):
        extra = TrainingRecord.objects.create(
            employee=self.employee, task=self.task, level=3, date=self.day(-16),
        )
        extra.lines.add(self.line)
        self.assertEqual(self.post(self.records[3], self.day(-18)).status_code, 302)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.last_trained, extra.date)
        self.assertEqual(self.entry.level, 3)

    def test_expiration_at_188_days_follows_corrected_date(self):
        self.records[1].delete()
        self.records[3].delete()
        Employee.objects.filter(pk=self.employee.pk).update(hire_date=self.day(-300))
        TrainingRecord.objects.filter(pk=self.record.pk).update(date=self.day(-189))
        MatrixEntry.objects.filter(pk=self.entry.pk).update(level=0, last_trained=self.day(-189))
        self.client.force_login(self.owner)
        before = self.client.get(reverse('employees_to_requalify'))
        self.assertEqual(len(before.context['employees_to_requalify']), 1)
        self.assertEqual(self.post(date=self.day(-187)).status_code, 302)
        after = self.client.get(reverse('employees_to_requalify'))
        self.assertEqual(after.context['employees_to_requalify'], [])
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.last_trained, self.day(-187))

    def test_date_and_linked_dates_are_saved_atomically(self):
        form = TrainingRecordForm({'date': self.day(-51)}, instance=self.record, user=self.owner)
        self.assertTrue(form.is_valid(), form.errors)
        with patch('matrix.forms.TrainingApprovalRequest.objects.filter') as requests:
            requests.return_value.update.side_effect = RuntimeError('Simulated write failure')
            with self.assertRaises(RuntimeError):
                form.save()
        self.record.refresh_from_db()
        self.assertEqual(self.record.date, self.day(-50))
        self.request.refresh_from_db()
        self.assertEqual(self.request.training_date, self.day(-50))

    def test_pending_record_does_not_change_matrix_or_create_rh_request(self):
        self.records[3].approval_status = TrainingRecord.ApprovalStatus.PENDING
        self.records[3].save(update_fields=['approval_status'])
        before = self.snapshot(self.records[3])
        self.assertEqual(self.post(self.records[3], self.day(-14)).status_code, 302)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.last_trained, self.day(-15))
        self.assertEqual(self.snapshot(self.records[3]), before)

    def test_permissions_deny_other_leader_anonymous_and_unmanaged_line(self):
        url = reverse('training_record_update', args=[self.record.pk])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {'date': self.day(-51)}).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.post(url, {'date': self.day(-51)}).status_code, 302)
        self.line.team_leaders.remove(self.owner)
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].non_field_errors())

    def test_multiline_record_keeps_all_lines_and_validates_each_line(self):
        second = ProductionLine.objects.create(code='DATE-B', name='Date B')
        second.team_leaders.add(self.owner)
        self.employee.visible_lines.add(second)
        self.record.lines.add(second)
        self.task.lines.add(second)
        self.assertEqual(self.post().status_code, 302)
        self.assertEqual(set(self.record.lines.all()), {self.line, second})
        later = TrainingRecord.objects.create(employee=self.employee, task=self.task, level=1, date=self.day(-47))
        later.lines.add(second)
        self.assertEqual(self.post(date=self.day(-52)).status_code, 200)

    def test_edit_page_preserves_historical_answers_and_disables_creation_js(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('training_record_update', args=[self.record.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Historical text')
        self.assertContains(response, 'Preserve answer comment')
        self.assertContains(response, 'Preserve general comment')
        self.assertContains(response, '<fieldset disabled')
        self.assertContains(response, f'value="{self.record.date.isoformat()}"')
        class Controls(HTMLParser):
            def __init__(self):
                super().__init__()
                self.names = []

            def handle_starttag(self, tag, attrs):
                if tag in ('input', 'select', 'textarea'):
                    self.names.append(dict(attrs).get('name'))

        controls = Controls()
        controls.feed(response.content.decode())
        self.assertNotIn('questionnaire_general_comment', controls.names)
        self.assertContains(response, "'use strict';\n\n    return;")
        self.assertFalse(response.context['form'].fields['date'].disabled)
        for name, field in response.context['form'].fields.items():
            if name != 'date':
                self.assertTrue(field.disabled)

    def test_creation_keeps_questionnaire_and_rh_validation_path(self):
        self.records[1].delete()
        self.records[3].delete()
        form = TrainingRecordForm({
            'employee': self.employee.pk, 'task': self.task.pk, 'line': self.line.pk,
            'level': 1, 'date': self.day(-40).isoformat(),
        }, user=self.owner)
        self.assertFalse(form.is_date_edit)
        self.assertFalse(form.fields['employee'].disabled)
        with patch.object(form, '_validate_questionnaire') as questionnaire_check:
            self.assertFalse(form.is_valid())
        questionnaire_check.assert_called_once()
        self.assertTrue(form.rh_request_created)
        self.assertEqual(TrainingApprovalRequest.objects.filter(approved__isnull=True).count(), 1)
