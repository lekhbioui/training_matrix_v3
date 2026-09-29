# admin.py

from django.utils import timezone
from django.contrib.messages import constants as message_constants
from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html
from django.urls import reverse
from django.db.models import Q
from import_export.admin import ImportExportModelAdmin

from .models import (
    Employee, LineTransfer, LineTransferApprovalRequest, PendingEmployee, ProductionLine, Task,
    MatrixEntry, TrainingApprovalRequest, TrainingRecord,
    RequalificationRecord, RequalificationApprovalRequest,
    QualityProfile, TrainingLevel
)
from .resources import TrainingRecordResource


# ═══════════════════════════════════════════════════════════════════════════════
# FILTRES PERSONNALISÉS
# ═══════════════════════════════════════════════════════════════════════════════

class YearFilter(admin.SimpleListFilter):
    title = 'Année'
    parameter_name = 'year'

    def lookups(self, request, model_admin):
        years = TrainingRecord.objects.dates('date', 'year', order='DESC')
        return [(str(d.year), str(d.year)) for d in years]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(date__year=self.value())
        return queryset


class MonthFilter(admin.SimpleListFilter):
    title = 'Mois'
    parameter_name = 'month'

    def lookups(self, request, model_admin):
        return [
            ('1','Janvier'),('2','Février'),('3','Mars'),
            ('4','Avril'),('5','Mai'),('6','Juin'),
            ('7','Juillet'),('8','Août'),('9','Septembre'),
            ('10','Octobre'),('11','Novembre'),('12','Décembre'),
        ]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(date__month=self.value())
        return queryset


class DayFilter(admin.SimpleListFilter):
    title = 'Jour'
    parameter_name = 'day'

    def lookups(self, request, model_admin):
        return [(str(i), str(i)) for i in range(1, 32)]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(date__day=self.value())
        return queryset


class WeekDayFilter(admin.SimpleListFilter):
    title = 'Jour de la semaine'
    parameter_name = 'weekday'

    def lookups(self, request, model_admin):
        return [
            ('2','Lundi'),('3','Mardi'),('4','Mercredi'),
            ('5','Jeudi'),('6','Vendredi'),('7','Samedi'),('1','Dimanche'),
        ]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(date__week_day=self.value())
        return queryset


class DateRangeFilter(admin.SimpleListFilter):
    title = 'Période'
    parameter_name = 'period'

    def lookups(self, request, model_admin):
        return [
            ('today',      "Aujourd'hui"),
            ('yesterday',  'Hier'),
            ('this_week',  'Cette semaine'),
            ('last_week',  'Semaine dernière'),
            ('this_month', 'Ce mois'),
            ('last_month', 'Mois dernier'),
            ('this_year',  'Cette année'),
            ('last_year',  'Année dernière'),
        ]

    def queryset(self, request, queryset):
        from datetime import timedelta
        today = timezone.now().date()

        mapping = {
            'today':      lambda: queryset.filter(date=today),
            'yesterday':  lambda: queryset.filter(date=today - timedelta(days=1)),
            'this_week':  lambda: queryset.filter(
                              date__gte=today - timedelta(days=today.weekday()),
                              date__lte=today),
            'last_week':  lambda: queryset.filter(
                              date__gte=today - timedelta(days=today.weekday() + 7),
                              date__lte=today - timedelta(days=today.weekday() + 1)),
            'this_month': lambda: queryset.filter(
                              date__year=today.year, date__month=today.month),
            'last_month': lambda: queryset.filter(
                              date__year=today.year if today.month > 1 else today.year - 1,
                              date__month=today.month - 1 if today.month > 1 else 12),
            'this_year':  lambda: queryset.filter(date__year=today.year),
            'last_year':  lambda: queryset.filter(date__year=today.year - 1),
        }
        fn = mapping.get(self.value())
        return fn() if fn else queryset


# ═══════════════════════════════════════════════════════════════════════════════
# FILTRES POUR EMPLOYEE
# ═══════════════════════════════════════════════════════════════════════════════

class HasUserAccountFilter(admin.SimpleListFilter):
    title = 'Compte utilisateur'
    parameter_name = 'has_user'

    def lookups(self, request, model_admin):
        return (
            ('yes', "✅ Avec compte (Chef d'équipe)"),
            ('no',  "❌ Sans compte (Opérateur)"),
        )

    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.filter(user__isnull=False)
        if self.value() == 'no':
            return queryset.filter(user__isnull=True)
        return queryset


class IsTeamLeaderFilter(admin.SimpleListFilter):
    title = 'Rôle'
    parameter_name = 'role'

    def lookups(self, request, model_admin):
        return (
            ('team_leader', "👑 Chefs d'équipe"),
            ('operator',    "👷 Opérateurs"),
        )

    def queryset(self, request, queryset):
        if self.value() == 'team_leader':
            return queryset.filter(
                user__isnull=False,
                user__managed_lines__isnull=False
            ).distinct()
        if self.value() == 'operator':
            return queryset.filter(
                Q(user__isnull=True) |
                Q(user__managed_lines__isnull=True)
            ).distinct()
        return queryset


# ✅ NOUVEAU : Filtre par ligne de visibilité
class VisibleLineFilter(admin.SimpleListFilter):
    """
    Filtre les employés selon les lignes
    dans lesquelles ils sont visibles (M2M visible_lines).
    """
    title = 'Visible dans la matrice de'
    parameter_name = 'visible_line'

    def lookups(self, request, model_admin):
        lines = ProductionLine.objects.all().order_by('code')
        return [(str(l.pk), f"{l.code} — {l.name}") for l in lines]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(
                visible_lines__id=self.value()
            ).distinct()
        return queryset


