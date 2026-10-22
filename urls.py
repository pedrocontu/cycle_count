from django.urls import path
from . import views

app_name = 'cycle_count'

urlpatterns = [
    path('login/', views.cc_login, name='login'),
    path('logout/', views.cc_logout, name='logout'),
    path('debug-odbc-drivers/', views.cc_debug_odbc_drivers, name='debug_odbc_drivers'),
    path('', views.cc_dashboard, name='dashboard'),
    path('api/dashboard.json', views.cc_api_dashboard, name='api_dashboard'),
    path('requests/', views.cc_requests_all, name='requests_all'),
    path('requests/new/', views.cc_request_new, name='request_new'),
    path('requests/<int:rid>/claim/', views.cc_request_claim, name='request_claim'),
    path('requests/<int:rid>/review/', views.cc_request_review, name='request_review'),
    path('requests/<int:rid>/cancel/', views.cc_request_cancel, name='request_cancel'),
    path('requests/<int:rid>/delete/', views.cc_request_delete, name='request_delete'),
    path('requests/delete_all_cancelled/', views.cc_request_delete_all_cancelled,
         name='request_delete_all_cancelled'),
    path('counts/<int:rid>/', views.cc_count_screen, name='count_screen'),
    path('counts/<int:rid>/save/', views.cc_count_save, name='count_save'),
    path('counts/<int:rid>/add-location/', views.cc_count_add_location, name='count_add_location'),
    path('counts/<int:rid>/delete-location/', views.cc_count_delete_location,
         name='count_delete_location'),
    path('counts/<int:rid>/explosion/', views.cc_count_explosion, name='count_explosion'),
    path('lookup/', views.cc_lookup, name='lookup'),
    path('admin/users/', views.cc_users_list, name='users_list'),
    path('admin/users/new/', views.cc_users_new, name='users_new'),
    path('admin/users/<int:uid>/toggle/', views.cc_users_toggle, name='users_toggle'),
    path('admin/users/<int:uid>/delete/', views.cc_users_delete, name='users_delete'),
    path('admin/users/<int:uid>/reset/', views.cc_users_reset, name='users_reset'),
    path('pou/map/', views.cc_pou_map, name='pou_map'),
]
