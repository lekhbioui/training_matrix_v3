# matrix/decorators.py

from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import redirect

def superuser_required(view_func):
    """
    Décorateur qui vérifie que l'utilisateur est super admin
    """
    def check_superuser(user):
        if not user.is_authenticated:
            return False
        if not user.is_superuser:
            return False
        return True
    
    decorated_view = user_passes_test(
        check_superuser,
        login_url='/accounts/login/',
        redirect_field_name=None
    )(view_func)
    
    return decorated_view


# matrix/decorators.py  ← AJOUTER à votre fichier existant

def quality_required(view_func):
    """
    Accès réservé aux comptes Qualité ou superusers.
    Bloque les chefs d'équipe normaux.
    """
    from functools import wraps
    from django.shortcuts import redirect
    from django.contrib import messages

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):

        if not request.user.is_authenticated:
            return redirect('quality_login')

        # Superuser → accès total
        if request.user.is_superuser:
            return view_func(request, *args, **kwargs)

        # Vérifier le profil qualité actif
        has_access = (
            hasattr(request.user, 'quality_profile')
            and request.user.quality_profile.is_active
        )

        if not has_access:
            messages.error(
                request,
                "⛔ Accès réservé au département Qualité."
            )
            return redirect('quality_login')

        return view_func(request, *args, **kwargs)

    return wrapper