# ✅ NOUVEAU : Filtre opérateurs multi-lignes
class MultiLineFilter(admin.SimpleListFilter):
    """Distingue les opérateurs mono-ligne des multi-lignes."""
    title = 'Présence multi-lignes'
    parameter_name = 'multi_line'

    def lookups(self, request, model_admin):
        return (
            ('yes', '🔀 Multi-lignes (≥ 2 matrices)'),
            ('no',  '➡️ Mono-ligne (1 matrice)'),
            ('none','⚠️ Aucune ligne de visibilité'),
        )

    def queryset(self, request, queryset):
        from django.db.models import Count
        if self.value() == 'yes':
            return queryset.annotate(
                vl_count=Count('visible_lines')
            ).filter(vl_count__gte=2)
        if self.value() == 'no':
            return queryset.annotate(
                vl_count=Count('visible_lines')
            ).filter(vl_count=1)
        if self.value() == 'none':
            return queryset.annotate(
                vl_count=Count('visible_lines')
            ).filter(vl_count=0)
        return queryset


# ═══════════════════════════════════════════════════════════════════════════════
# ✅ INLINE : Lignes de visibilité (lecture seule depuis Employee)
# ═══════════════════════════════════════════════════════════════════════════════

class VisibleLinesInline(admin.TabularInline):
    """
    Affiche en lecture seule les lignes de visibilité
    directement dans la fiche Employee.
    """
    model = Employee.visible_lines.through   # Table M2M intermédiaire
    verbose_name        = "Ligne de visibilité"
    verbose_name_plural = "Lignes de visibilité (matrices)"
    extra = 0

    # On laisse la gestion via filter_horizontal dans le form principal
    can_delete = True

    def has_add_permission(self, request, obj=None):
        return False   # Géré via le fieldset principal


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN EMPLOYEE
# ═══════════════════════════════════════════════════════════════════════════════

