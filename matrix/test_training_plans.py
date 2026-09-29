from datetime import timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import TrainingPlanImageForm, TrainingRecordForm
from .models import (
    Employee, MatrixEntry, ProductionLine, Task, TrainingApprovalRequest,
    TrainingLevel, TrainingQuestionnaire, TrainingRecord,
)


def plan_image(name="plan.png", image_format="PNG"):
    content = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(content, format=image_format)
    return SimpleUploadedFile(name, content.getvalue(), "image/" + image_format.lower())


class TrainingPlanTests(TestCase):
    def setUp(self):
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        media_settings = override_settings(MEDIA_ROOT=media.name)
        media_settings.enable()
        self.addCleanup(media_settings.disable)
        self.media_root = Path(media.name)
        users = get_user_model()
        self.chef = users.objects.create_user(username="plan-chef")
        self.other = users.objects.create_user(username="plan-other")
        self.rh = users.objects.create_superuser(username="plan-rh", email="rh@example.com", password=None)
        self.group_rh = users.objects.create_user(username="plan-group-rh")
        self.group_rh.groups.add(Group.objects.get_or_create(name="RH")[0])
        self.line = ProductionLine.objects.create(code="PLAN", name="Ligne du plan")
        self.line.team_leaders.add(self.chef, self.other)
        self.employee = Employee.objects.create(
            first_name="Plan", last_name="Operator", matricule="PLAN-001",
            manager=self.chef, line=self.line,
        )
        self.employee.visible_lines.add(self.line)
        self.task = Task.objects.create(code="PLAN-TASK", title="Tâche du plan", task_number=1)
        self.task.lines.add(self.line)

    def make_request(self, level=TrainingLevel.SUPERVISED, owner=None, approved=None, linked=True):
        record = None
        if linked:
            status = {None: "pending", True: "approved", False: "rejected"}[approved]
            record = TrainingRecord.objects.create(
                employee=self.employee, task=self.task, level=level,
                date=timezone.localdate(), approval_status=status,
            )
            record.lines.set([self.line])
        return TrainingApprovalRequest.objects.create(
            employee=self.employee, task=self.task, requested_level=level,
            requested_by=owner or self.chef, training_date=timezone.localdate(),
            line=self.line, training_record=record, approved=approved,
        )

    def upload_url(self, req):
        return reverse("training_plan_upload", args=[req.pk])

    def review(self, req, decision="approve", legacy=False):
        self.client.force_login(self.rh)
        if legacy:
            return self.client.post(reverse("rh_training_approvals"), {
                "request_id": req.pk, "action": decision, "rejection_reason": "Plan à revoir",
            })
        return self.client.post(reverse("review_training_request", args=[req.pk]), {
            "decision": decision, "rejection_reason": "Plan à revoir",
        })

    def test_creation_l_and_u_without_plan_keeps_existing_workflow(self):
        self.client.force_login(self.chef)
        for level, prerequisite, delay in [
            (TrainingLevel.SUPERVISED, TrainingLevel.IN_TRAINING, 8),
            (TrainingLevel.TRAINER, TrainingLevel.SUPERVISED, 22),
        ]:
            with self.subTest(level=level):
                task = Task.objects.create(code=f"CREATE-{level}", title=f"Création {level}", task_number=level + 5)
                task.lines.add(self.line)
                record = TrainingRecord.objects.create(
                    employee=self.employee, task=task, level=prerequisite,
                    date=timezone.localdate() - timedelta(days=delay), approval_status="approved",
                )
                record.lines.set([self.line])
                with patch.object(TrainingRecordForm, "_send_rh_email"):
                    response = self.client.post(reverse("training_record_create"), {
                        "employee": self.employee.pk, "task": task.pk, "level": level,
                        "date": timezone.localdate().isoformat(), "line": self.line.pk, "trainer": "",
                    })
                self.assertEqual(response.status_code, 302)
                req = TrainingApprovalRequest.objects.get(task=task, requested_level=level)
                self.assertEqual(req.requested_by, self.chef)
                self.assertIsNone(req.approved)
                self.assertFalse(req.training_plan_image)
                self.assertEqual(req.training_record.approval_status, "pending")
                self.assertFalse(MatrixEntry.objects.filter(task=task).exists())
                self.assertNotIn("training_plan_image", TrainingRecordForm(user=self.chef).fields)

    def test_list_only_shows_own_requests_and_filters_all_statuses(self):
        own = [self.make_request(approved=status) for status in (None, True, False)]
        other = self.make_request(owner=self.other)
        self.client.force_login(self.chef)
        response = self.client.get(reverse("training_requests_list"))
        self.assertEqual({r.pk for r in response.context["page_obj"]}, {r.pk for r in own})
        self.assertNotContains(response, self.upload_url(other))
        for status, req in zip(("pending", "approved", "rejected"), own):
            response = self.client.get(reverse("training_requests_list"), {"status": status})
            self.assertEqual([r.pk for r in response.context["page_obj"]], [req.pk])
        response = self.client.get(reverse("training_requests_list"), {"status": "invalid", "page": "bad"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_status"], "all")

    def test_upload_changes_only_image_and_never_approves(self):
        req = self.make_request()
        request_before = TrainingApprovalRequest.objects.values().get(pk=req.pk)
        record_before = TrainingRecord.objects.values().get(pk=req.training_record_id)
        counts = (TrainingApprovalRequest.objects.count(), TrainingRecord.objects.count(), TrainingQuestionnaire.objects.count())
        self.client.force_login(self.chef)
        self.assertContains(self.client.get(self.upload_url(req)), 'enctype="multipart/form-data"')
        response = self.client.post(self.upload_url(req), {
            "training_plan_image": plan_image(), "approved": "True", "requested_level": 0,
            "employee": 9999, "approval_status": "approved",
        })
        self.assertRedirects(response, reverse("training_requests_list"))
        request_after = TrainingApprovalRequest.objects.values().get(pk=req.pk)
        self.assertTrue(request_after.pop("training_plan_image"))
        request_before.pop("training_plan_image")
        self.assertEqual(request_after, request_before)
        self.assertEqual(TrainingRecord.objects.values().get(pk=req.training_record_id), record_before)
        self.assertEqual(counts, (TrainingApprovalRequest.objects.count(), TrainingRecord.objects.count(), TrainingQuestionnaire.objects.count()))
        self.assertFalse(MatrixEntry.objects.exists())

    def test_other_chef_cannot_open_upload_or_view_plan(self):
        req = self.make_request()
        req.training_plan_image.save("plan.png", plan_image())
        original = req.training_plan_image.name
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.upload_url(req)).status_code, 404)
        self.assertEqual(self.client.post(self.upload_url(req), {"training_plan_image": plan_image()}).status_code, 404)
        self.assertEqual(self.client.get(reverse("training_plan_view", args=[req.pk])).status_code, 404)
        req.refresh_from_db()
        self.assertEqual(req.training_plan_image.name, original)

    def test_replacement_pending_and_cleanup_after_commit(self):
        req = self.make_request()
        req.training_plan_image.save("plan.png", plan_image())
        old_name = req.training_plan_image.name
        self.client.force_login(self.chef)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.upload_url(req), {"training_plan_image": plan_image()})
        req.refresh_from_db()
        self.assertIsNone(req.approved)
        self.assertNotEqual(req.training_plan_image.name, old_name)
        self.assertTrue(req.has_training_plan)
        self.assertFalse(req.training_plan_image.storage.exists(old_name))

    def test_approved_and_rejected_plans_are_read_only(self):
        self.client.force_login(self.chef)
        for status in (True, False):
            req = self.make_request(approved=status)
            req.training_plan_image.save("plan.png", plan_image())
            original = req.training_plan_image.name
            self.assertEqual(self.client.get(self.upload_url(req)).status_code, 403)
            self.assertEqual(self.client.post(self.upload_url(req), {"training_plan_image": plan_image()}).status_code, 403)
            req.refresh_from_db()
            self.assertEqual(req.training_plan_image.name, original)
            self.assertEqual(req.approved, status)
            response = self.client.get(reverse("training_requests_list"))
            self.assertNotContains(response, self.upload_url(req))

    def test_lu_direct_and_legacy_approval_without_image_changes_nothing(self):
        for level in (TrainingLevel.SUPERVISED, TrainingLevel.TRAINER):
            for legacy in (False, True):
                for linked in (False, True):
                    with self.subTest(level=level, legacy=legacy, linked=linked):
                        req = self.make_request(level=level, linked=linked)
                        before = TrainingApprovalRequest.objects.values().get(pk=req.pk)
                        records = list(TrainingRecord.objects.values())
                        lines = list(TrainingRecord.lines.through.objects.values())
                        response = self.review(req, legacy=legacy)
                        self.assertEqual(response.status_code, 302)
                        self.assertEqual(TrainingApprovalRequest.objects.values().get(pk=req.pk), before)
                        self.assertEqual(list(TrainingRecord.objects.values()), records)
                        self.assertEqual(list(TrainingRecord.lines.through.objects.values()), lines)
                        self.assertFalse(MatrixEntry.objects.exists())

    def test_lu_upload_then_approval_keeps_current_synchronization(self):
        for level in (TrainingLevel.SUPERVISED, TrainingLevel.TRAINER):
            req = self.make_request(level=level)
            self.client.force_login(self.chef)
            self.client.post(self.upload_url(req), {"training_plan_image": plan_image()})
            req.refresh_from_db()
            self.assertIsNone(req.approved)
            response = self.review(req)
            self.assertEqual(response.status_code, 302)
            req.refresh_from_db()
            self.assertTrue(req.approved)
            self.assertEqual(req.training_record.approval_status, "approved")
            self.assertEqual(req.reviewed_by, self.rh)
            self.assertTrue(req.training_record.lines.filter(pk=self.line.pk).exists())
            self.assertEqual(MatrixEntry.objects.get(employee=self.employee, task=self.task).level, level)

    def test_refusal_without_plan_remains_allowed(self):
        for level in (TrainingLevel.SUPERVISED, TrainingLevel.TRAINER):
            req = self.make_request(level=level)
            self.review(req, decision="reject")
            req.refresh_from_db()
            self.assertFalse(req.approved)
            self.assertEqual(req.training_record.approval_status, "rejected")

    def test_level_i_without_plan_is_unaffected(self):
        req = self.make_request(level=TrainingLevel.IN_TRAINING)
        self.review(req)
        req.refresh_from_db()
        self.assertTrue(req.approved)
        self.assertEqual(req.training_record.approval_status, "approved")
        self.client.force_login(self.chef)
        self.assertEqual(self.client.post(self.upload_url(req), {"training_plan_image": plan_image()}).status_code, 404)

    def test_historical_approval_without_image_remains_approved(self):
        req = self.make_request(approved=True)
        before = TrainingApprovalRequest.objects.values().get(pk=req.pk)
        req.clean()
        self.review(req)
        self.assertEqual(TrainingApprovalRequest.objects.values().get(pk=req.pk), before)
        self.assertEqual(TrainingRecord.objects.get(pk=req.training_record_id).approval_status, "approved")

    def test_repair_of_pending_lu_record_still_needs_plan(self):
        req = self.make_request(approved=True)
        TrainingRecord.objects.filter(pk=req.training_record_id).update(approval_status="pending")
        self.review(req)
        req.refresh_from_db()
        self.assertTrue(req.approved)
        self.assertEqual(req.training_record.approval_status, "pending")

    def test_missing_stored_file_blocks_approval(self):
        req = self.make_request()
        req.training_plan_image = "training_plans/missing.png"
        req.save(update_fields=["training_plan_image"])
        self.review(req)
        req.refresh_from_db()
        self.assertIsNone(req.approved)
        self.assertEqual(self.client.get(reverse("training_plan_view", args=[req.pk])).status_code, 404)

    def test_plan_view_authorizes_owner_and_rh_only_and_prevents_caching(self):
        req = self.make_request()
        req.training_plan_image.save("plan.png", plan_image())
        for user in (self.chef, self.rh, self.group_rh):
            self.client.force_login(user)
            response = self.client.get(reverse("training_plan_view", args=[req.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "image/png")
            self.assertIn("no-store", response["Cache-Control"])
            self.assertEqual(response["X-Content-Type-Options"], "nosniff")
            self.assertTrue(b"".join(response.streaming_content))
            response.close()
        self.assertEqual(self.client.get("/media/" + req.training_plan_image.name).status_code, 404)

    def test_anonymous_users_are_redirected_and_csrf_is_enforced(self):
        req = self.make_request()
        for url in (reverse("training_requests_list"), self.upload_url(req), reverse("training_plan_view", args=[req.pk])):
            self.assertEqual(self.client.get(url).status_code, 302)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.chef)
        self.assertEqual(csrf_client.post(self.upload_url(req), {"training_plan_image": plan_image()}).status_code, 403)
        csrf_client.force_login(self.rh)
        self.assertEqual(csrf_client.post(reverse("review_training_request", args=[req.pk]), {"decision": "approve"}).status_code, 403)

    def test_chef_cannot_approve_and_get_cannot_approve(self):
        req = self.make_request()
        req.training_plan_image.save("plan.png", plan_image())
        url = reverse("review_training_request", args=[req.pk])
        self.client.force_login(self.chef)
        self.assertEqual(self.client.post(url, {"decision": "approve"}).status_code, 302)
        self.client.force_login(self.rh)
        self.assertEqual(self.client.get(url).status_code, 405)
        req.refresh_from_db()
        self.assertIsNone(req.approved)

    def test_rh_page_shows_missing_plan_and_disabled_approval(self):
        req = self.make_request()
        self.client.force_login(self.rh)
        response = self.client.get(reverse("rh_training_approvals"))
        self.assertContains(response, "En attente du plan de formation du Chef d’équipe.")
        self.assertContains(response, 'disabled style="opacity: .5; cursor: not-allowed;"')
        req.training_plan_image.save("plan.png", plan_image())
        response = self.client.get(reverse("rh_training_approvals"))
        self.assertContains(response, reverse("training_plan_view", args=[req.pk]))
        self.assertNotContains(response, 'disabled style="opacity: .5; cursor: not-allowed;"')

    def test_invalid_upload_preserves_existing_plan(self):
        req = self.make_request()
        req.training_plan_image.save("plan.png", plan_image())
        original = req.training_plan_image.name
        self.client.force_login(self.chef)
        response = self.client.post(self.upload_url(req), {
            "training_plan_image": SimpleUploadedFile("fake.png", b"not an image", "image/png"),
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        req.refresh_from_db()
        self.assertEqual(req.training_plan_image.name, original)
        response = self.client.post(self.upload_url(req), {})
        self.assertEqual(response.status_code, 200)
        self.assertIn("training_plan_image", response.context["form"].errors)

    def test_supported_formats_and_file_validation(self):
        for image_format in ("JPEG", "PNG", "WEBP"):
            with self.subTest(image_format=image_format):
                form = TrainingPlanImageForm(files={"training_plan_image": plan_image(image_format=image_format)})
                self.assertTrue(form.is_valid(), form.errors)
        form = TrainingPlanImageForm(files={"training_plan_image": plan_image(image_format="GIF")})
        self.assertFalse(form.is_valid())
        oversized = plan_image()
        oversized.size = 10 * 1024 * 1024 + 1
        form = TrainingPlanImageForm(files={"training_plan_image": oversized})
        self.assertFalse(form.is_valid())
        self.assertIn("10 Mo", str(form.errors))
        renamed = TrainingPlanImageForm(files={"training_plan_image": plan_image(name="misnamed.jpg")})
        self.assertTrue(renamed.is_valid())
        self.assertEqual(renamed.cleaned_data["training_plan_image"].name, "plan.png")

    def test_concurrent_decision_prevents_upload_and_removes_unused_file(self):
        req = self.make_request()
        self.client.force_login(self.chef)
        storage = req.training_plan_image.storage
        real_save = storage.save

        def decide_while_saving(*args, **kwargs):
            name = real_save(*args, **kwargs)
            TrainingApprovalRequest.objects.filter(pk=req.pk).update(approved=False)
            return name

        with patch.object(storage, "save", side_effect=decide_while_saving):
            self.client.post(self.upload_url(req), {"training_plan_image": plan_image()})
        req.refresh_from_db()
        self.assertFalse(req.approved)
        self.assertFalse(req.training_plan_image)
        self.assertFalse(any(p.is_file() for p in self.media_root.rglob("*")))

    def test_model_helpers_and_admin_cannot_approve_without_plan(self):
        req = self.make_request()
        with self.assertRaises(ValidationError):
            req.approve_request(self.rh)
        with self.assertRaises(ValidationError):
            req.training_record.approve(self.rh)
        req.approved = True
        with self.assertRaises(ValidationError):
            req.clean()
        req.refresh_from_db()
        self.assertIsNone(req.approved)
        admin_request = RequestFactory().get("/admin/")
        admin_request.user = self.rh
        self.assertIn("approved", admin.site._registry[TrainingApprovalRequest].get_readonly_fields(admin_request, req))
        self.assertIn("approval_status", admin.site._registry[TrainingRecord].get_readonly_fields(admin_request, req.training_record))
