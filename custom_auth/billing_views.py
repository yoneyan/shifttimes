import datetime

import stripe
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from custom_auth.models import Group, get_plan_by_price_id

# Group にキャッシュする Stripe 由来のフィールド
SUBSCRIPTION_FIELDS = (
    "stripe_subscription_id",
    "stripe_plan",
    "stripe_status",
    "stripe_current_period_end",
    "stripe_cancel_at_period_end",
    "updated_at",
)


def _stripe_ready():
    """Stripe が利用可能なら API キーを適用して True を返す。

    モジュール読み込み時ではなく呼び出しのたびに設定するので、
    テストの override_settings や環境変数の後読み込みでも取り違えない。
    """
    if not settings.STRIPE_SECRET_KEY:
        return False
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return True


def _get_admin_group(request, group_id):
    ug = request.user.usergroup_set.filter(group_id=group_id, is_admin=True).select_related("group").first()
    return ug.group if ug else None


def _to_datetime(unix_timestamp):
    """Stripe の UNIX 時刻を Django の設定に合わせた datetime に変換する。"""
    if not unix_timestamp:
        return None
    value = datetime.datetime.fromtimestamp(unix_timestamp, tz=datetime.UTC)
    if settings.USE_TZ:
        return value
    # USE_TZ=False では naive datetime は TIME_ZONE のローカル時刻として扱われる
    return timezone.make_naive(value, timezone.get_default_timezone())


def _subscription_items(subscription):
    """サブスクリプションの明細行を返す。

    StripeObject は dict のサブクラスなので、属性アクセス(subscription.items)だと
    dict.items メソッドに衝突する。必ずキーアクセスで取り出す。
    """
    return (subscription.get("items") or {}).get("data") or []


def _current_period_end(subscription):
    """請求期間の終了日を取り出す。

    API バージョン 2025-03-31 以降、current_period_end は Subscription から
    SubscriptionItem 側へ移動した。新旧どちらの形でも拾えるようにしている。
    """
    ends = [item.get("current_period_end") for item in _subscription_items(subscription)]
    ends = [end for end in ends if end]
    if ends:
        return max(ends)
    return subscription.get("current_period_end")


def _subscription_price_id(subscription):
    items = _subscription_items(subscription)
    if not items:
        return ""
    return (items[0].get("price") or {}).get("id", "")


def _sync_subscription(group, subscription):
    """Stripe のサブスクリプションの内容を Group に反映して保存する。"""
    status = subscription.get("status", "")
    plan_key, _ = get_plan_by_price_id(_subscription_price_id(subscription))

    group.stripe_subscription_id = subscription.get("id") or None
    group.stripe_status = status
    group.stripe_current_period_end = _to_datetime(_current_period_end(subscription))
    group.stripe_cancel_at_period_end = bool(subscription.get("cancel_at_period_end"))
    if status in ("canceled", "incomplete_expired"):
        # 契約が終了したらプランを失効させ、無料プランの上限に戻す
        group.stripe_plan = ""
        group.stripe_cancel_at_period_end = False
    else:
        group.stripe_plan = plan_key
    group.updated_at = timezone.now()
    group.save(update_fields=list(SUBSCRIPTION_FIELDS))
    return group


def _refresh_subscription(group):
    """Stripe から最新の契約状態を取り込む。失敗時はエラーメッセージを返す。"""
    if not group.stripe_subscription_id or not _stripe_ready():
        return None
    try:
        subscription = stripe.Subscription.retrieve(group.stripe_subscription_id)
    except stripe.error.StripeError:
        return "Stripe から契約情報を取得できませんでした。以下は最後に同期した内容です。"
    _sync_subscription(group, subscription)
    return None


def _downgrade_blocked_message(group, plan):
    """現在のメンバー数が移行先プランの上限を超えているなら理由を返す。"""
    limit = plan["max_members"]
    if limit is None or group.member_count <= limit:
        return None
    return (
        f"現在のメンバー数({group.member_count}名)が{plan['name']}プランの上限({limit}名)を超えています。"
        "メンバーを減らしてから変更してください。"
    )


