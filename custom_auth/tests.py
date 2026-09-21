import datetime
import io
import json
import urllib.error
import urllib.parse
from unittest import mock, skipUnless

import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from custom_auth import billing_views, line
from custom_auth.models import Group, LineAccount, UserGroup


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


@skipUnless(settings.BILLING_ENABLED, "ONPREMISE_MODE では課金機能が無効なため対象外")
class PlanResolutionTests(TestCase):
    """無償付与と Stripe 契約から、実際に適用されるプランが決まることを確認する。"""

    def setUp(self):
        self.group = Group.objects.create(name="shop", name_jp="店舗")

    def test_defaults_to_free_plan(self):
        self.assertEqual(self.group.plan_key, "")
        self.assertEqual(self.group.plan["name"], "Free")
        self.assertEqual(self.group.max_members, settings.FREE_PLAN["max_members"])

    def test_active_subscription_applies_its_plan(self):
        self.group.stripe_plan = "standard"
        self.group.stripe_status = "active"

        self.assertEqual(self.group.plan_key, "standard")
        self.assertEqual(self.group.max_members, settings.STRIPE_PLANS["standard"]["max_members"])

    def test_inactive_subscription_falls_back_to_free(self):
        self.group.stripe_plan = "standard"
        self.group.stripe_status = "canceled"

        self.assertFalse(self.group.has_active_subscription)
        self.assertEqual(self.group.plan_key, "")

    def test_past_due_still_grants_access(self):
        self.group.stripe_plan = "standard"
        self.group.stripe_status = "past_due"

        self.assertTrue(self.group.has_active_subscription)
        self.assertEqual(self.group.plan_key, "standard")

    def test_free_grant_applies_without_subscription(self):
        self.group.free_plan = "pro1"

        self.assertTrue(self.group.is_free_granted)
        self.assertEqual(self.group.plan_key, "pro1")

    def test_expired_free_grant_is_ignored(self):
        self.group.free_plan = "pro1"
        self.group.membership_expired_at = timezone.now() - datetime.timedelta(days=1)

        self.assertFalse(self.group.is_free_granted)
        self.assertEqual(self.group.plan_key, "")

    def test_unlimited_free_grant_has_no_member_cap(self):
        self.group.free_plan = "unlimited"

        self.assertIsNone(self.group.max_members)
        self.assertTrue(self.group.can_add_member(10000))

    def test_higher_of_free_grant_and_subscription_wins(self):
        self.group.free_plan = "standard"
        self.group.stripe_plan = "pro2"
        self.group.stripe_status = "active"

        self.assertEqual(self.group.plan_key, "pro2")

        self.group.free_plan = "unlimited"
        self.assertEqual(self.group.plan_key, "unlimited")


@skipUnless(settings.BILLING_ENABLED, "ONPREMISE_MODE では課金機能が無効なため対象外")
class MemberLimitTests(TestCase):
    """プランの人数上限がメンバー登録時に効くことを確認する。"""

    def setUp(self):
        user_model = get_user_model()
        self.admin_user = user_model.objects.create(
            username="admin", username_jp="管理者", email="admin@example.com", is_active=True,
        )
        self.group = Group.objects.create(name="shop", name_jp="店舗")
        UserGroup.objects.create(user=self.admin_user, group=self.group, is_admin=True)
        self.url = reverse("custom_auth_group:register_member", args=[self.group.id])

    def _fill_to_limit(self, limit):
        user_model = get_user_model()
        while self.group.member_count < limit:
            index = self.group.member_count
            user = user_model.objects.create(
                username=f"filler{index}",
                username_jp=f"埋め{index}",
                email=f"filler{index}@example.com",
                is_active=True,
            )
            UserGroup.objects.create(user=user, group=self.group)

    def test_registration_page_is_open_below_the_limit(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["limit_reached"])

    def test_registration_is_blocked_at_the_limit(self):
        self._fill_to_limit(settings.FREE_PLAN["max_members"])
        self.client.force_login(self.admin_user)

        response = self.client.get(self.url)

        self.assertTrue(response.context["limit_reached"])
        self.assertContains(response, "上限")

    def test_post_is_rejected_at_the_limit(self):
        self._fill_to_limit(settings.FREE_PLAN["max_members"])
        self.client.force_login(self.admin_user)
        before = self.group.member_count

        response = self.client.post(self.url, {
            "username": "newbie",
            "username_jp": "新人",
            "display_name": "新人",
            "email": "newbie@example.com",
            "password1": "verysecret-pass-1",
            "password2": "verysecret-pass-1",
        })

        self.assertTrue(response.context["limit_reached"])
        self.assertEqual(self.group.member_count, before)
        self.assertFalse(get_user_model().objects.filter(username="newbie").exists())

    def test_upgraded_plan_raises_the_limit(self):
        self._fill_to_limit(settings.FREE_PLAN["max_members"])
        self.group.stripe_plan = "standard"
        self.group.stripe_status = "active"
        self.group.save()
        self.client.force_login(self.admin_user)

        response = self.client.get(self.url)

        self.assertFalse(response.context["limit_reached"])


