from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import logging
from threading import Barrier
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connections
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from . import test_requalification_plans as fixtures
from .forms import RequalificationForm
from .models import Employee, ProductionLine, RequalificationApprovalRequest, RequalificationRecord, Task
from .services_requalification import create_requalification


class LeaderRequalificationTests(TestCase):
    setUp = fixtures.RequalificationPlanTests.setUp
    data = fixtures.RequalificationPlanTests.data

    def page(self):
        self.client.force_login(self.supervisor_admin)
        return self.client.get(reverse("rh_requalify_team_leader"))

    def submit(self, **extra):
        self.client.force_login(self.supervisor_admin)
        return self.client.post(reverse("rh_requalify_team_leader"), self.data(**extra))

    def create(self, **extra):
        self.assertEqual(self.submit(**extra).status_code, 302)
        return RequalificationApprovalRequest.objects.latest("pk")

    def test_first_creation_is_global_approved_without_plan(self):
        original_employee = Employee.objects.values().get(pk=self.employee.pk)
        req = self.create()
        self.assertTrue(req.approved)
        self.assertIsNone(req.task_id)
        self.assertFalse(req.training_plan_image)
        self.assertIsNone(req.requalification_record.training_record_id)
        self.assertEqual(Employee.objects.values().get(pk=self.employee.pk), original_employee)
        self.assertEqual(self.page().context["leaders_data"], [])

    def test_immediate_and_pre_expiry_posts_are_blocked_including_day_188(self):
        self.create()
        now = timezone.now()
        for days in (0, 1, 100, 187, 188):
            with self.subTest(days=days), patch("django.utils.timezone.now", return_value=now + timedelta(days=days)):
                self.assertEqual(self.page().context["leaders_data"], [])
                response = self.submit()
                self.assertEqual(response.status_code, 200)
                self.assertIn("J+189", str(response.context["form"].non_field_errors()))
        self.assertEqual(RequalificationApprovalRequest.objects.count(), 1)
        self.assertEqual(RequalificationRecord.objects.count(), 1)

    def test_day_189_allows_renewal(self):
        self.create()
        with patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(days=189)):
            self.assertEqual(len(self.page().context["leaders_data"]), 1)
            self.create()
        self.assertEqual(RequalificationRecord.objects.count(), 2)

    def test_other_line_is_still_eligible(self):
        other_line = ProductionLine.objects.create(code="OTHER", name="Other line")
        other_line.team_leaders.add(self.chef)
        self.task.lines.add(other_line)
        self.original.lines.add(other_line)
        self.create()
        lines = self.page().context["leaders_data"][0]["lines_to_requalify"]
        self.assertEqual([item["line"].pk for item in lines], [other_line.pk])
        req = self.create(line_id=other_line.pk)
        self.assertEqual(req.line, other_line)

    def test_never_trained_task_does_not_bypass_recent_global_requalification(self):
        task = Task.objects.create(code="NEW", title="Never trained", task_number=2)
        task.lines.add(self.line)
        self.create()
        self.assertEqual(self.page().context["leaders_data"], [])
        self.assertEqual(self.submit().status_code, 200)
        with patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(days=189)):
            self.create()
        self.assertEqual(RequalificationRecord.objects.count(), 2)

    def test_no_training_still_allows_first_creation_only(self):
        self.original.delete()
        self.create()
        self.assertEqual(self.page().context["leaders_data"], [])
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(RequalificationRecord.objects.count(), 1)

    def test_retrain_keeps_the_same_renewal_delay(self):
        req = self.create(result="RETRAIN")
        self.assertEqual(req.requalification_record.result, "RETRAIN")
        now = timezone.now()
        with patch("django.utils.timezone.now", return_value=now + timedelta(days=188)):
            self.assertEqual(self.page().context["leaders_data"], [])
            self.assertEqual(self.submit().status_code, 200)
        with patch("django.utils.timezone.now", return_value=now + timedelta(days=189)):
            self.create()

    def test_pending_global_or_task_request_blocks_global_creation(self):
        for task in (None, self.task):
            with self.subTest(task=task):
                req = RequalificationApprovalRequest.objects.create(
                    employee=self.employee, line=self.line, task=task,
                    requested_by=self.supervisor_admin, requested_result="PASS",
                    requalification_date=timezone.now().date(),
                )
                self.assertEqual(self.page().context["leaders_data"], [])
                response = self.submit()
                self.assertIn("en attente", str(response.context["form"].non_field_errors()))
                self.assertEqual(RequalificationApprovalRequest.objects.count(), 1)
                self.assertFalse(RequalificationRecord.objects.exists())
                req.delete()

    def test_recent_training_or_no_tasks_prevents_creation_after_expiry(self):
        self.create()
        with patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(days=189)):
            self.original.date = timezone.now().date()
            self.original.save()
            self.assertEqual(self.page().context["leaders_data"], [])
            response = self.submit()
            self.assertIn("Aucune tâche", str(response.context["form"].non_field_errors()))
            self.task.lines.clear()
            self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(RequalificationRecord.objects.count(), 1)

    def test_submitted_date_cannot_bypass_cooldown(self):
        self.create()
        now = timezone.now()
        self.assertEqual(self.submit(requalification_date=(now.date() + timedelta(days=189)).isoformat()).status_code, 200)
        with patch("django.utils.timezone.now", return_value=now + timedelta(days=189)):
            self.assertEqual(self.submit(requalification_date=(now.date() + timedelta(days=188)).isoformat()).status_code, 200)
        self.assertEqual(RequalificationRecord.objects.count(), 1)

    def test_service_rechecks_recent_requalification_without_view(self):
        self.create()
        form = RequalificationForm(self.data(), user=self.supervisor_admin, direct_leader=True)
        self.assertTrue(form.is_valid(), form.errors)
        with self.assertRaises(ValidationError):
            create_requalification(form=form, user=self.supervisor_admin, direct_leader=True)

    def test_post_rejects_non_leader_unassigned_line_and_self(self):
        operator = Employee.objects.create(matricule="OPERATOR", first_name="Other", last_name="Person")
        line = ProductionLine.objects.create(code="UNASSIGNED", name="Unassigned")
        self.assertEqual(self.submit(employee_id=operator.pk).status_code, 200)
        self.assertEqual(self.submit(line_id=line.pk).status_code, 200)
        self.employee.user = self.supervisor_admin
        self.employee.save()
        self.line.team_leaders.add(self.supervisor_admin)
        self.assertEqual(self.submit().status_code, 200)
        self.assertFalse(RequalificationApprovalRequest.objects.exists())

    def test_service_rechecks_assignment_after_form_validation(self):
        form = RequalificationForm(self.data(), user=self.supervisor_admin, direct_leader=True)
        self.assertTrue(form.is_valid(), form.errors)
        self.line.team_leaders.remove(self.chef)
        with self.assertRaises(PermissionDenied):
            create_requalification(form=form, user=self.supervisor_admin, direct_leader=True)
        self.assertFalse(RequalificationApprovalRequest.objects.exists())

    def test_role_permissions_are_checked_by_service(self):
        form = RequalificationForm(self.data(), user=self.other, direct_leader=True)
        self.assertTrue(form.is_valid(), form.errors)
        with self.assertRaises(PermissionDenied):
            create_requalification(form=form, user=self.other, direct_leader=True)

    def test_supervisor_without_superuser_has_same_cooldown(self):
        self.client.force_login(self.supervisor)
        url = reverse("rh_requalify_team_leader")
        self.assertEqual(self.client.post(url, self.data()).status_code, 302)
        self.assertEqual(self.client.post(url, self.data()).status_code, 200)
        self.assertEqual(RequalificationRecord.objects.count(), 1)

    def test_two_previously_open_forms_cannot_both_be_submitted(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.supervisor_admin)
        url = reverse("rh_requalify_team_leader")
        client.get(url)
        client.get(url)
        data = self.data(csrfmiddlewaretoken=client.cookies["csrftoken"].value)
        self.assertEqual(client.post(url, data).status_code, 302)
        self.assertEqual(client.post(url, data).status_code, 200)
        self.assertEqual(RequalificationRecord.objects.count(), 1)