@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):

    # ── Colonnes de la liste ────────────────────────────────────────────
    list_display = (
        "matricule",
        "full_name_display",
        "line",
        "visible_lines_display",   # ✅ NOUVEAU
        "manager",
        "user_status",
        "is_team_leader_display",
        "managed_lines_display",
    )

    # ── Filtres latéraux ────────────────────────────────────────────────
    list_filter = (
        HasUserAccountFilter,
        IsTeamLeaderFilter,
        VisibleLineFilter,          # ✅ NOUVEAU
        MultiLineFilter,            # ✅ NOUVEAU
        "line",
        "manager",
    )

    # ── Recherche ───────────────────────────────────────────────────────
    search_fields = (
        "first_name",
        "last_name",
        "matricule",
        "user__username",
        "user__email",
        "visible_lines__name",      # ✅ NOUVEAU
        "visible_lines__code",      # ✅ NOUVEAU
    )

    list_per_page = 50

    # ── Édition en liste (colonnes simples uniquement) ──────────────────
    list_editable = ("line", "manager")

    # ── Fieldsets du formulaire de modification ─────────────────────────
    fieldsets = (
        ("👤 Informations personnelles", {
            "fields": ("first_name", "last_name", "matricule", "hire_date"),
        }),
        ("🏭 Affectation principale", {
            "fields": ("line", "manager"),
            "description": (
                "<div style='background:#eff6ff;padding:10px;"
                "border-radius:5px;margin-bottom:10px;border-left:4px solid #3b82f6;'>"
                "ℹ️ La <strong>ligne principale</strong> détermine l'affectation "
                "habituelle de l'opérateur et son chef d'équipe responsable."
                "</div>"
            ),
        }),
        # ✅ NOUVEAU FIELDSET
        ("📊 Visibilité dans les matrices", {
            "fields": ("visible_lines",),
            "description": (
                "<div style='background:#f0fdf4;padding:10px;"
                "border-radius:5px;margin-bottom:10px;border-left:4px solid #22c55e;'>"
                "✅ Sélectionnez <strong>toutes les lignes</strong> dans lesquelles "
                "cet opérateur doit <strong>apparaître dans la matrice</strong>.<br>"
                "La ligne principale est automatiquement incluse.<br>"
                "Un opérateur peut être visible dans plusieurs matrices simultanément."
                "</div>"
            ),
        }),
        ("🔐 Compte utilisateur", {
            "fields": ("user",),
            "classes": ("collapse",),
            "description": (
                "<div style='background:#fff3cd;padding:10px;"
                "border-radius:5px;margin-bottom:10px;'>"
                "⚠️ Liez un compte utilisateur UNIQUEMENT si cet employé "
                "est un chef d'équipe qui doit se connecter à l'application."
                "</div>"
            ),
        }),
    )

    # ✅ Widget multi-sélection pour visible_lines
    filter_horizontal = ("visible_lines",)

    # ── Actions ─────────────────────────────────────────────────────────
    actions = [
        'link_to_user_auto',
        'unlink_user',
        'create_user_and_link',
        'sync_visible_lines_from_main',    # ✅ NOUVEAU
        'add_to_all_managed_lines',        # ✅ NOUVEAU
        'clear_visible_lines',             # ✅ NOUVEAU
    ]

    # ── Queryset ─────────────────────────────────────────────────────────
    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related(
            'line', 'manager', 'user'
        ).prefetch_related(
            'visible_lines'             # ✅ Pré-charger pour éviter N+1
        )
        if request.user.is_superuser or request.user.groups.filter(name="RH").exists():
            return qs
        return qs.filter(manager=request.user)

    def save_model(self, request, obj, form, change):
        if not obj.manager:
            obj.manager = request.user
        super().save_model(request, obj, form, change)

        # ✅ AUTO : S'assurer que la ligne principale est dans visible_lines
        if obj.line and obj.line not in obj.visible_lines.all():
            obj.visible_lines.add(obj.line)

    # ═══════════════════════════════════════════════════════════════════
    # COLONNES PERSONNALISÉES
    # ═══════════════════════════════════════════════════════════════════

    @admin.display(description='Nom complet', ordering='last_name')
    def full_name_display(self, obj):
        return f"{obj.first_name} {obj.last_name}"

    # ✅ NOUVELLE COLONNE : Lignes de visibilité
    @admin.display(description='Matrices (lignes visibles)')
    def visible_lines_display(self, obj):
        lines = obj.visible_lines.all()
        count = lines.count()

        if count == 0:
            return format_html(
                '<span style="background:#fee2e2;color:#991b1b;'
                'padding:3px 8px;border-radius:10px;font-size:11px;">'
                '⚠️ Aucune</span>'
            )

        # Afficher les badges de chaque ligne
        badges = []
        for line in lines[:4]:   # Max 4 badges
            # Ligne principale → badge vert, autres → badge bleu
            is_main = (obj.line and line.pk == obj.line.pk)
            bg    = '#dcfce7' if is_main else '#dbeafe'
            color = '#166534' if is_main else '#1e40af'
            icon  = '★' if is_main else '◆'

            badges.append(format_html(
                '<span style="background:{};color:{};padding:2px 7px;'
                'border-radius:8px;font-size:11px;margin:1px;'
                'display:inline-block;">{} {}</span>',
                bg, color, icon, line.code
            ))

        if count > 4:
            badges.append(format_html(
                '<span style="background:#f3f4f6;color:#6b7280;'
                'padding:2px 7px;border-radius:8px;font-size:11px;">'
                '+{} autres</span>',
                count - 4
            ))

        return format_html(" ".join([b for b in badges]))

    @admin.display(description='Compte')
    def user_status(self, obj):
        if obj.user:
            return format_html(
                '<span style="background:#D1FAE5;color:#065F46;'
                'padding:3px 8px;border-radius:10px;font-size:11px;">'
                '✅ {}</span>',
                obj.user.username
            )
        return format_html(
            '<span style="background:#FEE2E2;color:#991B1B;'
            'padding:3px 8px;border-radius:10px;font-size:11px;">'
            '❌ Non lié</span>'
        )

    @admin.display(description='Rôle')
    def is_team_leader_display(self, obj):
        if obj.user and ProductionLine.objects.filter(team_leaders=obj.user).exists():
            return format_html(
                '<span style="background:#EDE9FE;color:#5B21B6;'
                'padding:3px 8px;border-radius:10px;font-size:11px;">'
                '👑 Chef</span>'
            )
        return format_html(
            '<span style="background:#F3F4F6;color:#6B7280;'
            'padding:3px 8px;border-radius:10px;font-size:11px;">'
            '👷 Opérateur</span>'
        )

    @admin.display(description='Lignes gérées')
    def managed_lines_display(self, obj):
        if obj.user:
            lines = ProductionLine.objects.filter(team_leaders=obj.user)
            if lines.exists():
                return format_html(
                    '<span style="color:#059669;">{}</span>',
                    ", ".join([l.code for l in lines])
                )
        return format_html('<span style="color:#999;">—</span>')

    # ═══════════════════════════════════════════════════════════════════
    # ACTIONS PERSONNALISÉES
    # ═══════════════════════════════════════════════════════════════════

    @admin.action(description="🔗 Lier automatiquement à un User (par nom/matricule)")
    def link_to_user_auto(self, request, queryset):
        linked, errors = 0, []

        for employee in queryset:
            if employee.user:
                continue

            user = User.objects.filter(
                Q(first_name__iexact=employee.first_name,
                  last_name__iexact=employee.last_name) |
                Q(username__iexact=employee.matricule)
            ).first()

            if user:
                if not Employee.objects.filter(user=user).exists():
                    employee.user = user
                    employee.save()
                    linked += 1
                else:
                    errors.append(f"{employee}: User '{user.username}' déjà lié")
            else:
                errors.append(f"{employee}: Aucun User trouvé")

        if linked:
            self.message_user(request, f"✅ {linked} employé(s) lié(s).")
        if errors:
            self.message_user(
                request,
                f"⚠️ Erreurs: {'; '.join(errors[:5])}{'...' if len(errors) > 5 else ''}",
                level=message_constants.WARNING
            )

    @admin.action(description="🔓 Supprimer la liaison User")
    def unlink_user(self, request, queryset):
        count = queryset.filter(user__isnull=False).update(user=None)
        self.message_user(request, f"✅ {count} liaison(s) supprimée(s).")

    @admin.action(description="👑 Créer un compte User et lier")
    def create_user_and_link(self, request, queryset):
        created, errors = 0, []

        for employee in queryset:
            if employee.user:
                errors.append(f"{employee}: déjà un compte")
                continue

            base = (
                f"{employee.first_name[0].lower()}{employee.last_name.lower()}"
            )
            base = ''.join(c for c in base if c.isalnum())[:20]

            username, counter = base, 1
            while User.objects.filter(username=username).exists():
                username = f"{base}{counter}"
                counter += 1

            user = User.objects.create_user(
                username=username,
                first_name=employee.first_name,
                last_name=employee.last_name,
                password='changeme123',
                is_staff=True,
            )
            employee.user = user
            employee.save()
            created += 1

        if created:
            self.message_user(
                request,
                f"✅ {created} compte(s) créé(s). Mot de passe : 'changeme123'"
            )
        if errors:
            self.message_user(
                request,
                f"⚠️ Ignorés: {'; '.join(errors[:3])}",
                level=message_constants.WARNING
            )

    # ✅ NOUVELLE ACTION : Synchroniser visible_lines depuis la ligne principale
    @admin.action(
        description="🔄 Synchroniser visible_lines depuis la ligne principale"
    )
    def sync_visible_lines_from_main(self, request, queryset):
        """
        Pour chaque employé sélectionné :
        - Ajoute sa ligne principale (FK line) dans visible_lines
        - Ne supprime pas les lignes déjà cochées
        """
        synced, skipped = 0, 0

        for emp in queryset:
            if emp.line:
                emp.visible_lines.add(emp.line)
                synced += 1
            else:
                skipped += 1

        if synced:
            self.message_user(
                request,
                f"✅ {synced} employé(s) synchronisé(s) : "
                f"ligne principale ajoutée dans visible_lines."
            )
        if skipped:
            self.message_user(
                request,
                f"⚠️ {skipped} employé(s) ignoré(s) : pas de ligne principale.",
                level=message_constants.WARNING
            )

    # ✅ NOUVELLE ACTION : Ajouter à toutes les lignes du manager
    @admin.action(
        description="➕ Ajouter aux matrices de toutes les lignes du manager"
    )
    def add_to_all_managed_lines(self, request, queryset):
        """
        Pour chaque employé sélectionné :
        - Récupère toutes les lignes gérées par son manager
        - Les ajoute toutes dans visible_lines
        """
        updated, skipped = 0, 0

        for emp in queryset:
            if emp.manager:
                managed = ProductionLine.objects.filter(
                    team_leaders=emp.manager
                )
                if managed.exists():
                    emp.visible_lines.add(*managed)
                    updated += 1
                else:
                    skipped += 1
            else:
                skipped += 1

        if updated:
            self.message_user(
                request,
                f"✅ {updated} employé(s) ajouté(s) à toutes "
                f"les lignes de leur manager."
            )
        if skipped:
            self.message_user(
                request,
                f"⚠️ {skipped} employé(s) ignoré(s) : manager ou lignes introuvables.",
                level=message_constants.WARNING
            )

    # ✅ NOUVELLE ACTION : Vider les lignes de visibilité
    @admin.action(description="🗑️ Vider les lignes de visibilité")
    def clear_visible_lines(self, request, queryset):
        """Retire toutes les lignes de visibilité des employés sélectionnés."""
        count = 0
        for emp in queryset:
            emp.visible_lines.clear()
            count += 1

        self.message_user(
            request,
            f"✅ Lignes de visibilité effacées pour {count} employé(s). "
            f"Pensez à re-synchroniser depuis la ligne principale.",
            level=message_constants.WARNING
        )


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN TASK
# ═══════════════════════════════════════════════════════════════════════════════

