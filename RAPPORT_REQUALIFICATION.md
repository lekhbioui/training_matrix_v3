# Correction du plan de formation dans les requalifications

## 1. Diagnostic

L'obligation était imposée dans plusieurs couches partagées entre chefs, RH et superviseurs. Masquer le champ seul ne suffisait donc pas.

| Fichier / fonction | Comportement avant correction |
| --- | --- |
| `matrix/forms.py`, `RequalificationForm.__init__()` | Héritait du champ de `TrainingPlanImageForm` et imposait `required=True` à tous les utilisateurs, y compris avec `direct_leader=True`. |
| `matrix/services_requalification.py`, `create_requalification()` | Lisait systématiquement `data["training_plan_image"]` et enregistrait le fichier, même pour une création RH/Superviseur. |
| `matrix/models.py`, `RequalificationApprovalRequest.save()` | Refusait toute nouvelle demande sans image, indépendamment de son auteur. |
| Même modèle, `validate_training_plan_for_approval()` | Imposait un plan disponible pour toute approbation. Cette méthode était appelée par le service de décision et par `RequalificationRecord.save()`. |
| `matrix/templates/matrix/employees_to_requalify.html` et `rh_requalify_team_leader.html` | Incluaient tous deux le fragment `requalification_plan_field.html`, qui affichait systématiquement le champ et son texte d'obligation. |
| `matrix/templates/matrix/rh_requalification_approvals.html` | Désactivait toute approbation sans plan, sans distinguer l'origine de la demande. |

### Modèles, formulaires et contraintes

- Le champ `RequalificationApprovalRequest.training_plan_image` était déjà déclaré avec `blank=True, null=True`. L'obligation provenait des validations Python, pas d'une contrainte de schéma imposant une image.
- `RequalificationRecord` conserve un lien vers la demande et éventuellement vers `TrainingRecord`. Il ne stocke pas une seconde copie du fichier.
- `TrainingRecord` ne porte pas le fichier du plan de requalification. Les plans des formations ordinaires sont gérés par `TrainingApprovalRequest` avec leurs propres règles : celles-ci sont conservées.
- La validation des images accepte JPEG, PNG et WEBP, limite la taille à 10 Mo et les dimensions à 25 millions de pixels, et normalise l'extension. Ces contrôles restent actifs pour les chefs.
- Les plans de requalification existants sont protégés contre le remplacement et contre les mises à jour directes du champ par QuerySet. L'administration Django exclut déjà le champ d'ajout et propose un lien de consultation.

### Vues et permissions existantes

- `employees_to_requalify()` / `_requalification_form_page()` présentent le formulaire partagé ; `process_requalification()` traite sa soumission.
- `rh_requalify_team_leader()` utilise le même formulaire et le même service avec `direct_leader=True`, pour une requalification globale par ligne.
- `rh_requalification_approvals()` et `decide_requalification()` contrôlent les décisions ; leur accès reste réservé aux superutilisateurs comme avant.
- `_get_requalification_plan()`, `requalification_plan_view()` et `requalification_plan_image()` permettent la consultation par l'auteur ou les utilisateurs autorisés par les permissions RH existantes. Les URL de consultation n'acceptent pas de modification par POST.
- Les chefs sont identifiés par `ProductionLine.team_leaders` (relation inverse `User.managed_lines`) et par `Employee.manager` (`User.managed_employees`). Le premier mécanisme est également utilisé par `Employee.is_team_leader()` ; le second contrôle la soumission pour un employé.
- Les groupes `RH` et `Superviseur` existent dans les contrôles métier. Les superutilisateurs disposent déjà des accès de gestion. Ces droits ne constituent pas une autorisation de joindre un plan en tant que chef.

## 2. Solution

La correction utilise les relations et groupes existants, sans nouvelle architecture ni migration.

| Profil / opération | Comportement obtenu |
| --- | --- |
| Chef identifié par ses lignes ou employés, hors profil de gestion | Champ visible et obligatoire lors de la création ; format et contenu de l'image contrôlés. |
| RH, Superviseur ou superutilisateur, même affecté à une ligne | Aucun champ d'ajout ; création autorisée sans plan dans les processus déjà permis. |
| POST forcé contenant une image par un profil de gestion | Fichier ignoré par le formulaire et le service ; aucun fichier enregistré. |
| Création ORM avec une image et un auteur non autorisé | Erreur de validation avant enregistrement du fichier. |
| Plan déjà enregistré | Consultation et lien d'historique conservés ; remplacement interdit. |
| Validation d'une demande de chef | Aucun nouveau fichier demandé au valideur ; le plan initial doit rester disponible, comme auparavant. |
| Validation d'une demande de gestion créée sans plan | Absence de plan acceptée côté serveur et bouton d'approbation disponible. |

