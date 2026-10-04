"""Outfit composition, duplication and the deterministic completeness model (Phase 8).

Everything about *what may go into an outfit* lives here so views, forms and the API share
one rule set (Phase 8 spec, section 14):

* **Ownership** -- both sides of an outfit item must belong to the same customer; the
  service re-checks even when the caller claims to have scoped the query already.
* **Roles are derived, not declared** -- a piece's wardrobe category maps to exactly one
  slot (:data:`ROLE_FOR_CATEGORY`), so the structure is always consistent and the future
  recommender can trust it. One garment per slot, except accessories; a dress excludes
  tops and bottoms (and vice versa) because a dress *is* the body layer.
* **Archive semantics** -- archived pieces cannot join *new* outfits but stay rendered in
  old ones; positions stay dense through one transaction.

Completeness (:func:`calculate_outfit_completeness`) is arithmetic, not taste: a dress
outfit needs a dress and shoes, everything else needs a top, a bottom and shoes. It returns
structured data so a future recommendation layer can extend it without rewriting it.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Prefetch, QuerySet

from apps.closet.models import ClosetItem, Outfit, OutfitItem
from apps.closet.services.errors import OutfitError

__all__ = [
    "ROLE_FOR_CATEGORY",
    "SINGLE_OCCUPANCY_ROLES",
    "add_item",
    "calculate_outfit_completeness",
    "create_outfit",
    "delete_outfit",
    "duplicate_outfit",
    "get_outfit_context",
    "get_user_outfits",
    "move_item_to",
    "remove_item",
    "set_outfit_status",
    "update_outfit",
]

Role = OutfitItem.Role
Category = ClosetItem.Category

# Wardrobe category -> outfit slot. Strict: the slot is a fact about the garment's kind,
# not a client preference, so forms render this map and the API re-validates against it.
ROLE_FOR_CATEGORY = {
    Category.TOPS: Role.TOP,
    Category.BOTTOMS: Role.BOTTOM,
    Category.OUTERWEAR: Role.OUTERWEAR,
    Category.DRESSES: Role.DRESS,
    Category.SHOES: Role.SHOES,
    Category.ACCESSORIES: Role.ACCESSORY,
}

# Slots that hold exactly one garment (layering lives in OUTERWEAR, not a second top).
SINGLE_OCCUPANCY_ROLES = {Role.TOP, Role.BOTTOM, Role.OUTERWEAR, Role.DRESS, Role.SHOES}

# A dress replaces the top/bottom pair; mixing the two shapes is refused, not silently fixed.
BODY_SLOTS = {Role.TOP, Role.BOTTOM}

# Outfit metadata fields a caller may set. Provenance (user, status) never appears here.
EDITABLE_FIELDS = ("name", "description", "occasion", "season", "style")

VALID_STATUSES = {status for status, _ in Outfit.Status.choices}

STATUS_LABELS = {
    Outfit.Status.DRAFT: "Drafts",
    Outfit.Status.SAVED: "Saved",
    Outfit.Status.ARCHIVED: "Archived",
}


def _optimized_items() -> QuerySet:
    """Outfit rows as the pages and API render them: garment, product link, imagery, once."""
    return OutfitItem.objects.select_related(
        "closet_item",
        "closet_item__variant",
        "closet_item__variant__product",
        "closet_item__variant__product__brand",
    ).prefetch_related("closet_item__variant__product__images")


def get_user_outfits(user, *, status: str = "") -> QuerySet:
    """One customer's outfits, newest-updated first, eager-loaded for list rendering.

    ``status`` filters to a single lifecycle state (``""`` means all); the completeness and
    thumbnail data on every card then come from the prefetch, not per-row queries.
    """
    queryset = Outfit.objects.filter(user=user).prefetch_related(
        Prefetch("items", queryset=_optimized_items())
    )
    if status in VALID_STATUSES:
        queryset = queryset.filter(status=status)
    return queryset.order_by("-updated_at")


def _validate(outfit: Outfit) -> None:
    """Run model validation in the service's vocabulary (forms and serializers pre-validate
    the same fields; this is the defensive layer for direct calls)."""
    from django.core.exceptions import ValidationError

    try:
        outfit.full_clean()
    except ValidationError as exc:
        if hasattr(exc, "message_dict"):
            detail = "; ".join(
                message for messages in exc.message_dict.values() for message in messages
            )
        else:
            detail = "; ".join(exc.messages)
        raise OutfitError(detail or "That outfit could not be saved.", code="invalid") from exc


def create_outfit(
    user,
    *,
    name: str,
    description: str = "",
    occasion: str = "",
    season: str = "",
    style: str = "",
    status: str = Outfit.Status.DRAFT,
) -> Outfit:
    """Start an outfit. Items are added afterwards, one validated insert at a time."""
    name = (name or "").strip()
    if not name:
        raise OutfitError("An outfit needs a name.", code="name_required")
    if status not in VALID_STATUSES:
        raise OutfitError("Unknown outfit status.", code="bad_status")
    outfit = Outfit(
        user=user,
        name=name,
        description=(description or "").strip(),
        occasion=occasion or "",
        season=season or "",
        style=style or "",
        status=status,
    )
    _validate(outfit)
    outfit.save()
    return outfit


def update_outfit(outfit: Outfit, **fields) -> Outfit:
    """Apply whitelisted metadata edits; unknown keys are a bug, not a silent drop."""
    unknown = set(fields) - set(EDITABLE_FIELDS)
    if unknown:
        raise OutfitError("Those fields cannot be edited.", code="unexpected_fields")
    for field, value in fields.items():
        if field == "name":
            value = (value or "").strip()
            if not value:
                raise OutfitError("An outfit needs a name.", code="name_required")
        elif field == "description":
            value = (value or "").strip()
        else:
            value = value or ""
        setattr(outfit, field, value)
    _validate(outfit)
    outfit.save()
    return outfit


def set_outfit_status(outfit: Outfit, status: str) -> Outfit:
    """Save, archive or restore. Moving an outfit never touches its garments."""
    if status not in VALID_STATUSES:
        raise OutfitError("Unknown outfit status.", code="bad_status")
    if outfit.status != status:
        outfit.status = status
        outfit.save(update_fields=["status", "updated_at"])
    return outfit


def delete_outfit(outfit: Outfit) -> None:
    """Hard-delete one outfit (its rows cascade). The wardrobe itself is untouched."""
    outfit.delete()


def duplicate_outfit(outfit: Outfit) -> Outfit:
    """Copy an outfit into a fresh draft, positions and all -- one transaction.

    The original is never mutated: the clone gets its own rows, its own id and its own
    timestamps, and starts as a DRAFT so the customer edits the copy rather than thinking
    they are editing the original.
    """
    with transaction.atomic():
        clone = Outfit.objects.create(
            user=outfit.user,
            name=f"{outfit.name} (copy)",
            description=outfit.description,
            occasion=outfit.occasion,
            season=outfit.season,
            style=outfit.style,
            status=Outfit.Status.DRAFT,
        )
        links = list(outfit.items.all())
        if links:
            OutfitItem.objects.bulk_create(
                OutfitItem(
                    outfit=clone,
                    closet_item=link.closet_item,
                    role=link.role,
                    position=link.position,
                    note=link.note,
                )
                for link in links
            )
    return clone


# =============================================================================
# Composition
# =============================================================================


def _require_link(outfit: Outfit, link: OutfitItem) -> OutfitItem:
    """Fetch a link *through the outfit*, never by id alone (ownership by construction)."""
    if link.outfit_id != outfit.pk:
        raise OutfitError("That item is not part of this outfit.", code="not_in_outfit")
    return link


def add_item(outfit: Outfit, closet_item: ClosetItem, *, note: str = "") -> OutfitItem:
    """Place an active wardrobe piece into an outfit, in its derived slot.

    Every rule fires here regardless of caller: same owner, active piece, not already in
    this outfit, one garment per single-occupancy slot, dress/top exclusivity. The role is
    computed from the category -- a payload never chooses it.
    """
    if closet_item.user_id != outfit.user_id:
        raise OutfitError("That item is not yours.", code="not_owner")
    if closet_item.status != ClosetItem.Status.ACTIVE:
        raise OutfitError(
            "Archived items cannot join new outfits. Restore it first.", code="archived"
        )
    existing = {link.closet_item_id: link for link in outfit.items.all()}
    if closet_item.pk in existing:
        raise OutfitError("That item is already in this outfit.", code="duplicate_item")

    role = ROLE_FOR_CATEGORY[closet_item.category]
    taken = {link.role for link in existing.values()}

    if role in SINGLE_OCCUPANCY_ROLES and role in taken:
        raise OutfitError(f"This outfit already has a {role} piece.", code="slot_taken")
    if role == Role.DRESS and taken & BODY_SLOTS:
        raise OutfitError(
            "A dress replaces the top and bottom. Remove them first.", code="dress_conflict"
        )
    if role in BODY_SLOTS and Role.DRESS in taken:
        raise OutfitError(
            "This outfit already has a dress, which replaces the top and bottom.",
            code="dress_conflict",
        )

    link = OutfitItem.objects.create(
        outfit=outfit,
        closet_item=closet_item,
        role=role,
        position=len(existing),
        note=(note or "").strip()[:200],
    )
    _touch(outfit)
    return link


def remove_item(outfit: Outfit, link: OutfitItem) -> None:
    """Take one garment out and close the gap in positions."""
    _require_link(outfit, link)
    link.delete()
    _renormalize(outfit)
    _touch(outfit)


def move_item_to(outfit: Outfit, link: OutfitItem, position: int) -> None:
    """Reorder: put ``link`` at ``position`` (0-based, clamped) and keep positions dense.

    The web builder sends up/down (which resolve to index +/- 1); the API sends an absolute
    index. Both land here, both inside one transaction.
    """
    _require_link(outfit, link)
    ordered = list(outfit.items.order_by("position", "pk"))
    if not ordered:
        raise OutfitError("That outfit has no items.", code="empty_outfit")
    current = next(i for i, row in enumerate(ordered) if row.pk == link.pk)
    target = max(0, min(int(position), len(ordered) - 1))
    ordered.insert(target, ordered.pop(current))
    for index, row in enumerate(ordered):
        if row.position != index:
            row.position = index
    OutfitItem.objects.bulk_update(ordered, ["position"])
    _touch(outfit)


def _renormalize(outfit: Outfit) -> None:
    """Repack positions to 0..n-1 after a removal (one bulk update, never per-row)."""
    rows = list(outfit.items.order_by("position", "pk"))
    changed = []
    for index, row in enumerate(rows):
        if row.position != index:
            row.position = index
            changed.append(row)
    if changed:
        OutfitItem.objects.bulk_update(changed, ["position"])


def _touch(outfit: Outfit) -> None:
    """Bump the outfit's updated_at so "last edited" follows composition changes."""
    outfit.save(update_fields=["updated_at"])


