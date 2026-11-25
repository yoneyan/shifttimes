from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from custom_auth.form import GroupAddForm, ProfileEditForm
from custom_auth.models import Group, UserGroup

GROUP_DATA = {
    "name_jp": "テスト店舗",
    "name": "test-shop",
    "postcode": "100-0001",
    "address_jp": "東京都千代田区",
    "address": "Chiyoda, Tokyo",
    "phone": "03-0000-0000",
}


class ProfileViewTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create(
            username="alice",
            username_jp="アリス",
            display_name="Alice",
            email="alice@example.com",
            is_active=True,
        )
        self.other = user_model.objects.create(
            username="bob",
            username_jp="ボブ",
            email="bob@example.com",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.url = reverse("custom_auth:edit_profile")

    def test_edit_profile_saves_username_and_username_jp(self):
        response = self.client.post(self.url, {
            "username": "alice2",
            "username_jp": "アリス2",
            "display_name": "Alice 2",
        })

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "done.html")
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "alice2")
        self.assertEqual(self.user.username_jp, "アリス2")
        self.assertEqual(self.user.display_name, "Alice 2")

    def test_edit_profile_keeping_own_names_is_allowed(self):
        response = self.client.post(self.url, {
            "username": "alice",
            "username_jp": "アリス",
            "display_name": "New Display",
        })

        self.assertTemplateUsed(response, "done.html")
        self.user.refresh_from_db()
        self.assertEqual(self.user.display_name, "New Display")

    def test_edit_profile_duplicate_username_shows_form_error(self):
        response = self.client.post(self.url, {
            "username": "bob",
            "username_jp": "ボブ",
            "display_name": "Alice",
        })

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "user/edit_profile.html")
        form = response.context["form"]
        self.assertIn("username", form.errors)
        self.assertIn("username_jp", form.errors)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "alice")
        self.assertEqual(self.user.username_jp, "アリス")

    def test_edit_profile_save_failure_shows_form_error(self):
        # 検証後に他ユーザが同じ名前を取った場合(IntegrityError)も 500 にしない
        with mock.patch.object(ProfileEditForm, "_check_unique"):
            response = self.client.post(self.url, {
                "username": "bob",
                "username_jp": "アリス",
                "display_name": "Alice",
            })

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "user/edit_profile.html")
        self.assertTrue(response.context["form"].non_field_errors())
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "alice")

    def test_change_email_duplicate_shows_form_error(self):
        response = self.client.post(reverse("custom_auth:email_change"), {
            "email": "bob@example.com",
        })

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "user/change_email.html")
        self.assertIn("email", response.context["form"].errors)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "alice@example.com")
        self.assertTrue(self.user.is_active)


class GroupViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(
            username="owner",
            username_jp="オーナー",
            email="owner@example.com",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.url = reverse("custom_auth_group:add")

    def test_group_add_form_rejects_disagree(self):
        form = GroupAddForm(data={**GROUP_DATA, "agree": "False"})

        self.assertFalse(form.is_valid())
        self.assertIn("agree", form.errors)

    def test_group_add_form_accepts_agree(self):
        form = GroupAddForm(data={**GROUP_DATA, "agree": "True"})

        self.assertTrue(form.is_valid(), form.errors)
        self.assertIs(form.cleaned_data["agree"], True)

    def test_add_group_disagree_does_not_create_group(self):
        response = self.client.post(self.url, {**GROUP_DATA, "agree": "False"})

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "group/add.html")
        self.assertContains(response, "規約に同意されていません")
        self.assertFalse(Group.objects.filter(name="test-shop").exists())

    def test_add_group_agree_creates_group(self):
        response = self.client.post(self.url, {**GROUP_DATA, "agree": "True"})

        self.assertTemplateUsed(response, "done.html")
        group = Group.objects.get(name="test-shop")
        self.assertTrue(UserGroup.objects.filter(user=self.user, group=group, is_admin=True).exists())

    def test_add_group_page_renders_agree_choices(self):
        response = self.client.get(self.url)

        self.assertContains(response, '<option value="False" selected>同意しない</option>', html=True)
        self.assertContains(response, '<option value="True">同意する</option>', html=True)
