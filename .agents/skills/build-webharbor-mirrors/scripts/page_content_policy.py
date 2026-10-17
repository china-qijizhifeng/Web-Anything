"""Shared content-level error page markers for mirror acquisition stages."""

from __future__ import annotations


ERROR_PAGE_MARKERS = (
    "enable javascript to continue",
    "javascript is disabled",
    "something went wrong",
    "technical difficulties",
    "verify you are human",
    "your current browser isn't compatible",
    "your current browser is not compatible",
    "browser isn't supported",
    "browser is not supported",
    "unsupported browser",
    "update your browser to continue",
    "upgrade your browser to continue",
    "/_sec/verify?provider=interstitial",
    "/interstitial/ic.html",
    "bm-verify=",
    "\u5fc5\u9808\u66f4\u65b0\u700f\u89bd\u5668\u624d\u80fd\u7e7c\u7e8c",
    "\u8acb\u5c07\u700f\u89bd\u5668\u66f4\u65b0\u81f3\u6700\u65b0\u7248\u672c",
    "\u5fc5\u987b\u66f4\u65b0\u6d4f\u89c8\u5668\u624d\u80fd\u7ee7\u7eed",
    "\u8bf7\u5c06\u6d4f\u89c8\u5668\u66f4\u65b0\u81f3\u6700\u65b0\u7248\u672c",
)


def content_error_reason(text: str) -> str | None:
    folded = text.casefold()
    return next((marker for marker in ERROR_PAGE_MARKERS if marker in folded), None)
