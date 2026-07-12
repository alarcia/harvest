from django.shortcuts import redirect, render

from .forms import PackageForm


def home(request):
    """Landing page. A placeholder until the calendar view is built."""
    return render(request, "packages/home.html")


def add_package(request):
    """Manual entry, open to anyone Cloudflare Access already let through.

    No login of our own: the app never distinguishes between the two
    allowlisted users. This is the only way alt-store packages get in at
    all, since that store generates no email.
    """
    if request.method == "POST":
        form = PackageForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect("home")
    else:
        form = PackageForm()
    return render(request, "packages/package_form.html", {"form": form})
