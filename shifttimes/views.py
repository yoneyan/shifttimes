from django.contrib.auth import logout as user_logout, authenticate, login as user_login
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect

from shifttimes.form import LoginForm


@login_required
def index(request):
    context = {}
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
