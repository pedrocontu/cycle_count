"""URL configuration for cycle_count_project."""
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('cycle_count.urls', namespace='cycle_count')),
]