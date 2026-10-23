from html.parser import HTMLParser

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .forms import EmployeeForm
from .models import (
    Employee, LineTransfer, LineTransferApprovalRequest, MatrixEntry,
    PendingEmployee, ProductionLine, Task, TrainingLevel, TrainingRecord,
)


class EmployeeVisibilityTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.owner = users.objects.create_user(username="visibility-owner")
        self.other = users.objects.create_user(username="visibility-other")
        self.outsider = users.objects.create_user(username="visibility-outsider")
        self.main = ProductionLine.objects.create(code="VIS-A", name="Main")
        self.extra = ProductionLine.objects.create(code="VIS-B", name="Extra")
        self.foreign = ProductionLine.objects.create(code="VIS-C", name="Foreign")
        for line in (self.main, self.extra):
            line.team_leaders.add(self.owner)
        self.foreign.team_leaders.add(self.other)
        self.employee = Employee.objects.create(
            first_name="Test", last_name="Operator", matricule="VIS-001",
            line=self.main, manager=self.owner, hire_date=timezone.localdate(),
        )
        self.employee.visible_lines.add(self.main, self.foreign)
        task = Task.objects.create(code="VIS-TASK", title="Qualified task")
        task.lines.add(self.main, self.extra)
        self.training = TrainingRecord.objects.create(
            employee=self.employee, task=task, level=TrainingLevel.TRAINER,
            date=timezone.localdate(), approval_status=TrainingRecord.ApprovalStatus.APPROVED,
        )
        self.training.lines.set([self.main, self.extra])
        MatrixEntry.objects.create(
            employee=self.employee, task=task, level=TrainingLevel.TRAINER,
            last_trained=timezone.localdate(), note="Preserve qualification",
        )
        LineTransferApprovalRequest.objects.create(
            employee=self.employee, from_line=self.main, to_line=self.foreign,
            new_manager=self.other, requested_by=self.owner,
        )

    def data(self, lines=None, **extra):
        return {
            "matricule": self.employee.matricule,
            "first_name": self.employee.first_name,
            "last_name": self.employee.last_name,
            "visible_lines": [line.pk for line in (lines or [])],
            **extra,
        }

    def snapshot(self):
        # Tous les champs et liens hors visible_lines doivent rester inchangés.
        return (
            Employee.objects.values().get(pk=self.employee.pk),
            list(MatrixEntry.objects.order_by("pk").values()),
            list(TrainingRecord.objects.order_by("pk").values()),
            list(TrainingRecord.lines.through.objects.order_by("pk").values()),
            list(LineTransfer.objects.order_by("pk").values()),
            list(LineTransferApprovalRequest.objects.order_by("pk").values()),
        )

    def update(self, data, user=None):
        self.client.force_login(user or self.owner)
        return self.client.post(reverse("employee_update", args=[self.employee.pk]), data)

    def test_add_then_remove_secondary_line_preserves_everything_else(self):
        before = self.snapshot()
        for selected, expected in (
            ([self.extra], {self.main.pk, self.extra.pk, self.foreign.pk}),
            ([], {self.main.pk, self.foreign.pk}),
        ):
            with self.subTest(selected=selected):
                response = self.update(self.data(selected))
                self.assertRedirects(response, reverse("employee_list"), fetch_redirect_response=False)
                self.assertEqual(set(self.employee.visible_lines.values_list("pk", flat=True)), expected)
                self.assertEqual(self.snapshot(), before)

    def test_main_line_and_manager_posted_changes_are_ignored(self):
        before = self.snapshot()
        response = self.update(self.data([self.extra], line=self.foreign.pk, manager=self.other.pk))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.employee.visible_lines.filter(pk=self.extra.pk).exists())

    def test_unchanged_matricule_is_accepted_and_posted_change_ignored(self):
        form = EmployeeForm(self.data([self.extra]), instance=self.employee, user=self.owner)
        self.assertTrue(form.is_valid(), form.errors)
        before = self.snapshot()
        response = self.update(self.data([self.extra], matricule="VIS-CHANGED"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.snapshot(), before)

    def test_forged_protected_fields_are_ignored_without_employee_update(self):
        before = self.snapshot()
        with CaptureQueriesContext(connection) as queries:
            response = self.update(self.data(
                [self.extra], matricule="FORGED", first_name="Forged", last_name="Forged",
                hire_date="invalid-date", manager=self.other.pk, line=self.foreign.pk,
            ))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(set(self.employee.visible_lines.all()), {self.main, self.extra, self.foreign})
        self.assertFalse(any(
            'UPDATE "matrix_employee"' in query["sql"] for query in queries
        ))

    def test_only_visibility_fields_are_needed_in_edit_post(self):
        before = self.snapshot()
        response = self.update({"visible_lines": [self.extra.pk]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.employee.visible_lines.filter(pk=self.main.pk).exists())

    def test_edit_page_displays_protected_information_readonly(self):
        class Controls(HTMLParser):
            def __init__(self):
                super().__init__()
                self.fields = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                key = attrs.get("name") or attrs.get("id")
                if tag in ("input", "select") and key:
                    self.fields[key] = attrs

        self.client.force_login(self.owner)
        response = self.client.get(reverse("employee_update", args=[self.employee.pk]))
        controls = Controls()
        controls.feed(response.content.decode())
        for name in ("matricule", "first_name", "last_name", "hire_date", "id_manager_display"):
            with self.subTest(name=name):
                self.assertIn("readonly", controls.fields[name])
        self.assertIn("disabled", controls.fields["line"])
        self.assertEqual(controls.fields["hire_date"]["value"], self.employee.hire_date.isoformat())
        self.assertEqual(controls.fields["id_manager_display"]["value"], self.owner.username)
        self.assertNotIn("disabled", controls.fields["visible_lines"])

    def test_unmanaged_line_cannot_be_added_even_with_forged_post(self):
        unmanaged = ProductionLine.objects.create(code="VIS-D", name="Forbidden")
        for line in (unmanaged, self.foreign):
            with self.subTest(line=line):
                response = self.update(self.data([self.extra, line]))
                self.assertEqual(response.status_code, 200)
                self.assertIn("visible_lines", response.context["form"].errors)
                self.assertEqual(set(self.employee.visible_lines.values_list("pk", flat=True)),
                                 {self.main.pk, self.foreign.pk})

    def test_owner_permission_enforced_by_view_and_form(self):
        before = self.snapshot()
        for user in (self.other, self.outsider):
            with self.subTest(user=user):
                response = self.update(self.data([self.foreign]), user=user)
                self.assertEqual(response.status_code, 404)
                form = EmployeeForm(self.data([self.foreign]), instance=self.employee, user=user)
                self.assertFalse(form.is_valid())
                self.assertIn("Vous ne gérez pas cet opérateur.", form.non_field_errors())
        self.client.logout()
        response = self.client.post(reverse("employee_update", args=[self.employee.pk]), self.data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.snapshot(), before)

    def test_main_line_outside_managed_lines_is_preserved(self):
        self.main.team_leaders.remove(self.owner)
        before = self.snapshot()
        response = self.update(self.data([self.extra]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(set(self.employee.visible_lines.values_list("pk", flat=True)),
                         {self.main.pk, self.extra.pk, self.foreign.pk})

    def test_edit_form_offers_managed_lines_and_locks_primary(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("employee_update", args=[self.employee.pk]))
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(set(form.fields["visible_lines"].queryset), {self.main, self.extra})
        self.assertTrue(form.fields["line"].disabled)
        self.assertContains(response, 'name="line"\n                            disabled')

    def test_commit_false_defers_visibility_until_save_m2m(self):
        before = self.snapshot()
        form = EmployeeForm(self.data([self.extra]), instance=self.employee, user=self.owner)
        self.assertTrue(form.is_valid(), form.errors)
        form.save(commit=False)
        self.assertFalse(self.employee.visible_lines.filter(pk=self.extra.pk).exists())
        form.save_m2m()
        self.assertTrue(self.employee.visible_lines.filter(pk=self.extra.pk).exists())
        self.assertTrue(self.employee.visible_lines.filter(pk=self.foreign.pk).exists())
        self.assertEqual(self.snapshot(), before)

    def test_creation_converts_pending_employee_with_multiple_lines(self):
        pending = PendingEmployee.objects.create(
            matricule="VIS-NEW", first_name="New", last_name="Operator",
        )
        self.client.force_login(self.owner)
        creation_form = EmployeeForm(user=self.owner)
        for field in creation_form.fields.values():
            self.assertFalse(field.disabled)
        response = self.client.post(reverse("employee_create"), {
            "matricule": pending.matricule, "first_name": pending.first_name,
            "last_name": pending.last_name, "line": self.main.pk,
            "visible_lines": [self.main.pk, self.extra.pk],
        })
        self.assertEqual(response.status_code, 302)
        created = Employee.objects.get(matricule="VIS-NEW")
        self.assertEqual(created.manager_id, self.owner.pk)
        self.assertEqual(created.line_id, self.main.pk)
        self.assertEqual(set(created.visible_lines.all()), {self.main, self.extra})
        self.assertFalse(PendingEmployee.objects.filter(pk=pending.pk).exists())

    def test_creation_rejects_existing_employee_and_unmanaged_line(self):
        before = self.snapshot()
        self.client.force_login(self.owner)
        response = self.client.post(reverse("employee_create"), self.data([self.main], line=self.main.pk))
        self.assertEqual(response.status_code, 200)
        self.assertIn("matricule", response.context["form"].errors)
        pending = PendingEmployee.objects.create(matricule="VIS-NEW", first_name="New", last_name="Test")
        response = self.client.post(reverse("employee_create"), {
            "matricule": pending.matricule, "first_name": "New", "last_name": "Test",
            "line": self.main.pk, "visible_lines": [self.foreign.pk],
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("visible_lines", response.context["form"].errors)
        self.assertTrue(PendingEmployee.objects.filter(pk=pending.pk).exists())
        self.assertEqual(self.snapshot(), before)
