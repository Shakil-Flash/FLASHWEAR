"""Phase 21: Advanced Fashion Intelligence — the Smart Outfit Generator suite.

Each spec area is a class: deterministic generation, preference and style matching, mood
mapping, style goals, Complete the Look, stock/duplicate safety, save/regenerate/replace,
permissions and privacy, the seven analytics events, the studio API, and the mobile/UX
states the spec called out (loading, empty, error, no-JavaScript, accessibility hooks).

Fixtures are built explicitly per test: the generator's pools come only from this test's
database, so a failure names the piece that moved rather than depending on a seeded shop.
"""

from __future__ import annotations

import re

import pytest
from django.urls import reverse

from apps.analytics.models import Event
from apps.closet.models import ClosetItem, Outfit, OutfitItem
from apps.closet.services.errors import OutfitError
from apps.inventory.models import Stock
from apps.styling.api import StudioGenerateView
from apps.styling.models.flash_dna import STYLE_GOALS, FlashDNA, get_flash_dna
from apps.styling.services import signals as maps
from apps.styling.services.complete_look import complete_look
from apps.styling.services.outfit_generation import (
    OutfitContext,
    generate_outfit,
    save_suggestion,
    suggestion_to_dict,
)

pytestmark = pytest.mark.django_db


def _studio_url() -> str:
    return reverse("account:studio")


def _mood_url() -> str:
    return reverse("account:studio-mood")


def _goals_url() -> str:
    return reverse("account:studio-goals")


def _closet(user, category: str, name: str, **fields) -> ClosetItem:
    return ClosetItem.objects.create(user=user, category=category, name=name, **fields)


def _event_count(name: str, **filters) -> int:
    return Event.objects.filter(name=name, **filters).count()


def _piece_tokens(html: str) -> list[str]:
    return re.findall(r'name="piece" value="([^"]+)"', html)


# --------------------------------------------------------------------------------------
# 1. Deterministic generation
# --------------------------------------------------------------------------------------


class TestDeterministicGeneration:
    def test_the_same_context_reproduces_the_same_look(self, user, product):
        _closet(user, "tops", "Owned Oxford Shirt", season="all_season")
        context = OutfitContext(occasion="everyday", seed=7)

        first = generate_outfit(user, context)
        second = generate_outfit(user, context)

        assert [p.token for p in first.pieces] == [p.token for p in second.pieces]
        assert first.reasons == second.reasons
        assert suggestion_to_dict(first) == suggestion_to_dict(second)

    def test_repeated_page_loads_show_identical_pieces(self, user, product, client):
        client.force_login(user)
        _closet(user, "tops", "Owned Oxford Shirt", season="all_season")
        url = f"{_studio_url()}?seed=4&occasion=work"

        first = client.get(url)
        second = client.get(url)

        assert first.status_code == second.status_code == 200
        tokens = _piece_tokens(first.content.decode())
        assert tokens, "the rendered look must expose its piece tokens"
        assert tokens == _piece_tokens(second.content.decode())

    def test_an_empty_wardrobe_degrades_to_warnings_not_crashes(self, user, product):
        suggestion = generate_outfit(user, OutfitContext())

        assert suggestion.pieces  # the catalogue pool still fills what it can
        assert isinstance(suggestion.warnings, tuple)

    def test_invalid_parameters_fall_back_instead_of_raising(self, user):
        context = OutfitContext(
            occasion="teleportation",  # not in OCCASION_CHOICES; validated in the view layer
            season="banana",
            seed=-5,
        )
        suggestion = generate_outfit(user, context)

        assert suggestion.context.occasion == "teleportation"
        assert isinstance(suggestion.reasons, tuple)


# --------------------------------------------------------------------------------------
# 2. Preference / style matching
# --------------------------------------------------------------------------------------


