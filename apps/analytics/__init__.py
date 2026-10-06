"""Phase 20: privacy-aware analytics -- centralized event recording, attribution and funnel reads.

One table of business events (who did what, when, with safe metadata), one first/last-touch
attribution row per visitor, and read-side helpers the back office aggregates from. The rules:

* **server-authoritative** -- events are recorded in the service or view that completed the
  action, never trusted from the client;
* **never raises** -- analytics is observational; a failure logs and returns ``None`` rather
  than breaking a shopper's request or a payment;
* **no unnecessary personal data** -- a random visitor id, the session key the shop already
  uses for guest carts, the authenticated user when known, and metadata scrubbed of anything
  credential-shaped before it is written.
"""
