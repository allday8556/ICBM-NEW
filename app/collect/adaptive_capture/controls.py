"""Synthetic V4 negative controls (Phase C C0; review `5312911203` Q3).

V4 needs a typed ``LOGIN`` and a typed ``NON_PRODUCT`` page, and neither is ever fetched from a
supplier: a real authenticated login capture adds private and session risk that V4 does not need.
These pages keep only the structural facts that make a product profile fail closed — a sign-in
form with no product, a listing of several products with no single product document — and hold no
credential, token, cookie, personal datum or real identifier. The mutation suite is derived from
each approved sample by the validation owner itself.
"""

from types import MappingProxyType
from typing import Final

from app.collect.adaptive.validation import NegativeClass

SYNTHETIC_LOGIN: Final = (
    "<!doctype html><html><head><title>Sign in</title></head><body>"
    '<div class="signin"><h2>Sign in</h2>'
    '<form action="/signin" method="post">'
    '<label>ID <input type="text" name="uid"></label>'
    '<label>Password <input type="password" name="pw"></label>'
    '<button type="submit">Sign in</button>'
    "</form></div></body></html>"
)

SYNTHETIC_NON_PRODUCT: Final = (
    "<!doctype html><html><head><title>Listing</title></head><body>"
    '<section class="listing"><h2>Items</h2><ul class="items">'
    '<li class="item"><a href="/product/a/1">Item one</a><span class="price">1,000</span></li>'
    '<li class="item"><a href="/product/b/2">Item two</a><span class="price">2,000</span></li>'
    '<li class="item"><a href="/product/c/3">Item three</a><span class="price">3,000</span></li>'
    "</ul></section></body></html>"
)

SYNTHETIC_NEGATIVES: Final = MappingProxyType(
    {NegativeClass.LOGIN: SYNTHETIC_LOGIN, NegativeClass.NON_PRODUCT: SYNTHETIC_NON_PRODUCT}
)