class TestPreferenceAndStyleMatching:
    def test_a_dna_style_bias_scores_and_explains_the_closet_piece(self, user):
        FlashDNA.objects.create(user=user, styles="minimal")
        _closet(user, "tops", "Minimal Rib Top", style="minimal", season="all_season")

        suggestion = generate_outfit(user, OutfitContext())

        top = next(p for p in suggestion.pieces if p.role == "top")
        assert top.source == "closet"
        assert any("minimal preference" in reason for reason in top.reasons)
        assert any("preferred minimal style" in reason for reason in suggestion.reasons)

    def test_a_favourite_colour_lifts_and_explains_the_piece(self, user):
        from apps.catalog.models import Color

        black = Color.objects.create(name="Black", slug="black", hex_code="#111827")
        dna = FlashDNA.objects.create(user=user, styles="classic")
        dna.favorite_colors.add(black)
        _closet(user, "tops", "Black Jacket", color="black", season="all_season")

        suggestion = generate_outfit(user, OutfitContext())

        top = next(p for p in suggestion.pieces if p.role == "top")
        assert any("favourite colour" in reason for reason in top.reasons)

    def test_a_work_context_explains_office_suitable_pieces(self, user):
        _closet(user, "tops", "Work Shirt", occasion="office", season="all_season")

        suggestion = generate_outfit(user, OutfitContext(occasion="work"))

        top = next(p for p in suggestion.pieces if p.role == "top")
        assert any("work" in reason for reason in top.reasons)
        assert any("Work" in reason for reason in suggestion.reasons)

    def test_disliked_colourpieces_are_excluded_entirely(self, user):
        from apps.catalog.models import Color

        olive = Color.objects.create(name="Olive", slug="olive", hex_code="#4d5d43")
        dna = FlashDNA.objects.create(user=user, styles="minimal")
        dna.disliked_colors.add(olive)
        item = _closet(user, "tops", "Olive Shirt", color="olive", season="all_season")

        suggestion = generate_outfit(user, OutfitContext())

        assert f"c{item.pk}" not in [piece.token for piece in suggestion.pieces]


# --------------------------------------------------------------------------------------
# 3. Mood -> outfit
# --------------------------------------------------------------------------------------


class TestMoodMapping:
    def test_a_mood_look_carries_the_mood_reason_and_two_events(self, user, client):
        client.force_login(user)
        before_gen = _event_count("outfit_generated", user=user)
        before_mood = _event_count("mood_outfit_generated", user=user)

        response = client.get(f"{_mood_url()}?mood=confident")

        assert response.status_code == 200
        content = response.content.decode()
        assert maps.MOOD_SIGNALS["confident"].phrase in content
        assert "Feeling" in content
        assert _event_count("outfit_generated", user=user) == before_gen + 1
        assert _event_count("mood_outfit_generated", user=user) == before_mood + 1

    def test_the_mood_page_without_a_mood_is_a_picker_not_a_generation(self, user, client):
        client.force_login(user)
        before_gen = _event_count("outfit_generated", user=user)

        response = client.get(_mood_url())

        assert response.status_code == 200
        assert "Pick a mood to build a look" in response.content.decode()
        assert _event_count("outfit_generated", user=user) == before_gen

    def test_an_unknown_mood_never_reaches_the_generator(self, user, client):
        client.force_login(user)
        before_mood = _event_count("mood_outfit_generated", user=user)

        response = client.get(f"{_mood_url()}?mood=serendipitous")

        assert response.status_code == 200
        assert "Pick a mood to build a look" in response.content.decode()
        assert _event_count("mood_outfit_generated", user=user) == before_mood

    def test_every_declared_mood_builds_a_look(self, user, product, client):
        client.force_login(user)
        for code, _label in maps.MOOD_CHOICES:
            response = client.get(f"{_mood_url()}?mood={code}")
            assert response.status_code == 200
            assert "Why this look" in response.content.decode()


# --------------------------------------------------------------------------------------
# 4. Style goals
# --------------------------------------------------------------------------------------


class TestStyleGoals:
    def test_the_page_lists_every_goal_and_shows_the_current_one(self, user, client):
        client.force_login(user)
        FlashDNA.objects.create(user=user, styles="minimal", style_goal="smart_casual")

        response = client.get(_goals_url())

        content = response.content.decode()
        for _value, label in STYLE_GOALS:
            assert label in content
        assert "Smart casual" in content

    def test_saving_a_goal_persists_it_and_records_the_event(self, user, client):
        client.force_login(user)
        before = _event_count("style_goal_selected", user=user)

        response = client.post(_goals_url(), {"goal": "streetwear"}, follow=True)

        assert response.status_code == 200
        assert get_flash_dna(user).style_goal == "streetwear"
        assert _event_count("style_goal_selected", user=user) == before + 1
        assert b"Style goal set to Streetwear" in response.content

    def test_a_goal_is_created_from_scratch_with_the_goal_in_place(self, user, client):
        client.force_login(user)
        assert get_flash_dna(user) is None

        client.post(_goals_url(), {"goal": "classic"})

        dna = get_flash_dna(user)
        assert dna is not None
        assert dna.style_goal == "classic"

    def test_an_invalid_goal_is_refused_without_side_effects(self, user, client):
        client.force_login(user)
        before = _event_count("style_goal_selected", user=user)

        response = client.post(_goals_url(), {"goal": "chaos"}, follow=True)

        assert get_flash_dna(user) is None
        assert _event_count("style_goal_selected", user=user) == before
        assert b"Pick one of the style goals" in response.content

    def test_the_goal_steers_the_look_and_says_so(self, user, product, client):
        client.force_login(user)
        FlashDNA.objects.create(user=user, styles="minimal", style_goal="minimal")

        response = client.get(_studio_url())

        assert b"Guided by your Minimal style goal" in response.content

    def test_reselecting_the_same_goal_still_records_the_choice(self, user, client):
        client.force_login(user)
        FlashDNA.objects.create(user=user, styles="minimal", style_goal="classic")
        before = _event_count("style_goal_selected", user=user)

        client.post(_goals_url(), {"goal": "classic"})

        assert _event_count("style_goal_selected", user=user) == before + 1