@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("code", "task_number", "title", "lines_display", "order")
    list_filter  = ("lines",)
    search_fields = ("code", "title")
    filter_horizontal = ("lines",)
    ordering = ("order", "task_number")

    @admin.display(description='Lignes')
    def lines_display(self, obj):
        lines = obj.lines.all()
        return ", ".join([l.code for l in lines]) if lines else "-"


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN MATRIX ENTRY
# ═══════════════════════════════════════════════════════════════════════════════

@admin.register(MatrixEntry)
class MatrixEntryAdmin(admin.ModelAdmin):
    list_display  = ("employee", "task", "level", "last_trained", "updated_at")
    list_filter   = ("level", "task", "employee__line")
    search_fields = ("employee__first_name", "employee__last_name", "task__title")

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "task":
            employee_id = request.GET.get("employee")
            if employee_id:
                emp = Employee.objects.filter(id=employee_id).first()
                if emp and emp.line:
                    kwargs["queryset"] = Task.objects.filter(lines=emp.line)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN TRAINING RECORD
# ═══════════════════════════════════════════════════════════════════════════════

@admin.register(TrainingRecord)
class TrainingRecordAdmin(ImportExportModelAdmin):
    resource_class = TrainingRecordResource

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj and TrainingApprovalRequest.objects.filter(
            training_record=obj, requested_level__in=(TrainingLevel.SUPERVISED, TrainingLevel.TRAINER),
        ).exists():
            fields += ["approval_status", "approved_by", "approved_at", "rejection_reason"]
        return fields

    list_display = ("date", "employee", "task", "level",
                    "trainer", "get_year", "get_month")
    list_filter  = (
        "level", DateRangeFilter, YearFilter,
        MonthFilter, DayFilter, "trainer", "lines",
    )
    date_hierarchy    = 'date'
    search_fields     = (
        'employee__first_name', 'employee__last_name',
        'task__title', 'trainer__first_name',
    )
    ordering          = ('-date',)
    filter_horizontal = ('lines',)

    @admin.display(description='Année', ordering='date')
    def get_year(self, obj):
        return obj.date.year if obj.date else '-'

    @admin.display(description='Mois')
    def get_month(self, obj):
        if obj.date:
            months = ['','Jan','Fév','Mar','Avr','Mai','Juin',
                      'Juil','Août','Sep','Oct','Nov','Déc']
            return months[obj.date.month]
        return '-'

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "task":
            employee_id = request.GET.get("employee")
            if employee_id:
                emp = Employee.objects.filter(id=employee_id).first()
                if emp and emp.line:
                    kwargs["queryset"] = Task.objects.filter(lines=emp.line)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN PRODUCTION LINE
# ═══════════════════════════════════════════════════════════════════════════════

