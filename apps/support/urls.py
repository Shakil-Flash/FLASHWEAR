"""URLs for FLASH Support, mounted at ``/support/``.

One ``app_name`` (Django requires exactly one per module). Every view is already behind
``login_required``, so the routes are registered plainly -- the decorator is the security
boundary, not the URL line.
"""

from django.urls import path

from apps.support import views

app_name = "support"

urlpatterns = [
    path("", views.support_home, name="home"),
    path("tickets/", views.ticket_list, name="ticket-list"),
    path("tickets/<str:number>/", views.ticket_detail, name="ticket-detail"),
    path("tickets/<str:number>/reply/", views.ticket_reply, name="ticket-reply"),
    path("tickets/<str:number>/close/", views.ticket_close, name="ticket-close"),
    path("tickets/<str:number>/reopen/", views.ticket_reopen, name="ticket-reopen"),
    path("create/", views.ticket_create, name="ticket-create"),
    # Not under tickets/: an attachment id is a different kind of handle and must never
    # collide with a ticket number in a log line or a referer.
    path("attachments/<int:pk>/", views.attachment_download, name="attachment"),
    path("staff/", views.staff_dashboard, name="staff-dashboard"),
    path(
        "staff/tickets/<str:number>/",
        views.staff_ticket_detail,
        name="staff-ticket-detail",
    ),
    path(
        "staff/tickets/<str:number>/assign/",
        views.staff_assign,
        name="staff-assign",
    ),
    path(
        "staff/tickets/<str:number>/status/",
        views.staff_status,
        name="staff-status",
    ),
    path(
        "staff/tickets/<str:number>/priority/",
        views.staff_priority,
        name="staff-priority",
    ),
    path(
        "staff/tickets/<str:number>/escalate/",
        views.staff_escalate,
        name="staff-escalate",
    ),
    path("staff/tickets/<str:number>/link/", views.staff_link, name="staff-link"),
    path("staff/tickets/<str:number>/note/", views.staff_note, name="staff-note"),
    path("staff/tickets/<str:number>/reply/", views.staff_reply, name="staff-reply"),
]