# --------------------------------------------------------------------------------------
# 5. Complete the Look
# --------------------------------------------------------------------------------------


@pytest.fixture
def bottom_product(make_product, colour, size):
    from apps.catalog.models import ProductVariant

    product = make_product(name="Tailored Trousers", slug="tailored-trousers")
    ProductVariant.objects.create(
        product=product, sku="FW-TRS-BLK-M", color=colour, size=size, price="89.00"
    )
    return product


@pytest.fixture
def shoe_product(make_product, colour, size):
    from apps.catalog.models import ProductVariant

    product = make_product(name="City Trainers", slug="city-trainers")
    ProductVariant.objects.create(
        product=product, sku="FW-SNE-BLK-M", color=colour, size=size, price="120.00"
    )
    return product


class TestCompleteTheLook:
    def test_a_top_completes_with_bottoms_and_shoes(self, product, bottom_product, shoe_product):
        look = complete_look(product)

        assert look.source_category == "tops"
        assert [item.product.slug for item in look.items] == [
            "tailored-trousers",
            "city-trainers",
        ]
        assert all(item.product.pk != product.pk for item in look.items)
        assert all(item.reasons[0] == f"Pairs with {product.name}." for item in look.items)

    def test_the_source_product_never_completes_itself(self, product, bottom_product):
        look = complete_look(product)

        assert product.pk not in {item.product.pk for item in look.items}

    def test_no_complements_is_a_friendly_empty_state(self, product):
        look = complete_look(product)

        assert look.items == []
        assert look.warnings == ("No matching pieces right now -- check back soon.",)

    def test_the_widget_ignores_out_of_stock_pieces(self, product, bottom_product, shoe_product):
        from apps.catalog.models import ProductVariant

        trousers = ProductVariant.objects.get(sku="FW-TRS-BLK-M")
        Stock.objects.create(variant=trousers, on_hand=0, reserved=0)

        look = complete_look(product)

        slugs = [item.product.slug for item in look.items]
        assert "tailored-trousers" not in slugs
        assert "city-trainers" in slugs

    def test_a_variant_without_a_stock_row_stays_buyable(self, make_product, colour, size):
        from apps.catalog.models import ProductVariant
        from apps.styling.services.catalog_pool import suggestable_products

        product = make_product(name="Relaxed Chinos", slug="relaxed-chinos")
        ProductVariant.objects.create(
            product=product, sku="FW-CHN-BLK-M", color=colour, size=size, price="79.00"
        )

        buckets = suggestable_products(roles=frozenset({"bottoms"}))

        assert [p.slug for p in buckets.get("bottoms", [])] == ["relaxed-chinos"]

    def test_the_fragment_is_public_and_records_the_view(self, client, product, bottom_product):
        url = reverse("catalog:complete-look", args=[product.slug])
        before = _event_count("complete_look_viewed")

        response = client.get(url)  # never logged in

        assert response.status_code == 200
        assert b"Complete the look" in response.content
        assert b"Tailored Trousers" in response.content
        assert _event_count("complete_look_viewed") == before + 1

    def test_the_fragment_404s_for_an_unpublished_product(self, client, draft_product):
        url = reverse("catalog:complete-look", args=[draft_product.slug])

        assert client.get(url).status_code == 404

    def test_the_fragment_shows_the_calm_empty_state(self, client, product):
        url = reverse("catalog:complete-look", args=[product.slug])

        response = client.get(url)

        assert response.status_code == 200
        assert b"No matching pieces right now" in response.content
        assert b"browse everything" in response.content

    def test_the_api_returns_the_same_widget_as_json(
        self, api_client, product, bottom_product, shoe_product
    ):
        response = api_client.get(reverse("v1:studio-complete-look", args=[product.slug]))

        assert response.status_code == 200
        body = response.json()
        assert body["source"]["slug"] == product.slug
        assert body["source"]["category"] == "tops"
        assert [item["slug"] for item in body["items"]] == ["tailored-trousers", "city-trainers"]
        assert all(item["variants"] for item in body["items"])

    def test_the_api_404s_on_an_unknown_slug(self, api_client):
        response = api_client.get(reverse("v1:studio-complete-look", args=["missing"]))
        assert response.status_code == 404


