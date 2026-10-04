"""Creator post product tagging model (Phase 12)."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class CreatorPostProduct(models.Model):
    """Product tagging on a creator post.

    Makes a post shoppable by attaching existing catalog products.
    Never duplicates Product or Variant data — price always comes from catalog.
    """

    post = models.ForeignKey(
        "creator.CreatorPost",
        on_delete=models.CASCADE,
        related_name="product_tags",
        verbose_name=_("post"),
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.PROTECT,
        related_name="creator_post_tags",
        verbose_name=_("product"),
    )
    display_order = models.PositiveIntegerField(
        _("display order"),
        default=0,
        help_text=_("Lower numbers appear first on the product card."),
    )
    label = models.CharField(
        _("product label"),
        max_length=128,
        blank=True,
        help_text=_("Optional label displayed on the product card, e.g. 'Worn by Creator'."),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("creator post product")
        verbose_name_plural = _("creator post products")
        constraints = [
            models.UniqueConstraint(
                fields=["post", "product"],
                name="unique_post_product_tag",
            ),
        ]
        indexes = [
            models.Index(fields=["post"], name="post_product_post_idx"),
            models.Index(fields=["product"], name="post_product_product_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.product.name} tagged on post {self.post_id}"