@admin.register(ProductionLine)
class ProductionLineAdmin(admin.ModelAdmin):
    list_display = (
        "code", "name",
        "team_leaders_display",
        "employee_count",
        "visible_employee_count",   # ✅ NOUVEAU
        "task_count",
    )
    search_fields     = ("code", "name")
    filter_horizontal = ("team_leaders",)

    fieldsets = (
        ('📋 Informations', {
            'fields': ('code', 'name'),
        }),
        ("👥 Chefs d'équipe", {
            'fields': ('team_leaders',),
            'description': (
                "<div style='background:#DBEAFE;padding:10px;"
                "border-radius:5px;margin-bottom:10px;'>"
                "ℹ️ Sélectionnez les utilisateurs chefs d'équipe de cette ligne."
                "</div>"
            ),
        }),
    )

    @admin.display(description="Chefs d'équipe")
    def team_leaders_display(self, obj):
        leaders = obj.team_leaders.all()
        if not leaders:
            return format_html('<span style="color:#999;">Aucun chef assigné</span>')

        parts = []
        for leader in leaders:
            has_profile = Employee.objects.filter(user=leader).exists()
            bg    = '#D1FAE5' if has_profile else '#FEF3C7'
            color = '#065F46' if has_profile else '#92400E'
            icon  = '✅' if has_profile else '⚠️'
            parts.append(format_html(
                '<span style="background:{};color:{};padding:2px 6px;'
                'border-radius:5px;margin:2px;display:inline-block;">{} {}</span>',
                bg, color, icon,
                leader.get_full_name() or leader.username
            ))
        return format_html(" ".join(parts))

    @admin.display(description='Employés (principale)')
    def employee_count(self, obj):
        """Opérateurs dont la ligne FK principale est cette ligne."""
        count = Employee.objects.filter(line=obj).count()
        return format_html(
            '<span style="background:#E0E7FF;color:#3730A3;'
            'padding:3px 8px;border-radius:10px;">{}</span>',
            count
        )

    # ✅ NOUVELLE COLONNE
    @admin.display(description='Visibles dans la matrice')
    def visible_employee_count(self, obj):
        """
        Opérateurs qui apparaissent dans la matrice de cette ligne
        (via visible_lines M2M).
        """
        count = Employee.objects.filter(visible_lines=obj).count()
        color_bg    = '#dcfce7' if count > 0 else '#f3f4f6'
        color_text  = '#166534' if count > 0 else '#6b7280'
        return format_html(
            '<span style="background:{};color:{};'
            'padding:3px 8px;border-radius:10px;font-weight:600;">'
            '👁 {}</span>',
            color_bg, color_text, count
        )

    @admin.display(description='Tâches')
    def task_count(self, obj):
        count = obj.tasks.count()
        return format_html(
            '<span style="background:#FCE7F3;color:#9D174D;'
            'padding:3px 8px;border-radius:10px;">{}</span>',
            count
        )


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN TRAINING APPROVAL REQUEST
# ═══════════════════════════════════════════════════════════════════════════════

@admin.register(TrainingApprovalRequest)
class TrainingApprovalRequestAdmin(admin.ModelAdmin):
    # Files are managed only by the owning leader through the dedicated view.
    exclude = ("training_plan_image",)
    readonly_fields = ("training_plan_link",)

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj and obj.requires_training_plan:
            fields += ["approved", "reviewed_by", "reviewed_at", "rejection_reason",
                       "requested_level", "training_record", "requested_by"]
        return fields

    @admin.display(description="Plan de formation")
    def training_plan_link(self, obj):
        if obj and obj.has_training_plan:
            return format_html('<a href="{}" target="_blank" rel="noopener">Voir le plan</a>',
                               reverse("training_plan_view", args=[obj.pk]))
        return "Aucun plan disponible"

    list_display  = (
        'employee', 'task', 'requested_level',
        'requested_by', 'status_display', 'date_requested', 'training_plan_link',
    )
    list_filter   = ('approved', 'requested_level', 'date_requested')
    search_fields = ('employee__last_name', 'employee__first_name', 'task__title')
    date_hierarchy = 'date_requested'

    @admin.display(description='Statut')
    def status_display(self, obj):
        if obj.approved is None:
            return format_html(
                '<span style="background:#FEF3C7;color:#92400E;'
                'padding:3px 8px;border-radius:10px;">⏳ En attente</span>'
            )
        if obj.approved:
            return format_html(
                '<span style="background:#D1FAE5;color:#065F46;'
                'padding:3px 8px;border-radius:10px;">✅ Approuvé</span>'
            )
        return format_html(
            '<span style="background:#FEE2E2;color:#991B1B;'
            'padding:3px 8px;border-radius:10px;">❌ Refusé</span>'
        )


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN LINE TRANSFER
# ═══════════════════════════════════════════════════════════════════════════════

