"""Domain services for FLASH Support (Phase 15).

Everything that changes a ticket, a message or an attachment goes through a function here --
views, forms and API serializers only collect input and translate errors. That is what keeps
one set of rules for the HTML surface and the JSON surface: the transition graph, the
ownership check, the internal-note rule and the notification policy are each written once.
"""
