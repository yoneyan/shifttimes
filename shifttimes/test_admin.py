"""管理画面のスモークテスト。

登録済みの全モデルについて一覧・追加・変更画面が表示できることを確認する。
list_display や search_fields に存在しないフィールドを書いた、といった設定ミスを検出する。
"""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from custom_auth.models import Group, UserGroup
from notice.models import Notice


class AdminSmokeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.superuser = user_model.objects.create_superuser(
            username="root", email="root@example.com", password="Sh1ftTimes-Passw0rd"
        )
        member = user_model.objects.create(
            username="member", username_jp="メンバー", email="member@example.com", is_active=True
        )
        group = Group.objects.create(name="shop", name_jp="店舗")
        UserGroup.objects.create(user=member, group=group, is_admin=True)
        Notice.objects.create(type1=Notice.SERVICE, title="お知らせ")

    def setUp(self):
        self.client.force_login(self.superuser)

    def _admin_url(self, model, view, *args):
        return reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_{view}", args=args)

    def test_admin_index(self):
        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)

    def test_changelist_and_add_pages(self):
        for model, model_admin in admin.site._registry.items():
            with self.subTest(model=model._meta.label):
                response = self.client.get(self._admin_url(model, "changelist"))
                self.assertEqual(response.status_code, 200)

                if model_admin.search_fields:
                    response = self.client.get(self._admin_url(model, "changelist"), {"q": "shop"})
                    self.assertEqual(response.status_code, 200)

                if model_admin.has_add_permission(response.wsgi_request):
                    response = self.client.get(self._admin_url(model, "add"))
                    self.assertEqual(response.status_code, 200)

    def test_change_and_history_pages(self):
        for model in admin.site._registry:
            obj = model._default_manager.first()
            if obj is None:
                continue
            with self.subTest(model=model._meta.label):
                response = self.client.get(self._admin_url(model, "change", obj.pk))
                self.assertEqual(response.status_code, 200)

                response = self.client.get(self._admin_url(model, "history", obj.pk))
                self.assertEqual(response.status_code, 200)

    def test_admin_requires_staff(self):
        self.client.logout()

        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 302)