La présence d'un plan initial continue d'imposer sa disponibilité à l'approbation, même si son auteur a depuis rejoint un profil de gestion. Les anciennes demandes de chefs sans plan restent soumises à la règle d'approbation préexistante.

Les droits de consultation et de décision, le seuil de 188 jours, les niveaux, les lignes, les liens d'historique et le traitement transactionnel ne sont pas modifiés. Les autorisations de gestion ont priorité sur les relations de chef pour le seul droit d'ajouter un plan.

## 3. Implémentation appliquée

### `matrix/services_requalification.py`

Deux fonctions distinguent le profil de gestion du droit d'ajout :

```python
def is_requalification_management(user):
    return user.is_authenticated and (
        is_requalification_rh(user)
        or user.groups.filter(name__iexact="Superviseur").exists()
    )


def can_add_requalification_plan(user):
    return user.is_authenticated and not is_requalification_management(user) and (
        user.managed_lines.exists() or user.managed_employees.exists()
    )
```

`is_requalification_rh()` conserve sa définition existante : superutilisateur ou membre du groupe RH. Le service revérifie l'acteur avant de stocker un fichier, même si un champ était injecté dans les données nettoyées :

```python
if can_add_requalification_plan(user):
    uploaded = data.get("training_plan_image")
    if not uploaded:
        raise ValidationError(
            "Le plan de formation est obligatoire pour créer une requalification."
        )
    uploaded.seek(0)
    stored_name = storage.save(field.generate_filename(req, uploaded.name), uploaded)
    req.training_plan_image = stored_name
req.save(force_insert=True)
```

### `matrix/forms.py`, `RequalificationForm.__init__()`

Le champ est retiré du formulaire pour les profils non autorisés et le parcours de gestion des chefs. Il n'entre donc pas dans `cleaned_data`, même si le POST contient un fichier :

```python
from .services_requalification import can_add_requalification_plan
if direct_leader or not can_add_requalification_plan(user):
    self.fields.pop("training_plan_image")
    return
plan = self.fields["training_plan_image"]
plan.label = "Plan de formation"
plan.required = True
```

### `matrix/models.py`

La validation Python devait être corrigée pour permettre les créations et validations de gestion sans plan. Aucun champ du schéma n'est changé :

```python
@property
def requires_training_plan(self):
    from .services_requalification import is_requalification_management
    return bool(self.training_plan_image) or not is_requalification_management(self.requested_by)

def validate_training_plan_for_approval(self):
    if self.requires_training_plan and not self.has_training_plan:
        raise ValidationError("Approbation impossible : le plan de formation est indisponible.")
```

Lors d'une nouvelle demande, `save()` refuse une image si l'auteur ne peut pas en ajouter ; il impose le fichier seulement si `requires_training_plan` est vrai. La validation d'image s'exécute uniquement lorsqu'un fichier est présent. Les protections du plan initial et du demandeur sur les enregistrements existants sont conservées.

Le message de `RequalificationRecord.save()` indique désormais qu'une demande approuvée est nécessaire, sans affirmer qu'un plan est systématiquement requis.

### Templates et messages

- `matrix/templates/matrix/requalification_plan_field.html` : fragment entouré de `{% if form.training_plan_image %}` ; le texte d'obligation disparaît avec le champ.
- `matrix/templates/matrix/rh_requalify_team_leader.html` : suppression de l'inclusion du champ d'ajout ; les liens de consultation dans l'historique restent présents.
- `matrix/templates/matrix/rh_requalification_approvals.html` : bouton désactivé uniquement avec `{% if req.requires_training_plan and not req.plan_available %}`.
- `matrix/views.py` : messages de succès et de nouvelle tentative adaptés pour ne plus prétendre qu'une création de gestion contient un plan ou nécessite sa resélection.
- `matrix/test_requalification_plans.py` : attentes de création RH/Superviseur corrigées et ajout de 13 tests. Un test existant utilisait d'anciens noms JavaScript ; il vérifie maintenant le sélecteur actuel et l'absence d'un champ de ligne dupliqué.

