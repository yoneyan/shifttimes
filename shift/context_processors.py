from custom_auth.models import Group, UserGroup
from shift.models import AttendanceSetting


ACTIVE_GROUP_STATUS = 1


def shift_admin_groups(request):
    user = request.user
    if not user.is_authenticated:
        return {
            "shift_admin_groups": [],
            "has_shift_admin_groups": False,
        }

    if user.is_staff:
        groups = list(Group.objects.filter(status=ACTIVE_GROUP_STATUS).order_by("name"))
    else:
        groups = [
            membership.group
            for membership in (
                UserGroup.objects.filter(user=user, is_admin=True, group__status=ACTIVE_GROUP_STATUS)
                .select_related("group")
                .order_by("group__name")
            )
        ]

    return {
        "shift_admin_groups": groups,
        "has_shift_admin_groups": bool(groups),
    }


def attendance_groups(request):
    """勤怠管理が有効な所属グループがあるかどうか（ナビゲーション表示用）"""
    user = request.user
    if not user.is_authenticated:
        return {"has_attendance_groups": False}

    settings_query = AttendanceSetting.objects.filter(is_enabled=True, group__status=ACTIVE_GROUP_STATUS)
    if not user.is_staff:
        settings_query = settings_query.filter(group__usergroup__user=user)

    return {"has_attendance_groups": settings_query.exists()}
