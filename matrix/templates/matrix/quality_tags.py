# matrix/templatetags/quality_tags.py

from django import template

register = template.Library()


@register.simple_tag
def matrix_cell(entry_map, emp_id, task_id):
    """
    Retourne les records pour un couple (employee_id, task_id).
    
    Usage dans le template :
        {% matrix_cell entry_map emp.pk task.pk as records %}
    """
    return entry_map.get((emp_id, task_id), [])


@register.filter
def get_item(dictionary, key):
    """
    Accès à un dictionnaire par clé dans les templates.
    
    Usage :
        {{ my_dict|get_item:my_key }}
    """
    if dictionary is None:
        return None
    return dictionary.get(key)