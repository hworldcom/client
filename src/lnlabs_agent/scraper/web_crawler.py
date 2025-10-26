# src/lnlabs_agent/scraper/web_crawler.py
from __future__ import annotations

from .base_crawler import BaseWebCrawler
from .company_flow import CompanyFlowMixin
from .mutuals_flow import MutualConnectionsMixin


class WebCrawler(CompanyFlowMixin, MutualConnectionsMixin, BaseWebCrawler):
    """Full crawler composed from reusable flow mixins and the shared base.
    Inherits all headless/browser helpers from BaseWebCrawler while adding
    company and profile mutual scraping capabilities via dedicated mixins.
    """

    pass