@admin.register(LineTransfer)
class LineTransferAdmin(admin.ModelAdmin):
    list_display = (
        'transfer_date', 'employee', 'from_line',
        'to_line', 'transferred_by', 'reason_short',
    )
    list_filter   = ('transfer_date', 'from_line', 'to_line')
    search_fields = ('employee__first_name', 'employee__last_name', 'reason')
    date_hierarchy = 'transfer_date'

    fields          = ('employee', 'to_line', 'reason')
    readonly_fields = ('from_line', 'transfer_date', 'transferred_by')

    def get_fields(self, request, obj=None):
        if obj:
            return (
                'employee', 'from_line', 'to_line',
                'transfer_date', 'transferred_by', 'reason',
            )
        return ('employee', 'to_line', 'reason')

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return (
                'employee', 'from_line', 'to_line',
                'transfer_date', 'transferred_by', 'reason',
            )
        return ('from_line', 'transfer_date', 'transferred_by')

    @admin.display(description='Raison')
    def reason_short(self, obj):
        if obj.reason:
            return obj.reason[:50] + '...' if len(obj.reason) > 50 else obj.reason
        return '-'

    def save_model(self, request, obj, form, change):
        if not change:
            employee = obj.employee
            old_line = employee.line
            new_line = obj.to_line

            obj.from_line       = old_line
            obj.transferred_by  = request.user
            obj.transfer_date   = timezone.now().date()

            super().save_model(request, obj, form, change)

            # Mettre à jour la ligne principale de l'employé
            employee.line    = new_line
            employee.manager = new_line.team_leaders.first()
            employee.save()

            # ✅ NOUVEAU : Mettre à jour visible_lines lors du transfert
            # Ajouter la nouvelle ligne dans visible_lines
            employee.visible_lines.add(new_line)
            # Optionnel : retirer l'ancienne ligne si elle était la seule
            if old_line:
                other_visible = employee.visible_lines.exclude(
                    pk=new_line.pk
                ).exclude(pk=old_line.pk)
                if not other_visible.exists():
                    # L'employé n'était que sur l'ancienne ligne → la retirer
                    employee.visible_lines.remove(old_line)

            # Mettre à jour les MatrixEntry
            new_tasks = Task.objects.filter(lines=new_line)
            MatrixEntry.objects.filter(
                employee=employee
            ).exclude(task__in=new_tasks).delete()

            for task in new_tasks:
                MatrixEntry.objects.get_or_create(
                    employee=employee,
                    task=task,
                    defaults={
                        'level': 0,
                        'transfer_date': timezone.now().date(),
                    }
                )

            self.message_user(
                request,
                f"✅ {employee} transféré : "
                f"'{old_line or 'Aucune'}' → '{new_line}'"
            )
        else:
            super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        return True

    def delete_model(self, request, obj):
        employee      = obj.employee
        all_transfers = LineTransfer.objects.filter(
            employee=employee
        ).order_by('transfer_date')
        last_transfer = all_transfers.last()

        if last_transfer and last_transfer.id == obj.id:
            previous_line = obj.from_line
            super().delete_model(request, obj)

            employee.line = previous_line
            if previous_line:
                employee.manager = previous_line.team_leaders.first()
                # ✅ NOUVEAU : Restaurer visible_lines
                employee.visible_lines.add(previous_line)
                employee.visible_lines.remove(obj.to_line)
            else:
                employee.manager = None
            employee.save()

            if previous_line:
                prev_tasks = Task.objects.filter(lines=previous_line)
                MatrixEntry.objects.filter(
                    employee=employee
                ).exclude(task__in=prev_tasks).delete()
                for task in prev_tasks:
                    MatrixEntry.objects.get_or_create(
                        employee=employee, task=task,
                        defaults={
                            'level': 0,
                            'transfer_date': timezone.now().date(),
                        }
                    )
            else:
                MatrixEntry.objects.filter(employee=employee).delete()

            self.message_user(
                request,
                f"✅ Transfert annulé. "
                f"{employee} restauré à '{previous_line or 'Aucune ligne'}'",
                level=message_constants.SUCCESS
            )
        else:
            super().delete_model(request, obj)
            self.message_user(
                request,
                "⚠️ Transfert supprimé de l'historique.",
                level=message_constants.WARNING
            )

    def delete_queryset(self, request, queryset):
        count = queryset.count()
        super().delete_queryset(request, queryset)
        self.message_user(
            request,
            f"⚠️ {count} transfert(s) supprimé(s) de l'historique.",
            level=message_constants.WARNING
        )


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN REQUALIFICATION RECORD
# ═══════════════════════════════════════════════════════════════════════════════

