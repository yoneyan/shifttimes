from django.urls import path

from . import views
from . import billing_views

app_name = "custom_auth_group"
urlpatterns = [
    path("", views.list_groups, name="index"),
    path("<int:group_id>/", views.list_group, name="list"),
    path("add/", views.add_group, name="add"),
    path("<int:group_id>/members/", views.group_members, name="members"),
    path("<int:group_id>/edit", views.edit_group, name="edit"),
    path("<int:group_id>/permission", views.group_permission, name="permission"),
    path("<int:group_id>/admin/", views.group_admin_home, name="admin_home"),
    path("<int:group_id>/register/", views.register_member, name="register_member"),
    path("<int:group_id>/register-admin/", views.register_admin, name="register_admin"),
    path("<int:group_id>/billing/", billing_views.billing, name="billing"),
    path("<int:group_id>/billing/checkout/", billing_views.create_checkout_session, name="billing_checkout"),
    path("<int:group_id>/billing/success/", billing_views.billing_success, name="billing_success"),
    path("<int:group_id>/billing/cancel/", billing_views.cancel_subscription, name="billing_cancel"),
]
