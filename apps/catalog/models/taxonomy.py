"""Merchandising taxonomy: categories, brands and collections.

The three answer three different questions about a product, and conflating them is the classic
catalogue mistake:

``Category``
    *What is it?* A hierarchy ("Men > T-Shirts") that is mutually exclusive: a product has exactly
    one place in the tree, because a shopper navigates a single path.

``Brand``
    *Who made it?* Flat, unique per product, and **nullable**. FLASHWEAR sells its own label
    alongside third-party ones, and plenty of products genuinely have no external brand yet, so
    "no brand" is a legal state rather than a placeholder row.

``Collection``
    *What is it in right now?* Many-to-many and time-boxed ("Summer '26", "Midnight", "Limited
    Edition"). A product is in as many as merchandising wants, and a collection can open and close
    without touching the product.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.base import SluggedModel


class Category(SluggedModel):
    """A node in the fashion taxonomy: Men > T-Shirts > Oversized Tees.

    The tree is a plain self-referencing foreign key. That supports any depth, which is what the
    schema needs; the storefront chooses to present two levels because that is how people browse
    clothes, not because the data model is limited to two.

    Invariants, and where each one is enforced:

    * a category is never its own parent -- check constraint (database) and ``clean`` (form);
    * a category is never moved underneath one of its own descendants -- ``check_tree_integrity``,
      which walks the parent chain with a visited set so a pre-existing cycle cannot spin forever;
    * a category is never deleted while a product points at it -- ``on_delete=PROTECT``. Hiding a
      discontinued department is what you want, not a cascade that orphans live products.
    """

    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="children",
        null=True,
        blank=True,
        verbose_name=_("parent category"),
        help_text=_("Leave empty for a top-level department."),
    )
    image = models.ImageField(
        _("image"),
        upload_to="catalog/categories/",
        blank=True,
        help_text=_("PNG, JPEG or WebP. Optional."),
    )

    class Meta:
        verbose_name = _("category")
        verbose_name_plural = _("categories")
        ordering = ("display_order", "name")
        constraints = [
            # ``parent = id`` is the only self-reference the database can see without walking the
            # graph; deeper cycles are caught in Python. The unique index on ``slug`` comes from
            # the field itself.
            models.CheckConstraint(
                condition=~Q(parent=F("id")),
                name="catalog_category_not_own_parent",
            ),
        ]
        indexes = [
            models.Index(fields=["parent", "display_order"], name="cat_cat_parent_order_idx"),
            models.Index(fields=["is_active", "display_order"], name="cat_cat_active_order_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("catalog:category-detail", kwargs={"slug": self.slug})

    # -- tree walking -------------------------------------------------------------------
    #
    # Deliberately plain Python. A tree library (django-mptt, treebeard) earns its keep when the
    # tree is queried structurally on every request -- "all descendants of X", subtree counts,
    # move-subtree operations. Phase 3 only walks *up* (breadcrumbs) on a handful of categories,
    # so a fixed two-query walk is enough. ``ancestors`` is the single place to swap in a library
    # if subtree queries ever become hot; every caller goes through it.

    def ancestors(self) -> list[Category]:
        """Return ancestors ordered root-first (``[Men, T-Shirts]`` for an Oversized Tee).

        Two queries regardless of depth: one cheap ``parent_id`` lookup per level, then a single
        fetch of every ancestor row. Depth is a handful of levels in a fashion taxonomy, and this
        keeps the breadcrumb on a product page at a fixed cost without a tree library.
        """
        cached = getattr(self, "_ancestor_cache", None)
        if cached is not None:
            return cached

        seen: set[int] = {self.pk} if self.pk else set()
        parent_ids: list[int] = []
        # ``parent_id`` is already on the instance -- it is the foreign key column, not a query --
        # so there is nothing to save by asking for the parent object first. (An earlier version
        # read ``self.parent.parent_id`` here, which is the *grandparent* and silently returned an
        # empty chain for a category created with ``Category(parent=department)``.)
        parent_id = self.parent_id

        while parent_id is not None and parent_id not in seen:
            seen.add(parent_id)
            parent_ids.append(parent_id)
            parent_id = (
                Category.objects.filter(pk=parent_id).values_list("parent_id", flat=True).first()
            )

        rows = {row.pk: row for row in Category.objects.filter(pk__in=parent_ids)}
        chain = [rows[pk] for pk in reversed(parent_ids) if pk in rows]
        self._ancestor_cache = chain
        return chain

    def breadcrumb_trail(self) -> list[Category]:
        """``ancestors()`` plus this category -- what a breadcrumb renders."""
        return [*self.ancestors(), self]

    def is_root(self) -> bool:
        return self.parent_id is None

    @property
    def depth(self) -> int:
        """How many ancestors sit above this node (``0`` for a top-level category)."""
        return len(self.ancestors())

    def check_tree_integrity(self) -> None:
        """Raise ``ValidationError`` if this category's parent is itself or a descendant.

        Called from :meth:`clean` (so a form shows a field error) and from :meth:`save` (so a shell,
        a data import or a management command cannot create a cycle behind the admin's back).
        """
        if self.parent_id is None:
            return
        if self.pk and self.parent_id == self.pk:
            raise ValidationError(
                {"parent": _("A category cannot be its own parent.")},
                code="self_parent",
            )

        # Walk up from the proposed parent. If we arrive back at this category, the edge would
        # close a loop. The visited set also guarantees termination on a pre-existing cycle.
        seen: set[int] = {self.pk} if self.pk else set()
        node = self.parent
        while node is not None:
            if node.pk in seen:
                raise ValidationError(
                    {"parent": _("That parent is below this category, which would create a loop.")},
                    code="cyclic_parent",
                )
            seen.add(node.pk)
            node = node.parent
            if node is not None and len(seen) > 1000:  # pragma: no cover - corrupt data guard
                raise ValidationError(
                    {"parent": _("The category tree is inconsistent; fix it before editing this.")},
                    code="tree_corrupt",
                )

    def clean(self) -> None:
        super().clean()
        self.check_tree_integrity()

    def save(self, *args, **kwargs):
        # Re-validated on every write, not only through a form.
        self.check_tree_integrity()
        super().save(*args, **kwargs)


class Brand(SluggedModel):
    """A label whose name appears on the product page.

    Nullable on ``Product``: an in-house FLASHWEAR product with no external brand is a normal
    state, and inventing a "No brand" row would put junk in a filter list.
    """

    logo = models.ImageField(
        _("logo"),
        upload_to="catalog/brands/",
        blank=True,
        help_text=_("PNG or WebP with transparency. Optional."),
    )
    website_url = models.URLField(
        _("website"),
        max_length=300,
        blank=True,
        help_text=_("http:// or https:// only."),
    )

    class Meta:
        verbose_name = _("brand")
        verbose_name_plural = _("brands")
        ordering = ("display_order", "name")
        constraints = [
            # Belt and braces against a ``javascript:`` URL reaching a customer as a link. Blank has
            # to be allowed explicitly: the column is ``blank=True``, so "no website" is a normal
            # state, and a negated ``= ""`` term would reject every one of those rows.
            models.CheckConstraint(
                condition=(Q(website_url="") | Q(website_url__regex=r"^https?://[^\s]+$")),
                name="catalog_brand_website_http_only",
            )
        ]
        indexes = [
            models.Index(fields=["is_active", "display_order"], name="cat_brand_active_order_idx"),
        ]

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("catalog:brand-detail", kwargs={"slug": self.slug})

    def clean(self):
        super().clean()
        url = (self.website_url or "").strip()
        self.website_url = url
        if url and not url.startswith(("http://", "https://")):
            raise ValidationError(
                {"website_url": _("Use a full http:// or https:// address.")},
                code="bad_url",
            )


class Collection(SluggedModel):
    """A merchandised, optionally time-boxed grouping of products.

    Many-to-many from ``Product``: "Midnight" and "Premium" are independent stories about the same
    garment. ``starts_at`` / ``ends_at`` make a collection a drop window instead of a permanent
    shelf; both are nullable because most collections are open-ended.
    """

    hero_image = models.ImageField(
        _("hero image"),
        upload_to="catalog/collections/",
        blank=True,
        help_text=_("Wide banner for the collection page and Open Graph. Optional."),
    )
    banner_image = models.ImageField(
        _("banner image"),
        upload_to="catalog/collections/",
        blank=True,
        help_text=_("Alternative wide image used on cards and in listings."),
    )
    is_featured = models.BooleanField(
        _("featured"),
        default=False,
        help_text=_("Show on the homepage and in the navigation."),
    )
    starts_at = models.DateTimeField(_("starts at"), null=True, blank=True)
    ends_at = models.DateTimeField(_("ends at"), null=True, blank=True)

    class Meta:
        verbose_name = _("collection")
        verbose_name_plural = _("collections")
        ordering = ("display_order", "name")
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(starts_at__isnull=True)
                    | Q(ends_at__isnull=True)
                    | Q(ends_at__gte=F("starts_at"))
                ),
                name="catalog_collection_window_ordered",
            )
        ]
        indexes = [
            models.Index(fields=["is_active", "display_order"], name="cat_coll_active_order_idx"),
            models.Index(fields=["is_featured", "starts_at"], name="cat_coll_featured_start_idx"),
        ]

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("catalog:collection-detail", kwargs={"slug": self.slug})

    @property
    def is_current(self) -> bool:
        """Whether the collection is inside its own date window right now."""
        now = timezone.now()
        if self.starts_at and now < self.starts_at:
            return False
        return not (self.ends_at and now > self.ends_at)

    @property
    def is_live(self) -> bool:
        """Active *and* inside its window -- the condition for a public collection page."""
        return self.is_active and self.is_current

    def clean(self):
        super().clean()
        if self.starts_at and self.ends_at and self.ends_at < self.starts_at:
            raise ValidationError(
                {"ends_at": _("The end date must be after the start date.")},
                code="window_reversed",
            )