@admin.register(RequalificationRecord)
class RequalificationRecordAdmin(admin.ModelAdmin):
    readonly_fields = ('approval_request', 'training_record')

    def has_add_permission(self, request):
        # New records must be produced by the form/decision transaction.
        return False

    list_display = [
        'requalification_date', 'employee_display', 'line_display',
        'task_display', 'previous_level_badge', 'result_badge',
        'new_level_badge', 'days_since_training', 'evaluated_by_display',
    ]
    list_filter = [
        'result', 'requalification_date',
        'line', 'new_level', 'evaluated_by',
    ]
    search_fields = [
        'employee__first_name', 'employee__last_name',
        'employee__matricule', 'task__title', 'task__code',
        'line__code', 'notes',
    ]
    ordering       = ['-requalification_date']
    list_per_page  = 25
    date_hierarchy = 'requalification_date'
    raw_id_fields  = ['employee', 'task', 'line', 'evaluated_by']

    fieldsets = (
        ('Informations principales', {
            'fields': ('employee', 'task', 'line', 'requalification_date'),
        }),
        ('Résultat', {
            'fields': ('previous_level', 'result', 'new_level'),
        }),
        ('Détails', {
            'fields': (
                'original_training_date', 'days_since_training',
                'evaluated_by', 'requalifier_name', 'notes',
            ),
        }),
    )

    actions = ['export_csv']

    @admin.display(description='Employé', ordering='employee__last_name')
    def employee_display(self, obj):
        if obj.employee:
            return format_html(
                '<strong>{} {}</strong><br>'
                '<small style="color:#666;">{}</small>',
                obj.employee.first_name,
                obj.employee.last_name,
                obj.employee.matricule,
            )
        return '-'

    @admin.display(description='Ligne', ordering='line__code')
    def line_display(self, obj):
        if obj.line:
            return format_html(
                '<span style="background:#e2e8f0;padding:2px 8px;'
                'border-radius:4px;font-size:12px;">{}</span>',
                obj.line.code
            )
        return '-'

    @admin.display(description='Tâche', ordering='task__title')
    def task_display(self, obj):
        if obj.task:
            title = (
                obj.task.title[:25] + '...'
                if len(obj.task.title) > 25
                else obj.task.title
            )
            return format_html(
                '<span title="{}">{}</span>', obj.task.title, title
            )
        return format_html('<em style="color:#999;">Général</em>')

    @admin.display(description='Ancien', ordering='previous_level')
    def previous_level_badge(self, obj):
        return self._level_badge(obj.previous_level)

    @admin.display(description='Nouveau', ordering='new_level')
    def new_level_badge(self, obj):
        return self._level_badge(obj.new_level)

    def _level_badge(self, level):
        config = {
            0: ('I', '#fef3c7', '#92400e'),
            1: ('L', '#dbeafe', '#1e40af'),
            3: ('U', '#dcfce7', '#166534'),
        }
        if level is not None and level in config:
            label, bg, color = config[level]
            return format_html(
                '<span style="background:{};color:{};padding:4px 10px;'
                'border-radius:50%;font-weight:bold;font-size:12px;">{}</span>',
                bg, color, label
            )
        return '-'

    @admin.display(description='Résultat', ordering='result')
    def result_badge(self, obj):
        config = {
            'PASS':      ('✓ Maintien',  '#dcfce7', '#166534'),
            'UPGRADE':   ('↑ Montée',    '#dbeafe', '#1e40af'),
            'DOWNGRADE': ('↓ Descente',  '#fff7ed', '#c2410c'),
            'RETRAIN':   ('⟳ À reformer','#fef2f2', '#dc2626'),
        }
        if obj.result in config:
            label, bg, color = config[obj.result]
            return format_html(
                '<span style="background:{};color:{};padding:4px 12px;'
                'border-radius:12px;font-weight:500;font-size:11px;">{}</span>',
                bg, color, label
            )
        return '-'

    @admin.display(description='Évaluateur', ordering='evaluated_by__last_name')
    def evaluated_by_display(self, obj):
        if obj.evaluated_by:
            return (
                f"{obj.evaluated_by.first_name} {obj.evaluated_by.last_name}"
            )
        return '-'

    @admin.action(description='✓ Marquer comme Réussi')
    def mark_as_pass(self, request, queryset):
        count = queryset.update(result='PASS')
        self.message_user(request, f'{count} requalification(s) marquée(s) comme réussie(s).')

    @admin.action(description='⟳ Marquer comme À reformer')
    def mark_as_retrain(self, request, queryset):
        count = queryset.update(result='RETRAIN')
        self.message_user(request, f'{count} requalification(s) marquée(s) à reformer.')

    @admin.action(description='📥 Exporter en CSV')
    def export_csv(self, request, queryset):
        import csv
        from django.http import HttpResponse

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = (
            'attachment; filename="requalifications.csv"'
        )
        response.write('\ufeff')

        writer = csv.writer(response, delimiter=';')
        writer.writerow([
            'Date', 'Employé', 'Matricule', 'Ligne', 'Tâche',
            'Ancien', 'Résultat', 'Nouveau', 'Jours', 'Évaluateur', 'Notes',
        ])
        levels = {0: 'I', 1: 'L', 3: 'U'}

        for obj in queryset:
            writer.writerow([
                obj.requalification_date.strftime('%d/%m/%Y') if obj.requalification_date else '',
                f"{obj.employee.first_name} {obj.employee.last_name}" if obj.employee else '',
                obj.employee.matricule if obj.employee else '',
                obj.line.code if obj.line else '',
                obj.task.title if obj.task else 'Général',
                levels.get(obj.previous_level, '-'),
                obj.get_result_display() if obj.result else '',
                levels.get(obj.new_level, '-'),
                obj.days_since_training or '',
                (f"{obj.evaluated_by.first_name} {obj.evaluated_by.last_name}"
                 if obj.evaluated_by else ''),
                obj.notes or '',
            ])
        return response

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'employee', 'task', 'line', 'evaluated_by'
        )

    def save_model(self, request, obj, form, change):
        if not obj.evaluated_by:
            obj.evaluated_by = request.user
        super().save_model(request, obj, form, change)


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN REQUALIFICATION APPROVAL REQUEST
# ═══════════════════════════════════════════════════════════════════════════════

@admin.register(RequalificationApprovalRequest)
class RequalificationApprovalRequestAdmin(admin.ModelAdmin):
    exclude = ('training_plan_image',)
    readonly_fields = ('approved', 'decision_date', 'decision_by', 'requested_by', 'training_plan_link')

    def has_add_permission(self, request):
        return False

    @admin.display(description='Plan de formation')
    def training_plan_link(self, obj):
        if obj and obj.has_training_plan:
            return format_html('<a href="{}" target="_blank" rel="noopener">Voir le plan</a>',
                               reverse('requalification_plan_view', args=[obj.pk]))
        return 'Plan indisponible'

    list_display = [
        'employee', 'task', 'requested_result',
        'requested_by', 'date_requested', 'status_display',
    ]
    list_filter   = ['approved', 'requested_result', 'date_requested', 'line']
    search_fields = [
        'employee__first_name', 'employee__last_name', 'employee__matricule',
    ]
    date_hierarchy = 'date_requested'

    @admin.display(description='Statut')
    def status_display(self, obj):
        if obj.approved is None:
            return format_html(
                '<span style="background:#FEF3C7;color:#92400E;'
                'padding:3px 8px;border-radius:10px;">⏳ En attente</span>'
            )
        if obj.approved:
            return format_html(
                '<span style="background:#D1FAE5;color:#065F46;'
                'padding:3px 8px;border-radius:10px;">✅ Approuvé</span>'
            )
        return format_html(
            '<span style="background:#FEE2E2;color:#991B1B;'
            'padding:3px 8px;border-radius:10px;">❌ Refusé</span>'
        )


