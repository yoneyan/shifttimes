from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from notice.models import Notice
from shift.models import ShiftEntry, ShiftDeadline


@login_required
def index(request):
    notices = Notice.objects.get_notice()

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
