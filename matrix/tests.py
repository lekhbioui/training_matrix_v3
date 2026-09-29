from datetime import timedelta
from io import BytesIO
from tempfile import TemporaryDirectory

from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import TrainingRecordForm
from .models import (
    Employee,
    ProductionLine,
    Task,
    TrainingApprovalRequest,
    TrainingLevel,
    TrainingRecord,
)


class ReviewTrainingRequestTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.rh_user = user_model.objects.create_superuser(
            username="rh-test",
            email="rh@example.com",
            password="test-password",
        )
        self.team_leader = user_model.objects.create_user(
            username="leader-test",
            password="test-password",
        )
        self.line = ProductionLine.objects.create(
            code="TEST-LINE",
            name="Test line",
        )
        self.line.team_leaders.add(self.team_leader)
        self.employee = Employee.objects.create(
            first_name="Test",
            last_name="Operator",
            matricule="TEST-001",
            line=self.line,
            manager=self.team_leader,
        )
        self.employee.visible_lines.add(self.line)
        self.task = Task.objects.create(
            code="TEST-TASK",
            task_number=1,
            title="Test task",
        )
        self.task.lines.add(self.line)
        self.training_record = TrainingRecord.objects.create(
            employee=self.employee,
            task=self.task,
            level=TrainingLevel.IN_TRAINING,
            date=timezone.localdate(),
            approval_status=TrainingRecord.ApprovalStatus.PENDING,
        )
        self.training_record.lines.add(self.line)

    def create_request(self):
        return TrainingApprovalRequest.objects.create(
            employee=self.employee,
            task=self.task,
            requested_level=TrainingLevel.IN_TRAINING,
            requested_by=self.team_leader,
            training_date=timezone.localdate(),
            line=self.line,
            training_record=self.training_record,
            approved=None,
        )

    def test_approve_level_i_updates_training_record_status(self):
        approval_request = self.create_request()
        record_count_before = TrainingRecord.objects.count()
        self.client.force_login(self.rh_user)

        response = self.client.post(
            reverse(
                "review_training_request",
                args=[approval_request.id],
            ),
            {"decision": "approve"},
        )

        approval_request.refresh_from_db()
        self.training_record.refresh_from_db()

        self.assertRedirects(
            response,
            reverse("rh_training_approvals"),
            fetch_redirect_response=False,
        )
        self.assertTrue(approval_request.approved)
        self.assertEqual(
            approval_request.training_record_id,
            self.training_record.id,
        )
        self.assertEqual(
            self.training_record.approval_status,
            TrainingRecord.ApprovalStatus.APPROVED,
        )
        self.assertTrue(
            self.training_record.lines.filter(
                pk=self.line.pk,
            ).exists()
        )
        self.assertEqual(
            TrainingRecord.objects.count(),
            record_count_before,
        )

    def test_approved_level_i_is_returned_by_matrix_filter(self):
        approval_request = self.create_request()
        self.client.force_login(self.rh_user)

        self.client.post(
            reverse(
                "review_training_request",
                args=[approval_request.id],
            ),
            {"decision": "approve"},
        )

        records_visible_in_matrix = TrainingRecord.objects.filter(
            employee_id__in=[self.employee.id],
            task_id__in=[self.task.id],
            lines=self.line,
            approval_status=TrainingRecord.ApprovalStatus.APPROVED,
        )

        self.assertTrue(
            records_visible_in_matrix.filter(
                pk=self.training_record.pk,
            ).exists()
        )

    def test_legacy_rh_post_uses_synchronized_review_flow(self):
        approval_request = self.create_request()
        record_count_before = TrainingRecord.objects.count()
        self.client.force_login(self.rh_user)

        response = self.client.post(
            reverse("rh_training_approvals"),
            {
                "request_id": approval_request.id,
                "action": "approve",
            },
        )

        approval_request.refresh_from_db()
        self.training_record.refresh_from_db()

        self.assertRedirects(
            response,
            reverse("rh_training_approvals"),
            fetch_redirect_response=False,
        )
        self.assertTrue(approval_request.approved)
        self.assertEqual(
            approval_request.reviewed_by_id,
            self.rh_user.id,
        )
        self.assertIsNotNone(approval_request.reviewed_at)
        self.assertEqual(
            self.training_record.approval_status,
            TrainingRecord.ApprovalStatus.APPROVED,
        )
        self.assertTrue(
            self.training_record.lines.filter(pk=self.line.pk).exists()
        )
        self.assertEqual(
            TrainingRecord.objects.count(),
            record_count_before,
        )

    def test_reject_level_i_rejects_existing_training_record(self):
        approval_request = self.create_request()
        self.client.force_login(self.rh_user)

        response = self.client.post(
            reverse(
                "review_training_request",
                args=[approval_request.id],
            ),
            {
                "decision": "reject",
                "rejection_reason": "Questionnaire insuffisant",
            },
        )

        approval_request.refresh_from_db()
        self.training_record.refresh_from_db()

        self.assertRedirects(
            response,
            reverse("rh_training_approvals"),
            fetch_redirect_response=False,
        )
        self.assertFalse(approval_request.approved)
        self.assertEqual(
            self.training_record.approval_status,
            TrainingRecord.ApprovalStatus.REJECTED,
        )
        self.assertFalse(
            TrainingRecord.objects.filter(
                pk=self.training_record.pk,
                lines=self.line,
                approval_status=TrainingRecord.ApprovalStatus.APPROVED,
            ).exists()
        )

    def test_approve_levels_l_and_u_keeps_existing_creation_flow(self):
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        media_settings = override_settings(MEDIA_ROOT=media.name)
        media_settings.enable()
        self.addCleanup(media_settings.disable)
        self.client.force_login(self.rh_user)

        for index, level in enumerate(
            (
                TrainingLevel.SUPERVISED,
                TrainingLevel.TRAINER,
            ),
            start=2,
        ):
            with self.subTest(level=level):
                task = Task.objects.create(
                    code=f"TEST-TASK-{index}",
                    task_number=index,
                    title=f"Test task {index}",
                )
                task.lines.add(self.line)
                approval_request = (
                    TrainingApprovalRequest.objects.create(
                        employee=self.employee,
                        task=task,
                        requested_level=level,
                        requested_by=self.team_leader,
                        training_date=timezone.localdate(),
                        line=self.line,
                        approved=None,
                    )
                )

                image_data = BytesIO()
                Image.new("RGB", (2, 2)).save(image_data, format="PNG")
                approval_request.training_plan_image.save(
                    "plan.png", SimpleUploadedFile("plan.png", image_data.getvalue(), "image/png"),
                )

                self.client.post(
                    reverse(
                        "review_training_request",
                        args=[approval_request.id],
                    ),
                    {"decision": "approve"},
                )

                approval_request.refresh_from_db()
                record = approval_request.training_record

                self.assertTrue(approval_request.approved)
                self.assertIsNotNone(record)
                self.assertEqual(record.level, level)
                self.assertEqual(
                    record.approval_status,
                    TrainingRecord.ApprovalStatus.APPROVED,
                )
                self.assertTrue(
                    record.lines.filter(pk=self.line.pk).exists()
                )


class TrainingRecordFormLevelLTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_superuser(
            username="form-rh-test",
            email="form-rh@example.com",
            password="test-password",
        )
        self.line = ProductionLine.objects.create(
            code="FORM-LINE",
            name="Form line",
        )
        self.employee = Employee.objects.create(
            first_name="Form",
            last_name="Operator",
            matricule="FORM-001",
            line=self.line,
            manager=self.user,
        )
        self.employee.visible_lines.add(self.line)
        self.task = Task.objects.create(
            code="FORM-TASK",
            task_number=10,
            title="Form task",
        )
        self.task.lines.add(self.line)
        self.record_i = TrainingRecord.objects.create(
            employee=self.employee,
            task=self.task,
            level=TrainingLevel.IN_TRAINING,
            date=timezone.localdate() - timedelta(days=8),
            approval_status=TrainingRecord.ApprovalStatus.PENDING,
        )
        self.record_i.lines.add(self.line)

    def make_level_l_form(self):
        return TrainingRecordForm(
            data={
                "employee": self.employee.pk,
                "task": self.task.pk,
                "level": TrainingLevel.SUPERVISED,
                "date": timezone.localdate().isoformat(),
                "trainer": "",
                "line": self.line.pk,
            },
            user=self.user,
            selected_line_id=self.line.pk,
        )

    def test_level_l_is_blocked_while_level_i_is_pending(self):
        form = self.make_level_l_form()

        self.assertFalse(form.is_valid())
        self.assertIn(
            "encore en attente d'approbation RH",
            " ".join(
                error.message
                for error in form.non_field_errors().as_data()
            ),
        )
        self.assertFalse(
            TrainingApprovalRequest.objects.filter(
                employee=self.employee,
                task=self.task,
                requested_level=TrainingLevel.SUPERVISED,
            ).exists()
        )

    def test_level_l_is_blocked_when_level_i_is_rejected(self):
        self.record_i.approval_status = (
            TrainingRecord.ApprovalStatus.REJECTED
        )
        self.record_i.save(update_fields=["approval_status"])
        form = self.make_level_l_form()

        self.assertFalse(form.is_valid())
        self.assertIn(
            "niveau I correspondant a été refusé",
            form.non_field_errors().as_text(),
        )
        self.assertFalse(
            TrainingApprovalRequest.objects.filter(
                employee=self.employee,
                task=self.task,
                requested_level=TrainingLevel.SUPERVISED,
            ).exists()
        )

    def test_level_l_request_is_allowed_when_level_i_is_approved(self):
        self.record_i.approval_status = (
            TrainingRecord.ApprovalStatus.APPROVED
        )
        self.record_i.save(update_fields=["approval_status"])
        form = self.make_level_l_form()

        self.assertFalse(form.is_valid())
        self.assertTrue(form.rh_request_created)
        self.assertTrue(
            TrainingApprovalRequest.objects.filter(
                employee=self.employee,
                task=self.task,
                requested_level=TrainingLevel.SUPERVISED,
                approved__isnull=True,
                line=self.line,
            ).exists()
        )
