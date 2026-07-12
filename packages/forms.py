from django import forms

from .models import Package


class PackageForm(forms.ModelForm):
    class Meta:
        model = Package
        fields = [
            "pickup_point",
            "description",
            "pickup_code",
            "is_vine",
            "cost",
            "state",
            "estimated_arrival",
            "actual_arrival",
            "deadline",
        ]
        widgets = {
            "estimated_arrival": forms.DateInput(attrs={"type": "date"}),
            "actual_arrival": forms.DateInput(attrs={"type": "date"}),
            "deadline": forms.DateInput(attrs={"type": "date"}),
        }