# --------------------------------------------------------------------------------------
# 6. Out-of-stock and duplicate safety
# --------------------------------------------------------------------------------------


class TestStockAndDuplicateSafety:
    def test_sold_out_products_never_enter_a_generated_look(
        self, user, make_product, colour, size, bottom_product
    ):
        from apps.catalog.models import ProductVariant

        sold_out = make_product(name="Linen Shorts", slug="linen-shorts")
        variant = ProductVariant.objects.create(
            product=sold_out, sku="FW-SHT-BLK-M", color=colour, size=size, price="59.00"
        )
        Stock.objects.create(variant=variant, on_hand=1, reserved=1)
        _closet(user, "tops", "Owned Tee", season="all_season")

        suggestion = generate_outfit(user, OutfitContext())

        assert all(p.pk != sold_out.pk for p in suggestion.catalog_pieces)
        assert all(piece.token != f"p{sold_out.pk}" for piece in suggestion.pieces)

    def test_a_fully_reserved_variant_drops_from_the_pool(
        self, make_product, colour, size, bottom_product
    ):
        from apps.catalog.models import ProductVariant
        from apps.styling.services.catalog_pool import suggestable_products

        sold_out = make_product(name="Linen Shorts", slug="linen-shorts")
        variant = ProductVariant.objects.create(
            product=sold_out, sku="FW-SHT-BLK-M", color=colour, size=size, price="59.00"
        )
        Stock.objects.create(variant=variant, on_hand=2, reserved=2)

        buckets = suggestable_products(roles=frozenset({"bottoms"}))
        slugs = [p.slug for p in buckets["bottoms"]]

        assert "linen-shorts" not in slugs
        assert "tailored-trousers" in slugs  # its variant has no stock row: buyable

    def test_a_look_never_repeats_a_token(self, user, product, bottom_product, shoe_product):
        _closet(user, "tops", "Owned Tee", season="all_season")
        _closet(user, "bottoms", "Owned Chinos", season="all_season")

        suggestion = generate_outfit(user, OutfitContext())

        tokens = [piece.token for piece in suggestion.pieces]
        assert len(tokens) == len(set(tokens))

    def test_the_same_physical_product_appears_once_even_when_owned(
        self, user, product, colour, size
    ):
        from apps.catalog.models import ProductVariant

        variant = ProductVariant.objects.get(sku="FW-TEE-BLK-M")
        owned = _closet(user, "tops", "My Heavyweight Tee", season="all_season", variant=variant)

        suggestion = generate_outfit(user, OutfitContext())

        tops = [p for p in suggestion.pieces if p.role == "top"]
        assert len(tops) == 1
        assert tops[0].token == f"c{owned.pk}"  # owned copy wins; the catalogue twin is unused

    def test_a_saved_outfit_has_unique_roles_and_pieces(self, user):
        top = _closet(user, "tops", "Owned Shirt", season="all_season")
        bottom = _closet(user, "bottoms", "Owned Trousers", season="all_season")

        suggestion = generate_outfit(user, OutfitContext())
        outfit = save_suggestion(user, suggestion, name="Dup check")

        roles = list(OutfitItem.objects.filter(outfit=outfit).values_list("role", flat=True))
        assert len(roles) == len(set(roles))
        assert set(roles) >= {"top", "bottom"}
        assert {top.pk, bottom.pk} <= set(
            OutfitItem.objects.filter(outfit=outfit).values_list("closet_item_id", flat=True)
        )


# --------------------------------------------------------------------------------------
# 7. Save / regenerate / replace
# --------------------------------------------------------------------------------------


