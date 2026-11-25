"""ログイン・アカウント有効化・プロフィール・2 段階認証・グループ画面のテスト。"""

import datetime
import uuid

import pyotp
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from custom_auth.models import Group, TOTPDevice, UserActivateToken, UserGroup

PASSWORD = "Sh1ftTimes-Passw0rd"


def create_user(username, *, is_active=True, password=PASSWORD, **extra):
    """パスワード付きのユーザを作る（create_user と違い有効化メールを送らない）。"""
    user = get_user_model()(
        username=username,
        username_jp=extra.pop("username_jp", f"{username}_jp"),
        email=extra.pop("email", f"{username}@example.com"),
        is_active=is_active,
        **extra,
    )
    user.set_password(password)
    user.save()
    return user


class LoginViewTests(TestCase):
    def setUp(self):
        self.user = create_user("alice")

    def test_login_page_renders(self):
        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "login.html")

    def test_login_with_valid_credentials(self):
        response = self.client.post(reverse("login"), {"username": "alice", "password": PASSWORD})

        self.assertRedirects(response, "/")
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.id)

    def test_login_with_wrong_password_is_rejected(self):
        response = self.client.post(reverse("login"), {"username": "alice", "password": "wrong-password"})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertTrue(response.context["form"].errors)

    def test_inactive_user_cannot_login(self):
        create_user("bob", is_active=False)

        response = self.client.post(reverse("login"), {"username": "bob", "password": PASSWORD})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_logout(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("logout"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "logout.html")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_logout_requires_login(self):
        response = self.client.get(reverse("logout"))

        self.assertEqual(response.status_code, 302)

    def test_top_page_shows_landing_for_anonymous_user(self):
        response = self.client.get(reverse("index"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "landing.html")

    def test_top_page_shows_dashboard_for_logged_in_user(self):
        group = Group.objects.create(name="shop", name_jp="店舗")
        UserGroup.objects.create(user=self.user, group=group, is_admin=True)
        self.client.force_login(self.user)

        response = self.client.get(reverse("index"))

        self.assertTemplateUsed(response, "index.html")
        self.assertTrue(response.context["has_admin_groups"])
        self.assertEqual([card["group"] for card in response.context["group_cards"]], [group])


class UserActivationTests(TestCase):
    def test_create_user_sends_activation_mail(self):
        user = get_user_model().objects.create_user(
            username="carol", username_jp="キャロル", email="carol@example.com", password=PASSWORD
        )

        self.assertFalse(user.is_active)
        token = UserActivateToken.objects.get(user=user)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["carol@example.com"])
        self.assertIn(f"/activate/{token.token}/", mail.outbox[0].body)

    def test_activation_link_activates_user(self):
        user = create_user("carol", is_active=False)
        token = UserActivateToken.objects.create(user=user)

        response = self.client.get(reverse("activate_user", args=[token.token]))

        self.assertContains(response, "アクティベーションが完了しました")
        user.refresh_from_db()
        token.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(token.is_used)

    def test_used_token_cannot_be_reused(self):
        user = create_user("carol", is_active=False)
        token = UserActivateToken.objects.create(user=user, is_used=True)

        response = self.client.get(reverse("activate_user", args=[token.token]))

        self.assertContains(response, "this token was used")
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_expired_token_is_rejected(self):
        user = create_user("carol", is_active=False)
        token = UserActivateToken.objects.create(
            user=user, expired_at=timezone.now() - datetime.timedelta(minutes=1)
        )

        response = self.client.get(reverse("activate_user", args=[token.token]))

        self.assertContains(response, "Token is not found")
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_unknown_token_is_rejected(self):
        response = self.client.get(reverse("activate_user", args=[uuid.uuid4()]))

        self.assertContains(response, "Token is not found")


class ProfileViewTests(TestCase):
    def setUp(self):
        self.user = create_user("alice", display_name="Alice")
        self.client.force_login(self.user)

    def test_profile_pages_require_login(self):
        self.client.logout()
        for name in (
            "custom_auth:index",
            "custom_auth:password_change",
            "custom_auth:email_change",
            "custom_auth:edit_profile",
            "custom_auth:list_two_auth",
            "custom_auth:add_two_auth",
        ):
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith(reverse("login")))

    def test_profile_page_renders(self):
        response = self.client.get(reverse("custom_auth:index"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "user/profile.html")

    def test_password_change(self):
        response = self.client.post(
            reverse("custom_auth:password_change"),
            {"old_password": PASSWORD, "new_password1": "N3w-Passw0rd!", "new_password2": "N3w-Passw0rd!"},
        )

        self.assertTemplateUsed(response, "done.html")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("N3w-Passw0rd!"))

    def test_password_change_requires_current_password(self):
        response = self.client.post(
            reverse("custom_auth:password_change"),
            {"old_password": "wrong", "new_password1": "N3w-Passw0rd!", "new_password2": "N3w-Passw0rd!"},
        )

        self.assertTemplateUsed(response, "user/change_password.html")
        self.assertIn("old_password", response.context["form"].errors)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_email_change_deactivates_user_until_confirmed(self):
        response = self.client.post(reverse("custom_auth:email_change"), {"email": "new@example.com"})

        self.assertTemplateUsed(response, "done.html")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "new@example.com")
        self.assertFalse(self.user.is_active)
        token = UserActivateToken.objects.get(user=self.user)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["new@example.com"])
        self.assertIn(f"/activate/{token.token}/", mail.outbox[0].body)

    def test_email_change_rejects_invalid_address(self):
        response = self.client.post(reverse("custom_auth:email_change"), {"email": "not-an-email"})

        self.assertTemplateUsed(response, "user/change_email.html")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "alice@example.com")
        self.assertEqual(len(mail.outbox), 0)

    def test_edit_profile_shows_current_values(self):
        response = self.client.get(reverse("custom_auth:edit_profile"))

        form = response.context["form"]
        self.assertEqual(form.initial["username"], "alice")
        self.assertEqual(form.initial["display_name"], "Alice")

    def test_edit_profile_updates_display_name(self):
        response = self.client.post(
            reverse("custom_auth:edit_profile"),
            {"username": "alice", "username_jp": "alice_jp", "display_name": "ありす"},
        )

        self.assertTemplateUsed(response, "done.html")
        self.user.refresh_from_db()
        self.assertEqual(self.user.display_name, "ありす")


