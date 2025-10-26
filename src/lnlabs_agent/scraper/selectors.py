# selectors.py — shared selector registry and small helpers
from __future__ import annotations

import random
import re
from typing import Iterable, List

SECOND_RX = re.compile(r"^\s*2nd(?:\s*degree)?\b", re.I)

def _ms(min_s: float = 0.15, max_s: float = 0.35) -> int:
    """Small human-like delay in ms."""
    return int(random.uniform(min_s, max_s) * 1000)

def _join(selectors: Iterable[str]) -> str:
    return ", ".join(selectors)

SELECTORS: dict[str, List[str]] = {
    # --- Global / layout ---
    "results_container": [
        "div.search-results-container",
        "div.search-results__list",
    ],
    "result_cards": [
        '[data-view-name="people-search-result"]',
        '[data-view-name="search-result"]',
        '[data-view-name="search-entity-result-universal-template"]',
        '[data-view-name^="search-entity-result-"]',
        '[data-chameleon-result-urn]',
        "li.reusable-search__result-container",
        "div.search-result__wrapper",
    ],
    "profile_link_in_card": [
        'a[href*="/in/"]'
    ],
    "company_link_anywhere": [
        "a[href*='/company/']"
    ],
    "profile_mutuals_link": [
        "a[data-control-name='topcard_view_all_connections']",
        "a[data-control-name='view_mutual_connections']",
        "a[href*='/network/mutuals/']",
        "a[href*='/search/results/people/?connectionOf=']",
        "a[href*='connectionOf=%5B']",
        "a:has-text('mutual connection')",
        "a:has(span:has-text('Mutual connections'))",
        "a:has-text('Mutual connections')",
        "a:has(span:has-text('Mutual connection'))",
        "a:has-text('Mutual connection')",
        "button:has(span:has-text('Mutual connections'))",
        "button:has-text('Mutual connections')",
        "button:has(span:has-text('Mutual connection'))",
        "button:has-text('Mutual connection')",
    ],
    "search_filters_nav": [
        "nav[aria-label='Search filters']",
        "#search-reusables__filters-bar nav[aria-label='Search filters']",
    ],
    "toolbar": [
        "div[role='toolbar']",
    ],
    "all_filters_button": [
        "button.search-reusables__all-filters-pill-button",
        "button[aria-label='All filters']",
        "button[aria-label='All Filters']",
        "button[aria-label*='All' i][aria-label*='filter' i]",
        "button.artdeco-pill:has-text('All filters')",
    ],
    "companies_pill": [
        "ul.search-reusables__filter-list li button:has-text('Companies')",
        "button.artdeco-pill:has-text('Companies')",
        "button:has-text('Companies')",
    ],
    "connections_pill": [
        "button[role='button']:has-text('Connections')",
        "[data-test-reusables-filters__filter-pill='CONNECTIONS']",
        "button:has-text('All filters')",
    ],
    "search_input": [
        "#global-nav-search input.search-global-typeahead__input",
        "header#global-nav input.search-global-typeahead__input",
        "header#global-nav input[role='combobox'][aria-autocomplete='list']",
        "input[data-view-name='search-global-typeahead-input']",
        "div[role='search'] input[data-testid='typeahead-input']",
        "input[data-testid='typeahead-input']",
        "div[role='search'] input[aria-autocomplete='list']",
        "input[placeholder='Search']",
        "input[aria-label='Search']",
    ],
    "search_expand_button": [
        "button.search-global-typeahead__collapsed-search-button",
    ],
    "company_top_card": [
        "div.org-top-card-summary-info-list",
        "section.org-top-card",
        "div.org-top-card__primary-content",
    ],
    "company_employees_link": [
        "div.org-top-card-summary-info-list a.org-top-card-summary-info-list__info-item-link",
        "a.org-top-card-summary-info-list__info-item-link",
        "a[href*='currentCompany=']",
        "a[href*='/search/results/people/?currentCompany=']",
        "a[href$='/people/']",
        "a[href*='/people/']",
        "a:has(span:has-text('employees'))",
        "a:has-text('employees')",
    ],
    "pagination_container": [
        "div.artdeco-pagination",
        "nav[aria-label='Pagination']",
        "ul.artdeco-pagination__pages",
    ],
    "pagination_next_button": [
        "button[data-testid='pagination-controls-next-button-visible']",
        "button.artdeco-pagination__button--next",
        "nav[aria-label='Pagination'] button[aria-label='Next']",
        "button[aria-label='Next']",
        "button:has-text('Next')",
    ],
    "pagination_next_hidden": [
        "button[data-testid='pagination-controls-next-button-hidden']",
    ],
    "pagination_page_label": [
        'button[aria-current="true"][aria-label^="Page"] span',
        "li.active button[aria-label^='Page'] span",
        "li.selected button[aria-label^='Page'] span",
    ],
    "pagination_active_li": [
        "div.artdeco-pagination ul.artdeco-pagination__pages li.active",
        "ul.artdeco-pagination__pages li.active",
        "li.artdeco-pagination__indicator.active",
        "li.artdeco-pagination__indicator.selected",
    ],
}