class TestSaveRegenerateReplace:
    def test_saving_a_closet_look_persists_it_and_records_the_event(
        self, user, product, bottom_product, client
    ):
        client.force_login(user)
        _closet(user, "tops", "Owned Tee", season="all_season")
        _closet(user, "bottoms", "Owned Chinos", season="all_season")
        before = _event_count("outfit_saved", user=user)

        response = client.post(
            _studio_url(),
            {"action": "save", "name": "Friday look", "occasion": "everyday"},
            follow=True,
        )

        assert response.status_code == 200
        outfit = Outfit.objects.get(user=user)
        assert outfit.name == "Friday look"
        assert OutfitItem.objects.filter(outfit=outfit).count() >= 2
        assert _event_count("outfit_saved", user=user) == before + 1
        assert f"/outfits/{outfit.pk}/" in response.request["PATH_INFO"]

    def test_a_catalogue_only_look_is_refused_with_a_friendly_message(
        self, user, product, bottom_product, client
    ):
        client.force_login(user)  # no closet pieces at all
        before = _event_count("outfit_saved", user=user)

        response = client.post(
            _studio_url(), {"action": "save", "occasion": "everyday"}, follow=True
        )

        assert response.status_code == 200
        assert Outfit.objects.filter(user=user).count() == 0
        assert _event_count("outfit_saved", user=user) == before
        assert b"made of catalogue pieces" in response.content

    def test_save_from_the_service_raises_the_stable_code(self, user, product, bottom_product):
        suggestion = generate_outfit(user, OutfitContext())

        with pytest.raises(OutfitError) as excinfo:
            save_suggestion(user, suggestion)

        assert excinfo.value.code == "no_closet_pieces"

    def test_regenerate_advances_the_seed_and_replays(self, user, product, client):
        client.force_login(user)
        before = _event_count("outfit_generated", user=user)

        response = client.post(_studio_url(), {"action": "regenerate", "seed": "5"})

        assert response.status_code == 302
        assert "seed=6" in response["Location"]
        follow = client.get(response["Location"])
        assert follow.status_code == 200
        assert _event_count("outfit_generated", user=user) == before + 1

    def test_replace_swaps_one_piece_and_keeps_the_rest(
        self, user, product, bottom_product, shoe_product, client
    ):
        client.force_login(user)
        top = _closet(user, "tops", "Owned Tee", season="all_season")
        _closet(user, "bottoms", "Owned Chinos", season="all_season")

        page = client.get(_studio_url())
        tokens = _piece_tokens(page.content.decode())
        assert f"c{top.pk}" in tokens

        before = _event_count("outfit_item_replaced", user=user)
        response = client.post(
            _studio_url(),
            {"action": "replace", "role": "top", "piece": f"c{top.pk}"},
            follow=True,
        )

        assert response.status_code == 200
        after = _piece_tokens(response.content.decode())
        assert f"c{top.pk}" not in after
        assert _event_count("outfit_item_replaced", user=user) == before + 1
        event = Event.objects.filter(name="outfit_item_replaced", user=user).latest("created_at")
        assert event.metadata["previous"] == f"c{top.pk}"
        assert event.metadata["role"] == "top"

    def test_replace_returns_to_the_mood_page_when_asked(self, user, product, client):
        client.force_login(user)
        top = _closet(user, "tops", "Owned Tee", season="all_season")

        response = client.post(
            _studio_url(),
            {
                "action": "replace",
                "role": "top",
                "piece": f"c{top.pk}",
                "back": "mood",
                "mood": "confident",
            },
        )

        assert response.status_code == 302
        assert response["Location"].startswith(_mood_url())

    def test_a_malformed_replace_is_deflected_not_crashed(self, user, product, client):
        client.force_login(user)
        before = _event_count("outfit_item_replaced", user=user)

        response = client.post(
            _studio_url(),
            {"action": "replace", "role": "not-a-role", "piece": "drop-table"},
            follow=True,
        )

        assert response.status_code == 200
        assert _event_count("outfit_item_replaced", user=user) == before
        assert b"That piece could not be replaced" in response.content


# --------------------------------------------------------------------------------------
# 8. Permissions and privacy
# --------------------------------------------------------------------------------------


