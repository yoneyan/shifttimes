import stripe
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

stripe.api_key = settings.STRIPE_SECRET_KEY


def _get_admin_group(request, group_id):
    ug = request.user.usergroup_set.filter(group_id=group_id, is_admin=True).select_related("group").first()
    return ug.group if ug else None


@login_required
def billing(request, group_id):
    group = _get_admin_group(request, group_id)
    if not group:
        return render(request, "error.html", {"text": "このグループにアクセスする権限がありません"})

    subscription = None
    if group.stripe_subscription_id and settings.STRIPE_SECRET_KEY:
        try:
            subscription = stripe.Subscription.retrieve(group.stripe_subscription_id)
        except stripe.error.StripeError:
            pass

    context = {
        "group": group,
        "subscription": subscription,
        "plans": settings.STRIPE_PLANS,
        "stripe_publishable_key": settings.STRIPE_PUBLISHABLE_KEY,
    }
    return render(request, "group/billing.html", context)


@login_required
def create_checkout_session(request, group_id):
    if request.method != "POST":
        return redirect(reverse("custom_auth_group:billing", args=[group_id]))

    group = _get_admin_group(request, group_id)
    if not group:
        return render(request, "error.html", {"text": "このグループにアクセスする権限がありません"})

    plan_key = request.POST.get("plan", "standard")
    plan = settings.STRIPE_PLANS.get(plan_key)
    if not plan or not plan["price_id"]:
        return render(request, "error.html", {"text": "プランが見つかりません"})

    if not settings.STRIPE_SECRET_KEY:
        return render(request, "error.html", {"text": "Stripe APIキーが設定されていません"})

    # Stripe Customer を取得 or 作成
    if not group.stripe_customer_id:
        customer = stripe.Customer.create(
            name=group.name_jp,
            metadata={"group_id": group.id},
        )
        group.stripe_customer_id = customer.id
        group.save(update_fields=["stripe_customer_id"])

    success_url = settings.SITE_URL + reverse("custom_auth_group:billing_success", args=[group_id]) + "?session_id={CHECKOUT_SESSION_ID}"
    cancel_url = settings.SITE_URL + reverse("custom_auth_group:billing", args=[group_id])

    session = stripe.checkout.Session.create(
        customer=group.stripe_customer_id,
        payment_method_types=["card"],
        mode="subscription",
        line_items=[{"price": plan["price_id"], "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"group_id": group.id, "plan": plan_key},
    )
    return redirect(session.url, permanent=False)


@login_required
def billing_success(request, group_id):
    group = _get_admin_group(request, group_id)
    if not group:
        return render(request, "error.html", {"text": "このグループにアクセスする権限がありません"})

    session_id = request.GET.get("session_id")
    if session_id and settings.STRIPE_SECRET_KEY:
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            if session.subscription:
                group.stripe_subscription_id = session.subscription
                group.save(update_fields=["stripe_subscription_id"])
        except stripe.error.StripeError:
            pass

    return render(request, "done.html", {"text": "サブスクリプションの登録が完了しました"})


@login_required
def cancel_subscription(request, group_id):
    if request.method != "POST":
        return redirect(reverse("custom_auth_group:billing", args=[group_id]))

    group = _get_admin_group(request, group_id)
    if not group or not group.stripe_subscription_id:
        return render(request, "error.html", {"text": "解約できるサブスクリプションがありません"})

    if settings.STRIPE_SECRET_KEY:
        stripe.Subscription.cancel(group.stripe_subscription_id)
        group.stripe_subscription_id = None
        group.save(update_fields=["stripe_subscription_id"])

    return render(request, "done.html", {"text": "サブスクリプションを解約しました"})


@csrf_exempt
@require_POST
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    if not settings.STRIPE_WEBHOOK_SECRET:
        return HttpResponse(status=400)

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)

    if event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        from custom_auth.models import Group as ShiftGroup
        ShiftGroup.objects.filter(stripe_subscription_id=sub["id"]).update(stripe_subscription_id=None)

    elif event["type"] == "invoice.payment_failed":
        sub_id = event["data"]["object"].get("subscription")
        if sub_id:
            from custom_auth.models import Group as ShiftGroup
            ShiftGroup.objects.filter(stripe_subscription_id=sub_id).update(membership_type=1)

    return HttpResponse(status=200)