# ═══════════════════════════════════════════════════════════════════════════════
# EXTENSION USER ADMIN
# ═══════════════════════════════════════════════════════════════════════════════

class EmployeeProfileInline(admin.StackedInline):
    model               = Employee
    can_delete          = False
    verbose_name        = "Profil Employé"
    verbose_name_plural = "Profil Employé"
    fk_name             = 'user'
    extra               = 0

    fieldsets = (
        ('Profil Employé lié', {
            'fields': (
                'first_name', 'last_name', 'matricule',
                'line', 'manager', 'hire_date', 'visible_lines',
            ),
        }),
    )
    # ✅ Afficher visible_lines dans l'inline aussi
    filter_horizontal = ('visible_lines',)

    def has_add_permission(self, request, obj=None):
        return False


class CustomUserAdmin(BaseUserAdmin):
    inlines = [EmployeeProfileInline]

    list_display = [
        'username', 'email', 'first_name', 'last_name',
        'is_staff', 'employee_profile_status', 'managed_lines_display',
    ]

    @admin.display(description='Profil Employé')
    def employee_profile_status(self, obj):
        try:
            if obj.employee_profile:
                return format_html(
                    '<span style="background:#D1FAE5;color:#065F46;'
                    'padding:3px 8px;border-radius:10px;">✅ {}</span>',
                    obj.employee_profile.matricule
                )
        except Employee.DoesNotExist:
            pass
        return format_html(
            '<span style="background:#FEE2E2;color:#991B1B;'
            'padding:3px 8px;border-radius:10px;">❌ Non lié</span>'
        )

    @admin.display(description="Chef d'équipe de")
    def managed_lines_display(self, obj):
        lines = obj.managed_lines.all()
        if lines.exists():
            return format_html(
                '<span style="background:#EDE9FE;color:#5B21B6;'
                'padding:3px 8px;border-radius:10px;">👑 {}</span>',
                ", ".join([l.code for l in lines[:3]])
            )
        return format_html('<span style="color:#999;">—</span>')


admin.site.unregister(User)
admin.site.register(User, CustomUserAdmin)


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN QUALITY PROFILE
# ═══════════════════════════════════════════════════════════════════════════════

@admin.register(QualityProfile)
class QualityProfileAdmin(admin.ModelAdmin):
    list_display  = ('user', 'get_fullname', 'is_active', 'created_at')
    list_filter   = ('is_active',)
    list_editable = ('is_active',)
    search_fields = ('user__username', 'user__first_name', 'user__last_name')

    @admin.display(description='Nom complet')
    def get_fullname(self, obj):
        return obj.user.get_full_name() or '—'



@admin.register(LineTransferApprovalRequest)
class LineTransferApprovalRequestAdmin(admin.ModelAdmin):
    list_display = [
        'employee',
        'from_line',
        'to_line',
        'new_manager',
        'requested_by',
        'date_requested',
        'approved',
        'decision_date',
    ]

    list_filter = [
        'approved',
        'date_requested',
        'from_line',
        'to_line',
    ]

    search_fields = [
        'employee__first_name',
        'employee__last_name',
        'employee__matricule',
    ]

    # ✅ Permet la suppression en masse
    actions = ['delete_selected']
# ═══════════════════════════════════════════════════════════════════════════════
# PERSONNALISATION DU SITE
# ═══════════════════════════════════════════════════════════════════════════════


# admin.py
@admin.register(PendingEmployee)
class PendingEmployeeAdmin(admin.ModelAdmin):
    list_display = [
        'matricule',
        'first_name',
        'last_name',
        'hire_date',
        'created_by',
        'created_at',
    ]
    search_fields = [
        'matricule',
        'first_name',
        'last_name',
    ]
    list_filter  = ['created_at', 'created_by']
    actions      = ['delete_selected']
admin.site.site_header  = "🏭 Gestion des Matrices de Formation"
admin.site.site_title   = "Admin Matrices"
admin.site.index_title  = "Tableau de bord administrateur"


# admin.py

from django.contrib import admin

from .models import (
    TrainingQuestion,
    TrainingQuestionnaire,
    TrainingQuestionAnswer,
)


@admin.register(TrainingQuestion)
class TrainingQuestionAdmin(admin.ModelAdmin):
    list_display = (
        "short_text",
        "level",
        "line",
        "task",
        "answer_type",
        "is_required",
        "is_blocking",
        "is_active",
        "order",
    )

    list_filter = (
        "level",
        "line",
        "answer_type",
        "is_required",
        "is_blocking",
        "is_active",
    )

    search_fields = (
        "text",
        "task__title",
        "task__code",
        "line__name",
        "line__code",
    )

    ordering = (
        "line",
        "task",
        "order",
    )

    def short_text(self, obj):
        return obj.text[:80]

    short_text.short_description = "Question"


class TrainingQuestionAnswerInline(admin.TabularInline):
    model = TrainingQuestionAnswer
    extra = 0
    can_delete = False
    readonly_fields = (
        "question",
        "question_text_snapshot",
        "boolean_answer",
        "text_answer",
        "comment",
        "is_compliant",
    )


@admin.register(TrainingQuestionnaire)
class TrainingQuestionnaireAdmin(admin.ModelAdmin):
    list_display = (
        "training_record",
        "line",
        "answered_by",
        "is_compliant",
        "completed_at",
    )

    list_filter = (
        "is_compliant",
        "line",
        "completed_at",
    )

    readonly_fields = (
        "training_record",
        "line",
        "answered_by",
        "is_compliant",
        "general_comment",
        "completed_at",
    )

    inlines = [
        TrainingQuestionAnswerInline,
    ]