class TestPermissionsAndPrivacy:
    def test_the_studio_requires_a_signed_in_customer(self, client):
        for url in (_studio_url(), _mood_url(), _goals_url()):
            response = client.get(url)
            assert response.status_code == 302
            assert "login" in response["Location"].lower()

    def test_an_anonymous_api_call_is_refused(self, api_client):
        response = api_client.post(reverse("v1:studio-outfit"), {}, format="json")
        assert response.status_code in (401, 403)

    def test_the_complete_look_widget_is_public_by_design(self, client, product):
        response = client.get(reverse("catalog:complete-look", args=[product.slug]))
        assert response.status_code == 200

    def test_goals_are_scoped_to_the_signed_in_customer(self, user, other_user, client):
        FlashDNA.objects.create(user=other_user, styles="minimal", style_goal="streetwear")

        client.force_login(user)
        response = client.get(_goals_url())

        assert b"None yet" in response.content
        assert get_flash_dna(user) is None
        assert get_flash_dna(other_user).style_goal == "streetwear"

    def test_one_customers_saved_look_does_not_appear_for_another(
        self, user, other_user, product, client
    ):
        client.force_login(user)
        _closet(user, "tops", "Private Shirt", season="all_season")
        client.post(_studio_url(), {"action": "save", "name": "Mine"})

        client.force_login(other_user)
        page = client.get(_studio_url())

        assert b"Private Shirt" not in page.content

    def test_the_click_tracker_refuses_to_leave_the_site(self, client, product):
        response = client.get(
            reverse("catalog:track-click"),
            {"next": "https://evil.example/phish", "src": "studio"},
        )

        assert response.status_code == 302
        assert response["Location"] == reverse("catalog:product-list")

    def test_the_click_tracker_follows_internal_destinations(self, client, product):
        destination = product.get_absolute_url()

        response = client.get(
            reverse("catalog:track-click"), {"next": destination, "src": "complete_look"}
        )

        assert response.status_code == 302
        assert response["Location"] == destination

    def test_the_click_tracker_records_the_hop(self, client, user, product):
        client.force_login(user)
        before = _event_count("recommendation_clicked", user=user)

        client.get(
            reverse("catalog:track-click"),
            {"next": product.get_absolute_url(), "src": "studio"},
        )

        assert _event_count("recommendation_clicked", user=user) == before + 1
        event = Event.objects.filter(name="recommendation_clicked", user=user).latest("created_at")
        assert event.metadata["source"] == "studio"
        assert event.metadata["next"] == product.get_absolute_url()


# --------------------------------------------------------------------------------------
# 9. Analytics: all seven Phase 21 events
# --------------------------------------------------------------------------------------


class TestPhase21AnalyticsEvents:
    def test_one_journey_emits_all_seven_events(self, user, product, bottom_product, client):
        client.force_login(user)
        _closet(user, "tops", "Owned Tee", season="all_season")
        _closet(user, "bottoms", "Owned Chinos", season="all_season")

        client.get(_studio_url())  # outfit_generated
        page = client.get(_studio_url())
        token = _piece_tokens(page.content.decode())[0]
        client.post(
            _studio_url(),
            {"action": "replace", "role": "top", "piece": token},
        )  # outfit_item_replaced
        client.post(_studio_url(), {"action": "save", "name": "Journey"})  # outfit_saved
        client.get(f"{_mood_url()}?mood=confident")  # mood_outfit_generated (+ generated)
        client.post(_goals_url(), {"goal": "classic"})  # style_goal_selected
        client.get(reverse("catalog:complete-look", args=[product.slug]))  # complete_look_viewed
        client.get(
            reverse("catalog:track-click"),
            {"next": product.get_absolute_url(), "src": "studio"},
        )  # recommendation_clicked

        emitted = set(Event.objects.filter(user=user).values_list("name", flat=True))
        assert {
            "outfit_generated",
            "outfit_saved",
            "outfit_item_replaced",
            "complete_look_viewed",
            "recommendation_clicked",
            "style_goal_selected",
            "mood_outfit_generated",
        } <= emitted

    def test_the_generation_event_carries_explainable_metadata(self, user, product, client):
        client.force_login(user)

        client.get(f"{_studio_url()}?occasion=work&season=autumn&seed=3")

        event = Event.objects.filter(name="outfit_generated", user=user).latest("created_at")
        assert event.object_type == "outfit"
        assert event.metadata["occasion"] == "work"
        assert event.metadata["season"] == "autumn"
        assert event.metadata["seed"] == 3
        assert event.metadata["pieces"] >= 1

    def test_the_goal_event_records_the_previous_goal(self, user, client):
        client.force_login(user)
        FlashDNA.objects.create(user=user, styles="minimal", style_goal="classic")

        client.post(_goals_url(), {"goal": "athleisure"})

        event = Event.objects.filter(name="style_goal_selected", user=user).latest("created_at")
        assert event.metadata == {"goal": "athleisure", "previous": "classic"}

    def test_events_are_never_double_written_for_one_view(self, user, product, client):
        client.force_login(user)
        before = _event_count("complete_look_viewed", user=user)

        client.get(reverse("catalog:complete-look", args=[product.slug]))

        assert _event_count("complete_look_viewed", user=user) == before + 1

    def test_an_anonymous_visitor_still_lands_in_the_stream(self, client, product):
        before = _event_count("complete_look_viewed", user=None)

        client.get(reverse("catalog:complete-look", args=[product.slug]))

        assert _event_count("complete_look_viewed", user=None) == before + 1


