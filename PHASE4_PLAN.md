# FLASHWEAR Phase 4 - Product Discovery/Search Implementation Plan

## Current State Analysis

### Existing Catalogue Architecture (Phase 3)
- **Models**: Product, ProductVariant, ProductImage, Category, Brand, Collection, Color, Size, Material, Fit, ProductTag
- **Product Visibility**: `Product.objects.published()` - status=ACTIVE, published_at<=now, category.is_active, brand.is_active (or null)
- **Variants**: ProductVariant with color, size, price, compare_at_price, is_active
- **Selectors** (`selectors.py`): Centralized query logic - `storefront_products`, `products_in_category`, `products_in_collection`, `products_for_brand`, `product_detail_queryset`, etc.
- **Services** (`services.py`): `build_variant_matrix`, `variant_conflict`, `publish/archive/unpublish`, `category_subtree_ids`, `product_gallery`
- **Views**: product_list, product_detail, category_list, category_detail, collection_list, collection_detail, brand_detail
- **API**: ProductListView, ProductDetailView, CategoryListView, CategoryDetailView, BrandListView, BrandDetailView, CollectionListView, CollectionDetailView
- **Current Filters**: category, brand, collection (API), sort (newest, price_asc, price_desc, featured)
- **URLs**: /products/, /products/<slug>/, /categories/, /categories/<slug>/, /collections/, /collections/<slug>/, /brands/<slug>/
- **API**: /api/v1/products/, /api/v1/products/<slug>/, /api/v1/categories/, etc.

## Phase 4 Requirements Summary

### Search
- Full-text search across product name, short_description, description, brand, category, collection, color, size, material, fit, tags
- Case-insensitive, Unicode-aware, whitespace-tolerant
- Search ranking: name > brand > category > collection > description
- Deterministic, explainable ranking

### Filters
- Category (with subtree)
- Brand
- Collection
- Color (with variant-aware filtering)
- Size (with variant-aware filtering)
- Material
- Fit
- Tags
- Price range (min/max, Decimal)

### Sorting
- Extend existing: newest, price_asc, price_desc, featured
- Add: name_asc, name_desc

### URL State
- All filters in query parameters
- Pagination preserves filters
- Active filter chips with remove links
- Clear all action

### API Parity
- All storefront filters/search available in API
- Same visibility rules

### Templates
- Search bar
- Filter sidebar (mobile: drawer)
- Active filter chips with remove
- Clear all
- Mobile filter drawer
- Accessible

### API Parity
- All storefront filters in API
- Same visibility rules

## Implementation Plan

### Phase 1: Core Discovery Service Layer
**File**: `apps/catalog/services/discovery.py`

Create new service module for discovery logic:
- `parse_discovery_params(request)` - parse and validate query params
- `apply_search(queryset, query)` - full-text search with ranking
- `apply_filters(queryset, filters)` - apply all filters
- `apply_sorting(queryset, sort)` - apply sorting
- `get_filter_options(queryset)` - faceted counts
- `build_discovery_queryset(base_queryset, params)` - main entry point

### Search Ranking Strategy
Since we start with SQLite (no pg_trgm/full-text), use:
- `icontains` on multiple fields with `Case/When` for ranking
- Weight: name=10, brand=8, category=6, collection=4, color/size/material/fit/tag=2, description=1
- For PostgreSQL: document migration path to `SearchVector` + `SearchRank`

### Filters Implementation
All filters as query params:
- `q` - search query
- `category` - category slug (subtree)
- `brand` - brand slug
- `collection` - collection slug
- `color` - color slug
- `size` - size code
- `material` - material slug
- `fit` - fit slug
- `tag` - tag slug
- `min_price` / `max_price` - Decimal
- `sort` - sort key (validated against allowlist)
- `page` / `page_size` - pagination

### Filter Logic
- Category: subtree via `category_subtree_ids`
- Brand/Collection: direct FK
- Color/Size/Material/Fit/Tag: filter through variants M2M
- Color/Size: variant-aware (must have matching active variant)
- Price: annotate price_min/price_max, filter on those

### Sorting
Extend existing VALID_SORTS:
- `newest` (default)
- `price_asc`
- `price_desc`
- `featured`
- `name_asc` (new)
- `name_desc` (new)

### Phase 2: Extend Selectors
**File**: `apps/catalog/selectors.py`
- Add `search_products(queryset, query)` - returns annotated queryset with `search_rank`
- Add `filter_products(queryset, filters_dict)` - applies all filters
- Add `get_filter_facets(queryset)` - returns facet counts
- Reuse existing `with_price_range`, `storefront_products`, etc.

### Phase 3: API Views
**File**: `apps/catalog/api.py`
- Extend `ProductListView.get_queryset()` with search/filter/sort params
- Add `ProductSearchSuggestionsView` for autocomplete
- Reuse selectors

