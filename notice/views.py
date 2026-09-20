from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from notice.forms import NoticeForm
from notice.models import Notice
from shift.models import ShiftEntry, ShiftDeadline


@login_required
def index(request):
    notices = Notice.objects.get_notice()

    # オンプレミスモードでは契約という概念がないので未課金の案内も出さない
    unpaid_groups = []
    if settings.BILLING_ENABLED:
        unpaid_groups = [
            ug.group for ug in request.user.usergroup_set.select_related("group").all()
            if not ug.group.stripe_subscription_id
        ]

    # 未確定（下書き）シフト希望があるグループ
    today = timezone.now().date()
    draft_entries = (
        ShiftEntry.objects.filter(
            user=request.user,
            work_date__gte=today,
            is_draft=True,
        )
        .select_related("group")
        .values("group_id", "group__name_jp", "group__name")
        .distinct()
    )
    draft_groups = [
        {
            "id": row["group_id"],
            "name_jp": row["group__name_jp"],
            "name": row["group__name"],
        }
        for row in draft_entries
    ]

    # 期限が近い or 過ぎているグループ
    deadline_alerts = []
    for ug in request.user.usergroup_set.select_related("group").all():
        dl = ShiftDeadline.objects.filter(
            group=ug.group, period_end__gte=today,
        ).order_by("deadline_date").first()
        if dl:
            days_left = (dl.deadline_date - today).days
            deadline_alerts.append({
                "group": ug.group,
                "deadline": dl,
                "days_left": days_left,
                "is_overdue": days_left < 0,
            })

    context = {
        "notices": notices,
        "unpaid_groups": unpaid_groups,
        "draft_groups": draft_groups,
        "deadline_alerts": deadline_alerts,
    }
    return render(request, "notice/index.html", context)


def _notice_status(notice, now):
    """一覧に出す掲示状態のラベル"""
    if not notice.is_active:
        return {"label": "停止", "css": "secondary"}
    if notice.start_at > now:
        return {"label": "掲示前", "css": "info"}
    if notice.end_at and notice.end_at <= now:
        return {"label": "終了", "css": "secondary"}
    return {"label": "掲示中", "css": "primary"}


@login_required
def manage(request):
    """運営向けの通知（お知らせ）の追加・編集"""
    if not request.user.is_staff:
        raise PermissionDenied

    if request.method == "POST":
        notice = Notice.objects.filter(id=request.POST.get("notice_id", "") or 0).first()
        form = NoticeForm(request.POST, instance=notice)
        if form.is_valid():
            saved_notice = form.save()
            messages.success(request, "通知「%s」を保存しました。" % saved_notice.title)
        else:
            messages.error(request, "通知を保存できませんでした。%s" % form.errors.as_text())
        return redirect("notice:manage")

    now = timezone.now()
    notice_rows = [
        {
            "notice": notice,
            "status": _notice_status(notice, now),
        }
        for notice in Notice.objects.order_by("-start_at", "-id")[:100]
    ]

    return render(request, "notice/manage.html", {
        "notice_rows": notice_rows,
        "type1_choices": Notice.TYPE1_CHOICES,
        "default_start_at": now,
    })


@login_required
def notice_delete(request, notice_id):
    """通知を削除する"""
    if not request.user.is_staff:
        raise PermissionDenied

    notice = get_object_or_404(Notice, id=notice_id)
    if request.method != "POST":
        raise PermissionDenied

    notice.delete()
    messages.success(request, "通知「%s」を削除しました。" % notice.title)
    return redirect("notice:manage")
