from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from custom_auth.models import Group, UserGroup


class GroupMemberViewTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.member = user_model.objects.create(
            username="member",
            username_jp="メンバー",
            email="member@example.com",
            is_active=True,
        )
        self.admin_user = user_model.objects.create(
            username="admin",
            username_jp="管理者",
            email="admin@example.com",
            is_active=True,
        )
        self.outsider = user_model.objects.create(
            username="outsider",
            username_jp="部外者",
            email="outsider@example.com",
            is_active=True,
        )
        self.group = Group.objects.create(name="shop", name_jp="店舗")
        self.other_group = Group.objects.create(name="other-shop", name_jp="別店舗")
        UserGroup.objects.create(user=self.member, group=self.group)
        UserGroup.objects.create(user=self.admin_user, group=self.group, is_admin=True)
        UserGroup.objects.create(user=self.outsider, group=self.other_group)

    def test_members_page_lists_all_group_members(self):
        self.client.force_login(self.member)

        response = self.client.get(reverse("custom_auth_group:members", args=[self.group.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "メンバー")
        self.assertContains(response, "管理者")
        self.assertNotContains(response, "部外者")
        self.assertEqual(response.context["member_count"], 2)
        self.assertEqual(response.context["admin_count"], 1)

    def test_members_page_requires_group_membership(self):
        self.client.force_login(self.outsider)

        response = self.client.get(reverse("custom_auth_group:members", args=[self.group.id]))

        self.assertTemplateUsed(response, "error.html")
        self.assertNotContains(response, "member@example.com")

    def test_email_is_visible_only_to_administrators(self):
        url = reverse("custom_auth_group:members", args=[self.group.id])

        self.client.force_login(self.member)
        member_response = self.client.get(url)
        self.client.force_login(self.admin_user)
        admin_response = self.client.get(url)

        self.assertNotContains(member_response, "member@example.com")
        self.assertContains(admin_response, "member@example.com")

    def test_members_page_requires_login(self):
        response = self.client.get(reverse("custom_auth_group:members", args=[self.group.id]))

        self.assertEqual(response.status_code, 302)


class GroupPermissionViewTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.member = user_model.objects.create(
            username="member",
            username_jp="メンバー",
            email="member@example.com",
            is_active=True,
        )
        self.admin_user = user_model.objects.create(
            username="admin",
            username_jp="管理者",
            email="admin@example.com",
            is_active=True,
        )
        self.group = Group.objects.create(name="shop", name_jp="店舗")
        self.member_group = UserGroup.objects.create(user=self.member, group=self.group)
        UserGroup.objects.create(user=self.admin_user, group=self.group, is_admin=True)

    def test_administrator_can_promote_member(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("custom_auth_group:permission", args=[self.group.id]),
            {"id": self.member_group.id, "admin": ""},
        )

        self.assertEqual(response.status_code, 302)
        self.member_group.refresh_from_db()
        self.assertTrue(self.member_group.is_admin)

    def test_member_cannot_promote_member(self):
        self.client.force_login(self.member)

        self.client.post(
            reverse("custom_auth_group:permission", args=[self.group.id]),
            {"id": self.member_group.id, "admin": ""},
        )

        self.member_group.refresh_from_db()
        self.assertFalse(self.member_group.is_admin)