class TwoAuthViewTests(TestCase):
    def setUp(self):
        self.user = create_user("alice")
        self.client.force_login(self.user)

    def test_add_page_shows_new_secret(self):
        response = self.client.get(reverse("custom_auth:add_two_auth"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["initial_check"])
        self.assertTrue(response.context["secret"])
        self.assertTrue(response.context["qr"])

    def test_register_device_with_valid_code(self):
        secret = pyotp.random_base32()

        response = self.client.post(
            reverse("custom_auth:add_two_auth"),
            {"id": "submit", "secret": secret, "title": "スマホ", "code": pyotp.TOTP(secret).now()},
        )

        self.assertRedirects(response, reverse("custom_auth:list_two_auth"))
        device = TOTPDevice.objects.get(user=self.user)
        self.assertEqual(device.title, "スマホ")
        self.assertEqual(device.secret, secret)
        self.assertTrue(TOTPDevice.objects.check_totp(user=self.user, code=pyotp.TOTP(secret).now()))

    def test_register_device_with_wrong_code_is_rejected(self):
        secret = pyotp.random_base32()
        wrong_code = str((int(pyotp.TOTP(secret).now()) + 1) % 1_000_000).zfill(6)

        response = self.client.post(
            reverse("custom_auth:add_two_auth"),
            {"id": "submit", "secret": secret, "title": "スマホ", "code": wrong_code},
        )

        self.assertEqual(response.context["error"], "コードが一致しません")
        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())

    def test_list_shows_only_own_devices(self):
        other = create_user("bob")
        TOTPDevice.objects.create_secret(user=self.user, title="mine", otp_secret=pyotp.random_base32())
        TOTPDevice.objects.create_secret(user=other, title="others", otp_secret=pyotp.random_base32())

        response = self.client.get(reverse("custom_auth:list_two_auth"))

        self.assertEqual([device.title for device in response.context["devices"]], ["mine"])

    def test_delete_own_device(self):
        TOTPDevice.objects.create_secret(user=self.user, title="mine", otp_secret=pyotp.random_base32())
        device = TOTPDevice.objects.get(user=self.user)

        self.client.post(reverse("custom_auth:list_two_auth"), {"id": "delete", "device_id": device.id})

        self.assertFalse(TOTPDevice.objects.filter(id=device.id).exists())

    def test_cannot_delete_other_users_device(self):
        other = create_user("bob")
        TOTPDevice.objects.create_secret(user=other, title="others", otp_secret=pyotp.random_base32())
        device = TOTPDevice.objects.get(user=other)

        self.client.post(reverse("custom_auth:list_two_auth"), {"id": "delete", "device_id": device.id})

        self.assertTrue(TOTPDevice.objects.filter(id=device.id).exists())


