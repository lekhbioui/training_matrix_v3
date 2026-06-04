from django.shortcuts import render
from .models import Employee, Task, MatrixEntry

def matrix_view(request):
    employees = Employee.objects.all()
    tasks = Task.objects.order_by("order")
    entry_map = {(e.task_id, e.employee_id): e for e in MatrixEntry.objects.all()}

    matrix = []
    for t in tasks:
        row = {"task": t, "cells": []}
        for emp in employees:
            row["cells"].append(entry_map.get((t.id, emp.id)))
        matrix.append(row)

    return render(request, "matrix/matrix.html", {"employees": employees, "matrix": matrix})