### Phase 3: Storefront Views
**File**: `apps/catalog/views.py`
- Extend `product_list` with search/filter/sort
- Update `category_detail`, `brand_detail`, `collection_detail` to accept additional filters
- Add `product_search` view for `/search/` endpoint
- Pass filter context to templates

### Phase 4: Templates
**Files**: 
- `templates/catalog/product_list.html` - add search bar, filter sidebar, active chips
- `templates/catalog/_filter_sidebar.html` - new partial
- `templates/catalog/_active_filters.html` - active filter chips
- `templates/catalog/_product_grid.html` - reuse
- Mobile filter drawer with Alpine.js
- Accessible markup (ARIA)

### Phase 5: API Search Suggestions
- New endpoint: `/api/v1/products/suggestions/?q=...`
- Returns: product names, categories, brands, collections
- Min query length: 2 chars
- Limit: 10 results

### Phase 6: Database Indexes
Add indexes for:
- Product: search fields (name, description), status+published_at+category+brand
- ProductVariant: color, size, is_active
- Category: parent_id, is_active
- Material, Fit, ProductTag: is_active, display_order

### Phase 7: Tests
- Model tests: search ranking, filter combinations
- Selector tests: query counts, filter combinations
- View tests: HTML rendering, URL params
- API tests: parity with storefront
- Performance: query count assertions

### Phase 8: Quality Gate
- Run full test suite
- Ruff, djLint, Django check, makemigrations check
- Coverage >= 90%

## File Creation Order
1. `apps/catalog/services/discovery.py` - Core discovery logic
2. `apps/catalog/selectors.py` - Extend with search/filter methods
2. `apps/catalog/api.py` - Extend API views
3. `apps/catalog/views.py` - Extend storefront views
4. `apps/catalog/urls.py` - Add search endpoint
5. `apps/catalog/serializers.py` - Add suggestion serializer
6. `apps/catalog/templatetags/catalog_extras.py` - Filter template tags
7. Templates (new partials + updates)
8. Static/JS - Alpine.js filter drawer
9. Database migration for indexes
10. Tests

## PostgreSQL Migration Path (Documented)
```python
# In discovery.py - when PostgreSQL available:
from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank


def search_products_pg(queryset, query):
    vector = (
        SearchVector("name", weight="A")
        + SearchVector("brand__name", weight="B")
        + SearchVector("category__name", weight="C")
        + SearchVector("description", weight="D")
    )
    query = SearchQuery(query)
    return (
        queryset.annotate(search=vector, rank=SearchRank(vector, query))
        .filter(search=query)
        .order_by("-rank")
    )
```

## Settings to Add
- `CATALOG_SEARCH_MIN_LENGTH = 2`
- `CATALOG_SUGGESTIONS_LIMIT = 10`
- `CATALOG_SEARCH_RANK_WEIGHTS` (for future PG tuning)

## URL Patterns to Add
- `GET /search/` - storefront search page
- `GET /api/v1/products/suggestions/?q=` - autocomplete
- Update existing `/products/` to accept all filter params

## Template Structure
```
templates/catalog/
├── product_list.html (updated)
├── product_detail.html (updated)
├── category_detail.html (updated)
├── brand_detail.html (updated)
├── collection_detail.html (updated)
├── search.html (new)
├── _filter_sidebar.html (new)
├── _active_filters.html (new)
├── _product_grid.html (reuse)
├── _pagination.html (reuse)
```

## Mobile Filter Drawer
- Alpine.js component
- Slide-in from left
- Focus trap
- Escape to close
- Overlay backdrop

## Accessibility Checklist
- [ ] Search input: label + aria-describedby
- [ ] Filter checkboxes: fieldset + legend
- [ ] Active filter chips: aria-label for remove
- [ ] Sort select: label + aria-live for results
- [ ] Pagination: nav + aria-label
- [ ] Mobile drawer: focus trap, ESC to close

## Acceptance Criteria Checklist
- [ ] Search "black tee" returns relevant products
- [ ] Color filter "black" + size "XL" = only products with Black XL variant
- [ ] Price range 1000-5000 works with decimals
- [ ] Sort by price_asc/desc/featured/name works
- [ ] URL preserves all params on pagination
- [ ] Active filter chips show and remove correctly
- [ ] Clear all resets to base URL
- [ ] Mobile filter drawer opens/closes
- [ ] API parity: same filters in /api/v1/products/
- [ ] Suggestions appear after 2 chars
- [ ] Empty state shows helpful message
- [ ] SEO: canonical URLs, meta tags on filtered pages
- [ ] All existing tests pass
- [ ] 90%+ coverage maintained