TEST_PLANS = {
    "standard": {
        "name": "Standard", "price_id": "price_standard", "amount": 1980,
        "max_members": 30, "rank": 10, "recommended": True,
    },
    "pro1": {
        "name": "Pro 1", "price_id": "price_pro1", "amount": 4980,
        "max_members": 100, "rank": 20, "recommended": False,
    },
}

# 2026-11-01 09:00 JST
PERIOD_END_TS = 1793491200


def fake_subscription(status="active", price_id="price_standard", cancel_at_period_end=False,
                      sub_id="sub_123", customer="cus_123", period_end=PERIOD_END_TS):
    """Stripe の Subscription オブジェクトを模したレスポンス。"""
    return {
        "id": sub_id,
        "status": status,
        "customer": customer,
        "cancel_at_period_end": cancel_at_period_end,
        "metadata": {},
        # API 2025-03-31 以降 current_period_end は item 側にある
        "items": {"data": [{
            "id": "si_1",
            "price": {"id": price_id},
            "current_period_end": period_end,
        }]},
    }


@skipUnless(settings.BILLING_ENABLED, "ONPREMISE_MODE では課金機能が無効なため対象外")
@override_settings(STRIPE_PLANS=TEST_PLANS, STRIPE_SECRET_KEY="sk_test_dummy")
class SubscriptionSyncTests(TestCase):
    """Stripe のレスポンスを Group にキャッシュする処理。"""

    def setUp(self):
        self.group = Group.objects.create(name="shop", name_jp="店舗")

    def test_sync_stores_plan_status_and_period_end(self):
        billing_views._sync_subscription(self.group, fake_subscription())

        self.group.refresh_from_db()
        self.assertEqual(self.group.stripe_subscription_id, "sub_123")
        self.assertEqual(self.group.stripe_plan, "standard")
        self.assertEqual(self.group.stripe_status, "active")
        self.assertFalse(self.group.stripe_cancel_at_period_end)
        self.assertIsNotNone(self.group.stripe_current_period_end)
        self.assertEqual(self.group.max_members, 30)

    def test_period_end_is_read_from_subscription_items(self):
        billing_views._sync_subscription(self.group, fake_subscription())

        expected = billing_views._to_datetime(PERIOD_END_TS)
        self.assertEqual(self.group.stripe_current_period_end, expected)
        # JST に変換されていること(UTC のままなら 00:00 になる)
        self.assertEqual(expected.hour, 9)

    def test_cancelled_subscription_clears_the_plan(self):
        billing_views._sync_subscription(self.group, fake_subscription())
        billing_views._sync_subscription(self.group, fake_subscription(status="canceled"))

        self.group.refresh_from_db()
        self.assertEqual(self.group.stripe_plan, "")
        self.assertFalse(self.group.has_active_subscription)
        self.assertEqual(self.group.max_members, settings.FREE_PLAN["max_members"])

    def test_cancel_at_period_end_is_recorded(self):
        billing_views._sync_subscription(self.group, fake_subscription(cancel_at_period_end=True))

        self.group.refresh_from_db()
        self.assertTrue(self.group.stripe_cancel_at_period_end)
        # 期間末までは利用できる
        self.assertTrue(self.group.has_active_subscription)

    def test_unknown_price_id_falls_back_to_free(self):
        billing_views._sync_subscription(self.group, fake_subscription(price_id="price_unknown"))

        self.assertEqual(self.group.stripe_plan, "")

    def test_subscription_id_is_read_from_new_invoice_shape(self):
        invoice = {"parent": {"subscription_details": {"subscription": "sub_abc"}}}

        self.assertEqual(billing_views._subscription_id_from_invoice(invoice), "sub_abc")

    def test_subscription_id_is_read_from_legacy_invoice_shape(self):
        invoice = {"subscription": "sub_legacy"}

        self.assertEqual(billing_views._subscription_id_from_invoice(invoice), "sub_legacy")