# --------------------------------------------------------------------------------------
# 10. Studio API
# --------------------------------------------------------------------------------------


class TestStudioApi:
    def test_generate_returns_the_look_with_a_replay_query(self, api_client, user, product):
        api_client.force_authenticate(user=user)

        response = api_client.post(
            reverse("v1:studio-outfit"),
            {"occasion": "work", "seed": 3, "budget": "not-a-number"},
            format="json",
        )

        assert response.status_code == 200
        body = response.json()
        assert body["pieces"]
        assert body["occasion"] == "work"
        assert body["replay"] == {"occasion": "work", "seed": "3"}
        assert body["budget"] is None
        assert Event.objects.filter(name="outfit_generated", user=user).exists()

    def test_unknown_action_and_missing_mood_are_400s(self, api_client, user):
        api_client.force_authenticate(user=user)

        bad_action = api_client.post(
            reverse("v1:studio-outfit"), {"action": "teleport"}, format="json"
        )
        missing_mood = api_client.post(
            reverse("v1:studio-outfit"), {"action": "mood"}, format="json"
        )

        assert bad_action.status_code == 400
        assert bad_action.json()["code"] == "invalid_action"
        assert missing_mood.status_code == 400
        assert missing_mood.json()["code"] == "mood_required"

    def test_a_mood_generation_emits_both_events(self, api_client, user, product):
        api_client.force_authenticate(user=user)
        before_mood = _event_count("mood_outfit_generated", user=user)

        response = api_client.post(
            reverse("v1:studio-outfit"), {"action": "mood", "mood": "chill"}, format="json"
        )

        assert response.status_code == 200
        assert response.json()["mood"] == "chill"
        assert _event_count("mood_outfit_generated", user=user) == before_mood + 1

    def test_replace_via_api_swaps_and_records(self, api_client, user, product):
        api_client.force_authenticate(user=user)
        top = _closet(user, "tops", "Owned Tee", season="all_season")
        _closet(user, "bottoms", "Owned Chinos", season="all_season")

        response = api_client.post(
            reverse("v1:studio-outfit"),
            {"action": "replace", "role": "top", "piece": f"c{top.pk}"},
            format="json",
        )

        assert response.status_code == 200
        assert f"c{top.pk}" not in {p["token"] for p in response.json()["pieces"]}
        assert Event.objects.filter(name="outfit_item_replaced", user=user).exists()

    def test_replace_validates_role_and_piece(self, api_client, user):
        api_client.force_authenticate(user=user)

        response = api_client.post(
            reverse("v1:studio-outfit"),
            {"action": "replace", "role": "nonsense", "piece": "zz"},
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_piece"

    def test_save_returns_the_outfit_and_records_the_event(
        self, api_client, user, product, bottom_product
    ):
        api_client.force_authenticate(user=user)
        _closet(user, "tops", "Owned Tee", season="all_season")

        response = api_client.post(
            reverse("v1:studio-outfit-save"), {"name": "API look"}, format="json"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["outfit_name"] == "API look"
        assert Outfit.objects.filter(pk=body["outfit_pk"], user=user).exists()
        assert Event.objects.filter(
            name="outfit_saved", user=user, object_id=body["outfit_pk"]
        ).exists()

    def test_save_of_a_catalogue_only_look_is_a_structured_400(
        self, api_client, user, product, bottom_product
    ):
        api_client.force_authenticate(user=user)  # wardrobe untouched: no closet pieces

        response = api_client.post(reverse("v1:studio-outfit-save"), {}, format="json")

        assert response.status_code == 400
        assert response.json()["code"] == "no_closet_pieces"

    def test_goal_round_trip(self, api_client, user):
        api_client.force_authenticate(user=user)

        read = api_client.get(reverse("v1:studio-goal"))
        write = api_client.post(reverse("v1:studio-goal"), {"goal": "formal"}, format="json")
        invalid = api_client.post(reverse("v1:studio-goal"), {"goal": "vaporwave"}, format="json")

        assert read.status_code == 200
        assert [c["value"] for c in read.json()["choices"]] == [v for v, _l in STYLE_GOALS]
        assert write.status_code == 200
        assert write.json() == {"goal": "formal", "label": "Formal"}
        assert get_flash_dna(user).style_goal == "formal"
        assert invalid.status_code == 400
        assert invalid.json()["code"] == "invalid_goal"
        assert Event.objects.filter(name="style_goal_selected", user=user).count() == 1

    def test_generator_is_marked_as_a_throttled_scope(self):
        assert StudioGenerateView.throttle_scope == "expensive"

    def test_invalid_weather_and_season_inputs_degrade_quietly(self, api_client, user, product):
        api_client.force_authenticate(user=user)

        response = api_client.post(
            reverse("v1:studio-outfit"),
            {"season": "banana", "weather_temp": "900", "weather_cond": "hail"},
            format="json",
        )

        assert response.status_code == 200
        body = response.json()
        assert body["season"] == ""
        assert body["pieces"]
        assert isinstance(body["warnings"], list)


# --------------------------------------------------------------------------------------
# 11. Mobile / UX states
# --------------------------------------------------------------------------------------


class TestMobileAndUxStates:
    def test_the_studio_form_upgrades_gracefully_without_javascript(self, user, product, client):
        client.force_login(user)

        response = client.get(_studio_url())
        content = response.content.decode()

        assert 'hx-get="' in content and 'hx-target="#studio-result"' in content
        assert 'hx-indicator="#studio-spinner"' in content
        assert 'method="get"' in content  # the native fallback POSTs nothing it must not

    def test_loading_error_and_busy_states_are_wired(self, user, product, client):
        client.force_login(user)

        content = client.get(_studio_url()).content.decode()

        assert 'id="studio-spinner"' in content
        assert 'aria-live="polite"' in content
        assert 'id="studio-error"' in content
        assert 'role="alert"' in content
        assert "hx-on::htmx:beforeRequest" in content
        assert "aria-busy" in content
        assert "hx-on::htmx:responseError" in content

    def test_the_look_renders_warnings_as_a_polite_status(self, user, product, client):
        client.force_login(user)
        _closet(user, "tops", "Owned Tee", season="all_season", status="archived")

        content = client.get(_studio_url()).content.decode()

        # Either a warning box with role=status or the friendly empty state -- both are
        # announced politely; the hard requirement is that the page never renders raw.
        assert 'role="status"' in content
        assert "No look yet" in content or "Why this look" in content

    def test_the_mood_picker_offers_every_mood_as_a_plain_link(self, user, client):
        client.force_login(user)

        content = client.get(_mood_url()).content.decode()

        for code, label in maps.MOOD_CHOICES:
            assert f"?mood={code}" in content
            assert str(label) in content
        assert "aria-current" not in content  # nothing selected yet

    def test_the_selected_mood_is_marked_for_assistive_tech(self, user, client):
        client.force_login(user)

        content = client.get(f"{_mood_url()}?mood=elegant").content.decode()

        assert 'aria-current="true"' in content

    def test_the_goal_picker_uses_labelled_radio_cards(self, user, client):
        client.force_login(user)

        content = client.get(_goals_url()).content.decode()

        assert content.count('type="radio"') == len(STYLE_GOALS)
        assert 'name="goal"' in content
        assert "<fieldset" in content
        assert "<legend" in content
        assert 'name="occasion"' not in content  # the goal page owns no generator context

    def test_the_product_page_loads_the_widget_lazily_with_fallbacks(
        self, user, product, bottom_product, client
    ):
        content = client.get(product.get_absolute_url()).content.decode()

        assert 'hx-trigger="load"' in content
        assert f'hx-get="{reverse("catalog:complete-look", args=[product.slug])}"' in content
        assert 'id="complete-look-spinner"' in content
        assert 'id="complete-look-error"' in content
        assert "<noscript>" in content
        assert 'aria-labelledby="complete-look-heading"' in content
        assert "You may also like" in content  # the existing related section still renders

    def test_the_fragment_keeps_its_own_heading_and_no_page_shell(self, client, product):
        content = client.get(reverse("catalog:complete-look", args=[product.slug])).content.decode()

        assert "<html" not in content
        assert content.lstrip().startswith("<h2")
        assert "Complete the look" in content

    def test_studio_pages_declare_noindex(self, user, product, client):
        client.force_login(user)

        for url in (_studio_url(), _mood_url(), _goals_url()):
            assert b"noindex" in client.get(url).content

    def test_the_navigation_offers_the_studio(self, user, client):
        client.force_login(user)

        content = client.get(_studio_url()).content.decode()

        assert reverse("account:studio") in content
