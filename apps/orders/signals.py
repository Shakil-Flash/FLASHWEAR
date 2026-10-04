"""Order lifecycle signals (Phase 7).

Two signals, both sent **synchronously inside the transaction** that caused the transition:

* ``order_paid`` -- sent by ``apps.payments`` the moment a payment succeeds and the order
  becomes ``PAID``. Listeners (FLASH Points earning/consumption) are transactional with the
  payment: if points cannot be written, the payment block rolls back, exactly like a failed
  stock consumption would.
* ``order_cancelled`` -- sent by :func:`apps.orders.services.cancel_order` after the unpaid
  order transitions. Listeners release anything the checkout was holding (stock does this
  directly; points do it through this signal).

No ``transaction.on_commit`` delivery: it would be friendlier to slow listeners, but Phase 7
tests (and any operator running without a mail queue) must see the effect of a payment in the
same test transaction it caused. Keep listeners fast and idempotent.

Payload: ``sender`` is the ``Order`` class, ``order`` is the instance already carrying its new
status. Import the module to use them -- the objects are created at import time and shared
through Python's module cache:

    from apps.orders.signals import order_paid
"""

from __future__ import annotations

from django.dispatch import Signal

order_paid = Signal()
"""Sent with ``sender=Order, order=<paid order>`` after PENDING_PAYMENT -> PAID."""

order_cancelled = Signal()
"""Sent with ``sender=Order, order=<cancelled order>`` after an unpaid order is cancelled."""
