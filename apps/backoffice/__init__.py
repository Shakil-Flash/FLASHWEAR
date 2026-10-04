"""FLASHWEAR Back Office: the internal operations layer (Phase 16).

An *operations layer over the existing domains*: every screen reads through selectors,
every write calls the domain service that already owns the rule, and every staff action
is appended to one audit log. The app owns no business model besides that log -- orders,
stock, points, moderation state and tickets all stay where they were.
"""