@override_settings(STRIPE_PLANS=TEST_PLANS, STRIPE_SECRET_KEY="sk_test_dummy",
                   SITE_URL="http://testserver")
@skipUnless(settings.BILLING_ENABLED, "ONPREMISE_MODE では課金機能が無効なため対象外")
class BillingViewTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin_user = user_model.objects.create(
            username="admin", username_jp="管理者", email="admin@example.com", is_active=True,
        )
        self.member = user_model.objects.create(
            username="member", username_jp="メンバー", email="member@example.com", is_active=True,
        )
        self.group = Group.objects.create(name="shop", name_jp="店舗")
        UserGroup.objects.create(user=self.admin_user, group=self.group, is_admin=True)
        UserGroup.objects.create(user=self.member, group=self.group)

    def _subscribe(self, plan="standard", status="active", cancel_at_period_end=False):
        self.group.stripe_customer_id = "cus_123"
        self.group.stripe_subscription_id = "sub_123"
        self.group.stripe_plan = plan
        self.group.stripe_status = status
        self.group.stripe_cancel_at_period_end = cancel_at_period_end
        self.group.save()

    def test_billing_page_requires_group_admin(self):
        self.client.force_login(self.member)

        response = self.client.get(reverse("custom_auth_group:billing", args=[self.group.id]))

        self.assertTemplateUsed(response, "error.html")

    def test_billing_page_shows_free_plan_when_unsubscribed(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("custom_auth_group:billing", args=[self.group.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_plan_key"], "")
        self.assertFalse(response.context["has_active_subscription"])
        self.assertContains(response, "Standard")

    @mock.patch("stripe.Subscription.retrieve")
    def test_billing_page_refreshes_state_from_stripe(self, retrieve):
        retrieve.return_value = fake_subscription(price_id="price_pro1")
        self._subscribe(plan="standard")
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("custom_auth_group:billing", args=[self.group.id]))

        retrieve.assert_called_once_with("sub_123")
        self.group.refresh_from_db()
        self.assertEqual(self.group.stripe_plan, "pro1")
        self.assertEqual(response.context["current_plan_key"], "pro1")

    @mock.patch("stripe.Subscription.retrieve", side_effect=stripe.error.APIConnectionError("down"))
    def test_billing_page_survives_stripe_outage(self, _retrieve):
        self._subscribe(plan="standard")
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("custom_auth_group:billing", args=[self.group.id]))

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["error"])
        # 最後に同期した内容で表示を続ける
        self.assertEqual(response.context["current_plan_key"], "standard")

    def test_downgrade_is_blocked_when_members_exceed_the_target_plan(self):
        user_model = get_user_model()
        for index in range(31):
            user = user_model.objects.create(
                username=f"filler{index}", username_jp=f"埋め{index}",
                email=f"filler{index}@example.com", is_active=True,
            )
            UserGroup.objects.create(user=user, group=self.group)
        self._subscribe(plan="pro1")
        self.client.force_login(self.admin_user)

        with mock.patch("stripe.Subscription.retrieve", return_value=fake_subscription(price_id="price_pro1")):
            response = self.client.get(reverse("custom_auth_group:billing", args=[self.group.id]))

        standard = next(plan for plan in response.context["plans"] if plan["key"] == "standard")
        self.assertIsNotNone(standard["blocked_reason"])

    @mock.patch("stripe.checkout.Session.create")
    def test_checkout_is_skipped_when_already_subscribed(self, create):
        self._subscribe()
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("custom_auth_group:billing_checkout", args=[self.group.id]),
            {"plan": "pro1"},
        )

        create.assert_not_called()
        self.assertRedirects(
            response, reverse("custom_auth_group:billing", args=[self.group.id]),
            fetch_redirect_response=False,
        )

    @mock.patch("stripe.checkout.Session.create")
    @mock.patch("stripe.Customer.create")
    def test_checkout_creates_customer_and_session(self, customer_create, session_create):
        customer_create.return_value = mock.Mock(id="cus_new")
        session_create.return_value = mock.Mock(url="https://checkout.stripe.com/c/pay/test")
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("custom_auth_group:billing_checkout", args=[self.group.id]),
            {"plan": "standard"},
        )

        self.group.refresh_from_db()
        self.assertEqual(self.group.stripe_customer_id, "cus_new")
        self.assertEqual(session_create.call_args.kwargs["mode"], "subscription")
        self.assertEqual(
            session_create.call_args.kwargs["line_items"],
            [{"price": "price_standard", "quantity": 1}],
        )
        self.assertEqual(response.status_code, 302)

    @mock.patch("stripe.Subscription.modify")
    @mock.patch("stripe.Subscription.retrieve")
    def test_change_plan_swaps_the_subscription_item(self, retrieve, modify):
        retrieve.return_value = fake_subscription(price_id="price_standard")
        modify.return_value = fake_subscription(price_id="price_pro1")
        self._subscribe(plan="standard")
        self.client.force_login(self.admin_user)

        self.client.post(
            reverse("custom_auth_group:billing_change_plan", args=[self.group.id]),
            {"plan": "pro1"},
        )

        self.assertEqual(
            modify.call_args.kwargs["items"],
            [{"id": "si_1", "price": "price_pro1"}],
        )
        self.assertEqual(modify.call_args.kwargs["proration_behavior"], "create_prorations")
        self.group.refresh_from_db()
        self.assertEqual(self.group.stripe_plan, "pro1")

    @mock.patch("stripe.Subscription.modify")
    def test_cancel_schedules_cancellation_at_period_end(self, modify):
        modify.return_value = fake_subscription(cancel_at_period_end=True)
        self._subscribe()
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("custom_auth_group:billing_cancel", args=[self.group.id]),
        )

        modify.assert_called_once_with("sub_123", cancel_at_period_end=True)
        self.group.refresh_from_db()
        self.assertTrue(self.group.stripe_cancel_at_period_end)
        # 期間末までは有効なまま
        self.assertTrue(self.group.has_active_subscription)
        self.assertTemplateUsed(response, "done.html")

    @mock.patch("stripe.Subscription.modify")
    def test_resume_clears_the_scheduled_cancellation(self, modify):
        modify.return_value = fake_subscription(cancel_at_period_end=False)
        self._subscribe(cancel_at_period_end=True)
        self.client.force_login(self.admin_user)

        self.client.post(reverse("custom_auth_group:billing_resume", args=[self.group.id]))

        modify.assert_called_once_with("sub_123", cancel_at_period_end=False)
        self.group.refresh_from_db()
        self.assertFalse(self.group.stripe_cancel_at_period_end)

    @mock.patch("stripe.billing_portal.Session.create")
    def test_customer_portal_redirects_to_stripe(self, create):
        create.return_value = mock.Mock(url="https://billing.stripe.com/p/session/test")
        self._subscribe()
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("custom_auth_group:billing_portal", args=[self.group.id]),
        )

        self.assertEqual(create.call_args.kwargs["customer"], "cus_123")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://billing.stripe.com/p/session/test")

    def test_mutating_endpoints_reject_non_admin_members(self):
        self._subscribe()
        self.client.force_login(self.member)

        for name in ("billing_checkout", "billing_change_plan", "billing_cancel",
                     "billing_resume", "billing_portal"):
            with self.subTest(endpoint=name):
                response = self.client.post(
                    reverse(f"custom_auth_group:{name}", args=[self.group.id]), {"plan": "pro1"},
                )
                self.assertTemplateUsed(response, "error.html")