class ConcurrentLeaderRequalificationTests(TransactionTestCase):
    setUp = fixtures.RequalificationPlanTests.setUp
    data = fixtures.RequalificationPlanTests.data

    def test_simultaneous_posts_create_only_one_approved_requalification(self):
        url = reverse("rh_requalify_team_leader")
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(self.supervisor_admin)
        barrier = Barrier(2, timeout=10)

        def synchronized_creation(*args, **kwargs):
            barrier.wait()
            return create_requalification(*args, **kwargs)

        def submit(client):
            try:
                return client.post(url, self.data()).status_code
            finally:
                connections.close_all()

        with patch("matrix.views.create_requalification", side_effect=synchronized_creation):
            with patch.object(logging.getLogger("matrix.views"), "disabled", True):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(submit, client) for client in clients]
                    statuses = [future.result(timeout=20) for future in futures]
        self.assertEqual(statuses.count(302), 1)
        self.assertTrue(all(status in (200, 302, 409) for status in statuses))
        self.assertEqual(RequalificationApprovalRequest.objects.count(), 1)
        self.assertEqual(RequalificationRecord.objects.count(), 1)
        self.assertTrue(RequalificationApprovalRequest.objects.get().approved)
        # A retry after a SQLite lock conflict must still respect the cooldown.
        response = clients[0].post(url, self.data())
        self.assertIn("J+189", str(response.context["form"].non_field_errors()))
        self.assertEqual(RequalificationRecord.objects.count(), 1)