## 4. Vérification

Les tests utilisent une base Django de test et des répertoires temporaires pour les fichiers. Aucune migration n'a été créée ou appliquée et aucun plan existant n'a été remplacé.

### Commandes et résultats

| Vérification | Résultat |
| --- | --- |
| Avant correction : `venv\Scripts\python.exe -B manage.py test --noinput` | 108 tests : 101 réussis, 6 échecs, 1 erreur. |
| Tests ciblés après première correction : `venv\Scripts\python.exe -B manage.py test matrix.test_requalification_plans --noinput` | 66 tests réussis. |
| Suite finale, avec deux scénarios complémentaires : `venv\Scripts\python.exe -B manage.py test --noinput` | 121 tests : 115 réussis, 5 échecs, 1 erreur. Les 68 tests de requalification réussissent. |
| `venv\Scripts\python.exe -B manage.py check` | Aucun problème signalé. |
| `venv\Scripts\python.exe -B manage.py makemigrations --check --dry-run` | `No changes detected`. |

### Scénarios demandés

| Scénario | Résultat vérifié |
| --- | --- |
| Chef sans plan | Erreur ; aucune demande ni fichier créé. |
| Chef avec plan valide | Demande créée en attente de validation. |
| Formulaire RH ou Superviseur | Champ et texte d'obligation absents, même avec une affectation de chef. |
| Création de gestion sans fichier | Réussite dans les parcours global et par tâche autorisés ; liens de formation conservés. |
| RH/Superviseur superadministrateur approuve sans nouveau fichier | Réussite ; nom et contenu du plan initial inchangés. |
| Fichier envoyé par POST forcé lors d'une création de gestion | Ignoré, y compris si le contenu est invalide ; aucun fichier stocké. |
| Tentative de remplacement lors d'une décision | Ignorée ; document initial inchangé. |
| Consultation autorisée | Page et image accessibles au chef auteur, RH et superviseur superadministrateur. |
| Accès direct non autorisé | Autre chef et superviseur sans privilèges de consultation refusés ; utilisateur anonyme redirigé. |
| POST sur les URL de lecture du plan | HTTP 405 ; aucun remplacement. |
| Chef ouvrant ou soumettant le parcours de gestion | Accès refusé par redirection ; aucune création. |
| Plan initial absent ou illisible | Approbation de la demande de chef toujours bloquée sans effets partiels. |

Les contrôles existants d'images invalides, de taille, de dimensions, de CSRF, de doublons, de transactions et de conservation de l'historique passent également.

### Échecs préexistants encore présents

Les six résultats suivants étaient déjà présents dans la suite avant intervention ; aucun nouvel échec n'est apparu :

| Test | Constat |
| --- | --- |
| `TrainingPlanHistoryTests.test_approval_then_history_reuses_file_without_data_changes_or_duplication` | Erreur : le test cherche `streaming_content` sur une réponse HTML. |
| `TrainingPlanHistoryTests.test_approved_l_and_u_link_to_the_original_plan_for_rh` | Attend `image/png`, reçoit `text/html`. |
| `TrainingPlanHistoryTests.test_missing_file_is_unavailable_in_history_and_returns_clean_404` | Attend HTTP 404, reçoit HTTP 200. |
| `TrainingPlanTests.test_missing_stored_file_blocks_approval` | Attend HTTP 404 sur la page du plan ordinaire, reçoit HTTP 200. |
| `TrainingPlanTests.test_plan_view_authorizes_owner_and_rh_only_and_prevents_caching` | Attend `image/png`, reçoit `text/html`. |
| `TrainingRecordFormLevelLTests.test_level_l_is_blocked_while_level_i_is_pending` | Différence de libellé : le test attend « encore en attente », le formulaire renvoie « en attente ». |

Ces tests concernent les formations ordinaires et leurs attentes de réponse ou de message. Ils n'ont pas été modifiés dans cette correction ciblée.

Le contrôle `git diff --check` signale également des espaces finaux préexistants dans des portions non modifiées de `matrix/views.py` (lignes 3798, 3803, 3809, 3815, 3823 et 8257).

Les réponses HTML et les accès HTTP ont été vérifiés par le client de test Django. Aucune vérification visuelle interactive dans un navigateur ni validation sur une instance déployée n'a été exécutée.