class GroupViewTests(TestCase):
    def setUp(self):
        self.admin_user = create_user("admin")
        self.member = create_user("member")
        self.outsider = create_user("outsider")
        self.group = Group.objects.create(name="shop", name_jp="店舗", postcode="100-0001")
        UserGroup.objects.create(user=self.admin_user, group=self.group, is_admin=True)
        UserGroup.objects.create(user=self.member, group=self.group)

    def _group_add_data(self, **overrides):
        data = {
            "agree": "True",
            "name_jp": "新店舗",
            "name": "new-shop",
            "postcode": "100-0002",
            "address_jp": "東京都千代田区",
            "address": "Chiyoda, Tokyo",
            "phone": "03-0000-0000",
        }
        data.update(overrides)
        return data

    def test_list_groups_marks_admin_groups(self):
        other_group = Group.objects.create(name="cafe", name_jp="カフェ")
        UserGroup.objects.create(user=self.member, group=other_group, is_admin=True)
        self.client.force_login(self.member)

        response = self.client.get(reverse("custom_auth_group:index"))

        administrator = {entry["data"].id: entry["administrator"] for entry in response.context["groups"]}
        self.assertEqual(administrator, {self.group.id: False, other_group.id: True})

    def test_list_single_group(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("custom_auth_group:list", args=[self.group.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["groups"][0]["data"], self.group)
        self.assertTrue(response.context["groups"][0]["administrator"])

    def test_add_group_makes_user_its_admin(self):
        self.client.force_login(self.outsider)

        response = self.client.post(reverse("custom_auth_group:add"), self._group_add_data())

        self.assertTemplateUsed(response, "done.html")
        group = Group.objects.get(name="new-shop")
        self.assertTrue(UserGroup.objects.get(user=self.outsider, group=group).is_admin)
        self.outsider.refresh_from_db()
        self.assertFalse(self.outsider.allow_group_add)

    def test_add_group_is_rejected_when_not_allowed(self):
        self.outsider.allow_group_add = False
        self.outsider.save()
        self.client.force_login(self.outsider)

        response = self.client.post(reverse("custom_auth_group:add"), self._group_add_data())

        self.assertEqual(response.context["error"], "グループの新規登録が申請不可能です")
        self.assertFalse(Group.objects.filter(name="new-shop").exists())

    def test_admin_can_edit_group(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("custom_auth_group:edit", args=[self.group.id]),
            {"postcode": "150-0001", "address_jp": "渋谷区", "address": "Shibuya", "phone": "03", "country": "Japan"},
        )

        self.assertTemplateUsed(response, "done.html")
        self.group.refresh_from_db()
        self.assertEqual(self.group.postcode, "150-0001")

    def test_member_cannot_edit_group(self):
        self.client.force_login(self.member)

        response = self.client.post(
            reverse("custom_auth_group:edit", args=[self.group.id]),
            {"postcode": "150-0001", "address_jp": "渋谷区", "address": "Shibuya", "phone": "03", "country": "Japan"},
        )

        self.assertTemplateUsed(response, "group/edit.html")
        self.group.refresh_from_db()
        self.assertEqual(self.group.postcode, "100-0001")

    def test_outsider_cannot_open_group_pages(self):
        self.client.force_login(self.outsider)
        for name in ("edit", "permission", "admin_home", "members"):
            with self.subTest(name=name):
                response = self.client.get(reverse(f"custom_auth_group:{name}", args=[self.group.id]))
                self.assertTemplateUsed(response, "error.html")

    def test_admin_home_is_only_for_admins(self):
        self.client.force_login(self.member)
        member_response = self.client.get(reverse("custom_auth_group:admin_home", args=[self.group.id]))
        self.client.force_login(self.admin_user)
        admin_response = self.client.get(reverse("custom_auth_group:admin_home", args=[self.group.id]))

        self.assertTemplateUsed(member_response, "error.html")
        self.assertTemplateUsed(admin_response, "group/admin.html")
        self.assertEqual(admin_response.context["member_count"], 2)


class GroupAddDuplicateNameTests(TransactionTestCase):
    # IntegrityError を握りつぶして画面にエラーを出す流れを確認する。
    # TestCase だとテスト全体を包むトランザクションが壊れて後続のクエリが失敗するため TransactionTestCase を使う

    def test_add_group_reports_duplicated_name(self):
        Group.objects.create(name="shop", name_jp="店舗")
        user = create_user("alice")
        self.client.force_login(user)

        response = self.client.post(
            reverse("custom_auth_group:add"),
            {
                "agree": "True",
                "name_jp": "新店舗",
                "name": "shop",
                "postcode": "100-0002",
                "address_jp": "東京都千代田区",
                "address": "Chiyoda, Tokyo",
                "phone": "03-0000-0000",
            },
        )

        self.assertIn("グループの登録に失敗しました", str(response.context["error"]))
        self.assertFalse(UserGroup.objects.filter(user=user).exists())
        user.refresh_from_db()
        self.assertTrue(user.allow_group_add)
