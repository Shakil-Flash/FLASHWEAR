"""Back Office services (Phase 16).

Three kinds of thing live here, and nothing else:

* :mod:`apps.backoffice.services.audit` -- the append-only record of what staff did;
* :mod:`apps.backoffice.services.operations` -- the *only* place the back office writes to
  another domain, always by calling that domain's own service and then writing one audit
  row;
* :mod:`apps.backoffice.services.dashboard` / :mod:`apps.backoffice.services.alerts` --
  pure reads over authoritative rows.

No business rule is re-implemented here: if a rule exists in ``apps.orders`` it is
invoked from ``apps.orders``, never restated.
"""