# =============================================================================
# Completeness (deterministic structure, not fashion intelligence)
# =============================================================================

HUMAN_ROLE = {
    Role.TOP: "top",
    Role.BOTTOM: "bottom",
    Role.OUTERWEAR: "outerwear",
    Role.DRESS: "dress",
    Role.SHOES: "shoes",
    Role.ACCESSORY: "accessory",
}


def calculate_outfit_completeness(outfit: Outfit) -> dict:
    """Return the outfit's structural completeness as data, never prose alone.

    Rules (deterministic, deliberately dumb):

    * a dress outfit needs the dress and shoes -- accessories and outerwear are optional;
    * anything else needs a top, a bottom and shoes.

    ``status`` is ``complete`` / ``almost`` (exactly one gap) / ``incomplete`` (two or
    more), and ``label`` names the specific gap ("Missing shoes") so the UI can be useful
    without pretending to have taste. Reads from the prefetched item cache when one exists,
    so a list of outfits does not cost a query per row.
    """
    links = list(outfit.items.all())
    roles = {link.role for link in links}
    if Role.DRESS in roles:
        required = [Role.DRESS, Role.SHOES]
    else:
        required = [Role.TOP, Role.BOTTOM, Role.SHOES]
    missing = [role for role in required if role not in roles]
    if not missing:
        status, label = "complete", "Complete"
    elif len(missing) == 1:
        status = "almost"
        label = f"Missing {HUMAN_ROLE[missing[0]]}"
    else:
        status = "incomplete"
        human = [HUMAN_ROLE[role] for role in missing]
        label = f"Missing {', '.join(human[:-1])} and {human[-1]}"
    return {
        "is_complete": not missing,
        "status": status,
        "label": label,
        "missing_roles": missing,
        "required_roles": required,
    }


def get_outfit_context(outfit: Outfit) -> dict:
    """Everything a preview (or a future recommender) needs, in one shape.

    This is the extension point Phase 8 promises: the AI stylist consumes this dict rather
    than re-walking the tables, so adding intelligence later changes no schema.
    """
    links = list(outfit.items.all())
    return {
        "outfit": outfit,
        "items": links,
        "completeness": calculate_outfit_completeness(outfit),
        "roles": [link.role for link in links],
    }
