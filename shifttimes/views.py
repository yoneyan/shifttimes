from django.contrib.auth import logout as user_logout, authenticate, login as user_login
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.utils import timezone

from notice.models import Notice
from shift.models import ShiftEntry
from shifttimes.form import LoginForm


def index(request):
    if not request.user.is_authenticated:
        return render(request, "landing.html", {})

    user = request.user
    today = timezone.now().date()

    memberships = list(user.usergroup_set.select_related("group").all())
    admin_group_ids = {membership.group_id for membership in memberships if membership.is_admin}

    group_cards = [
        {"group": membership.group, "is_admin": membership.group_id in admin_group_ids}
        for membership in memberships
    ]

    total_draft_count = ShiftEntry.objects.filter(
        user=user, work_date__gte=today, is_draft=True,
    ).count()

    context = {
        "group_cards": group_cards,
        "has_admin_groups": bool(admin_group_ids),
        "total_draft_count": total_draft_count,
        "notices": Notice.objects.get_notice()[:3],
    }
    return render(request, "index.html", context)


def login(request):
    if request.method == 'POST':
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            if user:
                user_login(request, user)
                return redirect("/")

    else:
        form = LoginForm()
    context = {'form': form}
    return render(request, "login.html", context)


@login_required
def logout(request):
    user_logout(request)
    context = {}
    return render(request, "logout.html", context)
