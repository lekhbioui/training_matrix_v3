from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import Employee, ProductionLine, Task, TrainingApprovalRequest, TrainingLevel, TrainingRecord
from .test_training_plans import plan_image


class TrainingPlanHistoryTests(TestCase):
    def setUp(self):
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        self.media_root = Path(media.name)
        settings = override_settings(MEDIA_ROOT=media.name)
        settings.enable()
        self.addCleanup(settings.disable)
        users = get_user_model()
        self.chef = users.objects.create_user(username="history-chef")
        self.other = users.objects.create_user(username="history-other")
        self.rh = users.objects.create_superuser(username="history-admin", email="rh@example.com", password=None)
        self.group_rh = users.objects.create_user(username="history-rh")
        self.group_rh.groups.add(Group.objects.get_or_create(name="RH")[0])
        self.line = ProductionLine.objects.create(code="HISTORY", name="Ligne historique")
        self.line.team_leaders.add(self.chef)
        self.employee = Employee.objects.create(
            matricule="HISTORY-001", first_name="History", last_name="Operator",
            manager=self.chef, line=self.line,
        )
        self.task = Task.objects.create(code="HISTORY", title="Formation historique", task_number=1)
        self.task.lines.add(self.line)
        self.history_url = reverse("employee_history", args=[self.employee.pk])

    def make_training(self, level=TrainingLevel.SUPERVISED, approved=True, linked=True, with_plan=True):
        record = TrainingRecord.objects.create(
            employee=self.employee, task=self.task, level=level, date=timezone.localdate(),
            approval_status={True: "approved", False: "rejected", None: "pending"}[approved],
        )
        record.lines.set([self.line])
        req = None
        if linked:
            req = TrainingApprovalRequest.objects.create(
                employee=self.employee, task=self.task, requested_level=level,
                requested_by=self.chef, training_record=record, approved=approved, line=self.line,
                training_date=record.date,
            )
            if with_plan:
                req.training_plan_image.save("plan.png", plan_image())
        return record, req

    def history(self, user=None, **filters):
        self.client.force_login(user or self.rh)
        return self.client.get(self.history_url, filters)

    def test_approved_l_and_u_link_to_the_original_plan_for_rh(self):
        for level in (TrainingLevel.SUPERVISED, TrainingLevel.TRAINER):
            _, req = self.make_training(level=level)
            url = reverse("training_plan_view", args=[req.pk])
            for user in (self.rh, self.group_rh):
                response = self.history(user)
                self.assertContains(response, url)
                self.assertContains(response, "Voir le plan")
                self.assertNotContains(response, reverse("training_plan_upload", args=[req.pk]))
                document = self.client.get(url)
                self.assertEqual(document.status_code, 200)
                self.assertEqual(document["Content-Type"], "image/png")
                document.close()

    def test_level_i_never_shows_a_plan_button(self):
        self.make_training(level=TrainingLevel.IN_TRAINING)
        response = self.history()
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Voir le plan")
        self.assertEqual(response.context["formations"][0]["training_plan_label"], "—")

    def test_historical_lu_without_request_or_image_remains_visible(self):
        records = []
        for level in (TrainingLevel.SUPERVISED, TrainingLevel.TRAINER):
            for linked in (False, True):
                record, _ = self.make_training(level=level, linked=linked, with_plan=False)
                records.append(record)
        response = self.history()
        self.assertEqual(len(response.context["formations"]), 4)
        self.assertContains(response, "Plan indisponible", count=4)
        self.assertNotContains(response, "Voir le plan")
        self.assertEqual(TrainingRecord.objects.filter(pk__in=[r.pk for r in records], approval_status="approved").count(), 4)

    def test_pending_and_rejected_records_keep_current_history_without_archive_link(self):
        for status in (None, False):
            self.make_training(approved=status)
        response = self.history()
        self.assertEqual(len(response.context["formations"]), 2)
        self.assertNotContains(response, "Voir le plan")
        self.assertTrue(all(item["training_plan_label"] == "—" for item in response.context["formations"]))

    def test_only_authorized_chef_gets_the_link_and_direct_access(self):
        _, req = self.make_training()
        url = reverse("training_plan_view", args=[req.pk])
        self.assertContains(self.history(self.chef), url)
        document = self.client.get(url)
        self.assertEqual(document.status_code, 200)
        document.close()
        response = self.history(self.other)
        self.assertNotContains(response, url)
        self.assertContains(response, "Accès réservé")
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(self.client.get(self.history_url).status_code, 302)

    def test_missing_file_is_unavailable_in_history_and_returns_clean_404(self):
        _, req = self.make_training()
        req.training_plan_image.storage.delete(req.training_plan_image.name)
        response = self.history()
        self.assertContains(response, "Plan indisponible")
        self.assertNotContains(response, "Voir le plan")
        self.assertEqual(self.client.get(reverse("training_plan_view", args=[req.pk])).status_code, 404)

    def test_approval_then_history_reuses_file_without_data_changes_or_duplication(self):
        record, req = self.make_training(approved=None, with_plan=False)
        self.client.force_login(self.chef)
        self.client.post(reverse("training_plan_upload", args=[req.pk]), {"training_plan_image": plan_image()})
        req.refresh_from_db()
        original_name = req.training_plan_image.name
        original_bytes = Path(req.training_plan_image.path).read_bytes()
        self.client.force_login(self.rh)
        self.client.post(reverse("review_training_request", args=[req.pk]), {"decision": "approve"})
        before = TrainingApprovalRequest.objects.values().get(pk=req.pk)
        record_before = TrainingRecord.objects.values().get(pk=record.pk)
        response = self.client.get(self.history_url)
        url = reverse("training_plan_view", args=[req.pk])
        self.assertContains(response, url)
        document = self.client.get(url)
        self.assertEqual(b"".join(document.streaming_content), original_bytes)
        document.close()
        self.assertEqual(TrainingApprovalRequest.objects.values().get(pk=req.pk), before)
        self.assertEqual(TrainingRecord.objects.values().get(pk=record.pk), record_before)
        req.refresh_from_db()
        self.assertEqual(req.training_plan_image.name, original_name)
        self.assertEqual(len([p for p in self.media_root.rglob("*") if p.is_file()]), 1)
        self.assertEqual(TrainingRecord.objects.count(), 1)
        self.assertEqual(TrainingApprovalRequest.objects.count(), 1)
        self.assertNotIn("training_plan_image", {field.name for field in TrainingRecord._meta.fields})

    def test_history_filters_statistics_and_transfers_are_preserved(self):
        self.make_training(level=TrainingLevel.SUPERVISED)
        self.make_training(level=TrainingLevel.TRAINER)
        response = self.history(level=TrainingLevel.SUPERVISED, line=self.line.pk)
        self.assertEqual(len(response.context["formations"]), 1)
        self.assertEqual(response.context["stats"]["total_formations"], 1)
        self.assertEqual(len(response.context["transfers"]), 1)
        self.assertContains(response, "Transfert de Ligne")

    def test_history_query_count_does_not_grow_with_number_of_plans(self):
        self.make_training()
        self.client.force_login(self.rh)
        with CaptureQueriesContext(connection) as one:
            response = self.client.get(self.history_url)
        self.assertEqual(response.status_code, 200)
        for _ in range(7):
            self.make_training()
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(self.history_url)
        self.assertEqual(len(response.context["formations"]), 8)
        self.assertEqual(len(one), len(many))
        # Also cover old records that rely on their task's production lines.
        TrainingRecord.lines.through.objects.all().delete()
        with CaptureQueriesContext(connection) as fallback:
            response = self.client.get(self.history_url)
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(fallback), len(many))
