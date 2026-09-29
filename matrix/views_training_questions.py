from django.contrib import messages
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .decorators import rh_required
from .forms_training_questions import TrainingQuestionCreateForm, TrainingQuestionForm
from .models import ProductionLine, Task, TrainingQuestion


def _persist_question_form(request, form, *, editing):
    if editing:
        form.save()
        messages.success(request, "La question a été modifiée avec succès.")
        return True
    try:
        questions = form.save_questions(request.user)
    except ValidationError as exc:
        form.add_error(None, exc)
        return False
    except DatabaseError:
        form.add_error(None, "Aucune question n’a été ajoutée. Veuillez réessayer.")
        return False
    messages.success(request, "La question a été ajoutée avec succès." if len(questions) == 1
                     else f"{len(questions)} questions ont été ajoutées avec succès.")
    return True


@rh_required
def training_question_list(request):
    # ── Vérifier si on modifie une question existante ──
    edit_pk = request.GET.get("edit") or request.POST.get("edit_pk")
    editing_question = None
    show_modal = False

    if edit_pk:
        try:
            editing_question = TrainingQuestion.objects.get(pk=int(edit_pk))
        except (TrainingQuestion.DoesNotExist, ValueError, TypeError):
            pass

    # ── Traitement POST ──
    form_class = TrainingQuestionForm if editing_question else TrainingQuestionCreateForm
    form_kwargs = {"instance": editing_question} if editing_question else {}
    if request.method == "POST":
        form = form_class(request.POST, **form_kwargs)
        if form.is_valid() and _persist_question_form(request, form, editing=bool(editing_question)):
            return redirect("training_question_list")
        else:
            show_modal = True
    else:
        form = form_class(**form_kwargs)
        if edit_pk and editing_question:
            show_modal = True

    # ── Queryset + filtres ──
    questions = TrainingQuestion.objects.select_related("line", "task")

    search = request.GET.get("q", "").strip()
    if search:
        questions = questions.filter(
            Q(text__icontains=search)
            | Q(task__title__icontains=search)
            | Q(task__code__icontains=search)
            | Q(line__name__icontains=search)
            | Q(line__code__icontains=search)
        )

    exact_filters = {
        "level": "level",
        "line": "line_id",
        "task": "task_id",
        "answer_type": "answer_type",
    }
    for parameter, field in exact_filters.items():
        value = request.GET.get(parameter, "").strip()
        if value:
            questions = questions.filter(**{field: value})

    for parameter in ("is_active", "is_required", "is_blocking"):
        value = request.GET.get(parameter, "").strip()
        if value in {"true", "false"}:
            questions = questions.filter(**{parameter: value == "true"})

    questions = questions.order_by("line__code", "task__order", "order", "id")
    page = Paginator(questions, 20).get_page(request.GET.get("page"))

    # ── Stats ──
    all_qs = TrainingQuestion.objects.all()
    active_count = all_qs.filter(is_active=True).count()
    required_count = all_qs.filter(is_required=True).count()
    blocking_count = all_qs.filter(is_blocking=True).count()

    return render(request, "matrix/rh/training_questions/list.html", {
        "page_obj": page,
        "lines": ProductionLine.objects.order_by("code", "name"),
        "tasks": Task.objects.order_by("code", "title"),
        "level_choices": TrainingQuestion._meta.get_field("level").choices,
        "answer_type_choices": TrainingQuestion.AnswerType.choices,
        "filters": request.GET,
        "form": form,
        "show_modal": show_modal,
        "editing_question": editing_question,
        "active_count": active_count,
        "required_count": required_count,
        "blocking_count": blocking_count,
    })


def _save_question(request, instance=None):
    form = (TrainingQuestionForm(request.POST or None, instance=instance) if instance
            else TrainingQuestionCreateForm(request.POST if request.method == "POST" else None))
    if request.method == "POST" and form.is_valid() and _persist_question_form(request, form, editing=bool(instance)):
        return redirect("training_question_list")
    return render(request, "matrix/rh/training_questions/form.html", {
        "form": form,
        "question": instance,
    })


@rh_required
def training_question_create(request):
    return _save_question(request)


@rh_required
def training_question_update(request, pk):
    return _save_question(request, get_object_or_404(TrainingQuestion, pk=pk))


@rh_required
@require_POST
def training_question_toggle_active(request, pk):
    question = get_object_or_404(TrainingQuestion, pk=pk)
    question.is_active = not question.is_active
    question.save(update_fields=["is_active", "updated_at"])
    messages.success(request, f"La question a été {'activée' if question.is_active else 'désactivée'}.")
    return redirect("training_question_list")


@rh_required
def training_question_delete(request, pk):
    question = get_object_or_404(TrainingQuestion, pk=pk)
    if request.method == "POST":
        if question.answers.exists():
            question.is_active = False
            question.save(update_fields=["is_active", "updated_at"])
            messages.warning(
                request,
                "La question possède déjà des réponses et a donc été désactivée au lieu d'être supprimée.",
            )
        else:
            question.delete()
            messages.success(request, "La question a été supprimée avec succès.")
        return redirect("training_question_list")
    return render(request, "matrix/rh/training_questions/confirm_delete.html", {"question": question})


@rh_required
def training_question_tasks_by_line(request):
    line_id = request.GET.get("line_id")
    tasks = Task.objects.none()
    if not line_id and request.GET.get("all_tasks") == "1":
        tasks = Task.objects.order_by("order", "code")
    elif line_id:
        try:
            tasks = Task.objects.filter(lines__pk=int(line_id)).distinct().order_by("order", "code")
        except (TypeError, ValueError):
            pass
    return JsonResponse({"tasks": list(tasks.values("id", "code", "title"))})