@login_required
def billing(request, group_id):
    group = _get_admin_group(request, group_id)
    if not group:
        return render(request, "error.html", {"text": "このグループにアクセスする権限がありません"})

    error = _refresh_subscription(group)

    current_plan = group.plan
    plans = [
        {
            **plan,
            "key": key,
            "is_current": key == group.plan_key,
            "is_upgrade": plan["rank"] > current_plan["rank"],
            "blocked_reason": _downgrade_blocked_message(group, plan),
        }
        for key, plan in settings.STRIPE_PLANS.items()
    ]

    context = {
        "group": group,
        "error": error,
        "plans": plans,
        "free_plan": settings.FREE_PLAN,
        "unlimited_plan": settings.UNLIMITED_PLAN,
        "current_plan": current_plan,
        "current_plan_key": group.plan_key,
        "member_count": group.member_count,
        "max_members": group.max_members,
        "is_free_granted": group.is_free_granted,
        "has_active_subscription": group.has_active_subscription,
        "stripe_enabled": bool(settings.STRIPE_SECRET_KEY),
        "stripe_publishable_key": settings.STRIPE_PUBLISHABLE_KEY,
        "contact_email": settings.CONTACT_EMAIL,
    }
    return render(request, "group/billing.html", context)


@login_required
def create_checkout_session(request, group_id):
    if request.method != "POST":
        return redirect(reverse("custom_auth_group:billing", args=[group_id]))

    group = _get_admin_group(request, group_id)
    if not group:
        return render(request, "error.html", {"text": "このグループにアクセスする権限がありません"})

    if group.has_active_subscription:
        # 二重契約を防ぐ。プラン変更は change_plan 側で行う
        return redirect(reverse("custom_auth_group:billing", args=[group_id]))

    plan_key = request.POST.get("plan", "standard")
    plan = settings.STRIPE_PLANS.get(plan_key)
    if not plan or not plan["price_id"]:
        return render(request, "error.html", {"text": "プランが見つかりません"})

    if not _stripe_ready():
        return render(request, "error.html", {"text": "Stripe APIキーが設定されていません"})

    blocked = _downgrade_blocked_message(group, plan)
    if blocked:
        return render(request, "error.html", {"text": blocked})

    # Stripe Customer を取得 or 作成
    if not group.stripe_customer_id:
        customer = stripe.Customer.create(
            name=group.name_jp,
            metadata={"group_id": group.id},
        )
        group.stripe_customer_id = customer.id
        group.save(update_fields=["stripe_customer_id"])

    success_url = (
        settings.SITE_URL
        + reverse("custom_auth_group:billing_success", args=[group_id])
        + "?session_id={CHECKOUT_SESSION_ID}"
    )
    cancel_url = settings.SITE_URL + reverse("custom_auth_group:billing", args=[group_id])

    session = stripe.checkout.Session.create(
        customer=group.stripe_customer_id,
        mode="subscription",
        line_items=[{"price": plan["price_id"], "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"group_id": group.id, "plan": plan_key},
        subscription_data={"metadata": {"group_id": group.id, "plan": plan_key}},
    )
    return redirect(session.url, permanent=False)


@login_required
def billing_success(request, group_id):
    group = _get_admin_group(request, group_id)
    if not group:
        return render(request, "error.html", {"text": "このグループにアクセスする権限がありません"})

    # 本来の契約確定は Webhook で行う。ここはユーザが戻ってきた時に
    # 画面へ即座に反映するための補助でしかないので、失敗しても致命ではない
    session_id = request.GET.get("session_id")
    if session_id and _stripe_ready():
        try:
            session = stripe.checkout.Session.retrieve(session_id, expand=["subscription"])
            subscription = session.get("subscription")
            if isinstance(subscription, str):
                subscription = stripe.Subscription.retrieve(subscription)
            if subscription:
                _sync_subscription(group, subscription)
        except stripe.error.StripeError:
            pass

    return render(request, "done.html", {"text": "サブスクリプションの登録が完了しました"})


@login_required
def change_plan(request, group_id):
    if request.method != "POST":
        return redirect(reverse("custom_auth_group:billing", args=[group_id]))

    group = _get_admin_group(request, group_id)
    if not group:
        return render(request, "error.html", {"text": "このグループにアクセスする権限がありません"})

    if not group.stripe_subscription_id or not group.has_active_subscription:
        return render(request, "error.html", {"text": "変更できるサブスクリプションがありません"})

    plan_key = request.POST.get("plan", "")
    plan = settings.STRIPE_PLANS.get(plan_key)
    if not plan or not plan["price_id"]:
        return render(request, "error.html", {"text": "プランが見つかりません"})

    if plan_key == group.stripe_plan:
        return render(request, "error.html", {"text": "すでにこのプランをご契約中です"})

    blocked = _downgrade_blocked_message(group, plan)
    if blocked:
        return render(request, "error.html", {"text": blocked})

    if not _stripe_ready():
        return render(request, "error.html", {"text": "Stripe APIキーが設定されていません"})

    try:
        subscription = stripe.Subscription.retrieve(group.stripe_subscription_id)
        items = _subscription_items(subscription)
        if not items:
            return render(request, "error.html", {"text": "サブスクリプションの明細が取得できませんでした"})
        updated = stripe.Subscription.modify(
            group.stripe_subscription_id,
            items=[{"id": items[0]["id"], "price": plan["price_id"]}],
            # 日割りの計算と差額請求は Stripe に任せる
            proration_behavior="create_prorations",
            cancel_at_period_end=False,
            metadata={"group_id": group.id, "plan": plan_key},
        )
    except stripe.error.StripeError:
        return render(request, "error.html", {"text": "プランの変更に失敗しました。時間をおいて再度お試しください。"})

    _sync_subscription(group, updated)
    return render(request, "done.html", {"text": f"プランを{plan['name']}に変更しました"})


@login_required
def cancel_subscription(request, group_id):
    if request.method != "POST":
        return redirect(reverse("custom_auth_group:billing", args=[group_id]))

    group = _get_admin_group(request, group_id)
    if not group or not group.stripe_subscription_id:
        return render(request, "error.html", {"text": "解約できるサブスクリプションがありません"})

    if not _stripe_ready():
        return render(request, "error.html", {"text": "Stripe APIキーが設定されていません"})

    try:
        # 即時解約ではなく期間末解約。支払い済みの期間は最後まで使える
        updated = stripe.Subscription.modify(group.stripe_subscription_id, cancel_at_period_end=True)
    except stripe.error.StripeError:
        return render(request, "error.html", {"text": "解約処理に失敗しました。時間をおいて再度お試しください。"})

    _sync_subscription(group, updated)
    return render(request, "done.html", {
        "text": "サブスクリプションの解約を受け付けました。現在の請求期間の終了日までご利用いただけます。",
    })


@login_required
def resume_subscription(request, group_id):
    """期間末解約を取り消して契約を継続する。"""
    if request.method != "POST":
        return redirect(reverse("custom_auth_group:billing", args=[group_id]))

    group = _get_admin_group(request, group_id)
    if not group or not group.stripe_subscription_id or not group.stripe_cancel_at_period_end:
        return render(request, "error.html", {"text": "継続できるサブスクリプションがありません"})

    if not _stripe_ready():
        return render(request, "error.html", {"text": "Stripe APIキーが設定されていません"})

    try:
        updated = stripe.Subscription.modify(group.stripe_subscription_id, cancel_at_period_end=False)
    except stripe.error.StripeError:
        return render(request, "error.html", {"text": "継続処理に失敗しました。時間をおいて再度お試しください。"})

    _sync_subscription(group, updated)
    return render(request, "done.html", {"text": "サブスクリプションの解約を取り消しました"})


@login_required
def customer_portal(request, group_id):
    """支払い方法の変更や請求書の閲覧を Stripe のカスタマーポータルに委譲する。"""
    if request.method != "POST":
        return redirect(reverse("custom_auth_group:billing", args=[group_id]))

    group = _get_admin_group(request, group_id)
    if not group:
        return render(request, "error.html", {"text": "このグループにアクセスする権限がありません"})

    if not group.stripe_customer_id:
        return render(request, "error.html", {"text": "お支払い情報がまだ登録されていません"})

    if not _stripe_ready():
        return render(request, "error.html", {"text": "Stripe APIキーが設定されていません"})

    try:
        session = stripe.billing_portal.Session.create(
            customer=group.stripe_customer_id,
            return_url=settings.SITE_URL + reverse("custom_auth_group:billing", args=[group_id]),
        )
    except stripe.error.StripeError:
        return render(request, "error.html", {
            "text": "カスタマーポータルを開けませんでした。時間をおいて再度お試しください。",
        })

    return redirect(session.url, permanent=False)


def _group_for_customer(customer_id):
    if not customer_id:
        return None
    return Group.objects.filter(stripe_customer_id=customer_id).first()


def _group_for_subscription(subscription):
    """Webhook のサブスクリプションから対象グループを特定する。"""
    group = Group.objects.filter(stripe_subscription_id=subscription.get("id")).first()
    if group:
        return group
    group_id = (subscription.get("metadata") or {}).get("group_id")
    if group_id:
        group = Group.objects.filter(id=group_id).first()
        if group:
            return group
    return _group_for_customer(subscription.get("customer"))


def _subscription_id_from_invoice(invoice):
    """請求書からサブスクリプション ID を取り出す。

    API バージョン 2025-03-31 以降 Invoice.subscription は廃止され、
    parent.subscription_details.subscription に移動している。
    """
    parent = invoice.get("parent") or {}
    details = parent.get("subscription_details") or {}
    subscription = details.get("subscription") or invoice.get("subscription")
    if isinstance(subscription, dict):
        return subscription.get("id")
    return subscription


def _handle_checkout_completed(session):
    if session.get("mode") != "subscription":
        return
    subscription_id = session.get("subscription")
    if not subscription_id or not _stripe_ready():
        return

    group = None
    group_id = (session.get("metadata") or {}).get("group_id")
    if group_id:
        group = Group.objects.filter(id=group_id).first()
    if not group:
        group = _group_for_customer(session.get("customer"))
    if not group:
        return

    try:
        subscription = stripe.Subscription.retrieve(subscription_id)
    except stripe.error.StripeError:
        return
    _sync_subscription(group, subscription)


def _handle_subscription_event(subscription):
    group = _group_for_subscription(subscription)
    if group:
        _sync_subscription(group, subscription)


def _handle_invoice_event(invoice):
    """支払いの成否で契約ステータスが変わるため、最新状態を取り直す。"""
    subscription_id = _subscription_id_from_invoice(invoice)
    if not subscription_id or not _stripe_ready():
        return

    group = Group.objects.filter(stripe_subscription_id=subscription_id).first()
    if not group:
        group = _group_for_customer(invoice.get("customer"))
    if not group:
        return

    try:
        subscription = stripe.Subscription.retrieve(subscription_id)
    except stripe.error.StripeError:
        return
    _sync_subscription(group, subscription)


@csrf_exempt
@require_POST
def stripe_webhook(request):
    if not settings.STRIPE_WEBHOOK_SECRET:
        return HttpResponse(status=400)

    try:
        event = stripe.Webhook.construct_event(
            request.body,
            request.META.get("HTTP_STRIPE_SIGNATURE", ""),
            settings.STRIPE_WEBHOOK_SECRET,
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)

    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        _handle_checkout_completed(obj)
    elif event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        _handle_subscription_event(obj)
    elif event_type in ("invoice.payment_succeeded", "invoice.payment_failed"):
        _handle_invoice_event(obj)

    return HttpResponse(status=200)
