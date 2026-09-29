from django.db import transaction

from .models import (
    TrainingQuestion,
    TrainingQuestionnaire,
    TrainingQuestionAnswer,
)


@transaction.atomic
def save_training_questionnaire(
    *,
    training_record,
    form,
    answered_by,
    line,
    general_comment="",
):
    questionnaire, created = (
        TrainingQuestionnaire.objects.get_or_create(
            training_record=training_record,
            defaults={
                "line": line,
                "answered_by": answered_by,
                "general_comment": general_comment,
                "is_compliant": True,
            },
        )
    )

    if not created:
        questionnaire.line = line
        questionnaire.answered_by = answered_by
        questionnaire.general_comment = general_comment

        # Pour une modification du niveau I,
        # remplacer les anciennes réponses.
        questionnaire.answers.all().delete()

    questionnaire_is_compliant = True
    answers_to_create = []

    for item in form.training_questions:
        question = item["question"]
        answer_field = item["answer_field"]
        comment_field = item["comment_field"]

        answer_value = form.cleaned_data.get(answer_field)
        comment_value = (
            form.cleaned_data
            .get(comment_field, "")
            .strip()
        )

        answer_is_compliant = True

        if (
            question.answer_type
            == TrainingQuestion.AnswerType.YES_NO
            and question.expected_boolean is not None
        ):
            answer_is_compliant = (
                answer_value == question.expected_boolean
            )

        if question.is_blocking and not answer_is_compliant:
            questionnaire_is_compliant = False

        answers_to_create.append(
            TrainingQuestionAnswer(
                questionnaire=questionnaire,
                question=question,
                question_text_snapshot=question.text,
                answer_type_snapshot=question.answer_type,
                boolean_answer=(
                    answer_value
                    if question.answer_type
                    == TrainingQuestion.AnswerType.YES_NO
                    else None
                ),
                text_answer=(
                    answer_value
                    if question.answer_type
                    == TrainingQuestion.AnswerType.TEXT
                    else ""
                ),
                comment=comment_value,
                is_compliant=answer_is_compliant,
            )
        )

    questionnaire.is_compliant = questionnaire_is_compliant
    questionnaire.save()

    TrainingQuestionAnswer.objects.bulk_create(
        answers_to_create
    )

    return questionnaire