@override_settings(STRIPE_PLANS=TEST_PLANS, STRIPE_SECRET_KEY="sk_test_dummy",
                   STRIPE_WEBHOOK_SECRET="whsec_test")
@skipUnless(settings.BILLING_ENABLED, "ONPREMISE_MODE では課金機能が無効なため対象外")
class StripeWebhookTests(TestCase):
    def setUp(self):
        self.group = Group.objects.create(
            name="shop", name_jp="店舗", stripe_customer_id="cus_123",
        )
        self.url = reverse("stripe_webhook")

    def _post(self, event):
        with mock.patch("stripe.Webhook.construct_event", return_value=event):
            return self.client.post(self.url, data="{}", content_type="application/json")

    @override_settings(STRIPE_WEBHOOK_SECRET="")
    def test_webhook_is_rejected_without_a_secret(self):
        response = self.client.post(self.url, data="{}", content_type="application/json")

        self.assertEqual(response.status_code, 400)

    def test_webhook_rejects_an_invalid_signature(self):
        error = stripe.error.SignatureVerificationError("bad signature", "sig_header")
        with mock.patch("stripe.Webhook.construct_event", side_effect=error):
            response = self.client.post(self.url, data="{}", content_type="application/json")

        self.assertEqual(response.status_code, 400)

    @mock.patch("stripe.Subscription.retrieve")
    def test_checkout_completed_links_the_subscription(self, retrieve):
        retrieve.return_value = fake_subscription()
        event = {"type": "checkout.session.completed", "data": {"object": {
            "mode": "subscription",
            "subscription": "sub_123",
            "customer": "cus_123",
            "metadata": {"group_id": str(self.group.id), "plan": "standard"},
        }}}

        response = self._post(event)

        self.assertEqual(response.status_code, 200)
        self.group.refresh_from_db()
        self.assertEqual(self.group.stripe_subscription_id, "sub_123")
        self.assertEqual(self.group.stripe_plan, "standard")
        self.assertTrue(self.group.has_active_subscription)

    def test_subscription_updated_syncs_the_plan(self):
        self.group.stripe_subscription_id = "sub_123"
        self.group.stripe_plan = "standard"
        self.group.stripe_status = "active"
        self.group.save()
        event = {"type": "customer.subscription.updated",
                 "data": {"object": fake_subscription(price_id="price_pro1")}}

        self._post(event)

        self.group.refresh_from_db()
        self.assertEqual(self.group.stripe_plan, "pro1")

    def test_subscription_deleted_drops_the_group_to_free(self):
        self.group.stripe_subscription_id = "sub_123"
        self.group.stripe_plan = "standard"
        self.group.stripe_status = "active"
        self.group.save()
        event = {"type": "customer.subscription.deleted",
                 "data": {"object": fake_subscription(status="canceled")}}

        self._post(event)

        self.group.refresh_from_db()
        self.assertEqual(self.group.stripe_plan, "")
        self.assertEqual(self.group.max_members, settings.FREE_PLAN["max_members"])

    @mock.patch("stripe.Subscription.retrieve")
    def test_payment_failure_records_past_due(self, retrieve):
        retrieve.return_value = fake_subscription(status="past_due")
        self.group.stripe_subscription_id = "sub_123"
        self.group.stripe_plan = "standard"
        self.group.stripe_status = "active"
        self.group.save()
        event = {"type": "invoice.payment_failed", "data": {"object": {
            "customer": "cus_123",
            "parent": {"subscription_details": {"subscription": "sub_123"}},
        }}}

        self._post(event)

        self.group.refresh_from_db()
        self.assertEqual(self.group.stripe_status, "past_due")

    def test_unhandled_event_is_acknowledged(self):
        response = self._post({"type": "customer.created", "data": {"object": {"id": "cus_123"}}})

        self.assertEqual(response.status_code, 200)


