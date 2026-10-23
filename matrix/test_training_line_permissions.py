import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Employee, LineTransfer, LineTransferApprovalRequest, MatrixEntry, ProductionLine,
    Task, TrainingApprovalRequest, TrainingQuestionAnswer, TrainingQuestionnaire, TrainingRecord,
)


class TrainingLinePermissionsTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.x = users.objects.create_user('line-x', first_name='Chef', last_name='X')
        self.y = users.objects.create_user('line-y', first_name='Chef', last_name='Y')
        self.rh = users.objects.create_superuser(username='line-rh')
        self.a = ProductionLine.objects.create(code='PERM-A', name='Old line')
        self.b = ProductionLine.objects.create(code='PERM-B', name='New line')
        self.a.team_leaders.add(self.x)
        self.b.team_leaders.add(self.y)
        self.employee = Employee.objects.create(
            first_name='Test', last_name='Operator', matricule='PERM-1',
            manager=self.x, line=self.a,
        )
        self.employee.visible_lines.add(self.a)
        self.task = Task.objects.create(code='PERM-T', title='Shared task')
        self.task.lines.add(self.a, self.b)
        self.historical = TrainingRecord.objects.create(
            employee=self.employee, task=self.task, level=3, date=timezone.localdate(),
            note='Historical qualification',
        )
        self.historical.lines.add(self.a)
        self.entry = MatrixEntry.objects.create(
            employee=self.employee, task=self.task, level=3,
            last_trained=timezone.localdate(), note='Preserve qualification',
        )
        # Exécuter le vrai workflow de transfert, jusqu'à l'approbation RH.
        self.client.force_login(self.x)
        response = self.client.post(reverse('team_leader_transfer'), {
            'employee_id': self.employee.pk, 'target_chef_id': self.y.pk,
            'target_line_id': self.b.pk, 'reason': 'Interline transfer',
        })
        self.assertEqual(response.status_code, 302)
        request = LineTransferApprovalRequest.objects.get(employee=self.employee)
        self.client.force_login(self.rh)
        response = self.client.post(reverse('rh_transfer_approvals'), {
            'request_id': request.pk, 'action': 'approve',
        })
        self.assertEqual(response.status_code, 302)
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.manager_id, self.y.pk)
        self.assertEqual(self.employee.line_id, self.b.pk)
        self.assertEqual(set(self.employee.visible_lines.all()), {self.a, self.b})
        self.client.force_login(self.y)

    def snapshot(self):
        return (
            list(Employee.objects.order_by('pk').values()),
            list(Employee.visible_lines.through.objects.order_by('pk').values()),
            list(TrainingRecord.objects.order_by('pk').values()),
            list(TrainingRecord.lines.through.objects.order_by('pk').values()),
            list(MatrixEntry.objects.order_by('pk').values()),
            list(TrainingApprovalRequest.objects.order_by('pk').values()),
            list(TrainingQuestionnaire.objects.order_by('pk').values()),
            list(TrainingQuestionAnswer.objects.order_by('pk').values()),
            list(LineTransfer.objects.order_by('pk').values()),
            list(LineTransferApprovalRequest.objects.order_by('pk').values()),
        )

    def rendered_lines(self):
        response = self.client.get(reverse('training_record_create'), {'employee': self.employee.pk})
        self.assertEqual(response.status_code, 200)
        script = response.content.decode().split(f'"{self.employee.pk}": {{', 1)[1]
        lines = script.split('visibleLines: [', 1)[1].split(']', 1)[0]
        return response, {int(pk) for pk in re.findall(r'id:\s*(\d+)', lines)}

    def api(self, **params):
        return self.client.get(reverse('get_tasks_by_employee'), params)

    def test_transferred_operator_shows_only_new_authorized_line(self):
        before = self.snapshot()
        response, lines = self.rendered_lines()
        self.assertEqual(lines, {self.b.pk})
        self.assertEqual(set(response.context['form'].fields['line'].queryset), {self.b})
        tasks = self.api(line_id=self.b.pk, employee_id=self.employee.pk)
        self.assertEqual(tasks.status_code, 200)
        self.assertEqual([task['id'] for task in tasks.json()['tasks']], [self.task.pk])
        self.assertEqual(self.snapshot(), before)

    def test_old_line_available_only_when_managed_and_visible(self):
        self.a.team_leaders.add(self.y)
        before = self.snapshot()
        _, lines = self.rendered_lines()
        self.assertEqual(lines, {self.a.pk, self.b.pk})
        self.assertEqual(self.api(line_id=self.a.pk, employee_id=self.employee.pk).status_code, 200)
        self.assertEqual(self.snapshot(), before)
        self.employee.visible_lines.remove(self.a)
        _, lines = self.rendered_lines()
        self.assertEqual(lines, {self.b.pk})
        self.assertEqual(self.api(line_id=self.a.pk, employee_id=self.employee.pk).status_code, 403)

    def test_forged_forbidden_line_is_refused_with_clear_error_and_no_writes(self):
        before = self.snapshot()
        response = self.client.post(reverse('training_record_create'), {
            'employee': self.employee.pk, 'line': self.a.pk, 'task': self.task.pk,
            'level': 0, 'date': timezone.localdate().isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['form'].errors.as_data()['line'][0].code, 'invalid_choice')
        self.assertContains(response, "Cette ligne n&#x27;est pas autorisée pour le chef connecté.")
        self.assertNotContains(response, 'Merci de corriger les erreurs du formulaire.')
        self.assertEqual(self.snapshot(), before)

    def test_missing_line_error_is_explicit(self):
        before = self.snapshot()
        response = self.client.post(reverse('training_record_create'), {
            'employee': self.employee.pk, 'task': self.task.pk, 'line': '',
            'level': 0, 'date': timezone.localdate().isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Sélectionnez une ligne de formation autorisée.')
        self.assertEqual(self.snapshot(), before)

    def test_api_rejects_forbidden_lines_and_other_operators_with_no_writes(self):
        before = self.snapshot()
        for params in ({'line_id': self.a.pk},
                       {'line_id': self.a.pk, 'employee_id': self.employee.pk}):
            with self.subTest(params=params):
                response = self.api(**params)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()['tasks'], [])
        self.client.force_login(self.x)
        for params in ({'employee_id': self.employee.pk},
                       {'employee_id': self.employee.pk, 'line_id': self.a.pk},
                       {'line_id': self.b.pk}):
            with self.subTest(params=params):
                self.assertEqual(self.api(**params).status_code, 403)
        self.client.logout()
        self.assertEqual(self.api(line_id=self.b.pk).status_code, 302)
        self.assertEqual(self.snapshot(), before)

    def test_api_employee_only_and_superuser_compatibility(self):
        self.assertEqual(self.api(employee_id=self.employee.pk).status_code, 200)
        self.client.force_login(self.rh)
        self.assertEqual(self.api(line_id=self.a.pk, employee_id=self.employee.pk).status_code, 200)
        self.assertEqual(self.api(line_id='invalid').status_code, 400)

    def test_authorized_new_training_preserves_historical_training_and_qualification(self):
        historical = TrainingRecord.objects.values().get(pk=self.historical.pk)
        qualification = MatrixEntry.objects.values().get(pk=self.entry.pk)
        response = self.client.post(reverse('training_record_create'), {
            'employee': self.employee.pk, 'line': self.b.pk, 'task': self.task.pk,
            'level': 0, 'date': timezone.localdate().isoformat(),
        })
        self.assertEqual(response.status_code, 302)
        created = TrainingRecord.objects.exclude(pk=self.historical.pk).get()
        self.assertEqual(created.approval_status, TrainingRecord.ApprovalStatus.PENDING)
        self.assertEqual(set(created.lines.all()), {self.b})
        self.assertEqual(TrainingRecord.objects.values().get(pk=self.historical.pk), historical)
        self.assertEqual(MatrixEntry.objects.values().get(pk=self.entry.pk), qualification)
        self.assertEqual(set(self.employee.visible_lines.all()), {self.a, self.b})
