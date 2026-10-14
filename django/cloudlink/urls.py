from django.urls import path
from .views import DashboardView, SetBandwidthView, AddBaseDomainView, RemoveBaseDomainView, ResetHostKeyView

urlpatterns = [
    path('', DashboardView.as_view(), name='dashboard'),
    path('host-key/reset/', ResetHostKeyView.as_view(), name='reset_host_key'),
    path('bandwidth/', SetBandwidthView.as_view(), name='set_bandwidth'),
    path('base-domains/add/', AddBaseDomainView.as_view(), name='add_base_domain'),
    path('base-domains/<str:domain>/remove/', RemoveBaseDomainView.as_view(), name='remove_base_domain'),
]