LINE_SETTINGS = {
    "LINE_LOGIN_CHANNEL_ID": "1234567890",
    "LINE_LOGIN_CHANNEL_SECRET": "channel-secret",
    "SITE_URL": "https://example.com",
}


@override_settings(**LINE_SETTINGS)
class LineLoginViewTests(TestCase):
    """LINE ログイン。外部通信は custom_auth.line.fetch_profile を差し替えて止める"""

    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create(
            username="liner",
            username_jp="連携ユーザ",
            email="liner@example.com",
            is_active=True,
        )
        self.other_user = user_model.objects.create(
            username="other",
            username_jp="別ユーザ",
            email="other@example.com",
            is_active=True,
        )

    def _start(self, url_name):
        """認可 URL へのリダイレクトを踏んで、session に state を積ませる"""
        response = self.client.get(reverse(url_name))
        self.assertEqual(response.status_code, 302)
        return self.client.session["line_oauth_state"]

    def _callback(self, state, code="auth-code"):
        return self.client.get(reverse("custom_auth_line:callback"),
                               {"code": code, "state": state})

    def test_login_start_redirects_to_line_with_state(self):
        response = self.client.get(reverse("custom_auth_line:login"))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(line.AUTHORIZATION_URL))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(response["Location"]).query)
        self.assertEqual(query["client_id"], ["1234567890"])
        self.assertEqual(query["redirect_uri"], ["https://example.com/line/callback/"])
        self.assertEqual(query["state"], [self.client.session["line_oauth_state"]])
        self.assertEqual(query["nonce"], [self.client.session["line_oauth_nonce"]])

    @override_settings(LINE_LOGIN_CHANNEL_ID="", LINE_LOGIN_CHANNEL_SECRET="")
    def test_login_start_is_refused_when_not_configured(self):
        response = self.client.get(reverse("custom_auth_line:login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "LINEログインは利用できません")

    @override_settings(LINE_LOGIN_CHANNEL_ID="", LINE_LOGIN_CHANNEL_SECRET="")
    def test_login_page_hides_the_button_when_not_configured(self):
        response = self.client.get(reverse("login"))

        self.assertNotContains(response, "LINEでログイン")

    def test_login_page_shows_the_button_when_configured(self):
        response = self.client.get(reverse("login"))

        self.assertContains(response, "LINEでログイン")

    @mock.patch("custom_auth.line.fetch_profile")
    def test_linked_account_can_log_in(self, fetch_profile):
        LineAccount.objects.create(user=self.user, line_user_id="U0001")
        fetch_profile.return_value = {
            "line_user_id": "U0001", "display_name": "新しい名前", "picture_url": "",
        }
        state = self._start("custom_auth_line:login")

        response = self._callback(state)

        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.id))
        account = LineAccount.objects.get(user=self.user)
        self.assertEqual(account.display_name, "新しい名前")
        self.assertIsNotNone(account.last_login_at)

    @mock.patch("custom_auth.line.fetch_profile")
    def test_unlinked_account_is_not_signed_up(self, fetch_profile):
        fetch_profile.return_value = {
            "line_user_id": "U-unknown", "display_name": "未連携", "picture_url": "",
        }
        state = self._start("custom_auth_line:login")

        response = self._callback(state)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "連携されていません")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(get_user_model().objects.count(), 2)

    @mock.patch("custom_auth.line.fetch_profile")
    def test_inactive_user_cannot_log_in(self, fetch_profile):
        self.user.is_active = False
        self.user.save()
        LineAccount.objects.create(user=self.user, line_user_id="U0001")
        fetch_profile.return_value = {
            "line_user_id": "U0001", "display_name": "", "picture_url": "",
        }
        state = self._start("custom_auth_line:login")

        response = self._callback(state)

        self.assertContains(response, "有効化されていません")
        self.assertNotIn("_auth_user_id", self.client.session)

    @mock.patch("custom_auth.line.fetch_profile")
    def test_callback_rejects_a_mismatched_state(self, fetch_profile):
        LineAccount.objects.create(user=self.user, line_user_id="U0001")
        self._start("custom_auth_line:login")

        response = self._callback("forged-state")

        self.assertContains(response, "セッションが無効です")
        self.assertNotIn("_auth_user_id", self.client.session)
        fetch_profile.assert_not_called()

    @mock.patch("custom_auth.line.fetch_profile")
    def test_state_cannot_be_replayed(self, fetch_profile):
        LineAccount.objects.create(user=self.user, line_user_id="U0001")
        fetch_profile.return_value = {
            "line_user_id": "U0001", "display_name": "", "picture_url": "",
        }
        state = self._start("custom_auth_line:login")
        self._callback(state)
        self.client.logout()

        response = self._callback(state)

        self.assertContains(response, "セッションが無効です")
        self.assertNotIn("_auth_user_id", self.client.session)

    @mock.patch("custom_auth.line.fetch_profile")
    def test_callback_reports_a_line_side_failure(self, fetch_profile):
        fetch_profile.side_effect = line.LineLoginError("LINEとの通信に失敗しました。")
        state = self._start("custom_auth_line:login")

        response = self._callback(state)

        self.assertContains(response, "LINEとの通信に失敗しました。")

    def test_callback_reports_a_cancelled_consent(self):
        state = self._start("custom_auth_line:login")

        response = self.client.get(reverse("custom_auth_line:callback"),
                                   {"error": "access_denied", "state": state})

        self.assertContains(response, "中断されました")

    @mock.patch("custom_auth.line.fetch_profile")
    def test_logged_in_user_can_link_an_account(self, fetch_profile):
        self.client.force_login(self.user)
        fetch_profile.return_value = {
            "line_user_id": "U0001", "display_name": "たろう", "picture_url": "https://img/1",
        }
        state = self._start("custom_auth_line:link")

        response = self._callback(state)

        self.assertRedirects(response, reverse("custom_auth:line_account"))
        account = LineAccount.objects.get(user=self.user)
        self.assertEqual(account.line_user_id, "U0001")
        self.assertEqual(account.display_name, "たろう")

    @mock.patch("custom_auth.line.fetch_profile")
    def test_line_account_cannot_be_linked_to_two_users(self, fetch_profile):
        LineAccount.objects.create(user=self.other_user, line_user_id="U0001")
        self.client.force_login(self.user)
        fetch_profile.return_value = {
            "line_user_id": "U0001", "display_name": "", "picture_url": "",
        }
        state = self._start("custom_auth_line:link")

        response = self._callback(state)

        self.assertContains(response, "別のユーザに連携済み")
        self.assertFalse(LineAccount.objects.filter(user=self.user).exists())

    def test_link_requires_login(self):
        response = self.client.get(reverse("custom_auth_line:link"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_unlink_removes_the_link(self):
        LineAccount.objects.create(user=self.user, line_user_id="U0001")
        self.client.force_login(self.user)

        response = self.client.post(reverse("custom_auth_line:unlink"))

        self.assertRedirects(response, reverse("custom_auth:line_account"))
        self.assertFalse(LineAccount.objects.filter(user=self.user).exists())

    def test_unlink_rejects_get(self):
        LineAccount.objects.create(user=self.user, line_user_id="U0001")
        self.client.force_login(self.user)

        response = self.client.get(reverse("custom_auth_line:unlink"))

        self.assertEqual(response.status_code, 405)
        self.assertTrue(LineAccount.objects.filter(user=self.user).exists())

    def test_account_page_shows_the_link_status(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("custom_auth:line_account"))

        self.assertContains(response, "まだ連携されていません")

        LineAccount.objects.create(user=self.user, line_user_id="U0001", display_name="たろう")
        response = self.client.get(reverse("custom_auth:line_account"))

        self.assertContains(response, "連携済み")
        self.assertContains(response, "たろう")


@override_settings(**LINE_SETTINGS)
class LineClientTests(TestCase):
    """LINE の API を叩く部分。urlopen を差し替えてネットワークには出ない"""

    def _response(self, payload):
        response = mock.MagicMock()
        response.read.return_value = json.dumps(payload).encode()
        response.__enter__.return_value = response
        return response

    @mock.patch("urllib.request.urlopen")
    def test_fetch_profile_exchanges_the_code_and_verifies_the_id_token(self, urlopen):
        urlopen.side_effect = [
            self._response({"access_token": "at", "id_token": "it"}),
            self._response({"sub": "U0001", "name": "たろう", "picture": "https://img/1"}),
        ]

        profile = line.fetch_profile("auth-code", "nonce-value")

        self.assertEqual(profile, {
            "line_user_id": "U0001", "display_name": "たろう", "picture_url": "https://img/1",
        })
        token_request, verify_request = (call.args[0] for call in urlopen.call_args_list)
        self.assertEqual(token_request.full_url, line.TOKEN_URL)
        token_body = urllib.parse.parse_qs(token_request.data.decode())
        self.assertEqual(token_body["code"], ["auth-code"])
        self.assertEqual(token_body["client_secret"], ["channel-secret"])
        self.assertEqual(token_body["redirect_uri"], ["https://example.com/line/callback/"])
        self.assertEqual(verify_request.full_url, line.VERIFY_URL)
        verify_body = urllib.parse.parse_qs(verify_request.data.decode())
        self.assertEqual(verify_body["id_token"], ["it"])
        # nonce は LINE 側で ID トークンと突き合わせてもらう
        self.assertEqual(verify_body["nonce"], ["nonce-value"])

    @mock.patch("urllib.request.urlopen")
    def test_http_error_becomes_a_line_login_error(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError(
            line.TOKEN_URL, 400, "Bad Request", {}, io.BytesIO(b'{"error":"invalid_grant"}')
        )

        with self.assertRaises(line.LineLoginError):
            line.fetch_profile("auth-code", "nonce-value")

    @mock.patch("urllib.request.urlopen")
    def test_a_token_response_without_id_token_is_an_error(self, urlopen):
        urlopen.return_value = self._response({"access_token": "at"})

        with self.assertRaises(line.LineLoginError):
            line.fetch_profile("auth-code", "nonce-value")
