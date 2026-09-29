from datetime import timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import re

from PIL import Image
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection
from django.db.backends.utils import CursorWrapper
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .forms import RequalificationForm, TrainingPlanImageForm
from .models import (
    Employee, LineTransfer, MatrixEntry, ProductionLine, RequalificationApprovalRequest,
    RequalificationRecord, Task, TrainingApprovalRequest, TrainingLevel,
    TrainingQuestionnaire, TrainingQuestionAnswer, TrainingRecord,
)
from .test_training_plans import plan_image


class RequalificationPlanTests(TestCase):
    def setUp(self):
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        self.media_root = Path(media.name)
        setting = override_settings(MEDIA_ROOT=media.name)
        setting.enable()
        self.addCleanup(setting.disable)
        users = get_user_model()
        self.chef = users.objects.create_user(username="requal-chef")
        self.other = users.objects.create_user(username="requal-other")
        self.rh = users.objects.create_superuser(username="requal-admin", email="rh@example.com", password=None)
        self.group_rh = users.objects.create_user(username="requal-rh")
        self.group_rh.groups.add(Group.objects.get_or_create(name="RH")[0])
        self.supervisor = users.objects.create_user(username="requal-supervisor")
        self.supervisor.groups.add(Group.objects.get_or_create(name="Superviseur")[0])
        self.supervisor_admin = users.objects.create_superuser(
            username="requal-supervisor-admin", email="supervisor@example.com", password=None,
        )
        self.supervisor_admin.groups.add(Group.objects.get(name="Superviseur"))
        self.line = ProductionLine.objects.create(code="RQ1", name="Requalification line")
        self.line.team_leaders.add(self.chef)
        self.employee = Employee.objects.create(
            matricule="RQ1", first_name="Requalification", last_name="Operator",
            line=self.line, manager=self.chef, user=self.chef,
        )
        self.employee.visible_lines.add(self.line)
        self.task = Task.objects.create(code="RQ1", title="Requalification task", task_number=1)
        self.task.lines.add(self.line)
        self.original = TrainingRecord.objects.create(
            employee=self.employee, task=self.task, level=TrainingLevel.SUPERVISED,
            date=timezone.now().date() - timedelta(days=200),
        )
        self.original.lines.set([self.line])
        self.create_url = reverse("process_requalification")
        self.review_url = reverse("rh_requalification_approvals")
        self.history_url = reverse("employee_history", args=[self.employee.pk])
        self.client.force_login(self.chef)

    def data(self, **extra):
        return {"employee_id": self.employee.pk, "line_id": self.line.pk,
                "result": "PASS", "requalification_date": timezone.now().date().isoformat(),
                "notes": "Notes conservées", **extra}

    def create(self, **extra):
        response = self.client.post(self.create_url, self.data(training_plan_image=plan_image(), **extra))
        self.assertEqual(response.status_code, 302)
        return RequalificationApprovalRequest.objects.latest("pk")

    def snapshot(self):
        return {
            model.__name__: list(model.objects.order_by("pk").values())
            for model in (RequalificationApprovalRequest, RequalificationRecord, TrainingRecord,
                          MatrixEntry, TrainingRecord.lines.through, TrainingQuestionnaire,
                          TrainingQuestionAnswer, LineTransfer)
        }

    def stored_files(self):
        return [p for p in self.media_root.rglob("*") if p.is_file()]

    def review(self, req, action="approve", user=None):
        self.client.force_login(user or self.rh)
        return self.client.post(self.review_url, {"request_id": req.pk, "action": action})

    def legacy_request(self, approved=None):
        # Simulate a row predating the feature; raw fixture loading deliberately
        # bypasses runtime save guards, just as historical migration models do.
        req = RequalificationApprovalRequest(
            employee=self.employee, line=self.line, requested_by=self.chef,
            requested_result="PASS", requested_new_level=0, approved=approved,
            requalification_date=timezone.now().date(), date_requested=timezone.now(),
        )
        req.save_base(raw=True, force_insert=True)
        return req

    def legacy_record(self):
        rec = RequalificationRecord(
            employee=self.employee, line=self.line, result="PASS", new_level=0,
            evaluated_by=self.chef, requalification_date=timezone.now().date(),
            created_at=timezone.now(),
        )
        rec.save_base(raw=True, force_insert=True)
        return rec

    def assert_invalid_image(self, uploaded):
        before = self.snapshot()
        response = self.client.post(self.create_url, self.data(training_plan_image=uploaded))
        self.assertEqual(response.status_code, 200)
        self.assertIn("training_plan_image", response.context["form"].errors)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.stored_files())

    def test_missing_image_creates_nothing_and_preserves_form_values(self):
        before = self.snapshot()
        response = self.client.post(self.create_url, self.data())
        self.assertContains(response, "Le plan de formation est obligatoire pour créer une requalification.")
        self.assertContains(response, "Notes conservées")
        self.assertEqual(response.context["form"].data["employee_id"], str(self.employee.pk))
        self.assertEqual(response.context["form"].data["line_id"], str(self.line.pk))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.stored_files())

    def test_fake_image_is_rejected(self):
        self.assert_invalid_image(SimpleUploadedFile("fake.png", b"not an image", "image/png"))

    def test_empty_image_is_rejected(self):
        self.assert_invalid_image(SimpleUploadedFile("empty.png", b"", "image/png"))

    def test_gif_is_rejected(self):
        self.assert_invalid_image(plan_image(image_format="GIF"))

    def test_jpeg_png_webp_create_pending_without_other_objects(self):
        for image_format in ("JPEG", "PNG", "WEBP"):
            with self.subTest(image_format=image_format):
                response = self.client.post(self.create_url, self.data(training_plan_image=plan_image(image_format=image_format)))
                self.assertEqual(response.status_code, 302)
                req = RequalificationApprovalRequest.objects.latest("pk")
                self.assertIsNone(req.approved)
                self.assertIsNone(req.decision_date)
                self.assertTrue(req.has_training_plan)
                self.assertFalse(RequalificationRecord.objects.exists())
                self.assertEqual(TrainingRecord.objects.count(), 1)
                self.assertFalse(MatrixEntry.objects.exists())
                self.assertFalse(TrainingQuestionnaire.objects.exists())
                self.review(req, "reject")
                self.client.force_login(self.chef)

    def test_over_ten_megabytes_is_rejected(self):
        content = plan_image().read()
        self.assert_invalid_image(SimpleUploadedFile("large.png", content + b"x" * (10 * 1024 * 1024), "image/png"))

    def test_over_twenty_five_million_pixels_is_rejected(self):
        content = BytesIO()
        Image.new("1", (5001, 5000)).save(content, format="PNG")
        self.assert_invalid_image(SimpleUploadedFile("large.png", content.getvalue(), "image/png"))

    def test_normalized_extension_and_unique_path(self):
        req = self.create()
        self.assertTrue(req.training_plan_image.name.startswith("requalification_plans/"))
        self.assertNotIn("None", req.training_plan_image.name)
        self.assertTrue(req.training_plan_image.name.endswith(".png"))
        form = RequalificationForm(self.data(), files={"training_plan_image": plan_image(name="wrong.jpg")}, user=self.chef)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["training_plan_image"].name, "plan.png")

    def test_validation_itself_never_creates_objects_or_stored_files(self):
        before = self.snapshot()
        form = RequalificationForm(self.data(), files={"training_plan_image": plan_image()}, user=self.chef)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.stored_files())

    def test_storage_failure_creates_no_request(self):
        storage = RequalificationApprovalRequest._meta.get_field("training_plan_image").storage
        before = self.snapshot()
        with patch.object(storage, "save", side_effect=OSError("storage failure")):
            response = self.client.post(self.create_url, self.data(training_plan_image=plan_image()))
        self.assertContains(response, "Enregistrement impossible")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.stored_files())

    def test_database_failure_after_upload_removes_file(self):
        before = self.snapshot()
        with patch.object(RequalificationApprovalRequest, "save", side_effect=IntegrityError("SQL failure")):
            response = self.client.post(self.create_url, self.data(training_plan_image=plan_image()))
        self.assertContains(response, "Enregistrement impossible")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.stored_files())

    def test_duplicate_pending_request_does_not_store_another_file(self):
        self.create()
        before = self.snapshot()
        response = self.client.post(self.create_url, self.data(training_plan_image=plan_image()))
        self.assertContains(response, "déjà en attente")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.stored_files()), 1)

    def test_wrong_manager_is_forbidden_before_image_validation(self):
        self.client.force_login(self.other)
        before = self.snapshot()
        response = self.client.post(self.create_url, self.data())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_business_data_creates_nothing(self):
        for values in ({"result": "INVALID"}, {"employee_id": "bad"}, {"line_id": "bad"},
                       {"task_id": "bad"}, {"requalification_date": "bad"}, {"new_level": 2}):
            with self.subTest(values=values):
                before = self.snapshot()
                response = self.client.post(self.create_url, self.data(training_plan_image=plan_image(), **values))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(self.stored_files())

    def test_global_approval_creates_linked_record_only(self):
        req = self.create()
        self.review(req)
        req.refresh_from_db()
        self.assertTrue(req.approved)
        rec = req.requalification_record
        self.assertIsNone(rec.task_id)
        self.assertIsNone(rec.training_record_id)
        self.assertEqual(rec.line, self.line)
        self.assertEqual(TrainingRecord.objects.count(), 1)
        self.assertFalse(MatrixEntry.objects.exists())

    def test_task_approval_links_training_and_updates_selected_line_and_matrix(self):
        second = ProductionLine.objects.create(code="RQ2", name="Second line")
        self.employee.visible_lines.add(second)
        second.team_leaders.add(self.chef)
        self.task.lines.add(second)
        req = self.create(task_id=self.task.pk, line_id=second.pk, new_level=TrainingLevel.TRAINER)
        self.review(req)
        rec = RequalificationRecord.objects.get(approval_request=req)
        training = rec.training_record
        self.assertEqual(training.approval_status, "approved")
        self.assertEqual(list(training.lines.all()), [second])
        self.assertEqual(training.level, TrainingLevel.TRAINER)
        self.assertEqual(MatrixEntry.objects.get(employee=self.employee, task=self.task).level, TrainingLevel.TRAINER)
        self.assertFalse(TrainingQuestionnaire.objects.exists())

    def test_missing_file_blocks_approval_without_any_writes(self):
        req = self.create(task_id=self.task.pk)
        req.training_plan_image.storage.delete(req.training_plan_image.name)
        before = self.snapshot()
        response = self.review(req)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.snapshot(), before)
        response = self.client.get(self.review_url)
        self.assertContains(response, "Plan indisponible")
        self.assertFalse(response.context["pending"][0].plan_available)
        self.assertContains(response, 'title="Approuver" disabled')

    def test_unreadable_file_blocks_approval_without_any_writes(self):
        req = self.create()
        before = self.snapshot()
        with patch.object(req.training_plan_image.storage, "open", side_effect=PermissionError("unreadable")):
            self.review(req)
        self.assertEqual(self.snapshot(), before)

    def test_legacy_pending_without_plan_can_be_rejected_but_not_approved(self):
        req = self.legacy_request()
        before = self.snapshot()
        self.review(req)
        self.assertEqual(self.snapshot(), before)
        self.review(req, "reject")
        req.refresh_from_db()
        self.assertFalse(req.approved)
        self.assertFalse(RequalificationRecord.objects.exists())

    def test_rejection_with_plan_keeps_file_and_creates_no_record(self):
        req = self.create()
        name = req.training_plan_image.name
        self.review(req, "reject")
        req.refresh_from_db()
        self.assertFalse(req.approved)
        self.assertEqual(req.training_plan_image.name, name)
        self.assertTrue(req.has_training_plan)
        self.assertFalse(RequalificationRecord.objects.exists())

    def test_record_failure_rolls_back_whole_approval(self):
        req = self.create(task_id=self.task.pk)
        before = self.snapshot()
        with patch.object(RequalificationRecord.objects, "create", side_effect=IntegrityError("record failure")):
            self.review(req)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.stored_files()), 1)

    def test_training_failure_rolls_back_whole_approval(self):
        req = self.create(task_id=self.task.pk)
        before = self.snapshot()
        with patch.object(TrainingRecord.objects, "create", side_effect=IntegrityError("training failure")):
            self.review(req)
        self.assertEqual(self.snapshot(), before)

    def test_matrix_failure_rolls_back_whole_approval(self):
        req = self.create(task_id=self.task.pk)
        before = self.snapshot()
        with patch.object(MatrixEntry.objects, "update_or_create", side_effect=IntegrityError("matrix failure")):
            self.review(req)
        self.assertEqual(self.snapshot(), before)

    def test_repeated_decision_is_not_applied_twice(self):
        req = self.create(task_id=self.task.pk)
        self.review(req)
        before = self.snapshot()
        self.review(req)
        self.review(req, "reject")
        self.assertEqual(self.snapshot(), before)

    def test_decision_permissions_and_get_are_unchanged(self):
        req = self.create()
        before = self.snapshot()
        for user in (self.chef, self.other, self.group_rh, self.supervisor):
            self.review(req, user=user)
            self.assertEqual(self.snapshot(), before)
        self.client.force_login(self.rh)
        self.client.get(self.review_url, {"request_id": req.pk, "action": "approve"})
        self.assertEqual(self.snapshot(), before)

    def test_rh_page_links_to_the_plan_before_decision(self):
        req = self.create()
        self.client.force_login(self.rh)
        response = self.client.get(self.review_url)
        self.assertContains(response, reverse("requalification_plan_view", args=[req.pk]))
        self.assertTrue(response.context["pending"][0].plan_available)

    def test_owner_group_rh_and_superuser_can_read_page_and_image(self):
        req = self.create()
        page = reverse("requalification_plan_view", args=[req.pk])
        image_url = reverse("requalification_plan_image", args=[req.pk])
        for user in (self.chef, self.group_rh, self.rh, self.supervisor_admin):
            with self.subTest(user=user):
                self.client.force_login(user)
                response = self.client.get(page)
                self.assertContains(response, image_url)
                self.assertIn("no-store", response["Cache-Control"])
                document = self.client.get(image_url)
                self.assertEqual(document.status_code, 200)
                self.assertEqual(document["Content-Type"], "image/png")
                self.assertEqual(document["X-Content-Type-Options"], "nosniff")
                self.assertIn("no-store", document["Cache-Control"])
                self.assertTrue(b"".join(document.streaming_content))
                document.close()

    def test_unauthorized_page_and_direct_image_are_denied(self):
        req = self.create()
        for user in (self.other, self.supervisor):
            self.client.force_login(user)
            for route in ("requalification_plan_view", "requalification_plan_image"):
                self.assertEqual(self.client.get(reverse(route, args=[req.pk])).status_code, 404)
        self.client.logout()
        for route in ("requalification_plan_view", "requalification_plan_image"):
            self.assertEqual(self.client.get(reverse(route, args=[req.pk])).status_code, 302)

    def test_no_public_media_or_deferred_upload(self):
        req = self.create()
        self.assertEqual(self.client.get("/media/" + req.training_plan_image.name).status_code, 404)
        self.assertEqual(self.client.post(reverse("requalification_plan_view", args=[req.pk]), {"training_plan_image": plan_image()}).status_code, 405)
        self.assertEqual(self.client.post(reverse("requalification_plan_image", args=[req.pk]), {}).status_code, 405)

    def test_missing_file_returns_404_on_both_routes(self):
        req = self.create()
        req.training_plan_image.storage.delete(req.training_plan_image.name)
        for route in ("requalification_plan_view", "requalification_plan_image"):
            self.assertEqual(self.client.get(reverse(route, args=[req.pk])).status_code, 404)

    def test_normal_request_id_does_not_resolve_as_requalification(self):
        normal = TrainingApprovalRequest.objects.create(
            pk=1234, employee=self.employee, task=self.task, requested_level=1,
            requested_by=self.chef, training_date=timezone.now().date(),
        )
        normal.training_plan_image.save("plan.png", plan_image())
        self.assertEqual(self.client.get(reverse("requalification_plan_view", args=[normal.pk])).status_code, 404)

    def test_same_file_in_both_histories_after_approval_without_copies(self):
        req = self.create()
        name = req.training_plan_image.name
        original_bytes = Path(req.training_plan_image.path).read_bytes()
        self.review(req)
        before = self.snapshot()
        url = reverse("requalification_plan_view", args=[req.pk])
        for history in (self.history_url, reverse("requalification_history")):
            self.assertContains(self.client.get(history), url)
        response = self.client.get(reverse("requalification_plan_image", args=[req.pk]))
        self.assertEqual(b"".join(response.streaming_content), original_bytes)
        response.close()
        req.refresh_from_db()
        self.assertEqual(req.training_plan_image.name, name)
        self.assertEqual(len(self.stored_files()), 1)
        self.assertEqual(self.snapshot(), before)
        for model in (RequalificationRecord, TrainingRecord):
            self.assertNotIn("training_plan_image", {f.name for f in model._meta.fields})

    def test_global_requalification_is_visible_in_operator_history(self):
        req = self.create()
        self.review(req)
        response = self.client.get(self.history_url)
        events = response.context["formation_events"]
        self.assertEqual(len(events), 2)
        self.assertEqual(sum(bool(e.get("requalification")) for e in events), 1)
        self.assertContains(response, "Requalification globale")

    def test_linked_training_is_one_event_even_at_level_i(self):
        req = self.create(task_id=self.task.pk, new_level=0)
        self.review(req)
        response = self.client.get(self.history_url)
        events = response.context["formation_events"]
        self.assertEqual(len(events), 2)  # old training + the new single event
        self.assertEqual(sum(bool(e.get("requalification")) for e in events), 1)
        self.assertContains(response, reverse("requalification_plan_view", args=[req.pk]), count=1)

    def test_legacy_approved_rows_remain_editable_and_visible_without_plan(self):
        req = self.legacy_request(approved=True)
        rec = self.legacy_record()
        req.notification_seen = True
        req.save()
        rec.notes = "Ancien historique conservé"
        rec.save()
        before = self.snapshot()
        for history in (self.history_url, reverse("requalification_history")):
            self.assertContains(self.client.get(history), "Plan indisponible")
        self.assertEqual(self.snapshot(), before)
        req.refresh_from_db()
        self.assertTrue(req.approved)

    def test_history_hides_other_owners_document_link(self):
        req = self.create()
        self.review(req)
        self.client.force_login(self.other)
        response = self.client.get(self.history_url)
        self.assertContains(response, "Accès réservé")
        self.assertNotContains(response, reverse("requalification_plan_view", args=[req.pk]))

    def test_history_filters_statistics_and_transfers_remain_consistent(self):
        req = self.create(task_id=self.task.pk, new_level=3)
        self.review(req)
        response = self.client.get(self.history_url, {"line": self.line.pk, "level": 3})
        self.assertEqual(len(response.context["formation_events"]), 1)
        self.assertEqual(response.context["stats"]["total_formations"], 1)
        self.assertEqual(response.context["stats"]["total_transferts"], 1)
        self.assertContains(response, "Transfert de Ligne")

    def test_history_query_count_is_constant_for_more_requalifications(self):
        self.legacy_record()
        self.client.force_login(self.rh)
        with CaptureQueriesContext(connection) as first:
            self.client.get(self.history_url)
        for _ in range(5):
            self.legacy_record()
        with CaptureQueriesContext(connection) as more:
            self.client.get(self.history_url)
        self.assertEqual(len(first), len(more))

    def test_direct_management_creation_does_not_require_image(self):
        for user in (self.rh, self.group_rh, self.supervisor_admin):
            self.client.force_login(user)
            response = self.client.post(self.create_url, self.data())
            self.assertEqual(response.status_code, 302)
            req = RequalificationApprovalRequest.objects.latest("pk")
            self.assertTrue(req.approved)
            self.assertFalse(req.training_plan_image)
            self.assertEqual(req.requalification_record.approval_request, req)
        self.assertFalse(self.stored_files())

    def test_management_direct_leader_creation_without_plan_remains_global(self):
        url = reverse("rh_requalify_team_leader")
        for user in (self.rh, self.group_rh, self.supervisor, self.supervisor_admin):
            with self.subTest(user=user):
                self.client.force_login(user)
                response = self.client.post(url, self.data())
                self.assertEqual(response.status_code, 302)
                req = RequalificationApprovalRequest.objects.latest("pk")
                self.assertTrue(req.approved)
                self.assertEqual(req.requested_by, user)
                self.assertFalse(req.training_plan_image)
                self.assertIsNone(req.requalification_record.training_record_id)
                self.assertEqual(TrainingRecord.objects.count(), 1)
                # Each role exercises a first creation, independently of cooldown.
                req.requalification_record.delete()
                req.delete()
        self.assertFalse(self.stored_files())

    def test_direct_management_task_creation_without_plan_keeps_training_links(self):
        for user in (self.rh, self.supervisor_admin):
            self.client.force_login(user)
            response = self.client.post(self.create_url, self.data(task_id=self.task.pk, new_level=3))
            self.assertEqual(response.status_code, 302)
            req = RequalificationApprovalRequest.objects.latest("pk")
            self.assertTrue(req.approved)
            self.assertFalse(req.training_plan_image)
            training = req.requalification_record.training_record
            self.assertEqual(training.employee, self.employee)
            self.assertEqual(training.task, self.task)
            self.assertEqual(list(training.lines.all()), [self.line])
            self.assertEqual(MatrixEntry.objects.get(employee=self.employee, task=self.task).level, 3)

    def test_non_admin_supervisor_manager_can_submit_without_upload(self):
        self.employee.manager = self.supervisor
        self.employee.save()
        self.client.force_login(self.supervisor)
        response = self.client.post(self.create_url, self.data(training_plan_image=plan_image()))
        self.assertEqual(response.status_code, 302)
        req = RequalificationApprovalRequest.objects.get()
        self.assertIsNone(req.approved)  # Preserve the existing decision permissions.
        self.assertFalse(req.training_plan_image)
        self.review(req)
        req.refresh_from_db()
        self.assertTrue(req.approved)
        self.assertFalse(self.stored_files())

    def test_management_creation_forms_hide_upload_even_with_team_assignments(self):
        for user in (self.rh, self.group_rh, self.supervisor, self.supervisor_admin):
            self.line.team_leaders.add(user)
            self.employee.manager = user
            self.employee.save()
            self.client.force_login(user)
            for route in ("employees_to_requalify", "rh_requalify_team_leader"):
                with self.subTest(user=user, route=route):
                    response = self.client.get(reverse(route))
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn("training_plan_image", response.context["form"].fields)
                    self.assertNotContains(response, 'name="training_plan_image"')
                    self.assertNotContains(response, "obligatoire pour créer une requalification")

    def test_management_forged_upload_is_ignored_on_both_creation_routes(self):
        for user in (self.rh, self.group_rh, self.supervisor_admin, self.supervisor):
            self.client.force_login(user)
            routes = ["rh_requalify_team_leader"]
            if user != self.supervisor:
                routes.append("process_requalification")
            for route in routes:
                with self.subTest(user=user, route=route):
                    response = self.client.post(reverse(route), self.data(
                        training_plan_image=SimpleUploadedFile("fake.png", b"invalid", "image/png"),
                    ))
                    self.assertEqual(response.status_code, 302)
                    req = RequalificationApprovalRequest.objects.latest("pk")
                    self.assertTrue(req.approved)
                    self.assertFalse(req.training_plan_image)
                    req.requalification_record.delete()
                    req.delete()
        self.assertFalse(self.stored_files())

    def test_service_ignores_injected_management_plan(self):
        from .services_requalification import create_requalification
        form = RequalificationForm(self.data(), user=self.rh)
        self.assertTrue(form.is_valid(), form.errors)
        form.cleaned_data["training_plan_image"] = plan_image()
        req = create_requalification(form=form, user=self.rh)
        self.assertTrue(req.approved)
        self.assertFalse(req.training_plan_image)
        self.assertFalse(self.stored_files())

    def test_management_orm_creation_allows_no_plan_and_rejects_upload(self):
        for user in (self.rh, self.group_rh, self.supervisor, self.supervisor_admin):
            with self.subTest(user=user):
                values = dict(employee=self.employee, requested_by=user, requested_result="PASS",
                              requalification_date=timezone.now().date())
                req = RequalificationApprovalRequest.objects.create(**values)
                self.assertFalse(req.requires_training_plan)
                with self.assertRaises(ValidationError):
                    RequalificationApprovalRequest.objects.create(**values, training_plan_image=plan_image())
                req.training_plan_image = plan_image()
                with self.assertRaises(ValidationError):
                    req.save()
        self.assertFalse(self.stored_files())

    def test_management_approval_without_upload_preserves_original_file(self):
        for user in (self.rh, self.supervisor_admin):
            self.client.force_login(self.chef)
            req = self.create()
            original = Path(req.training_plan_image.path).read_bytes()
            name = req.training_plan_image.name
            response = self.review(req, user=user)
            self.assertEqual(response.status_code, 302)
            req.refresh_from_db()
            self.assertTrue(req.approved)
            self.assertEqual(req.training_plan_image.name, name)
            self.assertEqual(Path(req.training_plan_image.path).read_bytes(), original)

    def test_management_decision_ignores_replacement_upload(self):
        for user in (self.rh, self.supervisor_admin):
            self.client.force_login(self.chef)
            req = self.create()
            name = req.training_plan_image.name
            original = Path(req.training_plan_image.path).read_bytes()
            self.client.force_login(user)
            response = self.client.post(self.review_url, {
                "request_id": req.pk, "action": "approve", "training_plan_image": plan_image(),
            })
            self.assertEqual(response.status_code, 302)
            req.refresh_from_db()
            self.assertTrue(req.approved)
            self.assertEqual(req.training_plan_image.name, name)
            self.assertEqual(Path(req.training_plan_image.path).read_bytes(), original)
        self.assertEqual(len(self.stored_files()), 2)

    def test_management_pending_request_without_plan_can_be_approved(self):
        req = RequalificationApprovalRequest.objects.create(
            employee=self.employee, requested_by=self.rh, requested_result="PASS",
            requested_new_level=0, requalification_date=timezone.now().date(),
        )
        self.client.force_login(self.supervisor_admin)
        response = self.client.get(self.review_url)
        self.assertNotContains(response, 'title="Approuver" disabled')
        self.review(req, user=self.supervisor_admin)
        req.refresh_from_db()
        self.assertTrue(req.approved)
        self.assertTrue(RequalificationRecord.objects.filter(approval_request=req).exists())

    def test_existing_plan_stays_required_after_author_becomes_management(self):
        req = self.create()
        self.chef.groups.add(Group.objects.get(name="RH"))
        req.training_plan_image.storage.delete(req.training_plan_image.name)
        before = self.snapshot()
        self.review(req)
        self.assertEqual(self.snapshot(), before)

    def test_only_team_relations_enable_the_creation_upload(self):
        form = RequalificationForm(user=self.other)
        self.assertNotIn("training_plan_image", form.fields)
        self.line.team_leaders.remove(self.chef)
        # A manager remains a chief in the existing employee-based workflow.
        form = RequalificationForm(user=self.chef)
        self.assertTrue(form.fields["training_plan_image"].required)

    def test_chef_cannot_access_management_creation_route(self):
        before = self.snapshot()
        url = reverse("rh_requalify_team_leader")
        self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(self.client.post(url, self.data(training_plan_image=plan_image())).status_code, 302)
        self.assertEqual(self.snapshot(), before)

    def test_management_plan_urls_are_read_only(self):
        req = self.create()
        original = Path(req.training_plan_image.path).read_bytes()
        for user in (self.rh, self.supervisor_admin):
            self.client.force_login(user)
            for route in ("requalification_plan_view", "requalification_plan_image"):
                response = self.client.post(reverse(route, args=[req.pk]), {"training_plan_image": plan_image()})
                self.assertEqual(response.status_code, 405)
        self.assertEqual(Path(req.training_plan_image.path).read_bytes(), original)
        self.assertEqual(len(self.stored_files()), 1)

    def test_direct_creation_failure_cleans_file_and_rolls_back_everything(self):
        self.client.force_login(self.rh)
        before = self.snapshot()
        with patch.object(RequalificationRecord.objects, "create", side_effect=IntegrityError("failure")):
            self.client.post(self.create_url, self.data(training_plan_image=plan_image(), task_id=self.task.pk))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.stored_files())

    def test_leader_cannot_self_requalify_on_direct_route(self):
        self.chef.groups.add(Group.objects.get(name="Superviseur"))
        before = self.snapshot()
        response = self.client.post(reverse("rh_requalify_team_leader"), self.data(training_plan_image=plan_image()))
        self.assertContains(response, "Vous ne pouvez pas vous requalifier vous-même")
        self.assertEqual(self.snapshot(), before)

    def test_admin_cannot_create_or_change_decision_or_upload(self):
        req = self.legacy_request()
        request = RequestFactory().get("/admin/")
        request.user = self.rh
        request_admin = admin.site._registry[RequalificationApprovalRequest]
        record_admin = admin.site._registry[RequalificationRecord]
        self.assertFalse(request_admin.has_add_permission(request))
        self.assertFalse(record_admin.has_add_permission(request))
        self.assertIn("approved", request_admin.get_readonly_fields(request, req))
        self.assertIn("training_plan_image", request_admin.exclude)
        self.assertNotIn("mark_as_pass", record_admin.get_actions(request))
        self.assertNotIn("mark_as_retrain", record_admin.get_actions(request))

    def test_model_and_queryset_cannot_bypass_plan_or_decision(self):
        values = dict(employee=self.employee, requested_by=self.chef, requested_result="PASS",
                      requalification_date=timezone.now().date())
        with self.assertRaises(ValidationError):
            RequalificationApprovalRequest.objects.create(**values)
        with self.assertRaises(ValidationError):
            RequalificationRecord.objects.create(employee=self.employee, result="PASS", requalification_date=timezone.now().date())
        req = self.legacy_request()
        req.approved = True
        with self.assertRaises(ValidationError):
            req.save()
        with self.assertRaises(ValidationError):
            RequalificationApprovalRequest.objects.filter(pk=req.pk).update(approved=True)
        with self.assertRaises(ValidationError):
            RequalificationApprovalRequest.objects.bulk_create([RequalificationApprovalRequest(**values)])

    def test_initial_plan_and_origin_relations_are_read_only(self):
        req = self.create()
        original = req.training_plan_image.name
        req.training_plan_image = "different.png"
        with self.assertRaises(ValidationError):
            req.save()
        req.refresh_from_db()
        self.assertEqual(req.training_plan_image.name, original)
        self.review(req)
        rec = RequalificationRecord.objects.get(approval_request=req)
        rec.approval_request = None
        with self.assertRaises(ValidationError):
            rec.save()

    def test_replacement_with_same_name_and_owner_change_are_blocked(self):
        req = self.create()
        name = req.training_plan_image.name
        req.training_plan_image = plan_image(name=name)
        with self.assertRaises(ValidationError):
            req.save()
        req.refresh_from_db()
        req.requested_by = self.other
        with self.assertRaises(ValidationError):
            req.save()
        with self.assertRaises(ValidationError):
            RequalificationApprovalRequest.objects.filter(pk=req.pk).update(requested_by=self.other)
        self.assertEqual(len(self.stored_files()), 1)

    def test_concurrent_decision_is_not_overwritten_by_metadata_save(self):
        req = self.create()
        original_save_table = RequalificationApprovalRequest._save_table
        def decide_before_write(instance, *args, **kwargs):
            self.review(req)
            return original_save_table(instance, *args, **kwargs)
        req.notes = "Métadonnées modifiées"
        with patch.object(RequalificationApprovalRequest, "_save_table", decide_before_write):
            req.save()
        req.refresh_from_db()
        self.assertTrue(req.approved)
        self.assertIsNotNone(req.decision_date)
        self.assertEqual(req.notes, "Métadonnées modifiées")

    def test_creation_pages_are_multipart_and_bound_to_required_image_field(self):
        response = self.client.get(reverse("employees_to_requalify"))
        self.assertContains(response, 'enctype="multipart/form-data"')
        self.assertContains(response, 'name="training_plan_image"')

    def test_direct_form_receives_valid_line_data_for_javascript(self):
        self.client.force_login(self.supervisor)
        response = self.client.get(reverse("rh_requalify_team_leader"))
        script = re.search(r'<script id="requalification-leader-lines" type="application/json">(.*?)</script>',
                           response.content.decode(), re.S)
        self.assertIsNotNone(script)
        data = json.loads(script.group(1))
        self.assertEqual(data[0]["employee_id"], self.employee.pk)
        self.assertEqual(data[0]["lines_detail"][0]["id"], self.line.pk)
        self.assertNotIn("training_plan_image", response.context["form"].fields)
        self.assertNotContains(response, 'name="training_plan_image"')
        # The current template has one line selector and no hidden duplicate.
        self.assertContains(response, 'name="line_id"', count=1)
        self.assertContains(response, 'selectLine.disabled = true')

    def test_csrf_is_required_for_creation(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.chef)
        before = self.snapshot()
        self.assertEqual(client.post(self.create_url, self.data(training_plan_image=plan_image())).status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_normal_image_form_has_unchanged_required_message(self):
        form = TrainingPlanImageForm(data={})
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["training_plan_image"], ["Sélectionnez une image du plan de formation."])

    def test_orm_creation_validates_and_normalizes_image(self):
        values = dict(employee=self.employee, requested_by=self.chef, requested_result="PASS",
                      requalification_date=timezone.now().date())
        with self.assertRaises(ValidationError):
            RequalificationApprovalRequest.objects.create(
                **values, training_plan_image=SimpleUploadedFile("fake.png", b"fake", "image/png"),
            )
        req = RequalificationApprovalRequest.objects.create(**values, training_plan_image=plan_image(name="wrong.jpg"))
        self.assertTrue(req.training_plan_image.name.endswith(".png"))
        self.assertTrue(req.has_training_plan)
        req.training_plan_image.close()

    def test_orm_insert_failure_after_file_save_cleans_up(self):
        real_execute = CursorWrapper.execute
        def fail_request_insert(cursor, sql, params=None):
            if sql.startswith('INSERT INTO "matrix_requalificationapprovalrequest"'):
                self.assertEqual(len(self.stored_files()), 1)
                raise IntegrityError("insert failed after storage save")
            return real_execute(cursor, sql, params)
        with patch.object(CursorWrapper, "execute", autospec=True, side_effect=fail_request_insert):
            with self.assertRaises(IntegrityError):
                RequalificationApprovalRequest.objects.create(
                    employee=self.employee, requested_by=self.chef, requested_result="PASS",
                    requalification_date=timezone.now().date(), training_plan_image=plan_image(),
                )
        self.assertFalse(RequalificationApprovalRequest.objects.exists())
        self.assertFalse(self.stored_files())

    def test_notification_rerender_does_not_write_on_invalid_creation(self):
        self.legacy_request(approved=True)
        before = self.snapshot()
        self.client.post(self.create_url, self.data())
        self.assertEqual(self.snapshot(), before)

    def test_rh_history_links_to_the_original_plan(self):
        req = self.create()
        self.review(req)
        response = self.client.get(reverse("rh_employees_to_requalify"))
        self.assertContains(response, reverse("requalification_plan_view", args=[req.pk]))
