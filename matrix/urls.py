from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import CustomLoginView, EmployeeViewSet, TaskViewSet, MatrixEntryViewSet, employees_to_requalify, get_employee_tasks, matrix_view, process_requalification, requalification_history, rh_assign_team_leader, rh_manage_users, rh_user_create, rh_user_delete, rh_user_update
from django.contrib.auth import views as auth_views
from . import views

router = DefaultRouter()
router.register(r'employees', EmployeeViewSet)
router.register(r'tasks', TaskViewSet)
router.register(r'entries', MatrixEntryViewSet)

urlpatterns = [
    path('', CustomLoginView.as_view(), name='login'),
    path("api/", include(router.urls)),
    path("matrix/", matrix_view, name="matrix_view"),
    # path('', auth_views.LoginView.as_view(template_name='matrix/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='login'), name='logout'),
    path("employees/", views.employee_list, name="employee_list"),
    path("employees/add/", views.employee_create, name="employee_create"),
    path("employees/<int:pk>/edit/", views.employee_update, name="employee_update"),
    path("employees/<int:pk>/delete/", views.employee_delete, name="employee_delete"),
    path("trainingrecords/", views.training_record_list, name="training_record_list"),
    path("trainingrecords/add/", views.training_record_create, name="training_record_create"),
    path("trainingrecords/<int:pk>/edit/", views.training_record_update, name="training_record_update"),
    path("trainingrecords/<int:pk>/delete/", views.training_record_delete, name="training_record_delete"),
    path("matrix/line/<int:line_id>/", views.matrix_by_line, name="matrix_by_line"),
    path("matrix/lines/", views.line_list, name="line_list"),
    path("employee/<int:employee_id>/history/", views.employee_history, name="employee_history"),
    path('rh/', views.employee_list_rh, name='employee_list_rh'),  
    path('rh/tasks-lines/', views.rh_manage_tasks_lines, name='rh_manage_tasks_lines'),
    path('rh/tasks-lines/', views.rh_manage_tasks_lines, name='rh_manage_tasks_lines'),
    path('rh/task/<int:pk>/update/', views.task_update, name='task_update'),
    path('rh/task/<int:pk>/delete/', views.task_delete, name='task_delete'),
    path('rh/line/<int:pk>/update/', views.line_update, name='line_update'),
    path('rh/line/<int:pk>/delete/', views.line_delete, name='line_delete'),
    path('lines/<int:line_id>/', views.line_detail, name='line_detail'),
    path("rh/training-approvals/", views.rh_training_approvals, name="rh_training_approvals"),
    path('rh/dashboard/', views.rh_dashboard, name='rh_dashboard'),
    path('rh/transfer/', views.transfer_employee_view, name='transfer_employee'),
    path('rh/transfer/<int:employee_id>/', views.transfer_employee_view, name='transfer_employee_specific'),
    path('ajax/get-tasks-by-employee/', views.get_tasks_by_employee, name='get_tasks_by_employee'),
    path('rh/employees/create/', views.rh_employee_create, name='rh_employee_create'),
    path('rh/employees/<int:pk>/edit/', views.rh_employee_update, name='rh_employee_update'),
    path('rh/employees/<int:pk>/delete/', views.rh_employee_delete, name='rh_employee_delete'),
    path('rh/export/excel/', views.export_dashboard_excel, name='export_dashboard_excel'),
    path('rh/export/pdf/', views.export_dashboard_pdf, name='export_dashboard_pdf'),
    path('requalification/', employees_to_requalify, name='employees_to_requalify'),
    path('requalification/process/', process_requalification, name='process_requalification'),
    path('requalification/history/', requalification_history, name='requalification_history'),
    path('api/employee-tasks/', get_employee_tasks, name='get_employee_tasks'),
    path('rh/requalifications/', views.rh_employees_to_requalify, name='rh_employees_to_requalify'),
    path('rh/requalification-approvals/', views.rh_requalification_approvals, name='rh_requalification_approvals'),
    path('matrice/export-excel/', views.export_matrice_excel, name='export_matrice_excel'),

    # Transfert chef d'équipe (avec approbation RH)
path('team-leader/transfer/', views.team_leader_transfer_view, name='team_leader_transfer'),

# Approbation RH des transferts
path('rh/transfer-approvals/', views.rh_transfer_approvals, name='rh_transfer_approvals'),
path('rh/operators/', views.rh_operators_overview, name='rh_operators_overview'),
path('rh/train-team-leader/', views.rh_train_team_leader, name='rh_train_team_leader'),
path('api/tasks-by-line-rh/', views.get_tasks_by_line_for_rh, name='get_tasks_by_line_for_rh'),

    # ✅ Gestion des utilisateurs
    path('rh/users/', rh_manage_users, name='rh_manage_users'),
    path('rh/users/create/', rh_user_create, name='rh_user_create'),
    path('rh/users/<int:user_id>/edit/', rh_user_update, name='rh_user_update'),
    path('rh/users/<int:user_id>/delete/', rh_user_delete, name='rh_user_delete'),
    path('rh/users/assign/', rh_assign_team_leader, name='rh_assign_team_leader'),

path('change-password/', views.change_password_team_leader, name='change_password_team_leader'),


# ═══════════════════════════════════════════════════════════
    # ESPACE QUALITÉ
    # ═══════════════════════════════════════════════════════════


    path('quality/',
         views.quality_dashboard,
         name='quality_dashboard'),

    path('quality/operators/',
         views.quality_operators,
         name='quality_operators'),

    path('quality/operators/<int:employee_id>/',
         views.quality_operator_detail,
         name='quality_operator_detail'),

    path('quality/team-leaders/',
         views.quality_team_leaders,
         name='quality_team_leaders'),



    path('quality/lines/<int:line_id>/',
     views.quality_line_detail,
     name='quality_line_detail'),

         # ── API ───────────────────────────────────────────────────────────
    path(
        'api/employee/by-matricule/',
        views.get_employee_by_matricule,
        name='get_employee_by_matricule'
    ),

        path(
        'rh/employees/register/',
        views.rh_employee_register,
        name='rh_employee_register'
    ),

    path(
    'requalification/team-leaders/',
    views.rh_requalify_team_leader,
    name='rh_requalify_team_leader'
    ),
path(
    'rh/employees/register/',
    views.rh_employee_register,
    name='rh_employee_register',
),
path(
    'rh/employees/template/',
    views.download_excel_template,
    name='download_excel_template',
),
    
]
