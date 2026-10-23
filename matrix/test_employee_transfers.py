from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Employee, LineTransfer, LineTransferApprovalRequest, MatrixEntry,
    ProductionLine, Task, TrainingLevel, TrainingRecord,
)


class EmployeeTransferTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.x = users.objects.create_user(username="transfer-x")
        self.y = users.objects.create_user(username="transfer-y")
        self.other = users.objects.create_user(username="transfer-other")
        self.rh = users.objects.create_user(username="transfer-rh")
        self.rh.groups.add(Group.objects.get_or_create(name="RH")[0])
        self.admin = users.objects.create_superuser(username="transfer-admin")
        self.line = ProductionLine.objects.create(code="TR-A", name="A")
        self.line.team_leaders.add(self.x, self.y)
        self.second = ProductionLine.objects.create(code="TR-B", name="B")
        self.second.team_leaders.add(self.y)
        self.employee = Employee.objects.create(
            first_name="Test", last_name="Operator", matricule="TR-001",
            line=self.line, manager=self.x,
        )
        self.employee.visible_lines.add(self.line, self.second)
        self.task = Task.objects.create(code="TR-T1", title="Task A", task_number=1)
        self.task.lines.add(self.line)
        self.extra_task = Task.objects.create(code="TR-T2", title="Task B", task_number=2)
        self.extra_task.lines.add(self.second)
        for task in (self.task, self.extra_task):
            MatrixEntry.objects.create(
                employee=self.employee, task=task, level=TrainingLevel.TRAINER,
                last_trained=timezone.localdate(), note="Keep qualification",
            )

    def snapshot(self):
        employee = Employee.objects.values().get(pk=self.employee.pk)
        employee.pop("manager_id")
        return (
            employee,
            list(self.employee.visible_lines.order_by("pk").values_list("pk", flat=True)),
            list(MatrixEntry.objects.filter(employee=self.employee).order_by("pk").values()),
            list(TrainingRecord.objects.filter(employee=self.employee).order_by("pk").values()),
            list(TrainingRecord.lines.through.objects.order_by("pk").values()),
            list(LineTransfer.objects.filter(employee=self.employee).order_by("pk").values()),
        )

    def submit(self, manager=None, line=None, employee=None):
        self.client.force_login(self.x)
        return self.client.post(reverse("team_leader_transfer"), {
            "employee_id": (employee or self.employee).pk,
            "target_chef_id": (manager or self.y).pk,
            "target_line_id": (line or self.line).pk,
            "reason": "Change responsibility",
        })

    def decide(self, request, action="approve", user=None):
        self.client.force_login(user or self.rh)
        return self.client.post(reverse("rh_transfer_approvals"), {
            "request_id": request.pk, "action": action,
        })

    def direct(self, manager=None, line=None, user=None):
        self.client.force_login(user or self.rh)
        return self.client.post(reverse("transfer_employee"), {
            "employee": self.employee.pk, "new_line": (line or self.line).pk,
            "selected_team_leader": (manager or self.y).pk, "reason": "RH change",
        })

    def test_same_line_waits_for_rh_and_preserves_data_with_and_without_training(self):
        for has_training in (False, True):
            with self.subTest(has_training=has_training):
                if has_training:
                    record = TrainingRecord.objects.create(
                        employee=self.employee, task=self.task, level=TrainingLevel.TRAINER,
                        date=timezone.localdate(),
                        approval_status=TrainingRecord.ApprovalStatus.APPROVED,
                    )
                    record.lines.set([self.line, self.second])
                Employee.objects.filter(pk=self.employee.pk).update(manager=self.x)
                before = self.snapshot()
                self.assertEqual(self.submit().status_code, 302)
                request = LineTransferApprovalRequest.objects.get(approved__isnull=True)
                self.employee.refresh_from_db()
                self.assertEqual(self.employee.manager_id, self.x.pk)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.decide(request).status_code, 302)
                self.employee.refresh_from_db()
                request.refresh_from_db()
                self.assertEqual(self.employee.manager_id, self.y.pk)
                self.assertTrue(request.approved)
                self.assertEqual(request.decision_by_id, self.rh.pk)
                self.assertEqual(request.from_line_id, request.to_line_id)
                self.assertEqual(self.snapshot(), before)
                # A second approval must not replay the operation.
                self.decide(request)
                self.assertEqual(self.snapshot(), before)

    def test_rejection_keeps_operator_and_history(self):
        before = self.snapshot()
        self.submit()
        request = LineTransferApprovalRequest.objects.get()
        self.decide(request, "reject")
        request.refresh_from_db()
        self.employee.refresh_from_db()
        self.assertFalse(request.approved)
        self.assertEqual(self.employee.manager_id, self.x.pk)
        self.assertEqual(self.snapshot(), before)

    def test_same_manager_and_invalid_destination_are_rejected(self):
        for manager, line in ((self.x, self.line), (self.other, self.line), (self.x, self.second)):
            with self.subTest(manager=manager.pk, line=line.pk):
                self.submit(manager=manager, line=line)
                self.assertFalse(LineTransferApprovalRequest.objects.exists())

    def test_other_leaders_operator_cannot_be_transferred(self):
        Employee.objects.filter(pk=self.employee.pk).update(manager=self.y)
        self.submit()
        self.assertFalse(LineTransferApprovalRequest.objects.exists())

    def test_duplicate_pending_request_is_rejected(self):
        self.submit()
        self.submit()
        self.assertEqual(LineTransferApprovalRequest.objects.count(), 1)

    def test_approval_revalidates_owner_line_and_destination(self):
        for change in ("owner", "line", "destination", "same_manager"):
            with self.subTest(change=change):
                Employee.objects.filter(pk=self.employee.pk).update(manager=self.x, line=self.line)
                self.line.team_leaders.add(self.y)
                self.submit()
                request = LineTransferApprovalRequest.objects.filter(approved__isnull=True).latest("pk")
                if change == "owner":
                    Employee.objects.filter(pk=self.employee.pk).update(manager=self.other)
                elif change == "line":
                    Employee.objects.filter(pk=self.employee.pk).update(line=self.second)
                elif change == "destination":
                    self.line.team_leaders.remove(self.y)
                else:
                    request.new_manager = self.x
                    request.save()
                before = self.snapshot()
                self.decide(request)
                request.refresh_from_db()
                self.assertIsNone(request.approved)
                self.assertEqual(self.snapshot(), before)
                request.delete()

    def test_rh_routes_reject_leaders_and_unauthenticated_users(self):
        self.submit()
        request = LineTransferApprovalRequest.objects.get()
        for user in (self.x, self.other, None):
            with self.subTest(user=user):
                self.client.logout()
                if user:
                    self.client.force_login(user)
                for route, data in (
                    ("transfer_employee", {"employee": self.employee.pk, "new_line": self.line.pk,
                                           "selected_team_leader": self.y.pk}),
                    ("rh_transfer_approvals", {"request_id": request.pk, "action": "approve"}),
                ):
                    self.assertEqual(self.client.get(reverse(route)).status_code, 302)
                    self.assertEqual(self.client.post(reverse(route), data).status_code, 302)
        self.employee.refresh_from_db()
        request.refresh_from_db()
        self.assertEqual(self.employee.manager_id, self.x.pk)
        self.assertIsNone(request.approved)

    def test_direct_same_line_preserves_data_and_records_rh_history(self):
        for user in (self.rh, self.admin):
            with self.subTest(user=user):
                Employee.objects.filter(pk=self.employee.pk).update(manager=self.x)
                before = self.snapshot()
                self.assertEqual(self.direct(user=user).status_code, 302)
                self.employee.refresh_from_db()
                self.assertEqual(self.employee.manager_id, self.y.pk)
                self.assertEqual(self.snapshot(), before)
                history = LineTransferApprovalRequest.objects.latest("pk")
                self.assertTrue(history.approved)
                self.assertEqual(history.new_manager_id, self.y.pk)
                self.assertEqual(history.decision_by_id, user.pk)

    def test_direct_invalid_missing_and_same_manager_are_rejected(self):
        before = self.snapshot()
        for manager in (self.x, self.other):
            self.direct(manager=manager)
            self.employee.refresh_from_db()
            self.assertEqual(self.employee.manager_id, self.x.pk)
            self.assertFalse(LineTransferApprovalRequest.objects.exists())
        for value in ("", "invalid", "999999"):
            self.client.post(reverse("transfer_employee"), {
                "employee": self.employee.pk, "new_line": self.line.pk,
                "selected_team_leader": value,
            })
            self.employee.refresh_from_db()
            self.assertEqual(self.employee.manager_id, self.x.pk)
        self.assertEqual(self.snapshot(), before)

    def test_interline_approval_keeps_existing_transfer_behavior(self):
        self.submit(line=self.second)
        request = LineTransferApprovalRequest.objects.get()
        self.decide(request)
        self.assert_interline_result()
        self.assertFalse(self.employee.visible_lines.filter(pk=self.line.pk).exists())

    def test_interline_approval_preserves_training_and_old_visibility(self):
        record = TrainingRecord.objects.create(
            employee=self.employee, task=self.task, level=TrainingLevel.TRAINER,
            approval_status=TrainingRecord.ApprovalStatus.APPROVED,
        )
        self.submit(line=self.second)
        self.decide(LineTransferApprovalRequest.objects.get())
        self.assert_interline_result()
        self.assertTrue(self.employee.visible_lines.filter(pk=self.line.pk).exists())
        record.refresh_from_db()
        self.assertEqual(record.level, TrainingLevel.TRAINER)
        self.assertTrue(record.lines.filter(pk=self.line.pk).exists())

    def test_direct_interline_keeps_existing_transfer_behavior(self):
        self.direct(line=self.second)
        self.assert_interline_result()

    def assert_interline_result(self):
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.line_id, self.second.pk)
        self.assertEqual(self.employee.manager_id, self.y.pk)
        self.assertTrue(LineTransfer.objects.filter(
            employee=self.employee, from_line=self.line, to_line=self.second,
        ).exists())
        self.assertFalse(MatrixEntry.objects.filter(employee=self.employee, task=self.task).exists())
        self.assertEqual(MatrixEntry.objects.get(
            employee=self.employee, task=self.extra_task,
        ).level, TrainingLevel.TRAINER)
