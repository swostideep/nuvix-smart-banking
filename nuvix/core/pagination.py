"""Pagination defaults.

Transaction and message tables grow without bound, so the default is cursor
pagination: ``LIMIT/OFFSET`` degrades linearly with depth and drops rows when
new records land mid-scroll.
"""

from rest_framework.pagination import CursorPagination, PageNumberPagination


class CursorPagePagination(CursorPagination):
    page_size = 25
    max_page_size = 200
    page_size_query_param = "page_size"
    ordering = "-created_at"


class SmallPageNumberPagination(PageNumberPagination):
    """For small, bounded collections where a page count is genuinely useful."""

    page_size = 20
    max_page_size = 100
    page_size_query_param = "page_size"
